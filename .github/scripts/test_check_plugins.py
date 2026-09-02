#!/usr/bin/env python3
import base64
import json
import os
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

import check_plugins as cp

SAMPLE_README = """# Header

## 原生中文插件

### 界面与视图增强

| 插件 | 作者 | 核心功能 |
| --- | --- | --- |
| [Quiet Outline](https://github.com/guopenghui/obsidian-quiet-outline) | `guopenghui` | 大纲视图 |
| [Floating TOC](https://github.com/cumany/obsidian-floating-toc-plugin) | `cumany` | 浮动目录 |

### 其他工具

| 插件 | 作者 | 核心功能 |
| --- | --- | --- |
| [Text Finder](https://github.com/nyable/obsidian-text-finder) | `nyable` | 查找/替换 |

## 精选中文主题

|主题|作者|
|---|---|
|[Border](https://github.com/Akifyss/obsidian-border)|`Akifyss`|
"""


class TestParsing(unittest.TestCase):
    def test_plugin_section_bounds(self):
        start, end = cp.plugin_section_bounds(SAMPLE_README)
        self.assertIsNotNone(start)
        self.assertIsNotNone(end)
        self.assertLess(start, end)

    def test_plugin_section_bounds_missing(self):
        self.assertEqual(cp.plugin_section_bounds("# only header"), (None, None))

    def test_markdown_cells(self):
        self.assertEqual(cp.markdown_cells("| a | b |"), ["a", "b"])
        self.assertEqual(cp.markdown_cells("not a table"), [])
        self.assertEqual(cp.markdown_cells("| a |"), ["a"])

    def test_is_table_separator(self):
        self.assertTrue(cp.is_table_separator("| --- | --- | --- |"))
        self.assertTrue(cp.is_table_separator("| :--- | ---: |"))
        self.assertFalse(cp.is_table_separator("| a | b |"))
        self.assertFalse(cp.is_table_separator(""))

    def test_parse_plugin_rows(self):
        rows = cp.parse_plugin_rows(SAMPLE_README)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["section"], "界面与视图增强")
        self.assertEqual(rows[0]["repo"], "guopenghui/obsidian-quiet-outline")
        self.assertEqual(rows[2]["section"], "其他工具")

    def test_parse_current_plugins(self):
        existing = cp.parse_current_plugins(SAMPLE_README)
        self.assertIn("guopenghui/obsidian-quiet-outline", existing)
        self.assertIn("akifyss/obsidian-border", existing)
        self.assertEqual(len(existing), 4)

    def test_sort_plugin_tables_by_author(self):
        sorted_text = cp.sort_plugin_tables_by_author(SAMPLE_README)
        section = sorted_text[cp.plugin_section_bounds(sorted_text)[0]:cp.plugin_section_bounds(sorted_text)[1]]
        lines = [l for l in section.splitlines() if l.startswith("| [")]
        self.assertIn("cumany", lines[0])
        self.assertIn("guopenghui", lines[1])
        self.assertEqual(len(lines), 3)
        original_rows = sorted(
            l.strip() for l in SAMPLE_README.splitlines()
            if l.startswith("| [") and "obsidian-" in l
        )
        sorted_rows = sorted(l.strip() for l in lines if "obsidian-" in l)
        self.assertEqual(sorted_rows, original_rows)


class TestRowEditing(unittest.TestCase):
    def test_remove_plugin_rows(self):
        stale = [{"repo": "cumany/obsidian-floating-toc-plugin"}]
        text = cp.remove_plugin_rows(SAMPLE_README, stale)
        self.assertNotIn("obsidian-floating-toc-plugin", text)
        self.assertIn("obsidian-quiet-outline", text)

    def test_remove_plugin_rows_empty(self):
        self.assertEqual(cp.remove_plugin_rows(SAMPLE_README, []), SAMPLE_README)

    def test_append_rows_to_other_tools(self):
        rows = [{"name": "New Plugin", "repo": "newuser/new-plugin", "author": "newuser", "desc": "描述"}]
        text = cp.append_rows_to_other_tools(SAMPLE_README, rows)
        other = text[text.find("### 其他工具"):text.find("## 精选中文主题")]
        self.assertIn("newuser/new-plugin", other)
        self.assertIn("New Plugin", other)
        self.assertEqual(text.count("newuser/new-plugin"), 1)

    def test_append_rows_dedupes_existing(self):
        rows = [{"name": "Text Finder", "repo": "nyable/obsidian-text-finder", "author": "nyable", "desc": "x"}]
        text = cp.append_rows_to_other_tools(SAMPLE_README, rows)
        self.assertEqual(text.count("nyable/obsidian-text-finder"), 1)

    def test_append_sanitizes_pipes_in_desc(self):
        rows = [{"name": "P", "repo": "a/b", "author": "a", "desc": "x | y"}]
        text = cp.append_rows_to_other_tools(SAMPLE_README, rows)
        self.assertIn("x \\| y", text)


