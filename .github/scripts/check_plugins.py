#!/usr/bin/env python3
"""Scan the official Obsidian plugin list for Chinese-relevant plugins.

Modes:
  (default)       fetch official list + cache diff, score candidates, detect
                  stale rows, write GitHub Actions outputs
  --apply-readme  edit README.md only from ADD_ROWS/REMOVE_ROWS env vars,
                  write .github/pr-body.md and the PR title (no git, no network)
  --skip-stale    skip stale checks (dry runs / rate-limit constrained runs)

Improvements over the previous version:
  * quality scoring (stars, release downloads, release recency) in addition
    to text signals, split into "auto" (high confidence) and "review" tiers
  * stale detection uses the latest release date, falling back to pushed_at,
    instead of pushed_at alone
  * cache-only runs no longer create PRs (workflow commits the cache directly)
  * a denylist (denylist.json) prevents re-adding rejected plugins
  * no subprocess git/gh calls; PR creation is delegated to the workflow
  * cache pruned of plugin ids removed from the official list
"""
import base64
import json
import math
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import zhconv
except ImportError:
    zhconv = None

COMMUNITY_URL = "https://raw.githubusercontent.com/obsidianmd/obsidian-releases/master/community-plugins.json"
CACHE_FILE = ".github/scripts/checked_plugins.json"
DENY_FILE = ".github/scripts/denylist.json"
PR_BODY_FILE = ".github/pr-body.md"
README_PATH = "README.md"

PLUGIN_SECTION_START = "## 原生中文插件"
PLUGIN_SECTION_END = "## 精选中文主题"
OTHER_TOOLS_SECTION = "### 其他工具"

STALE_DAYS = int(os.environ.get("STALE_DAYS", "365"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "30"))
STALE_HTTP_TIMEOUT = int(os.environ.get("STALE_HTTP_TIMEOUT", "10"))
STALE_CHECK_WORKERS = int(os.environ.get("STALE_CHECK_WORKERS", "12"))
MIN_CN_SCORE = int(os.environ.get("MIN_CN_SCORE", "35"))
AUTO_TOTAL_SCORE = int(os.environ.get("AUTO_TOTAL_SCORE", "100"))
MIN_QUALITY_SCORE = int(os.environ.get("MIN_QUALITY_SCORE", "20"))

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
API_HEADERS = {"Authorization": f"Bearer {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}

LOCALE_NAME_RE = re.compile(r"^zh[\-]?(cn|tw|hant|hans|hk)?\.")
PLUGIN_ROW_RE = re.compile(
    r"^\| \[([^\]]+)\]\(https://github\.com/([^/|)]+/[^)|\s]+)\)\s*\|\s*`?([^`|]+)`?\s*\|",
    re.M,
)
CN_CITIES = [
    "china", "chinese", "taiwan", "hong kong", "beijing", "shanghai",
    "shenzhen", "guangzhou", "chengdu", "nanjing", "wuhan",
    "中国", "台湾", "香港",
]
CH_TOPICS = {"chinese", "zh", "zh-cn", "chinese-translation", "obsidian-zh"}

CN_SIGNAL_WEIGHTS = {
    "locale": 30,
    "topic": 15,
    "docs_cn": 15,
    "desc_cn": 10,
    "author_cn": 10,
    "location": 10,
    "name_cn": 5,
}


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def create_retry_session(retries=3, backoff_factor=0.3, timeout=None):
    if timeout is None:
        timeout = HTTP_TIMEOUT
    session = requests.Session()
    strategy = Retry(
        total=retries,
        connect=retries,
        read=retries,
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS"],
    )
    adapter = HTTPAdapter(max_retries=strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def gh_get(url):
    session = create_retry_session(timeout=HTTP_TIMEOUT)
    try:
        r = session.get(url, headers=API_HEADERS, timeout=HTTP_TIMEOUT)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        print(f"WARN: request failed for {url}: {e}", file=sys.stderr)
        return None


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def has_cn(s):
    return bool(re.search(r"[\u4e00-\u9fff]", s))


REVIEWED_TAIL_RE = re.compile(
    r"\s*[-–—]\s*this plugin has not been manually reviewed by obsidian staff\.?",
    re.IGNORECASE,
)


def extract_chinese_summary(readme_text, max_len=60):
    """First Chinese sentence in a repo README, trimmed for use as a
    fallback description. Returns '' when there is none."""
    if not readme_text:
        return ""
    for line in readme_text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "|", "!", "[", "```", ">", "-", "*")):
            continue
        if not has_cn(line):
            continue
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"[`*_#>]", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if len(re.findall(r"[\u4e00-\u9fff]", line)) >= 5:
            if len(line) > max_len:
                line = line[:max_len].rstrip("，。；：,.;: ") + "…"
            return line
    return ""


