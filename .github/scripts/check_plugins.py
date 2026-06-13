#!/usr/bin/env python3
import json, os, re, requests, base64, sys

COMMUNITY_URL = "https://raw.githubusercontent.com/obsidianmd/obsidian-releases/master/community-plugins.json"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
API_HEADERS = {"Authorization": f"Bearer {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
REPO_NAME = os.environ.get("GITHUB_REPOSITORY", "PKM-er/awesome-obsidian-zh")

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


TABLE_ROW = '| [{name}](https://github.com/{repo}) | `{author}` | {desc} |'


def categorize(desc):
    desc_lower = desc.lower()
    if any(kw in desc_lower for kw in ("sync", "同步", "publish", "发布", "export", "导出", "backup", "备份")):
        return "数据同步与集成"
    if any(kw in desc_lower for kw in ("image", "图片", "upload", "上传", "media", "多媒体")):
        return "多媒体与附件"
    if any(kw in desc_lower for kw in ("task", "任务", "calendar", "日历", "progress", "进度", "schedule")):
        return "任务与日程管理"
    if any(kw in desc_lower for kw in ("edit", "编辑", "format", "格式化", "typing", "输入", "拼音", "mindmap", "思维导图")):
        return "编辑与格式化"
    if any(kw in desc_lower for kw in ("ai", "chatgpt", "gpt", "llm", "agent")):
        return "AI 辅助"
    if any(kw in desc_lower for kw in ("link", "链接", "graph", "图谱", "backlink")):
        return "链接与知识管理"
    if any(kw in desc_lower for kw in ("theme", "主题", "view", "视图", "outline", "目录", "icon")):
        return "界面与视图增强"
    return "其他工具"


def main():
    output = os.environ.get("GITHUB_OUTPUT", "")

    print("Fetching community plugins...", file=sys.stderr)
    all_plugins = requests.get(COMMUNITY_URL).json()

    print("Fetching current README...", file=sys.stderr)
    r = gh_get(f"https://api.github.com/repos/{REPO_NAME}/contents/README.md")
    if not r:
        print("ERROR: cannot read README", file=sys.stderr)
        sys.exit(1)
    readme_text = base64.b64decode(r["content"]).decode("utf-8")
    existing = parse_current_plugins(readme_text)

    candidates = []
    pre_filtered = 0

    for p in all_plugins:
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

        pre_filtered += 1
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
                 "shenzhen", "guangzhou", "chengdu", "nanjing", "wuhan", "shenyang",
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
    medium = [c for c in candidates if 50 <= c["score"] < 70]

    # summary
    print(f"Total plugins: {len(all_plugins)}", file=sys.stderr)
    print(f"Pre-filtered (CN text): {pre_filtered}", file=sys.stderr)
    print(f"High-confidence: {len(high)}", file=sys.stderr)
    for c in high:
        print(f"  [{c['score']}] {c['name']} ({c['repo']}) — {', '.join(c['signals'])}", file=sys.stderr)
    print(f"Review candidates: {len(medium)}", file=sys.stderr)

    if not output:
        # Not in Actions, print to stdout for inspection
        print(json.dumps({"high": high, "medium": medium}, ensure_ascii=False, indent=2))
        return

    # Generate markdown table rows for high-confidence plugins
    rows = []
    for c in high:
        section = categorize(c["desc"])
        row = TABLE_ROW.format(name=c["name"], repo=c["repo"], author=c["author"], desc=c["desc"])
        rows.append({"section": section, "row": row, "name": c["name"], "repo": c["repo"]})

    # Write GITHUB_OUTPUT
    with open(output, "a", encoding="utf-8") as f:
        payload = json.dumps(rows, ensure_ascii=False)
        f.write(f"add_rows={payload}\n")
        f.write(f"high_count={len(high)}\n")
        f.write(f"medium_count={len(medium)}\n")
        medium_payload = json.dumps(medium, ensure_ascii=False)
        f.write(f"medium_candidates={medium_payload}\n")

    # Write step summary
    summary = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary:
        with open(summary, "w", encoding="utf-8") as f:
            f.write("## Chinese Plugin Scan Results\n\n")
            f.write(f"- Total plugins checked: {len(all_plugins)}\n")
            f.write(f"- Pre-filter candidates: {pre_filtered}\n")
            f.write(f"- High-confidence (score ≥70): {len(high)}\n")
            f.write(f"- Needs review (score 50-69): {len(medium)}\n\n")
            if high:
                f.write("### High-Confidence\n\n")
                for c in high:
                    f.write(f"- [{c['name']}](https://github.com/{c['repo']}) by `{c['author']}` — {c['desc']}\n")
            if medium:
                f.write("\n### Needs Review\n\n")
                for c in medium:
                    f.write(f"- [{c['name']}](https://github.com/{c['repo']}) by `{c['author']}` — {c['desc']}\n")


if __name__ == "__main__":
    main()
