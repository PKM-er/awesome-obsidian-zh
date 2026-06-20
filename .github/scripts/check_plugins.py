#!/usr/bin/env python3
import json, os, re, requests, base64, sys, subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

COMMUNITY_URL = "https://raw.githubusercontent.com/obsidianmd/obsidian-releases/master/community-plugins.json"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
API_HEADERS = {"Authorization": f"Bearer {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
REPO_NAME = os.environ.get("GITHUB_REPOSITORY", "PKM-er/awesome-obsidian-zh")
CACHE_FILE = ".github/scripts/checked_plugins.json"
PLUGIN_SECTION_START = "## 原生中文插件"
PLUGIN_SECTION_END = "## 精选中文主题"
OTHER_TOOLS_SECTION = "### 其他工具"
STALE_DAYS = int(os.environ.get("STALE_DAYS", "365"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "30"))
STALE_HTTP_TIMEOUT = int(os.environ.get("STALE_HTTP_TIMEOUT", "10"))
STALE_CHECK_WORKERS = int(os.environ.get("STALE_CHECK_WORKERS", "12"))
PLUGIN_ROW_RE = re.compile(
    r"^\| \[([^\]]+)\]\(https://github\.com/([^/|)]+/[^)|\s]+)\)\s*\|\s*`?([^`|]+)`?\s*\|",
    re.M,
)

LOCALE_PATHS = [
    "lang/zh.json", "lang/zh-cn.json", "lang/zh-CN.json",
    "locale/zh.json", "locale/zh-cn.json", "locale/zh-CN.json",
    "i18n/zh.json", "i18n/zh-cn.json", "i18n/zh-CN.json",
    "l10n/zh.json", "l10n/zh-cn.json", "l10n/zh-CN.json",
    "src/lang/zh.json", "src/lang/zh-cn.json",
    "src/locale/zh.json", "src/locale/zh-cn.json",
    "src/i18n/zh.json", "src/i18n/zh-cn.json",
    "src/l10n/zh.json", "src/l10n/zh-cn.json",
    "translations/zh.json", "translations/zh-cn.json",
    "src/lang/locale/zh-cn.ts", "src/translations/locale/zh-cn.ts",
    "src/lang/zh-cn.ts", "src/locale/zh-cn.ts", "src/i18n/zh-cn.ts",
]


def create_retry_session(retries=3, backoff_factor=0.3, timeout=None):
    """创建带有重试机制的 requests 会话"""
    if timeout is None:
        timeout = HTTP_TIMEOUT
    session = requests.Session()
    retry_strategy = Retry(
        total=retries,
        connect=retries,
        read=retries,
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
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
    except requests.exceptions.Timeout:
        print(f"WARN: Request timeout for {url}, returning None", file=sys.stderr)
        return None
    except requests.exceptions.RequestException as e:
        print(f"WARN: Request failed for {url}: {e}", file=sys.stderr)
        return None


def has_locale_file(repo):
    try:
        root = gh_get(f"https://api.github.com/repos/{repo}/contents/")
        if not root or not isinstance(root, list):
            return False
        dirs = {item["name"] for item in root if item["type"] == "dir"}
        candidates_dirs = {"lang", "locale", "i18n", "l10n", "translations", "src"}
        to_check = candidates_dirs & dirs
        for d in sorted(to_check):
            try:
                sub = gh_get(f"https://api.github.com/repos/{repo}/contents/{d}")
                if not isinstance(sub, list):
                    continue
                names = [item["name"].lower() for item in sub]
                if d == "src":
                    subdirs = {item["name"] for item in sub if item["type"] == "dir"}
                    for sd in subdirs & {"lang", "locale", "i18n", "l10n", "translations"}:
                        try:
                            sub2 = gh_get(f"https://api.github.com/repos/{repo}/contents/src/{sd}")
                            if isinstance(sub2, list):
                                names.extend(item["name"].lower() for item in sub2)
                        except Exception:
                            pass
                if any(re.search(r"^zh[\-]?(cn|tw|hant|hans)?\.", n) for n in names):
                    return True
            except Exception as e:
                print(f"WARN: Failed to check {d} in {repo}: {e}", file=sys.stderr)
                continue
        return False
    except Exception as e:
        print(f"WARN: has_locale_file failed for {repo}: {e}", file=sys.stderr)
        return False


# 修改：仅检查 README.md 以减少 API 请求
def has_chinese_docs(repo):
    """仅检查 README.md 是否包含中文字符（减少 API 调用）。"""
    try:
        r = gh_get(f"https://api.github.com/repos/{repo}/contents/README.md")
        if r and isinstance(r, dict) and r.get("content"):
            try:
                text = base64.b64decode(r["content"]).decode("utf-8", errors="ignore")
                if has_cn(text):
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def repo_info(repo):
    data = gh_get(f"https://api.github.com/repos/{repo}")
    if not data:
        return [], ""
    return data.get("topics", []), data.get("description", "") or ""


def author_location(author):
    data = gh_get(f"https://api.github.com/users/{author}")
    if not data:
        return ""
    return data.get("location", "") or ""


def parse_current_plugins(readme_text):
    existing = set()
    for m in re.finditer(r"\]\(https://github\.com/([^/]+/[^/)\s]+)\)", readme_text):
        existing.add(m.group(1).rstrip("/").lower())
    return existing


def has_cn(s):
    return bool(re.search(r"[\u4e00-\u9fff]", s))


def parse_github_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def plugin_section_bounds(text):
    start = text.find(PLUGIN_SECTION_START)
    end = text.find(PLUGIN_SECTION_END, start)
    if start == -1 or end == -1:
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


def stale_plugin_row(row, cutoff):
    session = create_retry_session(retries=2, timeout=STALE_HTTP_TIMEOUT)
    try:
        r = session.get(
            f"https://api.github.com/repos/{row['repo']}",
            headers=API_HEADERS,
            timeout=STALE_HTTP_TIMEOUT,
        )
        if r.status_code == 404:
            data = None
        else:
            r.raise_for_status()
            data = r.json()
    except requests.exceptions.Timeout:
        print(f"WARN: timeout reading repo metadata for {row['repo']}", file=sys.stderr)
        return None
    except requests.RequestException as exc:
        print(f"WARN: cannot read repo metadata for {row['repo']}: {exc}", file=sys.stderr)
        return None
    if not data:
        print(f"WARN: cannot read repo metadata for {row['repo']}", file=sys.stderr)
        return None
    pushed_at = data.get("pushed_at")
    pushed_time = parse_github_time(pushed_at)
    if not pushed_time or pushed_time >= cutoff:
        return None
    return {
        "name": row["name"],
        "repo": row["repo"],
        "full_name": data.get("full_name", row["repo"]),
        "author": row["author"],
        "section": row["section"],
        "pushed_at": pushed_at,
        "html_url": data.get("html_url", f"https://github.com/{row['repo']}"),
    }


def find_stale_plugins(readme_text):
    cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_DAYS)
    rows = parse_plugin_rows(readme_text)
    if not rows:
        return []
    stale = []
    workers = max(1, min(STALE_CHECK_WORKERS, len(rows)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(stale_plugin_row, row, cutoff) for row in rows]
        for future in as_completed(futures):
            stale_row = future.result()
            if stale_row:
                stale.append(stale_row)
    return sorted(stale, key=lambda r: (r["section"].casefold(), r["author"].casefold(), r["name"].casefold()))


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
    matches = list(re.finditer(r"^\| \[.*\n?", block, re.M))
    if not matches:
        return text

    insert_pos = section_start + matches[-1].end()
    new_rows = ""
    for r in sorted(rows, key=lambda row: row["author"].casefold()):
        new_rows += f'| [{r["name"]}](https://github.com/{r["repo"]}) | `{r["author"]}` | {r["desc"]} |\n'
    return text[:insert_pos] + new_rows + text[insert_pos:]


def load_checked():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except:
        return set()


def save_checked(ids):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f)


def write_scan_output(add_rows, remove_rows, cache_changed):
    output = os.environ.get("GITHUB_OUTPUT", "")
    update_needed = bool(add_rows or remove_rows or cache_changed)
    if not output:
        print(json.dumps({
            "add": add_rows,
            "remove": remove_rows,
            "cache_changed": cache_changed,
        }, ensure_ascii=False, indent=2))
        return
    with open(output, "a", encoding="utf-8") as f:
        f.write(f"add_rows={json.dumps(add_rows, ensure_ascii=False)}\n")
        f.write(f"remove_rows={json.dumps(remove_rows, ensure_ascii=False)}\n")
        f.write(f"high_count={len(add_rows)}\n")
        f.write(f"stale_count={len(remove_rows)}\n")
        f.write(f"cache_changed={str(cache_changed).lower()}\n")
        f.write(f"update_needed={str(update_needed).lower()}\n")


def read_current_readme():
    if os.path.exists("README.md"):
        with open("README.md", encoding="utf-8") as f:
            return f.read()
    r = gh_get(f"https://api.github.com/repos/{REPO_NAME}/contents/README.md")
    if not r:
        return ""
    return base64.b64decode(r["content"]).decode("utf-8")


def env_enabled(name):
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def auto_merge_pr(pr_url, head_sha, subject):
    merge_cmd = [
        "gh", "pr", "merge", pr_url,
        "--squash",
        "--delete-branch",
        "--match-head-commit", head_sha,
        "--subject", subject,
        "--body", "Automatically merged by the weekly plugin scan.",
    ]
    result = subprocess.run(merge_cmd)
    if result.returncode == 0:
        return
    print("Immediate PR merge failed; trying GitHub auto-merge.", file=sys.stderr)
    subprocess.run(merge_cmd + ["--auto"], check=True)


def run_scan():
    session = create_retry_session(timeout=HTTP_TIMEOUT)
    try:
        all_plugins = session.get(COMMUNITY_URL, timeout=HTTP_TIMEOUT).json()
    except requests.exceptions.RequestException as e:
        print(f"ERROR: cannot fetch community plugins list: {e}", file=sys.stderr)
        sys.exit(1)
    
    readme_text = read_current_readme()
    if not readme_text:
        print("ERROR: cannot read README", file=sys.stderr)
        sys.exit(1)
    existing = parse_current_plugins(readme_text)
    checked = load_checked()
    all_ids = {p["id"] for p in all_plugins}
    new_plugin_ids = set()

    is_first_run = len(checked) == 0
    if is_first_run:
        new_plugin_ids = all_ids
    else:
        new_plugin_ids = all_ids - checked

    new_plugins = [p for p in all_plugins if p["id"] in new_plugin_ids]
    if not new_plugins:
        print(f"No new plugins since last check ({len(checked)} known)", file=sys.stderr)

    print(f"Known: {len(checked)}, new: {len(new_plugins)}" + (" (full scan on first run)" if is_first_run else ""), file=sys.stderr)
    candidates = []
    scanned = 0

    for p in new_plugins:
        repo = p.get("repo", "")
        if not repo or "/" not in repo:
            continue
        if repo.lower() in existing:
            continue
        name = p.get("name", "")
        author = p.get("author", "")
        desc = p.get("description", "")
        if not (has_cn(name) or has_cn(author) or has_cn(desc)):
            continue
        scanned += 1
        score = 0
        signals = []
        if has_locale_file(repo):
            score += 50
            signals.append("locale")
        if has_cn(author):
            score += 15
            signals.append("author_cn")
        topics, repo_desc = repo_info(repo)
        ch_topics = {"chinese", "zh", "zh-cn", "chinese-translation", "obsidian-zh"}
        if any(t.lower() in ch_topics for t in topics):
            score += 20
            signals.append("topic")
        if repo_desc and has_cn(repo_desc) and len(re.findall(r"[\u4e00-\u9fff]", repo_desc)) > 5:
            score += 10
            signals.append("desc_cn")
        loc = author_location(author)
        cn_kw = ["china", "chinese", "taiwan", "hong kong", "beijing", "shanghai",
                 "shenzhen", "guangzhou", "chengdu", "nanjing", "wuhan",
                 "\u4e2d\u56fd", "\u53f0\u6e7e", "\u9999\u6e2f"]
        if any(kw in loc.lower() for kw in cn_kw):
            score += 15
            signals.append("location")
        if has_cn(name):
            score += 5
            signals.append("name_cn")
        # 新增：仓库中含中文文档加分（现在仅检查 README.md）
        try:
            if has_chinese_docs(repo):
                score += 20
                signals.append("docs_cn")
        except Exception:
            pass
        if score >= 50:
            candidates.append({
                "name": name, "repo": repo, "author": author,
                "desc": desc, "score": score, "signals": signals,
            })

    candidates.sort(key=lambda x: -x["score"])
    high = [c for c in candidates if c["score"] >= 50]
    stale = find_stale_plugins(readme_text)

    # Update cache
    all_ids = checked | new_plugin_ids
    cache_changed = all_ids != checked
    if cache_changed:
        save_checked(all_ids)

    print(
        f"Known plugins: {len(all_ids)}, new since last check: {len(new_plugins)}, "
        f"scanned: {scanned}, candidates: {len(high)}, stale: {len(stale)}",
        file=sys.stderr,
    )

    rows = []
    for c in high:
        rows.append({
            "name": c["name"], "repo": c["repo"], "author": c["author"], "desc": c["desc"],
        })

    write_scan_output(rows, stale, cache_changed)


def update_title(add_count, remove_count, date):
    if add_count and remove_count:
        return f"Update Chinese-relevant plugins ({date})"
    if add_count:
        return f"Add new Chinese-relevant plugins ({date})"
    if remove_count:
        return f"Remove stale Chinese-relevant plugins ({date})"
    return f"Update checked plugin cache ({date})"


def do_apply():
    rows = json.loads(os.environ["ADD_ROWS"])
    stale_rows = json.loads(os.environ.get("REMOVE_ROWS", "[]"))
    readme_path = "README.md"
    with open(readme_path, "r", encoding="utf-8") as f:
        text = f.read()
    rows_sorted = sorted(rows, key=lambda r: r["author"].casefold())
    stale_rows_sorted = sorted(stale_rows, key=lambda r: (r["section"].casefold(), r["author"].casefold(), r["name"].casefold()))
    text = remove_plugin_rows(text, stale_rows_sorted)
    text = append_rows_to_other_tools(text, rows_sorted)
    text = sort_plugin_tables_by_author(text)
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(text)
    date = os.popen("date +%Y%m%d").read().strip() or os.popen("powershell Get-Date -Format yyyyMMdd").read().strip()
    title = update_title(len(rows_sorted), len(stale_rows_sorted), date)
    body = f"## {title}\n\n"
    if rows_sorted:
        body += "### New Chinese-Relevant Plugins Detected\n\n"
        body += "The following plugins match the native-Chinese or Chinese-translation criteria:\n\n"
        body += "| Plugin | Author | Description |\n"
        body += "| --- | --- | --- |\n"
        for r in rows_sorted:
            body += f'| [{r["name"]}](https://github.com/{r["repo"]}) | `{r["author"]}` | {r["desc"]} |\n'
        body += "\n"
    if stale_rows_sorted:
        body += f"### Plugins Removed After {STALE_DAYS} Days Without Repository Updates\n\n"
        body += "| Plugin | Section | Last pushed | Repository |\n"
        body += "| --- | --- | --- | --- |\n"
        for r in stale_rows_sorted:
            body += f'| {r["name"]} | {r["section"]} | {r["pushed_at"]} | [{r["full_name"]}]({r["html_url"]}) |\n'
        body += "\n"
    if not rows_sorted and not stale_rows_sorted:
        body += "No README rows changed; this updates the checked plugin cache so future runs only scan newer plugin IDs.\n\n"
    body += "\n\n_This PR was automatically generated by the weekly plugin scan._"
    branch = f"auto/new-plugins-{date}"
    subprocess.run(["git", "checkout", "-b", branch], check=True)
    subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
    subprocess.run(["git", "add", readme_path, CACHE_FILE], check=True)
    r = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if r.returncode == 0:
        print("No changes to commit")
        return
    subprocess.run(["git", "commit", "-m", f"{title} [skip ci]"], check=True)
    head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "push", "--force", "origin", branch], check=True)
    pr = subprocess.run(["gh", "pr", "create",
                         "--title", title,
                         "--body", body,
                         "--head", branch],
                        check=True, text=True, stdout=subprocess.PIPE)
    pr_url = pr.stdout.strip().splitlines()[-1]
    print(f"Created PR: {pr_url}")
    if env_enabled("AUTO_MERGE"):
        auto_merge_pr(pr_url, head_sha, title)


if __name__ == "__main__":
    if "--apply" in sys.argv:
        do_apply()
    else:
        run_scan()