def clean_desc(desc, readme_text=None, max_len=60):
    """Normalize an official-list description for the README: strip the
    Obsidian review boilerplate, convert Traditional to Simplified Chinese,
    and fall back to a Chinese line from the repo README when the result
    contains no Chinese at all."""
    if not desc:
        return desc
    text = REVIEWED_TAIL_RE.sub("", desc).strip()
    if zhconv is not None:
        text = zhconv.convert(text, "zh-cn")
    text = text.strip()
    if not has_cn(text):
        summary = extract_chinese_summary(readme_text, max_len=max_len)
        if summary:
            return summary
    return text


def parse_github_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def utcnow():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# README parsing and editing (pure functions, no network)
# --------------------------------------------------------------------------

def plugin_section_bounds(text):
    start = text.find(PLUGIN_SECTION_START)
    if start == -1:
        return None, None
    end = text.find(PLUGIN_SECTION_END, start)
    if end == -1:
        return None, None
    return start, end


def markdown_cells(line):
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def is_table_separator(line):
    cells = markdown_cells(line)
    if not cells:
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def parse_plugin_rows(readme_text):
    start, end = plugin_section_bounds(readme_text)
    if start is None:
        return []
    section = readme_text[start:end]
    rows = []
    current_section = ""
    for line in section.splitlines():
        if line.startswith("### "):
            current_section = line[4:].strip()
            continue
        m = PLUGIN_ROW_RE.match(line)
        if not m:
            continue
        rows.append({
            "section": current_section,
            "name": m.group(1).strip(),
            "repo": m.group(2).rstrip("/"),
            "author": m.group(3).strip(),
            "line": line,
        })
    return rows


def parse_current_plugins(readme_text):
    existing = set()
    for m in re.finditer(r"\]\(https://github\.com/([^/]+/[^/)\s]+)\)", readme_text):
        existing.add(m.group(1).rstrip("/").lower())
    return existing


def table_author_sort_key(row):
    cells = markdown_cells(row)
    author = cells[1] if len(cells) > 1 else ""
    plugin = cells[0] if cells else ""
    author = re.sub(r"[`*_~\[\]()]", "", author).strip()
    plugin = re.sub(r"[`*_~\[\]()]", "", plugin).strip()
    return author.casefold(), plugin.casefold(), row.casefold()


def sort_plugin_tables_by_author(text):
    start, end = plugin_section_bounds(text)
    if start is None:
        return text

    before = text[:start]
    section = text[start:end]
    after = text[end:]
    lines = section.splitlines(keepends=True)
    sorted_lines = []
    i = 0

    while i < len(lines):
        if (
            i + 1 < len(lines)
            and markdown_cells(lines[i])
            and is_table_separator(lines[i + 1])
        ):
            header = lines[i]
            separator = lines[i + 1]
            rows = []
            i += 2
            while (
                i < len(lines)
                and markdown_cells(lines[i])
                and not is_table_separator(lines[i])
            ):
                rows.append(lines[i])
                i += 1

            header_cells = markdown_cells(header)
            if len(header_cells) > 1 and header_cells[1] == "作者":
                rows = sorted(rows, key=table_author_sort_key)
            sorted_lines.extend([header, separator, *rows])
            continue

        sorted_lines.append(lines[i])
        i += 1

    return before + "".join(sorted_lines) + after


