#!/usr/bin/env python3
import json, os, re, requests, base64, sys, subprocess

COMMUNITY_URL = "https://raw.githubusercontent.com/obsidianmd/obsidian-releases/master/community-plugins.json"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
API_HEADERS = {"Authorization": f"Bearer {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
REPO_NAME = os.environ.get("GITHUB_REPOSITORY", "PKM-er/awesome-obsidian-zh")
CACHE_FILE = ".github/scripts/checked_plugins.json"

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


def gh_get(url):
    r = requests.get(url, headers=API_HEADERS)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def has_locale_file(repo):
    for path in LOCALE_PATHS:
        r = gh_get(f"https://api.github.com/repos/{repo}/contents/{path}")
        if r and "content" in r:
            return True
    root = gh_get(f"https://api.github.com/repos/{repo}/contents/")
    if root and isinstance(root, list):
        for item in root:
            if item["type"] == "dir" and item["name"] in ("lang", "locale", "i18n", "l10n", "translations"):
                try:
                    sub = requests.get(item["url"], headers=API_HEADERS).json()
                    if isinstance(sub, list):
                        for f in sub:
                            if re.search(r"^zh[\-]?(cn|tw|hant|hans)?\.", f["name"].lower()):
                                return True
                except:
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


def load_checked():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except:
        return set()


def save_checked(ids):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f)


def run_scan():
    all_plugins = requests.get(COMMUNITY_URL).json()
    r = gh_get(f"https://api.github.com/repos/{REPO_NAME}/contents/README.md")
    if not r:
        print("ERROR: cannot read README", file=sys.stderr)
        sys.exit(1)
    readme_text = base64.b64decode(r["content"]).decode("utf-8")
    existing = parse_current_plugins(readme_text)
    checked = load_checked()
    all_ids = {p["id"] for p in all_plugins}

    is_first_run = len(checked) == 0
    if is_first_run:
        new_plugin_ids = all_ids
    else:
        new_plugin_ids = all_ids - checked
        if not new_plugin_ids:
            print(f"No new plugins since last check ({len(checked)} known)", file=sys.stderr)
            return

    new_plugins = [p for p in all_plugins if p["id"] in new_plugin_ids]

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
        if score >= 50:
            candidates.append({
                "name": name, "repo": repo, "author": author,
                "desc": desc, "score": score, "signals": signals,
            })

    candidates.sort(key=lambda x: -x["score"])
    high = [c for c in candidates if c["score"] >= 70]

    # Update cache
    all_ids = checked | new_plugin_ids
    save_checked(all_ids)

    print(f"Known plugins: {len(all_ids)}, new since last check: {len(new_plugins)}, scanned: {scanned}, candidates: {len(high)}", file=sys.stderr)

    output = os.environ.get("GITHUB_OUTPUT", "")
    if not output:
        print(json.dumps({"high": high}, ensure_ascii=False, indent=2))
        return

    rows = []
    for c in high:
        rows.append({
            "name": c["name"], "repo": c["repo"], "author": c["author"], "desc": c["desc"],
        })

    with open(output, "a", encoding="utf-8") as f:
        f.write(f"add_rows={json.dumps(rows, ensure_ascii=False)}\n")
        f.write(f"high_count={len(high)}\n")


def do_apply():
    rows = json.loads(os.environ["ADD_ROWS"])
    readme_path = "README.md"
    with open(readme_path, "r", encoding="utf-8") as f:
        text = f.read()
    anchor = "## 精选中文主题"
    insert_pos = text.rfind("| [Password Protection](https://github.com/qing3962/password-protection)")
    if insert_pos == -1:
        insert_pos = text.rfind("|", 0, text.find(anchor))
    eol = text.find("\n", insert_pos)
    line_end = text.find("\n", eol + 1) if text[eol + 1:eol + 2] != "\n" else eol
    rows_sorted = sorted(rows, key=lambda r: r["author"].lower())
    new_rows = ""
    for r in rows_sorted:
        new_rows += f'| [{r["name"]}](https://github.com/{r["repo"]}) | `{r["author"]}` | {r["desc"]} |\n'
    text = text[:line_end + 1] + new_rows + text[line_end + 1:]
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(text)
    body = "## New Chinese-Relevant Plugins Detected\n\n"
    body += "The following plugins match the native-Chinese or Chinese-translation criteria:\n\n"
    body += "| Plugin | Author | Description |\n"
    body += "| --- | --- | --- |\n"
    for r in rows_sorted:
        body += f'| [{r["name"]}](https://github.com/{r["repo"]}) | `{r["author"]}` | {r["desc"]} |\n'
    body += "\n\n_This PR was automatically generated by the weekly plugin scan._"
    date = os.popen("date +%Y%m%d").read().strip() or os.popen("powershell Get-Date -Format yyyyMMdd").read().strip()
    branch = f"auto/new-plugins-{date}"
    subprocess.run(["git", "checkout", "-b", branch], check=True)
    subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
    subprocess.run(["git", "add", readme_path, CACHE_FILE], check=True)
    r = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if r.returncode == 0:
        print("No changes to commit")
        return
    subprocess.run(["git", "commit", "-m", "Auto-add new Chinese-relevant plugins [skip ci]"], check=True)
    subprocess.run(["git", "push", "origin", branch], check=True)
    subprocess.run(["gh", "pr", "create",
                    "--title", f"Add new Chinese-relevant plugins ({date})",
                    "--body", body,
                    "--head", branch], check=True)


if __name__ == "__main__":
    if "--apply" in sys.argv:
        do_apply()
    else:
        run_scan()