class TestDescCleaning(unittest.TestCase):
    def test_strips_reviewed_boilerplate(self):
        d = "中文描述。 - This plugin has not been manually reviewed by Obsidian staff."
        self.assertEqual(cp.clean_desc(d), "中文描述。")

    def test_strips_boilerplate_variants(self):
        for tail in (
            " - This plugin has not been manually reviewed by Obsidian staff",
            "— This plugin has not been manually reviewed by Obsidian staff.",
            " – this plugin has not been manually reviewed by obsidian staff.",
        ):
            self.assertEqual(cp.clean_desc("描述" + tail), "描述")

    def test_traditional_to_simplified(self):
        d = "用系統內建繁中語音朗讀筆記,逐句反白跟讀,介面全繁體中文。"
        self.assertEqual(
            cp.clean_desc(d),
            "用系统内建繁中语音朗读笔记,逐句反白跟读,界面全繁体中文。",
        )

    def test_english_desc_falls_back_to_readme_chinese(self):
        readme = "# Hans Kanban\n\nA kanban plugin for Obsidian Bases.\n\n看板与瀑布流卡片视图插件，支持整卡按属性着色与泳道。\n"
        out = cp.clean_desc("Kanban and masonry card views for Bases.", readme)
        self.assertIn("看板", out)
        self.assertNotIn("kanban", out.lower())

    def test_english_desc_kept_when_no_chinese_readme(self):
        d = "Playlist-based note navigation for Obsidian."
        out = cp.clean_desc(d, "# Playdown\n\nCreate playlists.")
        self.assertEqual(out, d)

    def test_clean_is_idempotent(self):
        d = "用系統內建繁中語音朗讀筆記,逐句反白跟讀,介面全繁體中文。 - This plugin has not been manually reviewed by Obsidian staff."
        once = cp.clean_desc(d)
        self.assertEqual(cp.clean_desc(once), once)

    def test_empty_desc_passthrough(self):
        self.assertEqual(cp.clean_desc(""), "")

    def test_summary_truncates_long_lines(self):
        readme = "# X\n\n" + "这是一个非常长的中文描述句子" * 10 + "\n"
        out = cp.clean_desc("Some English text", readme, max_len=30)
        self.assertLessEqual(len(out), 31)

    def test_append_cleans_desc_idempotently(self):
        rows = [{"name": "P", "repo": "a/b", "author": "a",
                 "desc": "描述 - This plugin has not been manually reviewed by Obsidian staff."}]
        text = cp.append_rows_to_other_tools(SAMPLE_README, rows)
        self.assertIn("描述", text)
        self.assertNotIn("manually reviewed", text)


class TestScoring(unittest.TestCase):
    def test_signal_weights(self):
        cn, q = cp.compute_scores({"locale", "docs_cn", "topic", "author_cn", "name_cn"})
        self.assertEqual(cn, 30 + 15 + 15 + 10 + 5)

    def test_quality_star_scale(self):
        cn, q = cp.compute_scores({"locale"}, stars=1_000_000)
        self.assertEqual(q, 40)
        cn, q = cp.compute_scores({"locale"}, stars=100)
        self.assertEqual(q, 26)
        cn, q = cp.compute_scores({"locale"}, stars=1)
        self.assertEqual(q, 0)

    def test_quality_downloads_and_recency(self):
        _, q = cp.compute_scores({"locale"}, downloads=5000, release_age_days=30)
        self.assertEqual(q, 25)
        _, q = cp.compute_scores({"locale"}, downloads=500, release_age_days=30)
        self.assertEqual(q, 18)
        _, q = cp.compute_scores({"locale"}, downloads=0, release_age_days=300)
        self.assertEqual(q, 0)

    def test_classify_rejects_weak_cn(self):
        self.assertIsNone(cp.classify_tier(cn=30, q=80, first_run=False))
        self.assertIsNone(cp.classify_tier(cn=34, q=0, first_run=False))

    def test_classify_auto(self):
        self.assertEqual(cp.classify_tier(cn=65, q=51, first_run=False), "auto")

    def test_classify_review_on_low_quality(self):
        self.assertEqual(cp.classify_tier(cn=65, q=0, first_run=False), "review")

    def test_classify_first_run_never_auto(self):
        self.assertEqual(cp.classify_tier(cn=95, q=70, first_run=True), "review")

    def test_collect_signals(self):
        plugin = {"name": "中文插件", "author": "张伟", "description": "描述"}
        meta = {"topics": ["obsidian", "chinese"], "description": "一个很长的中文描述文字"}
        signals = cp.collect_signals(plugin, meta, "Shanghai, China", True, True)
        self.assertEqual(signals, {"locale", "topic", "docs_cn", "desc_cn", "author_cn", "location", "name_cn"})