def remove_plugin_rows(text, stale_rows):
    repos = {row["repo"].lower() for row in stale_rows}
    if not repos:
        return text

    lines = text.splitlines(keepends=True)
    kept = []
    for line in lines:
        m = PLUGIN_ROW_RE.match(line.rstrip("\r\n"))
        if m and m.group(2).rstrip("/").lower() in repos:
            continue
        kept.append(line)
    return "".join(kept)


def append_rows_to_other_tools(text, rows):
    rows = [r for r in rows if r.get("repo")]
    if not rows:
        return text

    start, plugin_end = plugin_section_bounds(text)
    if start is None:
        return text

    section_start = text.find(OTHER_TOOLS_SECTION, start, plugin_end)
    if section_start == -1:
        return text

    next_section = text.find("\n### ", section_start + len(OTHER_TOOLS_SECTION), plugin_end)
    section_end = next_section if next_section != -1 else plugin_end
    block = text[section_start:section_end]

    existing = parse_current_plugins(text)
    new_rows = ""
    for r in sorted(rows, key=lambda row: row["author"].casefold()):
        if r["repo"].lower() in existing:
            continue
        desc = clean_desc(r.get("desc", "")).replace("|", "\\|")
        new_rows += f'| [{r["name"]}](https://github.com/{r["repo"]}) | `{r["author"]}` | {desc} |\n'
    if not new_rows:
        return text

    matches = list(re.finditer(r"^\| \[.*\n?", block, re.M))
    if matches:
        insert_pos = section_start + matches[-1].end()
        return text[:insert_pos] + new_rows + text[insert_pos:]

    # Empty table: insert directly after the table separator line.
    lines = block.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if is_table_separator(line):
            insert_pos = section_start + sum(len(l) for l in lines[: i + 1])
            return text[:insert_pos] + new_rows + text[insert_pos:]
    return text


# --------------------------------------------------------------------------
# GitHub repo metadata (network)
# --------------------------------------------------------------------------

def has_locale_file(repo):
    """Check common locale dirs for a zh* file; 1-3 API calls."""
    root = gh_get(f"https://api.github.com/repos/{repo}/contents/")
    if not isinstance(root, list):
        return False
    dirs = {item["name"] for item in root if item["type"] == "dir"}
    candidates = {"lang", "locale", "i18n", "l10n", "translations", "src"} & dirs
    for d in sorted(candidates):
        sub = gh_get(f"https://api.github.com/repos/{repo}/contents/{d}")
        if not isinstance(sub, list):
            continue
        names = [item["name"].lower() for item in sub]
        if d == "src":
            for sd in {i["name"] for i in sub if i["type"] == "dir"} & {"lang", "locale", "i18n", "l10n", "translations"}:
                sub2 = gh_get(f"https://api.github.com/repos/{repo}/contents/src/{sd}")
                if isinstance(sub2, list):
                    names.extend(item["name"].lower() for item in sub2)
        if any(LOCALE_NAME_RE.match(n) for n in names):
            return True
    return False


def fetch_readme_text(repo):
    """Decoded README.md text for a repo, or None (no README / error)."""
    r = gh_get(f"https://api.github.com/repos/{repo}/contents/README.md")
    if isinstance(r, dict) and r.get("content"):
        try:
            return base64.b64decode(r["content"]).decode("utf-8", errors="ignore")
        except Exception:
            return None
    return None


def has_chinese_docs(repo):
    text = fetch_readme_text(repo)
    return bool(text and has_cn(text))


def repo_meta(repo):
    data = gh_get(f"https://api.github.com/repos/{repo}")
    if not data:
        return {}
    return {
        "topics": data.get("topics", []),
        "description": data.get("description", "") or "",
        "stars": data.get("stargazers_count", 0) or 0,
        "pushed_at": data.get("pushed_at", ""),
        "full_name": data.get("full_name", repo),
        "html_url": data.get("html_url", f"https://github.com/{repo}"),
    }


def author_location(author):
    data = gh_get(f"https://api.github.com/users/{author}")
    if not data:
        return ""
    return data.get("location", "") or ""


