#!/usr/bin/env python3
"""Scan the official Obsidian plugin list for Chinese-relevant plugins.

Modes:
  (default)       fetch official list + cache diff, score candidates, report
                  dormant rows, write GitHub Actions outputs. Review-tier rows
                  are routed to the fixed "新插件观察区" section instead of the
                  theme tables; rows already in that zone are re-checked and
                  returned as promote_rows / drop_repos (no human gate).
  --apply-readme  edit README.md only from the ADD_ROWS / OBSERVE_ROWS /
                  PROMOTE_ROWS / DROP_REPOS env vars,
                  write .github/pr-body.md and the PR title (no git, no network).
                  Also de-duplicates, re-files catch-all entries into their
                  keyword-matched section, scrubs broken descriptions and
                  regenerates the table of contents. Dormant plugins are never
                  removed here; see --freshness-readme.
  --cleanup-readme  one-off pass over README.md: dedupe, re-file 其他工具
                    entries, scrub broken descriptions, regenerate TOC.
  --freshness-readme  one-off pass: append [已归档]/[长期未更新] markers and a
                    '最后更新 YYYY-MM' freshness badge to existing plugin rows
                    from live repo metadata (archived flag + last push/release
                    older than STALE_DAYS). Cached for FRESHNESS_CACHE_DAYS;
                    idempotent and self-correcting.
  --validate      check README.md for duplicate rows and broken/placeholder
                  descriptions; exits non-zero on any issue (CI gate).
  --skip-stale    skip the dormancy report (dry runs / rate-limit constrained
                  runs). Dormant rows are only reported either way, so this
                  never affects the README.

Improvements over the previous version:
  * quality scoring (stars, release downloads, release recency) in addition
    to text signals, split into "auto" (high confidence) and "review" tiers
  * stale detection uses the latest release date, falling back to pushed_at,
    instead of pushed_at alone
  * dormant plugins are marked `[长期未更新]`, never deleted: a year without a
    commit is weak evidence that a plugin is unwanted
  * cache-only runs no longer create PRs (workflow commits the cache directly)
  * denylist (denylist.json) prevents re-adding rejected plugins
  * review-tier rows wait in the fixed "新插件观察区" section; later scans
    promote them to a theme table or drop them, with no human gate
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
import threading
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
FRESHNESS_CACHE_FILE = ".github/scripts/freshness_cache.json"
FRESHNESS_CACHE_DAYS = int(os.environ.get("FRESHNESS_CACHE_DAYS", "7"))
# Serializes the load->modify->save of the freshness cache so concurrent
# freshness checks (ThreadPoolExecutor) cannot clobber each other's writes.
_FRESH_LOCK = threading.Lock()

PLUGIN_SECTION_START = "## 原生中文插件"
PLUGIN_SECTION_END = "## 精选中文主题"
OTHER_TOOLS_SECTION = "### 其他工具"

# Fixed, permanent section that holds freshly-discovered plugins whose
# Chinese relevance is clear but whose traction is not yet proven. It lives
# inside the plugin section (so the TOC and every row pipeline covers it),
# always renders after the theme sections, and collapses its table inside a
# <details> block so the curated tables keep the reader's attention. Rows are
# promoted to the theme tables or dropped by the scan alone; no human gate.
OBSERVE_SECTION = "### 新插件观察区"
OBSERVE_SECTION_NAME = "新插件观察区"
# Starts with '>' so find_unmanaged_rows() never reads it as a broken row.
OBSERVE_NOTE = (
    "> 新收录的插件先在此观察；达到活跃标准后自动转入上方的主题表格，"
    "长期没有动静的自动移出。"
)
OBSERVE_EMPTY_NOTE = "_（暂无观察中的插件）_"
OBSERVE_TABLE_SKELETON = "| 插件 | 作者 | 核心功能 |\n| --- | --- | --- |\n"
# Collapsed by default: the zone is a long tail of unproven entries, so it must
# not push the curated tables down. GitHub renders Markdown inside <details> as
# long as a blank line separates the tags from the table.
OBSERVE_DETAILS_OPEN = "<details>"
OBSERVE_DETAILS_CLOSE = "</details>"
OBSERVE_SUMMARY_LABEL = "展开查看观察中的插件"


def observe_summary(count):
    """Collapsed-block summary line; the count says how much is hidden."""
    return f"<summary>{OBSERVE_SUMMARY_LABEL}（{count}）</summary>"


def observe_details_header(count):
    return OBSERVE_DETAILS_OPEN + "\n" + observe_summary(count) + "\n\n"

SECTION_KEYWORDS = {
    "界面与视图增强": ["光标", "大纲", "视图", "目录", "网格", "悬浮", "侧边", "面板", "toolbar",
                      "outline", "toc", "view", "explorer", "preview", "缩略", "缩图", "minimap"],
    "编辑与格式化": ["编辑", "格式化", "输入法", "标点", "拼音", "分词", "typing", "edit", "format",
                    "pinyin", "思维导图", "mindmap", "mind map", "markmind", "标注", "latex", "公式"],
    "链接与知识管理": ["链接", "知识", "块", "block", "link", "knowledge", "反向链接", "图谱", "graph",
                      "relation", "语义", "标签", "tag", "dataview"],
    "任务与日程管理": ["任务", "日程", "日历", "农历", "节气", "lunar", "calendar", "task", "进度",
                      "progress", "看板", "kanban", "日记", "diary", "memo", "thino", "提醒", "reminder", "计划"],
    "多媒体与附件": ["图片", "图床", "附件", "音频", "视频", "媒体", "emoji", "表情", "image", "upload",
                    "attachment", "audio", "video", "相册", "pdf", "ocr"],
    "AI 辅助": ["ai", "人工智能", "智能", "gpt", "llm", "agent", "rag", "mcp", "claude", "deepseek",
                "对话", "大模型", "模型", "copilot", "提示词", "prompt"],
    "数据同步与集成": ["同步", "云", "webdav", "坚果云", "微信读书", "weread", "知乎", "公众号", "wechat",
                      "豆瓣", "导出", "发布", "导入", "得到", "五彩", "邮件", "消息", "sync", "export",
                      "publish", "import", "douban", "zhihu", "webhook", "rss"],
    "效率与系统": ["翻译", "i18n", "插件管理", "管理器", "自动化", "批量", "文件操作", "文件处理",
                   "窗口", "密码", "加密", "安装", "brat", "字符", "清理", "正则", "快捷", "工具箱", "util"],
    "学术与写作": ["化学", "化学式", "chem", "论文", "排版", "缩进", "段落", "paper", "学术",
                   "参考文献", "citation", "公式"],
    "思维导图与阅读": ["思维", "导图", "脑图", "mind", "播放列表", "阅读", "导航", "playlist", "outline"],
    "娱乐与多媒体": ["象棋", "棋", "xiangqi", "朗读", "语音", "tts", "跟读", "游戏", "game"],
}
CLASSIFY_PRIORITY = [
    "任务与日程管理", "数据同步与集成", "AI 辅助", "编辑与格式化",
    "链接与知识管理", "多媒体与附件", "界面与视图增强",
    "效率与系统", "学术与写作", "思维导图与阅读", "娱乐与多媒体",
]


def classify_section(name, desc):
    """Best-fit section for a plugin from keyword hits in name+desc.
    Falls back to '其他工具'. Routes both new scans and re-files the
    catch-all section's misplaced entries."""
    text = f"{name} {desc}".lower()

    def kw_match(kw):
        # Short ASCII keywords (ai, rag, mcp, ...) must be whole words so they
        # don't match inside larger words like "Aindent" or "explain".
        if kw.isascii() and kw.isalpha() and len(kw) <= 3:
            return bool(re.search(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", text))
        return kw in text

    scores = {}
    for section, kws in SECTION_KEYWORDS.items():
        s = sum(1 for kw in kws if kw_match(kw))
        if s:
            scores[section] = s
    if not scores:
        return "其他工具"
    return max(
        scores,
        key=lambda s: (scores[s], -CLASSIFY_PRIORITY.index(s) if s in CLASSIFY_PRIORITY else 0),
    )


STALE_DAYS = int(os.environ.get("STALE_DAYS", "365"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "30"))
STALE_HTTP_TIMEOUT = int(os.environ.get("STALE_HTTP_TIMEOUT", "10"))
STALE_CHECK_WORKERS = int(os.environ.get("STALE_CHECK_WORKERS", "12"))
MIN_CN_SCORE = int(os.environ.get("MIN_CN_SCORE", "35"))
AUTO_TOTAL_SCORE = int(os.environ.get("AUTO_TOTAL_SCORE", "100"))
MIN_QUALITY_SCORE = int(os.environ.get("MIN_QUALITY_SCORE", "20"))

# Observation-zone thresholds. Promotion moves a row out of the observation
# section into the theme tables; expiry deletes it from the README entirely.
# Every threshold is env-overridable so CI can retune without a code change.
OBSERVE_PROMOTE_STARS = int(os.environ.get("OBSERVE_PROMOTE_STARS", "5"))
OBSERVE_PROMOTE_DOWNLOADS = int(os.environ.get("OBSERVE_PROMOTE_DOWNLOADS", "500"))
OBSERVE_MIN_AGE_DAYS = int(os.environ.get("OBSERVE_MIN_AGE_DAYS", "30"))
OBSERVE_EXPIRE_DAYS = int(os.environ.get("OBSERVE_EXPIRE_DAYS", "120"))
OBSERVE_EXPIRE_MIN_STARS = int(os.environ.get("OBSERVE_EXPIRE_MIN_STARS", "3"))
OBSERVE_EXPIRE_MIN_DOWNLOADS = int(os.environ.get("OBSERVE_EXPIRE_MIN_DOWNLOADS", "200"))
# Expiry also needs the repo to look abandoned. This inactivity window is not
# env-configurable on purpose: the six knobs above are the full tuning surface.
OBSERVE_EXPIRE_INACTIVE_DAYS = 180

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

# README-based Chinese evidence ("rule F"). Metadata alone is not enough: a
# plugin can be named, described and authored in English while its README is
# written entirely in Chinese (issue 36: Class Timetable, Vault Orrery).
KANA_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")  # hiragana + katakana
CN_MARK_RE = re.compile(r"中文|简体|繁體|繁体|汉语|漢語|汉化|漢化")
CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
# A README with this many Han characters is Chinese-authored; below it we
# require an explicit Chinese marker outside code spans (a language link,
# '中文' in prose) so a stray CJK filename cannot admit a plugin.
README_CJK_BODY_MIN = 200

CN_SIGNAL_WEIGHTS = {
    "locale": 30,
    "readme_zh": 20,
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
        # Surface the HTTP status (401 => bad token, 403 => rate limit,
        # None => network) so a broken GH_PAT shows up in logs instead of a
        # silent `{}` freshness cache.
        status = getattr(getattr(e, "response", None), "status_code", None)
        auth = "GITHUB_TOKEN set" if GITHUB_TOKEN else "GITHUB_TOKEN absent (unauthenticated)"
        print(f"WARN: request failed for {url}: {e} [HTTP {status}, {auth}]", file=sys.stderr)
        return None


def verify_api_auth(context=""):
    """Abort the run when the GitHub API token cannot be used.

    An unusable token used to degrade every API call into a silent `None`:
    the scan reported zero candidates, the freshness pass rewrote the README
    with no data, and the workflow still finished green for a month. An auth
    problem is deterministic, not transient, so it must fail the run loudly
    instead of degrading. Transient per-repo failures still degrade quietly.
    """
    where = f" ({context})" if context else ""
    if not GITHUB_TOKEN:
        print(
            f"ERROR: GITHUB_TOKEN is not set{where}. The plugin scan needs an "
            "authenticated GitHub API token: export GITHUB_TOKEN=$(gh auth token) "
            "locally, or set the GH_PAT secret for the workflow. Aborting before "
            "any README change.",
            file=sys.stderr,
        )
        sys.exit(1)
    session = create_retry_session(retries=0, timeout=min(HTTP_TIMEOUT, 15))
    try:
        # /rate_limit is free (does not consume the hourly budget) and returns
        # 401 for a rejected token, so it is the cheapest auth probe available.
        r = session.get(
            "https://api.github.com/rate_limit",
            headers=API_HEADERS,
            timeout=min(HTTP_TIMEOUT, 15),
        )
        if r.status_code == 401:
            print(
                f"ERROR: GitHub rejected GITHUB_TOKEN (HTTP 401){where}. The "
                "token is expired, revoked or malformed. Regenerate it and "
                "update the secret that feeds this workflow; until then the scan "
                "would silently find nothing. Aborting before any README change.",
                file=sys.stderr,
            )
            sys.exit(1)
        if r.status_code == 403:
            print(
                f"ERROR: GitHub refused the API request (HTTP 403){where} — "
                "rate limit exhausted or the token lacks access. Aborting before "
                "any README change.",
                file=sys.stderr,
            )
            sys.exit(1)
        r.raise_for_status()
        remaining = r.json().get("resources", {}).get("core", {}).get("remaining")
    except requests.exceptions.RequestException as e:
        print(
            f"ERROR: GitHub API unreachable{where}: {e}. Aborting before any "
            "README change.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(
        f"GitHub API auth OK{where}: {remaining} core requests remaining",
        file=sys.stderr,
    )


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def has_cn(s):
    return bool(re.search(r"[\u4e00-\u9fff]", s))


def outside_code(text):
    """README text with fenced blocks and inline code spans removed, so a
    Chinese path inside a code sample is not read as documentation language."""
    if not text:
        return ""
    return INLINE_CODE_RE.sub(" ", CODE_FENCE_RE.sub(" ", text))


def readme_zh_evidence(text):
    """True when the README is Chinese-authored. Returns the matching rule
    name ('cjk-body' / 'zh-word') or None, for use in scan diagnostics."""
    if not text or KANA_RE.search(text):
        return None
    if len(re.findall(r"[\u4e00-\u9fff]", text)) >= README_CJK_BODY_MIN:
        return "cjk-body"
    if CN_MARK_RE.search(outside_code(text)):
        return "zh-word"
    return None


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
    for raw in section.splitlines():
        # A '### ' heading may be glued to the previous line (e.g. a note or a
        # table row with no trailing newline). Split it out so section tracking
        # still works; otherwise every row until the next clean heading would be
        # mis-filed under an empty section and dropped by reorganize_sections.
        for line in re.split(r"(?=### )", raw):
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
        desc = clean_desc_for_readme(r.get("desc", ""), name=r.get("name", "")).replace("|", "\\|")
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

    # No rows and no separator: the emptied catch-all was left as just its
    # heading and a note (what reorganize_sections leaves behind after the
    # last row is re-filed). Rebuild the table skeleton instead of silently
    # dropping the rows -- the scan has already reported content_changed, so
    # a silent no-op here crashes the workflow's commit step (2026-10-07).
    skeleton = "| 插件 | 作者 | 核心功能 |\n| --- | --- | --- |\n"
    header_end = text.find("\n", section_start)
    if header_end == -1 or header_end + 1 > section_end:
        return text[:section_end] + "\n" + skeleton + new_rows + text[section_end:]
    insert_pos = header_end + 1
    return text[:insert_pos] + "\n" + skeleton + new_rows + text[insert_pos:]


def observe_section_bounds(text):
    """Return (start, end) of the fixed observation section, or (-1, -1)."""
    start, plugin_end = plugin_section_bounds(text)
    if start is None:
        return -1, -1
    section_start = text.find(OBSERVE_SECTION, start, plugin_end)
    if section_start == -1:
        return -1, -1
    next_section = text.find("\n### ", section_start + len(OBSERVE_SECTION), plugin_end)
    section_end = next_section if next_section != -1 else plugin_end
    return section_start, section_end


def parse_observe_rows(readme_text):
    """Rows currently sitting in the observation zone (never theme sections)."""
    return [r for r in parse_plugin_rows_full(readme_text) if r["section"] == OBSERVE_SECTION_NAME]


def append_rows_to_observe(text, rows):
    """Append rows at the end of the observation-zone table.

    The zone is a fixed section, so a missing section or table is rebuilt with
    the canonical skeleton (heading + explanatory quote + table) instead of
    silently dropping the rows: the scan has already reported content_changed,
    and a no-op here would leave the workflow's commit step with nothing new.
    """
    rows = [r for r in rows if r.get("repo")]
    if not rows:
        return text

    start, plugin_end = plugin_section_bounds(text)
    if start is None:
        return text

    existing = parse_current_plugins(text)
    new_rows = ""
    added = 0
    for r in sorted(rows, key=lambda row: row["author"].casefold()):
        if r["repo"].lower() in existing:
            continue
        desc = clean_desc_for_readme(r.get("desc", ""), name=r.get("name", "")).replace("|", "\\|")
        new_rows += f'| [{r["name"]}](https://github.com/{r["repo"]}) | `{r["author"]}` | {desc} |\n'
        added += 1
    if not new_rows:
        return text

    section_start, section_end = observe_section_bounds(text)
    if section_start == -1:
        # No section yet: create it at the end of the plugin section, right
        # before the theme section, so it stays inside the plugin section
        # (TOC + row pipelines cover it) and never buries the curated tables.
        prefix = text[:plugin_end].rstrip("\n") + "\n\n"
        block = (
            OBSERVE_SECTION + "\n\n" + OBSERVE_NOTE + "\n\n"
            + observe_details_header(added) + OBSERVE_TABLE_SKELETON + new_rows
            + "\n" + OBSERVE_DETAILS_CLOSE + "\n\n"
        )
        return prefix + block + text[plugin_end:]

    block = text[section_start:section_end]
    matches = list(re.finditer(r"^\| \[.*\n?", block, re.M))
    if matches:
        insert_pos = section_start + matches[-1].end()
        return text[:insert_pos] + new_rows + text[insert_pos:]

    # Table present but still empty: insert directly after the separator.
    lines = block.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if is_table_separator(line):
            insert_pos = section_start + sum(len(l) for l in lines[: i + 1])
            return text[:insert_pos] + new_rows + text[insert_pos:]

    # No table at all: rewrite the section body canonically, keeping the fixed
    # quote and dropping the empty-state placeholder.
    rebuilt = (
        OBSERVE_SECTION + "\n\n" + OBSERVE_NOTE + "\n\n"
        + observe_details_header(added) + OBSERVE_TABLE_SKELETON + new_rows
        + "\n" + OBSERVE_DETAILS_CLOSE + "\n\n"
    )
    tail = text[section_end:]
    return text[:section_start] + rebuilt + ("" if tail.startswith("\n") else "\n") + tail


def remove_rows_by_repo(text, repos):
    """Delete rows from the observation zone only.

    Matches the repo part of the first cell, case-insensitively. Main-table
    rows are never touched: that is what lets a promoted row leave the zone
    here and be re-added to a theme table later in the same pipeline, while a
    dropped row simply disappears.
    """
    wanted = {r.rstrip("/").lower() for r in (repos or []) if r}
    if not wanted:
        return text
    section_start, section_end = observe_section_bounds(text)
    if section_start == -1:
        return text
    block = text[section_start:section_end]
    kept = []
    removed = 0
    for line in block.splitlines(keepends=True):
        cells = split_row(line)
        if cells and len(cells) >= 3:
            m = PLUGIN_ROW_CELL_RE.match(cells[0])
            if m and m.group(2).rstrip("/").lower() in wanted:
                removed += 1
                continue
        kept.append(line)
    if not removed:
        return text
    kept_text = "".join(kept)
    if not re.search(r"^\| \[", kept_text, re.M):
        # The last row left: collapse the section back to its empty state so the
        # README never keeps an empty table (or an empty <details> block).
        rebuilt = (
            OBSERVE_SECTION + "\n\n" + OBSERVE_NOTE + "\n\n"
            + OBSERVE_EMPTY_NOTE + "\n\n"
        )
        tail = text[section_end:]
        return text[:section_start] + rebuilt + ("" if tail.startswith("\n") else "\n") + tail
    return text[:section_start] + kept_text + text[section_end:]


# --------------------------------------------------------------------------
# Row parsing / classification / cleanup helpers (pure, no network)
# --------------------------------------------------------------------------

def split_row(line):
    """Split a markdown table row into cells, honouring escaped '\\|'."""
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return None
    s = s[1:-1].replace("\\|", "\x00")
    parts = [p.replace("\x00", "|").strip() for p in s.split("|")]
    return parts


PLUGIN_ROW_CELL_RE = re.compile(
    r"\[([^\]]+)\]\(https://github\.com/([^/|)]+/[^)|\s]+)\)"
)


def parse_plugin_rows_full(readme_text):
    start, end = plugin_section_bounds(readme_text)
    if start is None:
        return []
    section = readme_text[start:end]
    rows = []
    current_section = ""
    for raw in section.splitlines():
        # A '### ' heading may be glued to the previous line (e.g. a note or a
        # table row with no trailing newline). Split it out so section tracking
        # still works; otherwise every row until the next clean heading would be
        # mis-filed under an empty section and dropped by reorganize_sections.
        for line in re.split(r"(?=### )", raw):
            if line.startswith("### "):
                current_section = line[4:].strip()
                continue
            cells = split_row(line)
            if not cells or len(cells) < 3:
                continue
            m = PLUGIN_ROW_CELL_RE.match(cells[0])
            if not m:
                continue
            rows.append({
                "section": current_section,
                "name": m.group(1).strip(),
                "repo": m.group(2).rstrip("/"),
                "author": cells[1].strip().strip("`").strip(),
                "desc": cells[2].strip(),
            })
    return rows


def is_broken_desc(desc):
    """True for placeholder / garbage descriptions that must not ship as-is."""
    if not desc:
        return True
    d = desc.strip()
    if "未分类" in d:
        return True
    if d.startswith("分类"):
        return True
    if re.search(r"install\s+--", d):
        return True
    if "pip install" in d or "npm install" in d or "npm i " in d:
        return True
    if d.endswith("…") or d.endswith("..."):
        return True
    return False


# Hand-verified Chinese descriptions for plugins whose source repo offers no
# usable description (placeholder / truncated / command-only). The automatic
# heuristic cannot recover these, so they are curated here and survive every
# cleanup / scan pass. Keyed by the exact plugin name as shown in the README.
CURATED_DESC = {
    "Agent Review": "结构化阅读批注：高亮、便签、关系线，支持人机协作，纯本地",
    "MeshSync": "无服务器局域网同步：自动发现设备、加密传输、亚秒级更新",
    "Bloomtype Publisher": "在 Obsidian 侧栏实时预览公众号排版，一键复制富文本到微信",
    "Dedao KB Sync": "同步得到大脑知识库与订阅内容到 Obsidian，按库/博主自动建文件夹并打标签",
    "Bi Ji Tong Bu": "同步微信「笔记同步助手」收集的公众号、小红书、得到等内容到 Obsidian",
    "Easy Bookkeeping": "本地优先的极简记账：键盘速录、手机表单、图表看板，数据存为 Markdown",
    "DSH Math Notes Assistant": "数学笔记的 AI 长期记忆助手：跨会话收集、打磨并连接证明与灵感，纯本地",
    # The entries below come from plugins whose upstream description was
    # English-only. They were rewritten into concise Chinese so the list reads
    # uniformly; keyed by name like the entries above and applied verbatim by
    # clean_desc_for_readme / clean_all_descriptions.
    "Vault Workbench": "在一个界面里浏览、预览、编辑多个本地库，不用切换窗口",
    "ParaFlow": "实时预览下的结构化列表编辑，附 Typora 风格的阅读与编辑样式",
    "Nav Dock": "把导航笔记和速查页放在侧栏图标旁的浮窗里，不占标签页",
    "Desktop Stickies": "拟物风格的桌面便笺与小组件，实时预览，贴壁纸，与库同步",
    "Zone Scroll Zoom": "用快捷键缩放界面或字号，缩放位置跟随鼠标",
    "Keyword Decorator": "给关键词加可视标记：文字样式、emoji、图标或图片，不改原文",
    "Visual Math": "不写 LaTeX 也能记数学、化学、物理笔记，可画函数图与三维曲面",
    "HandNote": "在笔记页上手写绘图，可转文字与公式，也能批注 PDF 与 Office 文档",
    "Formula Library": "可视化 LaTeX 编辑、公式库检索、模板、Mermaid 与函数绘图",
    "MindMap Studio": "在 Obsidian 里创建和编辑专业思维导图：六种布局、节点搜索、拖拽排序，支持内部链接、附件导入和图片导出",
    "Canvas Mind Map": "从 Markdown 生成可折叠的 Canvas 思维导图，内置七种布局",
    "With Keyline": "选区工具、块手柄、页面提及与文件图标等写作导航增强，配 Keyline 主题",
    "Link Peek": "悬停外链弹出 OpenGraph 预览卡片，支持实时预览、阅读视图与画布",
    "Thought Navigator": "可视化知识工作台：连接笔记、搭 MOC、做思维导图，把卡片盒变成可导航的思考地图",
    "Claudian Plus": "以 Codex 为主的多智能体工作台，把编码代理接进库里的笔记与会话",
    "Calendar Panel": "日记与周记的日历面板，用日历视图浏览和跳转每日笔记",
    "Qiaomu Audio": "把笔记导出成音频或带字幕视频，支持朗读与听写，可选系统、本地或云端语音",
    "Codian": "多provider的 AI 代理工作台：把本地编码代理接进库、会话、技能与工具",
    "YOLO": "Agent 式 AI 助手：对话、写作、白板、学习，支持 RAG、MCP、Skills 与工具调用",
    "HiLighter": "把高亮变成可管理的笔记卡片：多色标注、AI 分析、思维导图与 FSRS 复习",
    "claudian": "把 Claude Code／Codex 嵌进 Obsidian，当 AI 协作者做代码与写作辅助",
    "Foxycape PDF": "内嵌 PDF 阅读器，高亮、引文与深链都能回到原文",
    "Cozy Read Aloud": "朗读笔记、文本 PDF、本地 HTML 与网页，支持高亮、大纲导航与音频导出",
    "WeLink": "微信与库之间的消息桥：收发消息、附件与文章",
    "Messager": "通过微信、HTTP API 或邮件把消息发进 Obsidian",
    "Bamboo Immortals": "竹林修仙传：基于 OGAS 理念的中国风目标自动化管理",
    "MySecret": "用 AES-256-GCM 与 Argon2id 加密笔记和附件，不破坏搜索与关系图",
    "Bucket Sidebar": "按项目把笔记、PDF 和文件夹分组，不移动也不复制原文件",
    "Thinker TomaTodo": "把 Thinkers 计时会话镜像到番茄ToDo 桌面端，一边开始另一边也开始",
    "File Ignore": "用点前缀管理文件索引，真正隐藏或排除文件，体验类似 .gitignore",
    "Screenshot Selection": "把选中的内容截成 PNG 并复制到剪贴板",
    "Tab Tray": "给原生标签栏加上浏览器式的固定与分组，并持久保存",
    "Tab Groups": "像 Chrome 一样给标签分组并加彩色标签，可折叠、重排、整组拖动",
    "EzIndex": "为笔记和文件夹快速生成索引与目录",
    "Harbor Tab": "浏览器式首页标签页：即时搜索文件、星标与最近文件",
    "Bring to Front": "后台出现弹窗或通知时自动把 Obsidian 窗口置顶",
    "Chess Tree": "国际象棋（含 Chess960）变着树",
    "Better Search Result Views": "搜索结果按标题分组，文内查找统一在浮窗，跨换行、标签或页码也能搜到",
    "Inline Predict": "行内 AI 补全：灰字预览、分步接受、本地用量统计，兼容 OpenAI 接口",
    "LaC.CostSet": "用 TOML 管理个人物品清单：价格、日期、标签、回收价值与每日成本趋势",
    "WeChat Workbench": "公众号草稿的发布工作台：写作、排版、预览、复制与同步",
    "Syntax Palette": "给 Markdown 代码块上色，15 套主题，可选行号与自动换行",
    "Media Marker": "点转录文档里的时间戳即可跳转播放嵌入的音视频",
    "Linker": "自动嵌入附件和 YouTube 视频，同时让笔记保持普通 Wiki 链接",
    "Hierarchy Navigator": "在每篇笔记顶部渲染父级、同级、子级导航块，不改源文件",
    "University Schedule and Calendar": "课表侧栏：上课倒计时、课程模板、科目文件夹映射与自动链接笔记",
    "Today Sidebar": "原生侧栏：每日笔记、文件快捷入口与任务进度",
    "XXL-Job Flow Atlas": "把 xxl-job 导出的任务定义导入库内，生成任务流、任务笔记与依赖图",
    "PDF Sync Scroll": "多个 PDF 窗格与弹窗同步滚动，支持页面对齐、阅读进度与手动偏移",
    "xiaoji": "简洁的小记空间：在原生 Markdown 里记录、查找与回顾想法",
    "Whiteboat": "从一片静水开始，把一个念头变成新的 Canvas",
    "Kindle Mail": "把笔记导出成 EPUB，通过自己的 SMTP 邮箱发到 Kindle",
    "LingXi1949": "把笔记与上下文一键送给 AI 代理，支持本地与远程终端会话",
    "Language Leaf": "在原生 Markdown 里阅读、查词、收录词句，存成间隔重复闪卡",
    "Office Flow": "任务笔记的工作流：行内任务按钮、进度卡片、首页与收藏快捷入口",
    "xSync": "端到端加密的跨设备同步，可自建服务器并分享给朋友",
    "Pictether": "本地优先的外部附件管理，可选 S3 兼容备份与恢复",
    "Lean4": "在笔记里校验 Lean 4 代码块与共享会话，查看证明目标",
    "Checkbox Reminder": "给任意复选框设提醒，到期发桌面通知，并按到期日汇总",
    "Open Mode Control": "已有笔记用阅读视图打开、新笔记用编辑视图打开，可按文件或文件夹设例外",
    "Sidebar Tab Picker": "选择右侧栏显示哪些标签页、以什么顺序，重启后保持",
    "Hidontknow Support": "Hidontknow 主题的配套插件：让实时预览与阅读视图对齐",
    "Vault Codex": "与本地 Codex 对话、处理笔记，并审阅代理的每一步操作",
    "Nihong AI explaination": "选中日语词句，生成 AI 讲解笔记、语法拆解、翻译、离线词典与朗读",
    "Tuopan": "让 Obsidian 常驻系统托盘：开机隐藏启动、关窗不退出、再启动不弹选库窗",
    "gogoga icons": "gogoga 主题的配套插件：把内置 Lucide 图标换成 Phosphor 图标",
    "Octavo Toolbar": "选中文字弹出格式工具栏：加粗、链接、callout、块转换与纯文本复制",
    "Quiet Kanban": "给 Bases 用的紧凑看板视图：彩色卡片与可折叠预览",
    "Aio Explorer": "文件浏览器的一站式增强：排序、拖拽、专注模式、文件计数与清理",
    "HomePulse": "组件化的个人主页仪表盘：网格布局、拖拽编辑与工作流小组件",
    "Rich Treetable": "可折叠的 Markdown 表格，支持富文本单元格、分组与嵌套记录",
    "Super Table": "在笔记里做类数据库表格：类型化字段、筛选、排序、分组与多视图",
    "PaperMark": "把图片合成 PDF、加可搜索的 OCR 文字并编辑书签，默认本地处理",
    "Link Mindmap": "卢曼卡片盒式的链接思维导图：节点即笔记，用 frontmatter 建立关联",
    "IntelliScroll": "按间隔重复把旧笔记重新推到你面前，像刷信息流一样",
    "Zh Search": "面向中文的全文搜索：分词、BM25 相关度排序与摘要预览",
    "Inkwright": "在 Obsidian 里写、试玩与调试 ink 互动小说，可导出网页",
    "Card Walk": "用 Anki 排程复习笔记里的 #card 与挖空，手机上的作答几秒内同步回电脑",
    "Emacs Logseq Inspired Outline": "借鉴 Logseq 与 Emacs 的大纲编辑：整块选中、跨块移动、跳到父块",
    "Hover Outline": "编辑器角落藏着一列短横，悬停展开成大纲卡片",
    "Logseq Style Alias": "让别名链接直接打开带该别名的页面，而不是新建空页",
    "Logseq Style Fold": "把列表折叠状态写成 collapsed:: true 存进笔记，标记文字可配置",
    "Logseq Style Tasks": "面向 Logseq 式日志的关键词任务：todos 块、优先级简写与自定义循环热键",
    "Thread Studio": "从笔记写 X 推文、长帖与文章，带实时预览",
    "Meeting Live": "在笔记里开会：本地录音、火山引擎实时语音识别、AI 阶段总结与会议笔记",
    "Device Tracker": "在 frontmatter 里记下哪台设备创建、哪台最后编辑了这篇笔记",
    "Pin Indicator": "笔记被固定时，在「更多选项」按钮上显示一把锁",
    "Drawio Preview": "在 Obsidian 里直接预览 .drawio 图表，支持缩放、平移与多页",
    "Heading Guard": "按自定义规则给标题分级编号，编号变化时同步更新笔记内标题链接",
    "Notes Activity Calendar": "侧栏日历：展示笔记的创建与修改，带农历与每日笔记快捷入口",
    "IMA Share Sync": "把 IMA 里别人分享的文章与文件存进 Obsidian，方便回看与 AI 分析",
    "Collate": "并排对照两篇笔记，按词高亮差异",
    "Skill Switchboard": "在 Obsidian 里维护技能，并分发到各 agent 运行时",
    "Claude Studio": "Notion 风格的 AI 写作、对话与库操作，由本地 Claude Code CLI 驱动",
    "Best Mermaid": "接管阅读视图的 Mermaid 渲染：跟随主题、风格包、缩放平移与导出 PNG",
    "Read and Return": "跳到笔记开头，再一键回到原来的阅读位置",
    "GitHub Sync - Zev": "在 Obsidian 里打开 GitHub 仓库文件夹，不装 Git 也能同步",
    "Pluscan": "从库内的本地文件夹或 zip 安装插件，安装前有确认卡片",
    "BrainOS Graph": "用 Markdown 列表记录思路路径，在可编辑的图谱里浏览",
    "Planfold": "在一个首页里规划日、周、月、年，纯 Markdown 计划器",
    "AI Assistant Suite": "AI 提议标题、标签、文件夹与更整洁的正文，你确认后才应用",
    "Git Note History": "浏览笔记的 Git 历史、比较任意两版，可只恢复某一段",
    "Polyglot Helper": "一次在多个词典里查词，并把结果存成学习笔记",
    "Video Summary": "把视频变成可搜索的笔记，配浏览器扩展与自建 n8n 工作流",
    "Attachment Atlas": "找出重复与未被引用的图片，查看引用关系，回收时可恢复",
    "Hover Bar": "选中文字弹出轻量浮动工具栏，可调颜色、高亮与内链",
    "SixDots": "给 Markdown 块加拖拽手柄，像 Notion 一样重排段落与列表",
    "MarkToGo": "导出可分享的 Markdown，图片托管到 PicList，本地笔记不受影响",
    "Rich Search": "跨文件搜索：全词与正则，可限定目录与文件类型，也能搜 PDF、Excel 等格式",
    "ConfigSync Hub": "跨库同步配置：插件、主题、CSS 片段、快捷键与设置一次配置多库生效",
    "NyaHome": "统一的 Obsidian 主页与本地优先工作台，可选接入在线日历和邮箱",
    "TLT - Timeline Things": "以时间线为中心的任务与项目管理：日历、里程碑、人员负荷与消息中心",
    "Symbol Stripper": "选中文本，一键剔除指定字符或正则匹配的内容",
    "HTML Frontmatter": "用原生属性面板编辑 HTML 元数据，并纳入库的属性目录",
    "Animal Island Calendar": "纸感风格的日期时间选择器，给笔记属性选日期",
    "Easy Text Highlighter": "高亮、下划线并归类选中文字，可按颜色或样式筛选后复制或导出",
    "Feishu Docs Publisher": "通过本地 lark-cli 把笔记、文件夹与 PDF 发布到飞书文档并保持同步",
    "49 Notion GPT Style": "统一阅读样式、富文本粘贴、图片对齐、表格列宽与双窗滚动同步",
    "Trade Journal": "本地优先的交易日志：CSV 导入、盈亏看板、日历热力图与资金曲线",
    "File Master": "自建存储的附件管理：剪贴板自动上传、SHA-256 去重与零断链批量迁移",
    "Univer Sheet": "在 Obsidian 里打开、编辑和新建 Univer 表格与关系型数据库",
    "Tab Settings Enhanced": "增强标签管理：控制新标签打开位置、可搜索的标签导航与相对定位",
    "Cpp-Input-Guard": "写 C/C++ 笔记时，防止尖括号、方括号、星号等被 Markdown 误当语法",
    "Vault Agent": "国内可直连的 AI 库代理，支持 DeepSeek、GLM、Kimi、Qwen，能自主搜索与编辑笔记",
    "Vault Force Sync": "通过系统 git 把整库强制或增量同步到 GitHub 远端，大文件跳过并记日志",
    "One Week": "围绕课表安排一周：任务、习惯、月历与艾森豪威尔矩阵",
    "Baizer": "与库对话、行内 AI 补全，并把笔记编译成可搜索的知识库",
    "Cloudflare Image Uploader": "把网页剪藏的图片存进自己的 Cloudflare R2，避免图床失效",
    "Native PDF Theme Bridge": "导出原生 PDF 时保留当前阅读配色，并可逐页调间距",
    "Backleaf": "就 PDF 选区与 AI 讨论，并把结果存成结构化阅读笔记",
    "Countdown Day": "倒数日与纪念日管理，支持公历与农历",
    "NoteHarbor Sync": "借多种云存储在不同设备间同步笔记",
    "Family Tree Canvas": "在画布上手绘家谱：一行一代，不必为每个人建笔记",
    "Org Live": "在 Obsidian 里编辑与阅读 Org-mode 文件，带实时预览与阅读视图",
    # These three already had Chinese descriptions, but the upstream text was
    # long enough to bury the feature summary (one also carried an English
    # tail); keep them short so the list stays scannable.
    "AI Short Drama Assistant": "短剧创作六模块：选题评分、立项检查、单集写作、政策红线快检、46 项质检、去 AI 味改写与投稿格式检查",
    "Banxin Typography": "像 Word 一样用图形界面排版中文笔记：字号、中英文字体、行距、首行缩进、标题编号，内置国标公文与学术论文模板",
    # The upstream descriptions below were unusable: install instructions,
    # scraped layout fragments, marketing copy or release notes instead of a
    # feature summary. Each one was rewritten from the plugin's own README.
    "Download Pulse": "插件作者的下载分析看板：各插件下载量、排名与里程碑，可监控竞品并导出分享卡片",
    "Vault Pet": "写作养成游戏：每日字数目标、专注计时与完成任务换经验金币，像素猫随写作成长",
    "Cobrain": "文本副脑：以选中的笔记为对话材料，可讨论指定段落，材料、历史与草稿分别存档",
    "obsidian-manager": "更强的插件管理器：延迟启动、批量启停、分组与标签、从 GitHub 安装与冲突诊断",
    "OSS Sync and Share": "自建同步与分享：单文件服务端，同步笔记与附件，可协作编辑并发布公开博客",
    "CJK Emphasis Fix": "修复实时预览中加粗、斜体紧邻中日韩标点时被显示成星号的问题",
    "Four Layer Todo": "四层任务工作台：白板放当前任务，工作台、任务池与长期对象分层流转，数据存 Markdown",
    "ChatGPT DIL Local": "离线渲染 ChatGPT 导出的交互图表，把含 DIL 标签的 Markdown 转成可操作的伴生笔记",
    "Hover Image": "给笔记里的文字或链接绑定库内图片，鼠标悬停即可预览，不占正文空间",
    "GitHub Vault Sync (Desktop)": "把库直接当 Git 工作区与 GitHub 仓库同步：定时提交、拉取、三方合并，冲突时保留副本",
    "Community Manager CN Legacy": "旧版插件安装中心（停在 0.1.16，后续由 Wujie Hub 维护）：经镜像搜索、安装与升级插件",
    "Wujie Hub": "插件安装中心：经 GitHub 镜像搜索、安装与升级社区插件，并管理无界同步版本与回退",
    "Tag Graph": "Bases 关系图谱：标签、文件夹与属性成为枢纽节点，拖动笔记即可打标签，可合并、重命名或隐藏标签",
}

# Hand-verified section for the plugins that used to live in the catch-all
# "其他工具" bucket. Keyword scoring is unreliable for these (their names/desc
# barely overlap the section keywords), so they are pinned explicitly. Future
# scans still fall back to classify_section() for anything not listed here.
CURATED_SECTION = {
    "obsidian-i18n": "效率与系统",
    "obsidian-manager": "效率与系统",
    "Chem": "学术与写作",
    "Personal Assistant": "效率与系统",
    "MindCanvas": "思维导图与阅读",
    "Remember Settings Window": "效率与系统",
    "Hans TW TTS": "娱乐与多媒体",
    "Files Cooker": "效率与系统",
    "AindentPaper": "学术与写作",
    "China BRAT": "效率与系统",
    "Password Protection": "效率与系统",
    "Symbol Stripper": "效率与系统",
    "obsidian-xiangqi": "娱乐与多媒体",
    "Playdown": "思维导图与阅读",
    "MySecret": "效率与系统",
    "Bucket Sidebar": "界面与视图增强",
    "Tag Graph": "链接与知识管理",
    "Vault Pet": "娱乐与多媒体",
}



def clean_desc_for_readme(desc, readme_text=None, max_len=60, name=None):
    """Like clean_desc, but also flags placeholder/garbage text so the README
    never ships '分类: 未分类' or command snippets. Used by both the scan
    apply path and the one-off cleanup pass.
    If `name` matches a curated override, that hand-verified text is used
    verbatim (the automatic heuristic cannot recover these from the source)."""
    if name and name in CURATED_DESC:
        return CURATED_DESC[name].replace("|", "\\|")[:max_len]
    text = clean_desc(desc, readme_text, max_len=max_len)
    if is_broken_desc(text):
        return "[描述待补充]"
    return text


# Freshness markers kept in the description cell so they survive every
# cleanup / re-file pass. Bracket style matches the existing [描述待补充]
# placeholder. Archived outranks stale (a repo can be both).
FRESHNESS_MARKER_RE = re.compile(r"\s*\[(已归档|长期未更新)\]\s*$")
FRESHNESS_DATE_RE = re.compile(r"\s*·\s*最后更新\s*\d{4}-\d{2}")


def strip_freshness_marker(desc):
    if not desc:
        return desc
    return FRESHNESS_MARKER_RE.sub("", desc).strip()


def _format_update_ym(last_update):
    """Render an ISO GitHub timestamp as YYYY-MM for the freshness badge."""
    if not last_update:
        return ""
    try:
        return parse_github_time(last_update).strftime("%Y-%m")
    except Exception:
        return ""


def strip_freshness_decorations(desc):
    """Strip both the archived/stale marker and the last-update badge so a row
    can be re-decorated from fresh flags. Date is stripped first (it sits behind
    the marker), then the marker; order-independent so re-running stays
    idempotent."""
    if not desc:
        return desc
    desc = FRESHNESS_DATE_RE.sub("", desc)
    desc = FRESHNESS_MARKER_RE.sub("", desc)
    return desc.strip()


def append_freshness_marker(desc, flags):
    """Return desc with a single freshness marker appended. Idempotent: any
    prior marker is stripped first, so re-running self-corrects when a repo
    becomes active again."""
    desc = strip_freshness_marker(desc)
    if not desc:
        return desc
    if flags.get("archived"):
        return desc + " [已归档]"
    if flags.get("stale"):
        return desc + " [长期未更新]"
    return desc


def decorate_row_freshness(desc, flags):
    """Decorate a plugin row from fresh flags: an archived/stale marker (when
    applicable) followed by a '最后更新 YYYY-MM' badge (when repo metadata was
    fetched successfully). Idempotent and self-correcting."""
    if not flags.get("ok"):
        # Metadata unavailable (rate limit, network blip). Leave the row exactly
        # as it is: stripping here used to erase every freshness marker in the
        # README the first time the API token went bad.
        return desc
    desc = strip_freshness_decorations(desc)
    if not desc:
        return desc
    if flags.get("archived"):
        desc += " [已归档]"
    elif flags.get("stale"):
        desc += " [长期未更新]"
    if flags.get("ok") and flags.get("last_update"):
        ym = _format_update_ym(flags["last_update"])
        if ym:
            desc += f" · 最后更新 {ym}"
    return desc


def dedupe_plugin_rows(text):
    seen = set()
    out = []
    for line in text.splitlines(keepends=True):
        cells = split_row(line)
        if cells and len(cells) >= 3:
            m = PLUGIN_ROW_CELL_RE.match(cells[0])
            if m:
                repo = m.group(2).rstrip("/").lower()
                if repo in seen:
                    continue
                seen.add(repo)
        out.append(line)
    return "".join(out)


def reorganize_sections(text):
    """Re-file rows that landed in '其他工具' into their keyword-matched
    section (curated map wins, then keyword scoring). Curated-section rows
    are left where they are. Empty non-catch-all sections are dropped; an
    empty catch-all renders a short 'no uncategorized entries' note."""
    start, end = plugin_section_bounds(text)
    if start is None:
        return text
    head = text[:start]
    body = text[start:end]
    after = text[end:]
    first = body.find("\n### ")
    if first == -1:
        return text
    preamble = body[:first]          # "## 原生中文插件" + the %% note
    sections_block = body[first + 1:]
    rows = parse_plugin_rows_full(text)
    for r in rows:
        if r["section"] == "其他工具":
            t = CURATED_SECTION.get(r["name"]) or classify_section(r["name"], r["desc"])
            if t and t != "其他工具":
                r["section"] = t
    headings = [ln[4:].strip() for ln in sections_block.splitlines() if ln.startswith("### ")]
    # A '### ' heading may be glued to the previous line in the source (no
    # trailing newline); the parser still extracts it as a section. Include any
    # such section so its rows are re-filed instead of silently dropped.
    for r in rows:
        if r["section"] and r["section"] not in headings:
            headings.append(r["section"])
    # The observation zone is fixed and renders after every theme section,
    # wherever its heading happens to sit in the file.
    if OBSERVE_SECTION_NAME in headings:
        headings.remove(OBSERVE_SECTION_NAME)
        headings.append(OBSERVE_SECTION_NAME)
    grouped = {h: [] for h in headings}
    for r in rows:
        grouped.setdefault(r["section"], []).append(r)
    out = []
    for h in headings:
        items = grouped.get(h, [])
        if h == OBSERVE_SECTION_NAME:
            # Fixed section: its rows are never re-filed into a theme section
            # and the section survives even when empty, so the zone stays in
            # the README (and TOC) as the standing "under observation" marker.
            # The table lives inside a collapsed <details> block: the zone is a
            # long tail of unproven entries and must not bury the curated
            # tables. The block is only opened when there is something to show.
            out.append(f"### {h}\n\n")
            out.append(OBSERVE_NOTE + "\n\n")
            if not items:
                out.append(OBSERVE_EMPTY_NOTE + "\n\n")
                continue
            out.append(observe_details_header(len(items)))
            out.append("| 插件 | 作者 | 核心功能 |\n")
            out.append("| --- | --- | --- |\n")
            for r in sorted(items, key=lambda x: (x["author"].casefold(), x["name"].casefold())):
                desc = r["desc"].replace("|", "\\|")
                out.append(
                    f'| [{r["name"]}](https://github.com/{r["repo"]}) | `{r["author"]}` | {desc} |\n'
                )
            out.append("\n")
            out.append(OBSERVE_DETAILS_CLOSE + "\n\n")
            continue
        if not items:
            if h == "其他工具":
                out.append(f"### {h}\n\n")
                out.append("_（暂无未分类条目）_\n\n")
            continue
        out.append(f"### {h}\n\n")
        out.append("| 插件 | 作者 | 核心功能 |\n")
        out.append("| --- | --- | --- |\n")
        for r in sorted(items, key=lambda x: (x["author"].casefold(), x["name"].casefold())):
            desc = r["desc"].replace("|", "\\|")
            out.append(
                f'| [{r["name"]}](https://github.com/{r["repo"]}) | `{r["author"]}` | {desc} |\n'
            )
        out.append("\n")
    # Guarantee a newline between the (un-rebuilt) preamble and the first
    # rebuilt '### ' heading; otherwise a line that ends without one (e.g. a
    # '%%' note) gets glued to the heading and the parse round-trips it back
    # into the bad state.
    if not preamble.endswith("\n"):
        preamble += "\n"
    return head + preamble + "".join(out) + after


def clean_all_descriptions(text):
    out = []
    for line in text.splitlines(keepends=True):
        cells = split_row(line)
        if cells and len(cells) >= 3:
            m = PLUGIN_ROW_CELL_RE.match(cells[0])
            cur = cells[2].strip()
            if m:
                name, repo = m.group(1), m.group(2)
                author = cells[1].strip().strip("`")
                curated = CURATED_DESC.get(name)
                if is_broken_desc(cur) or cur == "[描述待补充]":
                    desc = curated if curated is not None else "[描述待补充]"
                    out.append(
                        f'| [{name}](https://github.com/{repo}) | `{author}` | {desc} |\n'
                    )
                    continue
                # A curated description also overrides an upstream English one, so the
                # list stays Chinese even after a scan re-renders the row.
                if curated is not None:
                    base = strip_freshness_decorations(cur)
                    if base != curated:
                        suffix = cur[len(base):] if cur.startswith(base) else ""
                        out.append(
                            f'| [{name}](https://github.com/{repo}) | `{author}` | {curated}{suffix} |\n'
                        )
                        continue
        out.append(line)
    return "".join(out)


def make_anchor(heading):
    a = heading.strip().lower()
    a = re.sub(r"[^\w\u4e00-\u9fff\- ]", "", a)
    return a.replace(" ", "-")


def generate_toc(text):
    lines = []
    for line in text.splitlines():
        if line.startswith("## ") and not line.startswith("### "):
            h = line[3:].strip()
            if h == "目录":
                continue
            lines.append(f"- [{h}](#{make_anchor(h)})")
        elif line.startswith("### "):
            h = line[4:].strip()
            lines.append(f"  - [{h}](#{make_anchor(h)})")
    return "## 目录\n\n" + "\n".join(lines) + "\n"


def apply_toc(text):
    toc = generate_toc(text)
    block = "<!-- TOC:START -->\n" + toc + "<!-- TOC:END -->"
    if "<!-- TOC:START -->" in text:
        s = text.index("<!-- TOC:START -->")
        e = text.index("<!-- TOC:END -->") + len("<!-- TOC:END -->")
        after = text[e:].lstrip("\n")
        return text[:s] + block + "\n\n" + after
    idx = text.find("## 简介")
    if idx == -1:
        return block + "\n\n" + text.lstrip("\n")
    before = text[:idx].rstrip("\n")
    after = text[idx:].lstrip("\n")
    return before + "\n\n" + block + "\n\n" + after


def find_unmanaged_rows(readme_text):
    """Plugin rows whose first cell is not a github.com plugin link.

    Every pipeline stage (cleanup, sort, freshness, stale removal) keys off
    PLUGIN_ROW_RE, so such a row is invisible to all of them and is silently
    deleted by the next cleanup pass. A row can only reach the README through
    a hand-written PR, so catching it in --validate is the last gate before a
    merged entry evaporates within 48 hours.
    """
    start, end = plugin_section_bounds(readme_text)
    if start is None:
        return []
    section = readme_text[start:end]
    offenders = []
    for raw in section.splitlines():
        for line in re.split(r"(?=### )", raw):
            cells = split_row(line)
            if not cells or len(cells) < 3:
                continue
            first = cells[0].strip()
            # Skip the header/separator rows of each table.
            if not first.startswith("["):
                continue
            if PLUGIN_ROW_CELL_RE.match(first):
                continue
            name = first.strip("[]").split("](")[0].strip()
            offenders.append(name or first)
    return offenders


def validate_readme(text):
    issues = []
    rows = parse_plugin_rows_full(text)
    seen = {}
    for r in rows:
        repo = r["repo"].lower()
        seen[repo] = seen.get(repo, 0) + 1
    dups = [repo for repo, c in seen.items() if c > 1]
    if dups:
        issues.append("duplicate plugin repos: " + ", ".join(dups))
    for r in rows:
        if is_broken_desc(r["desc"]):
            issues.append(f"broken/placeholder description: {r['name']} ({r['repo']})")
    unmanaged = find_unmanaged_rows(text)
    if unmanaged:
        issues.append(
            "rows not matching the github.com/user/repo format (the cleanup "
            "pass would delete these): " + ", ".join(unmanaged)
        )
    return issues


def cleanup_readme():
    with open(README_PATH, encoding="utf-8") as f:
        text = f.read()
    text = dedupe_plugin_rows(text)
    text = reorganize_sections(text)
    text = clean_all_descriptions(text)
    text = apply_toc(text)
    text = sort_plugin_tables_by_author(text)
    with open(README_PATH, "w", encoding="utf-8") as f:
        f.write(text)


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
        "created_at": data.get("created_at", "") or "",
        "full_name": data.get("full_name", repo),
        "html_url": data.get("html_url", f"https://github.com/{repo}"),
    }

def repo_exists(repo):
    """True / False / None. False only when GitHub says the repo is gone.

    `repo_meta` collapses every failure into `{}`, so it cannot tell a deleted
    repository (HTTP 404/410) from a rate limit. This probe is only paid for
    rows whose metadata came back empty, and it reports 'unknown' (None) for
    anything but a definitive 200 or 404/410.
    """
    session = create_retry_session(timeout=HTTP_TIMEOUT)
    try:
        r = session.get(
            f"https://api.github.com/repos/{repo}", headers=API_HEADERS, timeout=HTTP_TIMEOUT
        )
    except requests.exceptions.RequestException:
        return None
    if r.status_code == 200:
        return True
    if r.status_code in (404, 410):
        return False
    return None



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


def collect_signals(plugin, meta, author_loc, has_locale, has_docs, has_readme_zh=False):
    """All Chinese-relevance text signals for a plugin, as a set."""
    name, author = plugin.get("name", ""), plugin.get("author", "")
    signals = set()
    if has_locale:
        signals.add("locale")
    if has_readme_zh:
        signals.add("readme_zh")
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
    """Report dormant rows for visibility only.

    The result is passed through as scan output and printed in the run log; it
    is never applied to the README. Dormancy is surfaced by the `[长期未更新]`
    marker that --freshness-readme writes in place, which self-corrects when a
    dormant repository starts shipping again.
    """
    cutoff = utcnow() - timedelta(days=STALE_DAYS)
    rows = parse_plugin_rows(readme_text)
    if not rows:
        return []
    workers = max(1, min(STALE_CHECK_WORKERS, len(rows)))
    stale = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(stale_plugin_row, row, cutoff): row for row in rows}
        for future in as_completed(futures):
            try:
                stale_row = future.result()
            except Exception as e:
                print(f"WARN: stale check failed: {e}", file=sys.stderr)
                continue
            if stale_row:
                stale.append(stale_row)
    return stale


# --------------------------------------------------------------------------
# Observation zone (promote / keep / drop)
# --------------------------------------------------------------------------

def observe_last_activity_days(meta, cutoff):
    """Days since the most recent visible activity (push or latest release)."""
    dates = []
    for key in ("pushed_at", "latest_release"):
        try:
            parsed = parse_github_time(meta.get(key) or "")
        except ValueError:
            parsed = None
        if parsed:
            dates.append(parsed)
    if not dates:
        return None
    return (cutoff - max(dates)).days


def decide_observe_action(meta, downloads, cutoff):
    """Decide what happens to a row that already sits in the observation zone.

    Returns 'promote' (move it to the theme tables), 'drop' (delete the row) or
    'keep'. Pure on purpose -- no clock, no network -- so every boundary is
    unit-testable from fixed metadata, a download count and a cutoff.

    Incomplete metadata always degrades to 'keep': a rate-limited or failed
    API call must never delete a row from the README.
    """
    if not meta:
        return "keep"
    try:
        created = parse_github_time(meta.get("created_at") or "")
    except ValueError:
        created = None
    if not created:
        return "keep"
    age_days = (cutoff - created).days
    stars = int(meta.get("stars") or 0)
    downloads = int(downloads or 0)

    if age_days >= OBSERVE_MIN_AGE_DAYS and (
        stars >= OBSERVE_PROMOTE_STARS or downloads >= OBSERVE_PROMOTE_DOWNLOADS
    ):
        return "promote"

    if age_days >= OBSERVE_EXPIRE_DAYS:
        inactive = observe_last_activity_days(meta, cutoff)
        if (
            stars < OBSERVE_EXPIRE_MIN_STARS
            and downloads < OBSERVE_EXPIRE_MIN_DOWNLOADS
            and inactive is not None
            and inactive > OBSERVE_EXPIRE_INACTIVE_DAYS
        ):
            return "drop"
    return "keep"


def observe_repo_listed(repo, registry_repos):
    """True when the community registry still knows this repo or its owner.

    The owner check is deliberate: a plugin whose README link carries a typo or
    an old name (Thinkers-TomaToDo vs Thinker-TomaToDo) 404s while the plugin
    itself is alive in the registry, so the owner still publishing is enough
    reason never to delete the row automatically.
    """
    key = repo.casefold()
    if key in registry_repos:
        return True
    owner_prefix = key.split("/")[0] + "/"
    return any(r.startswith(owner_prefix) for r in registry_repos)


def decide_missing_repo_action(exists, listed):
    """Decide what happens to a row whose repository cannot be read.

    Deletion needs both halves of the evidence: GitHub says the repository is
    gone (exists is False) AND the community registry no longer lists the
    plugin or its owner. Unknown existence (None) or a listed plugin keeps the
    row -- a failed API call must never delete anything.
    """
    if exists is False and not listed:
        return "drop"
    return "keep"


def evaluate_observe_zone(rows, registry_repos=None):
    """Classify the rows already in the observation zone (network-bound).

    Returns (promote_rows, drop_repos). A row whose metadata cannot be read is
    left alone, and a promoted row's description is handed on undecorated so
    the freshness pass can re-decorate it in its new table.

    `registry_repos` is the set of casefolded `owner/name` strings from the
    official community list. Supplying it enables the only deletion path for a
    repository that GitHub no longer serves; callers that omit it delete
    nothing.
    """
    if not rows:
        return [], []
    cutoff = utcnow()
    promote_rows, drop_repos = [], []
    for row in rows:
        try:
            meta = repo_meta(row["repo"])
            if not meta:
                if registry_repos is not None and decide_missing_repo_action(
                    repo_exists(row["repo"]),
                    observe_repo_listed(row["repo"], registry_repos),
                ) == "drop":
                    print(
                        f"observe: dropping {row['repo']} -- repository is gone and the "
                        "community registry no longer lists it",
                        file=sys.stderr,
                    )
                    drop_repos.append(row["repo"])
                continue
            published, downloads = repo_releases(row["repo"])
            decision = decide_observe_action(
                {**meta, "latest_release": published}, downloads, cutoff
            )
        except Exception as e:
            print(f"WARN: observe check failed for {row['repo']}: {e}", file=sys.stderr)
            continue
        if decision == "promote":
            promote_rows.append({
                "name": row["name"],
                "repo": row["repo"],
                "author": row["author"],
                "desc": strip_freshness_decorations(row["desc"]),
                "tier": "auto",
                "promoted": True,
            })
        elif decision == "drop":
            drop_repos.append(row["repo"])
    return promote_rows, drop_repos


def load_freshness_cache():
    try:
        with open(FRESHNESS_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_freshness_cache(cache):
    try:
        with open(FRESHNESS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"WARN: cannot write freshness cache: {e}", file=sys.stderr)


def follow_repo_rename(repo, full_name):
    """Canonical 'owner/name' when GitHub reports that the repository moved.

    `repo_meta` follows GitHub's 301, so a row whose URL still carries the old
    owner is detectable. Returns `repo` unchanged when the answer is missing or
    identical (a case-only difference counts as identical)."""
    if not full_name or not isinstance(full_name, str) or "/" not in full_name:
        return repo
    if full_name.casefold() == (repo or "").casefold():
        return repo
    return full_name


def get_repo_freshness(repo, cutoff=None):
    """Return {'archived': bool, 'stale': bool, 'last_update': str, 'ok': bool}.

    Cached for FRESHNESS_CACHE_DAYS. Network / rate-limit failures degrade to
    neutral flags (no marker) so a scan or cleanup never crashes the CI.
    Successful results are persisted under a lock so concurrent checks cannot
    clobber each other; failed checks are NOT cached so they are retried on the
    next run instead of being frozen as "neutral" for a week."""
    repo = repo.rstrip("/")
    if cutoff is None:
        cutoff = utcnow() - timedelta(days=STALE_DAYS)
    with _FRESH_LOCK:
        cache = load_freshness_cache()
        cached = cache.get(repo)
        # Entries written before `full_name` was cached cannot reveal a rename,
        # so they count as a miss: one refetch fills the field in.
        if cached and cached.get("checked_at") and "full_name" in cached:
            try:
                age = (utcnow() - parse_github_time(cached["checked_at"])).days
            except Exception:
                age = 999
            if age <= FRESHNESS_CACHE_DAYS:
                return {k: cached.get(k, False)
                        for k in ("archived", "stale", "last_update", "ok", "full_name")}
    meta = repo_meta(repo)
    if not meta:
        flags = {"archived": False, "stale": False, "last_update": "", "ok": False,
                 "full_name": ""}
    else:
        published, _ = repo_releases(repo)
        flags = {
            "archived": bool(meta.get("archived")),
            "stale": bool(decide_stale(meta, published, cutoff)),
            "last_update": published or meta.get("pushed_at", ""),
            "ok": True,
            # GitHub follows the 301 of a renamed/transferred repo, so this is
            # the only place the canonical name shows up.
            "full_name": meta.get("full_name", repo),
        }
    flags["checked_at"] = utcnow().isoformat()
    if flags["ok"]:
        with _FRESH_LOCK:
            cache = load_freshness_cache()
            cache[repo] = flags
            save_freshness_cache(cache)
    return flags


def freshness_readme():
    """One-off pass over plugin rows: append [已归档] / [长期未更新] markers
    from live repo metadata. Idempotent and self-correcting. Theme rows live
    outside the plugin section and are left untouched."""
    with open(README_PATH, encoding="utf-8") as f:
        text = f.read()
    rows = parse_plugin_rows_full(text)
    if not rows:
        return
    verify_api_auth("freshness")
    cutoff = utcnow() - timedelta(days=STALE_DAYS)
    repos = {r["repo"] for r in rows}
    # Canonical names already present in the table. A rename that lands on one
    # of these would duplicate a row, and validate_readme rejects that, so such
    # a row is reported and left as it is.
    seen = {r["repo"].rstrip("/").casefold() for r in rows}
    flags_by_repo = {}
    workers = max(1, min(STALE_CHECK_WORKERS, len(repos)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        fut = {executor.submit(get_repo_freshness, repo, cutoff): repo for repo in repos}
        for future in as_completed(fut):
            repo = fut[future]
            try:
                flags_by_repo[repo] = future.result()
            except Exception as e:
                print(f"WARN: freshness check failed for {repo}: {e}", file=sys.stderr)
                flags_by_repo[repo] = {"archived": False, "stale": False, "last_update": "", "ok": False}
    ok_count = sum(1 for f in flags_by_repo.values() if f.get("ok"))
    if flags_by_repo and ok_count == 0:
        print(
            f"ERROR: freshness check completed with 0 successful GitHub API "
            f"calls for {len(flags_by_repo)} repos. Refusing to rewrite the "
            "README from a total metadata failure — see the auth errors above.",
            file=sys.stderr,
        )
        sys.exit(1)
    start, end = plugin_section_bounds(text)
    head = text[:start]
    body = text[start:end]
    after = text[end:]
    out = []
    for line in body.splitlines(keepends=True):
        cells = split_row(line)
        if cells and len(cells) >= 3:
            m = PLUGIN_ROW_CELL_RE.match(cells[0])
            if m and m.group(2).rstrip("/") in flags_by_repo:
                repo = m.group(2).rstrip("/")
                name = m.group(1).strip()
                author = cells[1].strip()
                flags = flags_by_repo[repo]
                moved = follow_repo_rename(repo, flags.get("full_name"))
                if moved != repo:
                    target = moved.rstrip("/").casefold()
                    if target != repo.casefold() and target in seen:
                        print(
                            f"rename conflict: {repo} -> {moved} (row already "
                            "present, left as is)",
                            file=sys.stderr,
                        )
                    else:
                        # GitHub still 301s the old URL, but the row then points
                        # at an owner that may be deleted, and a 404 row is
                        # dropped by the observation sweep. Follow the move.
                        print(f"followed rename: {repo} -> {moved}", file=sys.stderr)
                        repo = moved
                        author = f"`{moved.split('/')[0]}`"
                        seen.add(target)
                desc = decorate_row_freshness(cells[2].strip(), flags).replace("|", "\\|")
                line = f'| [{name}](https://github.com/{repo}) | {author} | {desc} |\n'
        out.append(line)
    with open(README_PATH, "w", encoding="utf-8") as f:
        f.write(head + "".join(out) + after)


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

def write_scan_output(add_rows, dormant_rows, cache_changed, first_run, has_review,
                      observe_rows=None, promote_rows=None, drop_repos=None):
    # Dormant plugins no longer count as a content change: they are marked in
    # place by --freshness-readme and stay in the README, so there is nothing
    # for --apply-readme to commit.
    observe_rows = observe_rows or []
    promote_rows = promote_rows or []
    drop_repos = drop_repos or []
    # "review needed" now means "rows entered the observation zone". Nothing
    # gates the commit on it any more, but the key is still consumed, so it has
    # to report the zone honestly instead of the old review-tier flag.
    has_review = bool(has_review or observe_rows)
    # Every one of the four row classes changes the README, so each has to wake
    # the apply step -- the workflow commits on content_changed alone.
    content_changed = bool(add_rows or observe_rows or promote_rows or drop_repos)
    # Reported only; nothing gates the commit on it any more (no human review
    # step exists). Kept because consumers may still read the key.
    auto_merge_ready = content_changed and not first_run and not has_review
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"add_rows={json.dumps(add_rows, ensure_ascii=False)}\n")
            f.write(f"observe_rows={json.dumps(observe_rows, ensure_ascii=False)}\n")
            f.write(f"promote_rows={json.dumps(promote_rows, ensure_ascii=False)}\n")
            f.write(f"drop_repos={json.dumps(drop_repos, ensure_ascii=False)}\n")
            f.write(f"stale_rows={json.dumps(dormant_rows, ensure_ascii=False)}\n")
            f.write(f"content_changed={str(content_changed).lower()}\n")
            f.write(f"cache_changed={str(cache_changed).lower()}\n")
            f.write(f"first_run={str(first_run).lower()}\n")
            f.write(f"has_review={str(has_review).lower()}\n")
            f.write(f"auto_merge_ready={str(auto_merge_ready).lower()}\n")
        return
    print(json.dumps({
        "add": add_rows,
        "observe": observe_rows,
        "promote": promote_rows,
        "drop": drop_repos,
        "stale": dormant_rows,
        "content_changed": content_changed,
        "cache_changed": cache_changed,
        "first_run": first_run,
        "has_review": has_review,
        "auto_merge_ready": auto_merge_ready,
    }, ensure_ascii=False, indent=2))

# --------------------------------------------------------------------------
# Scan flow
# --------------------------------------------------------------------------

def run_scan(skip_stale=False):
    verify_api_auth("scan")
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
        # Kept apart from the per-candidate `readme_text` below: the scan loop
        # overwrites that name with each candidate's own README, and the
        # observation/stale passes below must read the repository README.
        main_readme = f.read()
    existing = parse_current_plugins(main_readme)
    denied = load_denylist()
    cutoff = utcnow() - timedelta(days=STALE_DAYS)

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
        readme_text = None
        if not (has_cn(name) or has_cn(author) or has_cn(desc)):
            # Metadata is entirely English, but the README may still be
            # Chinese-authored (issue 36). Only pay for the README fetch here
            # when the metadata gate fails; otherwise it is fetched below.
            readme_text = fetch_readme_text(repo)
            if not readme_zh_evidence(readme_text):
                continue
        scanned += 1
        meta = repo_meta(repo)
        if not meta:
            continue
        if readme_text is None:
            readme_text = fetch_readme_text(repo)
        # location signal must query the owner login, not the display name:
        # Chinese display names 404 on /users/ and silently kill the signal
        owner_id = meta["full_name"].split("/")[0]
        zh_rule = readme_zh_evidence(readme_text)
        signals = collect_signals(
            p, meta,
            author_location(owner_id),
            has_locale_file(repo),
            bool(readme_text and has_cn(readme_text)),
            bool(zh_rule),
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
        # Author column must be the repo owner's GitHub ID (canonical case),
        # never the display name from the official list (often a Chinese
        # name/team name). The display name still feeds scoring signals.
        author_id = meta["full_name"].split("/")[0]
        candidates.append({
            "name": name, "repo": repo, "author": author_id,
            "desc": append_freshness_marker(
                clean_desc_for_readme(desc, readme_text, name=name),
                {
                    "archived": bool(meta.get("archived")),
                    "stale": bool(decide_stale(meta, published, cutoff)),
                    "last_update": published or meta.get("pushed_at", ""),
                    "ok": True,
                },
            ),
            "cn": cn, "q": q, "tier": tier, "signals": sorted(signals),
        })

    candidates.sort(key=lambda c: (-c["cn"] - c["q"], c["repo"].casefold()))
    # A candidate whose description could not be salvaged ships as
    # '[描述待补充]', which validate_readme rejects: apply drops the row and
    # content_changed ends up pointing at a commit with nothing in it, which
    # crashes the workflow. Skip them here; the cache backfill below still
    # records their ids so the next scan does not rediscover them.
    unshippable = [c for c in candidates if "[描述待补充]" in c["desc"]]
    if unshippable:
        print(
            f"skipped {len(unshippable)} candidate(s) with no usable description: "
            + ", ".join(c["repo"] for c in unshippable),
            file=sys.stderr,
        )
    candidates = [c for c in candidates if "[描述待补充]" not in c["desc"]]
    # auto rows go straight to a theme table; review rows land in the fixed
    # observation zone and are promoted or dropped by later scans.
    add_rows = [{
        "name": c["name"], "repo": c["repo"], "author": c["author"],
        "desc": c["desc"], "tier": c["tier"], "score": c["cn"] + c["q"],
    } for c in candidates if c["tier"] == "auto"]
    observe_rows = [{
        "name": c["name"], "repo": c["repo"], "author": c["author"],
        "desc": c["desc"], "tier": "review", "score": c["cn"] + c["q"],
    } for c in candidates if c["tier"] == "review"]
    # Re-check the rows already under observation. A failed API call leaves the
    # row alone (decide_observe_action degrades to keep), so neither a rate
    # limit nor a network blip can delete anything.
    registry_repos = {p["repo"].casefold() for p in all_plugins if p.get("repo")}
    promote_rows, drop_repos = evaluate_observe_zone(
        parse_observe_rows(main_readme), registry_repos
    )
    has_review = bool(observe_rows)

    dormant = find_stale_plugins(main_readme) if not skip_stale else []
    dormant_rows = [{
        "name": s["name"], "repo": s["repo"], "full_name": s["full_name"],
        "author": s["author"], "section": s["section"],
        "last_update": s["last_update"], "html_url": s["html_url"],
    } for s in dormant]

    cache_changed = bool(new_ids)
    if cache_changed:
        save_checked(all_ids)

    print(
        f"known={len(all_ids)} scanned={scanned} auto={len(add_rows)} "
        f"observe={len(observe_rows)} promote={len(promote_rows)} "
        f"drop={len(drop_repos)} dormant={len(dormant)} (reported, not removed)",
        file=sys.stderr,
    )
    write_scan_output(
        add_rows, dormant_rows, cache_changed, first_run, has_review,
        observe_rows=observe_rows, promote_rows=promote_rows, drop_repos=drop_repos,
    )


# --------------------------------------------------------------------------
# Apply flow
# --------------------------------------------------------------------------

def update_title(add_count, date):
    if add_count:
        return f"Add Chinese-relevant plugins ({date})"
    return f"No new Chinese-relevant plugins ({date})"

def build_pr_body(rows):
    title_rows = [r for r in rows if r["tier"] == "auto"]
    review_rows = [r for r in rows if r["tier"] == "review"]
    parts = []
    if title_rows:
        parts.append("### Auto-merge candidates (high confidence)\n")
        parts.append("| Plugin | Author | Description | Score |\n| --- | --- | --- | --- |\n")
        for r in sorted(title_rows, key=lambda r: r["author"].casefold()):
            desc = r["desc"].replace("|", "\\|")
        parts.append(f'| [{r["name"]}](https://github.com/{r["repo"]}) | `{r["author"]}` | {desc} | {r["score"]} |\n')
        parts.append("\\n")
    if review_rows:
        parts.append("### Review candidates (medium confidence)\n")
        parts.append("These match the Chinese-relevance criteria but lack strong quality signals; please verify before merging.\\n\\n")
        parts.append("| Plugin | Author | Description | Score |\n| --- | --- | --- | --- |\n")
        for r in sorted(review_rows, key=lambda r: r["author"].casefold()):
            desc = r["desc"].replace("|", "\\|")
        parts.append(f'| [{r["name"]}](https://github.com/{r["repo"]}) | `{r["author"]}` | {desc} | {r["score"]} |\n')
        parts.append("\\n")
    if review_rows:
        parts.append("_This PR has review-tier entries, so auto-merge was skipped._\n")
    parts.append("\\n_This PR was generated by the weekly plugin scan._")
    return "".join(parts)

def emit_output(name, value):
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def do_apply_readme():
    # Dormant plugins are marked `[长期未更新]` in place by --freshness-readme;
    # they are never deleted here. A year without a commit is weak evidence that
    # a plugin is unwanted -- plenty of finished utilities stop shipping while
    # remaining the best tool for their job. REMOVE_ROWS is deliberately ignored
    # so a stale emit from an older scan cannot delete anything either.
    #
    # Observation-zone pipeline; the order is load-bearing:
    #   1. remove dropped rows AND promoted rows from the zone. Promoted rows
    #      must leave first: append_rows_to_other_tools skips any repo already
    #      present anywhere in the README, so a row still in the zone could
    #      never be promoted.
    #   2. append auto + promoted rows to the catch-all table (re-filed into
    #      their theme section by reorganize_sections below).
    #   3. append review rows to the observation zone.
    #   4. dedupe keeps the first occurrence, which is why every removal has to
    #      happen before every append.
    add_rows = json.loads(os.environ.get("ADD_ROWS", "[]"))
    observe_rows = json.loads(os.environ.get("OBSERVE_ROWS", "[]"))
    promote_rows = json.loads(os.environ.get("PROMOTE_ROWS", "[]"))
    drop_repos = json.loads(os.environ.get("DROP_REPOS", "[]"))
    promote_repos = [r.get("repo", "") for r in promote_rows if r.get("repo")]
    with open(README_PATH, encoding="utf-8") as f:
        text = f.read()

    text = remove_rows_by_repo(text, list(drop_repos) + promote_repos)
    text = append_rows_to_other_tools(text, add_rows + promote_rows)
    text = append_rows_to_observe(text, observe_rows)
    text = dedupe_plugin_rows(text)
    text = reorganize_sections(text)
    text = clean_all_descriptions(text)
    text = apply_toc(text)
    text = sort_plugin_tables_by_author(text)
    with open(README_PATH, "w", encoding="utf-8") as f:
        f.write(text)

    added = len(add_rows) + len(promote_rows) + len(observe_rows)
    title = update_title(added, utcnow().strftime("%Y%m%d"))
    body = build_pr_body(add_rows)
    os.makedirs(os.path.dirname(PR_BODY_FILE), exist_ok=True)
    with open(PR_BODY_FILE, "w", encoding="utf-8") as f:
        f.write(body)
    emit_output("pr_title", title)

def main():
    args = sys.argv[1:]
    if "--apply-readme" in args:
        do_apply_readme()
    elif "--cleanup-readme" in args:
        cleanup_readme()
    elif "--freshness-readme" in args:
        freshness_readme()
    elif "--validate" in args:
        with open(README_PATH, encoding="utf-8") as f:
            text = f.read()
        issues = validate_readme(text)
        if issues:
            print("README validation FAILED:")
            for i in issues:
                print(" - " + i)
            sys.exit(1)
        print("README validation passed.")
    else:
        run_scan(skip_stale="--skip-stale" in args)


if __name__ == "__main__":
    main()
