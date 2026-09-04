#!/usr/bin/env python3
"""Scan the official Obsidian plugin list for Chinese-relevant plugins.

Modes:
  (default)       fetch official list + cache diff, score candidates, detect
                  stale rows, write GitHub Actions outputs
  --apply-readme  edit README.md only from ADD_ROWS/REMOVE_ROWS env vars,
                  write .github/pr-body.md and the PR title (no git, no network).
                  Also de-duplicates, re-files catch-all entries into their
                  keyword-matched section, scrubs broken descriptions and
                  regenerates the table of contents.
  --cleanup-readme  one-off pass over README.md: dedupe, re-file 其他工具
                    entries, scrub broken descriptions, regenerate TOC.
  --freshness-readme  one-off pass: append [已归档]/[长期未更新] markers and a
                    '最后更新 YYYY-MM' freshness badge to existing plugin rows
                    from live repo metadata (archived flag + last push/release
                    older than STALE_DAYS). Cached for FRESHNESS_CACHE_DAYS;
                    idempotent and self-correcting.
  --validate      check README.md for duplicate rows and broken/placeholder
                  descriptions; exits non-zero on any issue (CI gate).
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
        # Surface the HTTP status (401 => bad token, 403 => rate limit,
        # None => network) so a broken GH_PAT shows up in logs instead of a
        # silent `{}` freshness cache.
        status = getattr(getattr(e, "response", None), "status_code", None)
        auth = "GITHUB_TOKEN set" if GITHUB_TOKEN else "GITHUB_TOKEN absent (unauthenticated)"
        print(f"WARN: request failed for {url}: {e} [HTTP {status}, {auth}]", file=sys.stderr)
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
    return text


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
    for line in section.splitlines():
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
    headings = [ln[4:].strip() for ln in sections_block.splitlines() if ln.startswith("### ")]
    rows = parse_plugin_rows_full(text)
    for r in rows:
        if r["section"] == "其他工具":
            t = CURATED_SECTION.get(r["name"]) or classify_section(r["name"], r["desc"])
            if t and t != "其他工具":
                r["section"] = t
    grouped = {h: [] for h in headings}
    for r in rows:
        grouped.setdefault(r["section"], []).append(r)
    out = []
    for h in headings:
        items = grouped.get(h, [])
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
    return head + preamble + "".join(out) + after


def clean_all_descriptions(text):
    out = []
    for line in text.splitlines(keepends=True):
        cells = split_row(line)
        if cells and len(cells) >= 3:
            m = PLUGIN_ROW_CELL_RE.match(cells[0])
            cur = cells[2].strip()
            if m and (is_broken_desc(cur) or cur == "[描述待补充]"):
                name, repo = m.group(1), m.group(2)
                author = cells[1].strip().strip("`")
                desc = CURATED_DESC.get(name, "[描述待补充]")
                out.append(
                    f'| [{name}](https://github.com/{repo}) | `{author}` | {desc} |\n'
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
    block = f"<!-- TOC:START -->\n{toc}\n<!-- TOC:END -->\n"
    if "<!-- TOC:START -->" in text:
        s = text.index("<!-- TOC:START -->")
        e = text.index("<!-- TOC:END -->") + len("<!-- TOC:END -->")
        return text[:s] + block + text[e:]
    idx = text.find("## 简介")
    if idx == -1:
        return block + text
    return text[:idx] + block + text[idx:]


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
        if cached and cached.get("checked_at"):
            try:
                age = (utcnow() - parse_github_time(cached["checked_at"])).days
            except Exception:
                age = 999
            if age <= FRESHNESS_CACHE_DAYS:
                return {k: cached.get(k, False) for k in ("archived", "stale", "last_update", "ok")}
    meta = repo_meta(repo)
    if not meta:
        flags = {"archived": False, "stale": False, "last_update": "", "ok": False}
    else:
        published, _ = repo_releases(repo)
        flags = {
            "archived": bool(meta.get("archived")),
            "stale": bool(decide_stale(meta, published, cutoff)),
            "last_update": published or meta.get("pushed_at", ""),
            "ok": True,
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
    cutoff = utcnow() - timedelta(days=STALE_DAYS)
    repos = {r["repo"] for r in rows}
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
            "WARN: freshness check completed with 0 successful GitHub API calls. "
            "Repo metadata could not be fetched (check the GH_PAT secret and rate limits); "
            "no markers applied and the freshness cache is left unchanged.",
            file=sys.stderr,
        )
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
                desc = decorate_row_freshness(cells[2].strip(), flags_by_repo[repo]).replace("|", "\\|")
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
        if not (has_cn(name) or has_cn(author) or has_cn(desc)):
            continue
        scanned += 1
        meta = repo_meta(repo)
        if not meta:
            continue
        readme_text = fetch_readme_text(repo)
        # location signal must query the owner login, not the display name:
        # Chinese display names 404 on /users/ and silently kill the signal
        owner_id = meta["full_name"].split("/")[0]
        signals = collect_signals(
            p, meta,
            author_location(owner_id),
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
    text = dedupe_plugin_rows(text)
    text = reorganize_sections(text)
    text = clean_all_descriptions(text)
    text = apply_toc(text)
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