def repo_releases(repo):
    """Return (latest release published_at, total downloads)."""
    data = gh_get(f"https://api.github.com/repos/{repo}/releases?per_page=5")
    if not isinstance(data, list):
        return None, 0
    downloads = sum(a.get("download_count", 0) for r in data for a in r.get("assets", []))
    published = data[0].get("published_at") if data else None
    return published, downloads


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def compute_scores(signals, stars=0, downloads=0, release_age_days=None):
    """Return (cn_score, quality_score). cn_score = text signals, quality
    = stars/downloads/recency. High cn_score means clearly Chinese-related;
    high quality score means a real audience."""
    cn = sum(CN_SIGNAL_WEIGHTS.get(s, 0) for s in signals)
    q = 0
    if stars:
        q += min(40, int(math.log2(max(1, stars)) * 4))
    if downloads >= 1000:
        q += 15
    elif downloads >= 200:
        q += 8
    if release_age_days is not None and release_age_days <= 180:
        q += 10
    return cn, q


def classify_tier(cn, q, first_run):
    """None = reject, 'auto' = high confidence, 'review' = needs a human."""
    if cn < MIN_CN_SCORE:
        return None
    if not first_run and (cn + q) >= AUTO_TOTAL_SCORE and q >= MIN_QUALITY_SCORE:
        return "auto"
    return "review"


def collect_signals(plugin, meta, author_loc, has_locale, has_docs):
    """All Chinese-relevance text signals for a plugin, as a set."""
    name, author, desc = plugin.get("name", ""), plugin.get("author", ""), plugin.get("description", "")
    signals = set()
    if has_locale:
        signals.add("locale")
    if has_cn(author):
        signals.add("author_cn")
    if any(t.lower() in CH_TOPICS for t in meta.get("topics", [])):
        signals.add("topic")
    if has_cn(meta.get("description", "")) and len(re.findall(r"[\u4e00-\u9fff]", meta.get("description", ""))) > 5:
        signals.add("desc_cn")
    if any(kw in author_loc.lower() for kw in CN_CITIES):
        signals.add("location")
    if has_cn(name):
        signals.add("name_cn")
    if has_docs:
        signals.add("docs_cn")
    return signals


# --------------------------------------------------------------------------
# Stale detection
# --------------------------------------------------------------------------

def decide_stale(repo_data, latest_release_published, cutoff):
    """Return stale record dict or None. A plugin is stale only if both its
    pushed_at and its latest release predate the cutoff (release activity
    rescues repos whose default branch just happens to be quiet)."""
    pushed = parse_github_time(repo_data.get("pushed_at"))
    if pushed and pushed >= cutoff:
        return None
    released = parse_github_time(latest_release_published)
    if released and released >= cutoff:
        return None
    return {
        "last_update": latest_release_published or repo_data.get("pushed_at") or "unknown",
        "stale": True,
    }


def stale_plugin_row(row, cutoff):
    meta = repo_meta(row["repo"])
    if not meta:
        print(f"WARN: cannot read repo metadata for {row['repo']}", file=sys.stderr)
        return None
    decision = decide_stale(meta, None, cutoff)
    if decision is None:
        return None
    released, _ = repo_releases(row["repo"])
    decision = decide_stale(meta, released, cutoff)
    if decision is None:
        return None
    return {
        "name": row["name"],
        "repo": row["repo"],
        "full_name": meta["full_name"],
        "author": row["author"],
        "section": row["section"],
        "last_update": decision["last_update"],
        "html_url": meta["html_url"],
    }


def find_stale_plugins(readme_text):
    cutoff = utcnow() - timedelta(days=STALE_DAYS)
    rows = parse_plugin_rows(readme_text)
    if not rows:
        return []
    stale = []
    workers = max(1, min(STALE_CHECK_WORKERS, len(rows)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(stale_plugin_row, row, cutoff) for row in rows]
        for future in as_completed(futures):
            try:
                stale_row = future.result()
            except Exception as e:
                print(f"WARN: stale check failed: {e}", file=sys.stderr)
                continue
            if stale_row:
                stale.append(stale_row)
    return sorted(stale, key=lambda r: (r["section"].casefold(), r["author"].casefold(), r["name"].casefold()))


# --------------------------------------------------------------------------
# Cache and denylist
# --------------------------------------------------------------------------

def load_checked():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def save_checked(ids):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, ensure_ascii=False)