class TestStale(unittest.TestCase):
    def setUp(self):
        self.cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        fresh = "2026-06-01T00:00:00Z"
        old = "2024-06-01T00:00:00Z"
        self.fresh_meta = {"pushed_at": fresh}
        self.old_meta = {"pushed_at": old}

    def test_fresh_push_not_stale(self):
        self.assertIsNone(cp.decide_stale(self.fresh_meta, None, self.cutoff))

    def test_old_push_no_release_is_stale(self):
        self.assertIsNotNone(cp.decide_stale(self.old_meta, None, self.cutoff))

    def test_old_push_fresh_release_not_stale(self):
        self.assertIsNone(cp.decide_stale(self.old_meta, "2026-03-01T00:00:00Z", self.cutoff))

    def test_old_push_old_release_is_stale(self):
        decision = cp.decide_stale(self.old_meta, "2023-01-01T00:00:00Z", self.cutoff)
        self.assertIsNotNone(decision)
        self.assertEqual(decision["last_update"], "2023-01-01T00:00:00Z")

    def test_stale_fallback_to_pushed_at(self):
        decision = cp.decide_stale(self.old_meta, None, self.cutoff)
        self.assertEqual(decision["last_update"], "2024-06-01T00:00:00Z")


class TestCacheAndDeny(unittest.TestCase):
    def test_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cache.json")
            original = cp.CACHE_FILE
            cp.CACHE_FILE = path
            try:
                self.assertEqual(cp.load_checked(), set())
                cp.save_checked({"a/b", "c/d"})
                self.assertEqual(cp.load_checked(), {"a/b", "c/d"})
            finally:
                cp.CACHE_FILE = original

    def test_denylist_load(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as f:
            json.dump(["A/B", 123], f)
            path = f.name
        try:
            original = cp.DENY_FILE
            cp.DENY_FILE = path
            try:
                self.assertEqual(cp.load_denylist(), {"a/b"})
            finally:
                cp.DENY_FILE = original
        finally:
            os.unlink(path)


class TestOutputs(unittest.TestCase):
    def test_write_scan_output(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.txt")
            old = os.environ.get("GITHUB_OUTPUT")
            os.environ["GITHUB_OUTPUT"] = path
            try:
                cp.write_scan_output([{"repo": "a/b"}], [], True, False, True)
                with open(path, encoding="utf-8") as f:
                    content = f.read()
                self.assertIn("content_changed=true", content)
                self.assertIn("cache_changed=true", content)
                self.assertIn("first_run=false", content)
                self.assertIn("has_review=true", content)
                self.assertIn("auto_merge_ready=false", content)
            finally:
                if old:
                    os.environ["GITHUB_OUTPUT"] = old
                else:
                    os.environ.pop("GITHUB_OUTPUT", None)


class TestTitlesAndBody(unittest.TestCase):
    def test_update_title(self):
        self.assertEqual(cp.update_title(1, 0, "20260801"), "Add Chinese-relevant plugins (20260801)")
        self.assertEqual(cp.update_title(0, 1, "20260801"), "Remove stale plugins (20260801)")
        self.assertEqual(cp.update_title(1, 1, "20260801"), "Update Chinese-relevant plugins (20260801)")

    def test_build_pr_body_tiers(self):
        rows = [
            {"name": "A", "repo": "a/a", "author": "a", "desc": "d", "tier": "auto", "score": 120},
            {"name": "B", "repo": "b/b", "author": "b", "desc": "d", "tier": "review", "score": 60},
        ]
        body = cp.build_pr_body(rows, [])
        self.assertIn("Auto-merge candidates", body)
        self.assertIn("Review candidates", body)
        self.assertIn("auto-merge was skipped", body)

    def test_build_pr_body_stale(self):
        stale = [{"name": "X", "section": "其他工具", "author": "x", "last_update": "2024-01-01", "full_name": "x/x", "html_url": "https://github.com/x/x"}]
        body = cp.build_pr_body([], stale)
        self.assertIn("Plugins removed", body)
        self.assertIn("x/x", body)


class TestTimeHelpers(unittest.TestCase):
    def test_parse_github_time(self):
        t = cp.parse_github_time("2026-06-01T00:00:00Z")
        self.assertIsNotNone(t)
        self.assertEqual(t.tzinfo, timezone.utc)
        self.assertIsNone(cp.parse_github_time(""))


def fake_gh_get(url):
    """Canned GitHub API responses for the end-to-end scan test."""
    if url.startswith("https://api.github.com/repos/good/plugin/contents/"):
        if url.endswith("/contents/") or url.endswith("/contents"):
            return [{"name": "lang", "type": "dir"}, {"name": "README.md", "type": "file"}]
        if url.endswith("/lang"):
            return [{"name": "zh.json", "type": "file"}]
    if url == "https://api.github.com/repos/good/plugin/contents/README.md":
        return {"content": base64.b64encode("很棒的中文文档".encode()).decode()}
    if url == "https://api.github.com/repos/good/plugin":
        return {"topics": ["obsidian"], "description": "中文插件说明文字", "stargazers_count": 500, "pushed_at": "2026-07-01T00:00:00Z", "full_name": "good/plugin", "html_url": "https://github.com/good/plugin"}
    if url == "https://api.github.com/users/good":
        return {"location": "Shanghai, China"}
    if url == "https://api.github.com/repos/good/plugin/releases?per_page=5":
        return [{"published_at": "2026-06-01T00:00:00Z", "assets": [{"download_count": 2000}]}]
    if url == "https://api.github.com/repos/weak/plugin":
        return {"topics": [], "description": "random plugin", "stargazers_count": 2, "pushed_at": "2026-07-01T00:00:00Z", "full_name": "weak/plugin", "html_url": "https://github.com/weak/plugin"}
    if url == "https://api.github.com/repos/weak/plugin/contents/":
        return [{"name": "README.md", "type": "file"}]
    if url == "https://api.github.com/repos/weak/plugin/contents/README.md":
        return {"content": base64.b64encode("hello".encode()).decode()}
    if url == "https://api.github.com/users/weak":
        return {"location": ""}
    return None


class FakeSession:
    def __init__(self, plugins):
        self.plugins = plugins

    def get(self, url, timeout=None):
        return FakeResponse(self.plugins)


class FakeResponse:
    def __init__(self, plugins):
        self.plugins = plugins

    def json(self):
        return self.plugins


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workdir = self.tmp.name
        self._orig = {}
        for name in ("CACHE_FILE", "README_PATH", "PR_BODY_FILE", "DENY_FILE"):
            self._orig[name] = getattr(cp, name)
        cp.CACHE_FILE = os.path.join(self.workdir, "cache.json")
        cp.README_PATH = os.path.join(self.workdir, "README.md")
        cp.PR_BODY_FILE = os.path.join(self.workdir, "pr-body.md")
        cp.DENY_FILE = os.path.join(self.workdir, "deny.json")
        with open(cp.DENY_FILE, "w", encoding="utf-8") as f:
            json.dump(["rejected/repo"], f)
        with open(cp.README_PATH, "w", encoding="utf-8") as f:
            f.write(SAMPLE_README)
        self.old_env = os.environ.get("GITHUB_OUTPUT")
        os.environ.pop("GITHUB_OUTPUT", None)

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(cp, name, value)
        self.tmp.cleanup()
        if self.old_env:
            os.environ["GITHUB_OUTPUT"] = self.old_env
        else:
            os.environ.pop("GITHUB_OUTPUT", None)

    def test_scan_first_run_classifies_and_caches(self):
        plugins = [
            {"id": "good-plugin", "name": "好插件", "repo": "good/plugin", "author": "good", "description": "中文描述"},
            {"id": "weak-plugin", "name": "weak", "repo": "weak/plugin", "author": "weak", "description": "not chinese"},
            {"id": "rejected", "name": "被拒", "repo": "rejected/repo", "author": "r", "description": "中文"},
            {"id": "existing", "name": "已有", "repo": "guopenghui/obsidian-quiet-outline", "author": "g", "description": "中文"},
        ]
        with unittest.mock.patch.object(cp, "gh_get", side_effect=fake_gh_get), \
             unittest.mock.patch.object(cp, "create_retry_session", return_value=FakeSession(plugins)):
            cp.run_scan(skip_stale=True)

        with open(cp.CACHE_FILE, encoding="utf-8") as f:
            cached = set(json.load(f))
        self.assertIn("good-plugin", cached)
        self.assertIn("weak-plugin", cached)
        self.assertIn("rejected", cached)
        self.assertIn("existing", cached)

        # first run: good/plugin is review-tier (never auto on first run)
        self.assertTrue(cp.load_checked())
        self.assertEqual(cp.load_denylist(), {"rejected/repo"})

    def test_scan_second_run_proposes_auto_tier(self):
        plugins = [
            {"id": "good-plugin", "name": "好插件", "repo": "good/plugin", "author": "good", "description": "中文描述"},
            {"id": "old-plugin", "name": "老插件", "repo": "old/plugin", "author": "old", "description": "中文描述"},
        ]
        cp.save_checked({"old-plugin"})
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with unittest.mock.patch.object(cp, "gh_get", side_effect=fake_gh_get), \
             unittest.mock.patch.object(cp, "create_retry_session", return_value=FakeSession(plugins)), \
             redirect_stdout(out):
            cp.run_scan(skip_stale=True)
        data = json.loads(out.getvalue())
        self.assertFalse(data["first_run"])
        self.assertEqual(len(data["add"]), 1)
        self.assertEqual(data["add"][0]["repo"], "good/plugin")
        self.assertEqual(data["add"][0]["tier"], "auto")
        self.assertTrue(data["auto_merge_ready"])

    def test_apply_readme_full_cycle(self):
        os.environ["ADD_ROWS"] = json.dumps([{
            "name": "好插件", "repo": "good/plugin", "author": "good",
            "desc": "中文描述", "tier": "auto", "score": 130,
        }])
        os.environ["REMOVE_ROWS"] = json.dumps([{
            "name": "Text Finder", "repo": "nyable/obsidian-text-finder",
            "full_name": "nyable/obsidian-text-finder", "author": "nyable",
            "section": "其他工具", "last_update": "2024-01-01",
            "html_url": "https://github.com/nyable/obsidian-text-finder",
        }])
        cp.do_apply_readme()
        with open(cp.README_PATH, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("nyable/obsidian-text-finder", text)
        self.assertIn("good/plugin", text)
        with open(cp.PR_BODY_FILE, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("Auto-merge candidates", body)
        self.assertIn("Plugins removed", body)


class TestCleanup(unittest.TestCase):
    SAMPLE2 = """# H

## 原生中文插件

%% note %%

### AI 辅助

| 插件 | 作者 | 核心功能 |
| --- | --- | --- |
| [EVC](https://github.com/entire-vc/evc-team-relay-mcp) | `entire-vc` | 通过 MCP 协议读写笔记。 |

### 其他工具

| 插件 | 作者 | 核心功能 |
| --- | --- | --- |
| [AI Vault Assistant](https://github.com/1716775457damn/obsidian-ai-vault-assistant) | `x` | AI 对话整理 Vault。 |
| [Text Finder](https://github.com/nyable/obsidian-text-finder) | `nyable` | 查找替换。 |

## 精选中文主题

|主题|作者|
|---|---|
|[Border](https://github.com/Akifyss/obsidian-border)|`Akifyss`|
"""

    def test_classify_section(self):
        self.assertEqual(cp.classify_section("AI Vault Assistant", "AI 对话助手"), "AI 辅助")
        self.assertEqual(cp.classify_section("SmartTask", "高性能智能任务管理插件"), "任务与日程管理")
        self.assertEqual(cp.classify_section("LUMI Sync", "跨平台同步工具"), "数据同步与集成")
        self.assertEqual(cp.classify_section("Floating TOC", "浮动目录大纲"), "界面与视图增强")
        self.assertEqual(cp.classify_section("Obscure Thing", "一个很冷门的工具"), "其他工具")

    def test_is_broken_desc(self):
        self.assertTrue(cp.is_broken_desc("分类: 未分类"))
        self.assertTrue(cp.is_broken_desc("foo install --vault D:\\x"))
        self.assertTrue(cp.is_broken_desc("截断描述…"))
        self.assertFalse(cp.is_broken_desc("正常的中文描述。"))
        self.assertFalse(cp.is_broken_desc("Playlist-based note navigation for Obsidian."))

    def test_dedupe_removes_duplicate(self):
        dup = SAMPLE_README + "\n| [Quiet Outline](https://github.com/guopenghui/obsidian-quiet-outline) | `g` | 重复 |\n"
        out = cp.dedupe_plugin_rows(dup)
        self.assertEqual(out.count("guopenghui/obsidian-quiet-outline"), 1)

    def test_reorganize_moves_other_tools(self):
        out = cp.reorganize_sections(self.SAMPLE2)
        rows = cp.parse_plugin_rows_full(out)
        by_name = {r["name"]: r["section"] for r in rows}
        self.assertEqual(by_name["AI Vault Assistant"], "AI 辅助")
        self.assertEqual(by_name["Text Finder"], "其他工具")

    def test_generate_toc_anchor(self):
        toc = cp.generate_toc(self.SAMPLE2)
        self.assertIn("- [原生中文插件", toc)
        self.assertIn("](#ai-辅助)", toc)
        self.assertIn("](#精选中文主题)", toc)

    def test_apply_toc_idempotent(self):
        once = cp.apply_toc(self.SAMPLE2)
        self.assertIn("<!-- TOC:START -->", once)
        twice = cp.apply_toc(once)
        self.assertEqual(twice.count("<!-- TOC:START -->"), 1)

    def test_validate_flags_issues(self):
        bad = self.SAMPLE2.replace("查找替换。", "分类: 未分类")
        issues = cp.validate_readme(bad)
        self.assertTrue(any("broken/placeholder" in i for i in issues))

    def test_cleanup_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "README.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.SAMPLE2)
            original = cp.README_PATH
            cp.README_PATH = path
            try:
                cp.cleanup_readme()
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            finally:
                cp.README_PATH = original
        self.assertIn("<!-- TOC:START -->", text)
        rows = cp.parse_plugin_rows_full(text)
        by_name = {r["name"]: r["section"] for r in rows}
        self.assertEqual(by_name["AI Vault Assistant"], "AI 辅助")
        self.assertEqual(cp.validate_readme(text), [])

    def test_curated_desc_replaces_placeholder(self):
        # A curated plugin whose current cell is the placeholder must pick up
        # the hand-verified description; a non-curated broken row stays flagged.
        text = (
            "### 其他工具\n\n"
            "| 插件 | 作者 | 核心功能 |\n"
            "| --- | --- | --- |\n"
            "| [Agent Review](https://github.com/jiaoxiu20040903-crypto/Agent_Review) | `jiaoxiu20040903-crypto` | [描述待补充] |\n"
            "| [Some Plugin](https://github.com/foo/bar) | `foo` | 分类: 未分类 |\n"
        )
        out = cp.clean_all_descriptions(text)
        self.assertIn(cp.CURATED_DESC["Agent Review"], out)
        self.assertIn("[描述待补充]", out)  # non-curated broken row stays flagged
        self.assertNotIn("分类: 未分类", out)

    def test_clean_desc_for_readme_uses_curated(self):
        self.assertEqual(
            cp.clean_desc_for_readme("分类: 未分类", name="MeshSync"),
            cp.CURATED_DESC["MeshSync"],
        )

    def test_curated_section_map_covers_other_tools(self):
        # Every plugin that used to sit in the catch-all must be pinned to one
        # of the four new semantic sections.
        targets = set(cp.CURATED_SECTION.values())
        self.assertEqual(
            targets,
            {"效率与系统", "学术与写作", "思维导图与阅读", "娱乐与多媒体"},
        )
        self.assertEqual(cp.CURATED_SECTION["obsidian-xiangqi"], "娱乐与多媒体")
        self.assertEqual(cp.CURATED_SECTION["Chem"], "学术与写作")

    def test_reorganize_subsections(self):
        # The four new sections exist as headings; catch-all rows referencing
        # curated plugins must move into them, and an empty catch-all must
        # render the friendly 'no uncategorized entries' note.
        text = (
            "## 原生中文插件\n\n"
            "%% note %%\n\n"
            "### 效率与系统\n\n| 插件 | 作者 | 核心功能 |\n| --- | --- | --- |\n\n"
            "### 学术与写作\n\n| 插件 | 作者 | 核心功能 |\n| --- | --- | --- |\n\n"
            "### 思维导图与阅读\n\n| 插件 | 作者 | 核心功能 |\n| --- | --- | --- |\n\n"
            "### 娱乐与多媒体\n\n| 插件 | 作者 | 核心功能 |\n| --- | --- | --- |\n\n"
            "### 其他工具\n\n"
            "| 插件 | 作者 | 核心功能 |\n| --- | --- | --- |\n"
            "| [obsidian-xiangqi](https://github.com/west-shell/obsidian-xiangqi) | `west-shell` | 中国象棋变体树。 |\n"
            "| [Chem](https://github.com/Acylation/obsidian-chem) | `Acylation` | 化学式渲染。 |\n"
            "| [China BRAT](https://github.com/notesynchelper/chinabrat) | `notesynchelper` | BRAT 安装器。 |\n\n"
            "## 精选中文主题\n\n|主题|作者|\n|---|---|\n"
        )
        out = cp.reorganize_sections(text)
        rows = cp.parse_plugin_rows_full(out)
        by_name = {r["name"]: r["section"] for r in rows}
        self.assertEqual(by_name["obsidian-xiangqi"], "娱乐与多媒体")
        self.assertEqual(by_name["Chem"], "学术与写作")
        self.assertEqual(by_name["China BRAT"], "效率与系统")
        self.assertIn("暂无未分类条目", out)

    def test_classify_section_new_sections(self):
        # Future scans (no curated override) still route via keywords.
        self.assertEqual(cp.classify_section("Foo TTS", "用系统语音朗读笔记"), "娱乐与多媒体")
        self.assertEqual(cp.classify_section("Chem Draw", "渲染化学式结构式"), "学术与写作")
        self.assertEqual(cp.classify_section("Batch Renamer", "批量重命名文件"), "效率与系统")


class TestFreshness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workdir = self.tmp.name
        self._orig_readme = cp.README_PATH
        self._orig_cache = cp.FRESHNESS_CACHE_FILE
        cp.README_PATH = os.path.join(self.workdir, "README.md")
        cp.FRESHNESS_CACHE_FILE = os.path.join(self.workdir, "fresh.json")

    def tearDown(self):
        cp.README_PATH = self._orig_readme
        cp.FRESHNESS_CACHE_FILE = self._orig_cache
        self.tmp.cleanup()

    def test_strip_and_append_marker(self):
        self.assertEqual(
            cp.append_freshness_marker("中文描述", {"archived": False, "stale": False, "ok": True}),
            "中文描述",
        )
        self.assertEqual(
            cp.append_freshness_marker("中文描述", {"archived": True, "stale": False, "ok": True}),
            "中文描述 [已归档]",
        )
        self.assertEqual(
            cp.append_freshness_marker("中文描述", {"archived": False, "stale": True, "ok": True}),
            "中文描述 [长期未更新]",
        )
        # archived outranks stale
        self.assertEqual(
            cp.append_freshness_marker("中文描述", {"archived": True, "stale": True, "ok": True}),
            "中文描述 [已归档]",
        )

    def test_marker_idempotent_and_self_correcting(self):
        marked = cp.append_freshness_marker("中文描述", {"archived": True, "stale": False, "ok": True})
        self.assertEqual(
            cp.append_freshness_marker(marked, {"archived": True, "stale": False, "ok": True}),
            marked,
        )
        # repo becomes active -> marker stripped on re-run
        self.assertEqual(
            cp.append_freshness_marker(marked, {"archived": False, "stale": False, "ok": True}),
            "中文描述",
        )

    def test_marker_not_broken(self):
        self.assertFalse(cp.is_broken_desc("中文描述 [已归档]"))
        self.assertFalse(cp.is_broken_desc("中文描述 [长期未更新]"))
        self.assertFalse(cp.is_broken_desc(cp.strip_freshness_marker("中文描述 [已归档]")))

    def test_get_repo_freshness_archived(self):
        with unittest.mock.patch.object(cp, "repo_meta", return_value={"archived": True, "pushed_at": "2020-01-01T00:00:00Z"}), \
             unittest.mock.patch.object(cp, "repo_releases", return_value=("2020-01-01T00:00:00Z", 0)):
            flags = cp.get_repo_freshness("x/y")
        self.assertTrue(flags["archived"])
        self.assertTrue(flags["stale"])

    def test_get_repo_freshness_fresh(self):
        recent = cp.utcnow().isoformat()
        with unittest.mock.patch.object(cp, "repo_meta", return_value={"archived": False, "pushed_at": recent}), \
             unittest.mock.patch.object(cp, "repo_releases", return_value=(recent, 0)):
            flags = cp.get_repo_freshness("x/y")
        self.assertFalse(flags["archived"])
        self.assertFalse(flags["stale"])

    def test_get_repo_freshness_network_fail_neutral(self):
        with unittest.mock.patch.object(cp, "repo_meta", return_value={}):
            flags = cp.get_repo_freshness("x/y")
        self.assertFalse(flags["archived"])
        self.assertFalse(flags["stale"])
        self.assertFalse(flags["ok"])

    def test_freshness_readme_marks_rows_and_skips_themes(self):
        readme = (
            "## 原生中文插件\n\n"
            "### 其他工具\n\n"
            "| 插件 | 作者 | 核心功能 |\n| --- | --- | --- |\n"
            "| [Arch](https://github.com/a/arch) | `a` | 已归档插件 |\n"
            "| [Live](https://github.com/b/live) | `b` | 活跃插件 |\n\n"
            "## 精选中文主题\n\n"
            "|主题|作者|\n|---|---|\n"
            "|[Zen](https://github.com/laughmaker/Zen)|`laughmaker`|\n"
        )
        with open(cp.README_PATH, "w", encoding="utf-8") as f:
            f.write(readme)
        flags = {
            "a/arch": {"archived": True, "stale": True, "ok": True},
            "b/live": {"archived": False, "stale": False, "ok": True},
        }
        with unittest.mock.patch.object(
            cp, "get_repo_freshness",
            side_effect=lambda repo, cutoff=None: flags.get(repo, {"archived": False, "stale": False, "ok": True}),
        ):
            cp.freshness_readme()
        with open(cp.README_PATH, encoding="utf-8") as f:
            out = f.read()
        self.assertIn("已归档插件 [已归档]", out)
        self.assertIn("活跃插件", out)
        self.assertNotIn("活跃插件 [", out)
        # theme row outside plugin section must be untouched
        self.assertIn("|[Zen](https://github.com/laughmaker/Zen)|`laughmaker`|", out)

    def test_get_repo_freshness_failure_not_cached(self):
        # A check that yields no metadata must NOT be persisted, so the next
        # run re-attempts it instead of freezing a neutral result for a week.
        with unittest.mock.patch.object(cp, "repo_meta", return_value={}):
            flags = cp.get_repo_freshness("fail/repo")
        self.assertFalse(flags["ok"])
        self.assertNotIn("fail/repo", cp.load_freshness_cache())

    def test_get_repo_freshness_concurrent_no_lost_writes(self):
        # Regression guard for the cache write race: 25 repos checked through a
        # 12-worker pool must all land in the cache (no last-writer-wins loss).
        repos = [f"owner{i}/repo{i}" for i in range(25)]
        recent = cp.utcnow().isoformat()

        def fake_meta(repo):
            return {"archived": False, "pushed_at": recent}

        with unittest.mock.patch.object(cp, "repo_meta", side_effect=fake_meta), \
             unittest.mock.patch.object(cp, "repo_releases", return_value=(recent, 0)):
            with cp.ThreadPoolExecutor(max_workers=12) as ex:
                list(ex.map(lambda r: cp.get_repo_freshness(r), repos))
        cache = cp.load_freshness_cache()
        self.assertEqual(len(cache), len(repos))
        for r in repos:
            self.assertIn(r, cache)
            self.assertTrue(cache[r]["ok"])


if __name__ == "__main__":
    unittest.main()