def load_denylist():
    try:
        with open(DENY_FILE, encoding="utf-8") as f:
            return {r.strip().lower() for r in json.load(f) if isinstance(r, str)}
    except (OSError, ValueError):
        return set()


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------

def write_scan_output(add_rows, remove_rows, cache_changed, first_run, has_review):
    content_changed = bool(add_rows or remove_rows)
    auto_merge_ready = content_changed and not first_run and not has_review
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"add_rows={json.dumps(add_rows, ensure_ascii=False)}\n")
            f.write(f"remove_rows={json.dumps(remove_rows, ensure_ascii=False)}\n")
            f.write(f"content_changed={str(content_changed).lower()}\n")
            f.write(f"cache_changed={str(cache_changed).lower()}\n")
            f.write(f"first_run={str(first_run).lower()}\n")
            f.write(f"has_review={str(has_review).lower()}\n")
            f.write(f"auto_merge_ready={str(auto_merge_ready).lower()}\n")
        return
    print(json.dumps({
        "add": add_rows,
        "remove": remove_rows,
        "cache_changed": cache_changed,
        "first_run": first_run,
        "has_review": has_review,
        "auto_merge_ready": auto_merge_ready,
    }, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------
# Scan flow
# --------------------------------------------------------------------------

def run_scan(skip_stale=False):
    session = create_retry_session(timeout=HTTP_TIMEOUT)
    try:
        all_plugins = session.get(COMMUNITY_URL, timeout=HTTP_TIMEOUT).json()
    except requests.exceptions.RequestException as e:
        print(f"ERROR: cannot fetch community plugins list: {e}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(all_plugins, list):
        print("ERROR: unexpected community plugins payload", file=sys.stderr)
        sys.exit(1)

    with open(README_PATH, encoding="utf-8") as f:
        readme_text = f.read()
    existing = parse_current_plugins(readme_text)
    denied = load_denylist()

    checked = load_checked()
    all_ids = {p["id"] for p in all_plugins}
    checked &= all_ids  # prune ids removed from the official list
    first_run = len(checked) == 0
    new_ids = all_ids - checked if not first_run else all_ids

    new_plugins = [p for p in all_plugins if p["id"] in new_ids]
    print(f"known={len(checked)} new={len(new_plugins)}" + (" (full scan on first run)" if first_run else ""), file=sys.stderr)

    candidates = []
    scanned = 0
    for p in new_plugins:
        repo = p.get("repo", "")
        if not repo or "/" not in repo or repo.lower() in existing or repo.lower() in denied:
            continue
        name, author, desc = p.get("name", ""), p.get("author", ""), p.get("description", "")
        if not (has_cn(name) or has_cn(author) or has_cn(desc)):
            continue
        scanned += 1
        meta = repo_meta(repo)
        if not meta:
            continue
        readme_text = fetch_readme_text(repo)
        signals = collect_signals(
            p, meta,
            author_location(author),
            has_locale_file(repo),
            bool(readme_text and has_cn(readme_text)),
        )
        cn, q = compute_scores(signals, meta["stars"], 0, None)
        if cn < MIN_CN_SCORE:
            continue
        published, downloads = repo_releases(repo)
        release_age = None
        if published:
            released = parse_github_time(published)
            if released:
                release_age = (utcnow() - released).days
        cn, q = compute_scores(signals, meta["stars"], downloads, release_age)
        tier = classify_tier(cn, q, first_run)
        if not tier:
            continue
        candidates.append({
            "name": name, "repo": repo, "author": author,
            "desc": clean_desc(desc, readme_text),
            "cn": cn, "q": q, "tier": tier, "signals": sorted(signals),
        })

    candidates.sort(key=lambda c: (-c["cn"] - c["q"], c["repo"].casefold()))
    add_rows = [{
        "name": c["name"], "repo": c["repo"], "author": c["author"],
        "desc": c["desc"], "tier": c["tier"], "score": c["cn"] + c["q"],
    } for c in candidates]
    has_review = any(c["tier"] == "review" for c in candidates)

    stale = find_stale_plugins(readme_text) if not skip_stale else []
    stale_rows = [{
        "name": s["name"], "repo": s["repo"], "full_name": s["full_name"],
        "author": s["author"], "section": s["section"],
        "last_update": s["last_update"], "html_url": s["html_url"],
    } for s in stale]

    cache_changed = bool(new_ids)
    if cache_changed:
        save_checked(all_ids)

    print(
        f"known={len(all_ids)} scanned={scanned} auto={sum(1 for c in candidates if c['tier'] == 'auto')} "
        f"review={sum(1 for c in candidates if c['tier'] == 'review')} stale={len(stale)}",
        file=sys.stderr,
    )
    write_scan_output(add_rows, stale_rows, cache_changed, first_run, has_review)


# --------------------------------------------------------------------------
# Apply flow
# --------------------------------------------------------------------------

def update_title(add_count, remove_count, date):
    if add_count and remove_count:
        return f"Update Chinese-relevant plugins ({date})"
    if add_count:
        return f"Add Chinese-relevant plugins ({date})"
    return f"Remove stale plugins ({date})"


def build_pr_body(rows, stale_rows):
    title_rows = [r for r in rows if r["tier"] == "auto"]
    review_rows = [r for r in rows if r["tier"] == "review"]
    parts = []
    if title_rows:
        parts.append("### Auto-merge candidates (high confidence)\n")
        parts.append("| Plugin | Author | Description | Score |\n| --- | --- | --- | --- |\n")
        for r in sorted(title_rows, key=lambda r: r["author"].casefold()):
            desc = r["desc"].replace("|", "\\|")
            parts.append(f'| [{r["name"]}](https://github.com/{r["repo"]}) | `{r["author"]}` | {desc} | {r["score"]} |\n')
        parts.append("\n")
    if review_rows:
        parts.append("### Review candidates (medium confidence)\n")
        parts.append("These match the Chinese-relevance criteria but lack strong quality signals; please verify before merging.\n\n")
        parts.append("| Plugin | Author | Description | Score |\n| --- | --- | --- | --- |\n")
        for r in sorted(review_rows, key=lambda r: r["author"].casefold()):
            desc = r["desc"].replace("|", "\\|")
            parts.append(f'| [{r["name"]}](https://github.com/{r["repo"]}) | `{r["author"]}` | {desc} | {r["score"]} |\n')
        parts.append("\n")
    if stale_rows:
        parts.append(f"### Plugins removed after {STALE_DAYS} days without repository or release activity\n\n")
        parts.append("| Plugin | Section | Last activity | Repository |\n| --- | --- | --- | --- |\n")
        for r in sorted(stale_rows, key=lambda r: (r["section"].casefold(), r["author"].casefold(), r["name"].casefold())):
            parts.append(f'| {r["name"]} | {r["section"]} | {r["last_update"]} | [{r["full_name"]}]({r["html_url"]}) |\n')
        parts.append("\n")
    if review_rows:
        parts.append("_This PR has review-tier entries, so auto-merge was skipped._\n")
    parts.append("\n_This PR was generated by the weekly plugin scan._")
    return "".join(parts)


def emit_output(name, value):
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def do_apply_readme():
    rows = json.loads(os.environ.get("ADD_ROWS", "[]"))
    stale_rows = json.loads(os.environ.get("REMOVE_ROWS", "[]"))
    with open(README_PATH, encoding="utf-8") as f:
        text = f.read()

    text = remove_plugin_rows(text, stale_rows)
    text = append_rows_to_other_tools(text, rows)
    text = sort_plugin_tables_by_author(text)
    with open(README_PATH, "w", encoding="utf-8") as f:
        f.write(text)

    title = update_title(len(rows), len(stale_rows), utcnow().strftime("%Y%m%d"))
    body = build_pr_body(rows, stale_rows)
    os.makedirs(os.path.dirname(PR_BODY_FILE), exist_ok=True)
    with open(PR_BODY_FILE, "w", encoding="utf-8") as f:
        f.write(body)
    emit_output("pr_title", title)


def main():
    args = sys.argv[1:]
    if "--apply-readme" in args:
        do_apply_readme()
    else:
        run_scan(skip_stale="--skip-stale" in args)


if __name__ == "__main__":
    main()
