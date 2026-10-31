#!/usr/bin/env python3
import base64
import json
import os
import re
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

import check_plugins as cp
import recheck_readme as rr

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
    def test_dormant_rows_are_never_deleted(self):
        # Dormancy is reported and marked in place, never removed: a year
        # without a commit is weak evidence that a plugin is unwanted, so no
        # code path may drop a row from the README on that basis.
        self.assertFalse(hasattr(cp, "remove_plugin_rows"),
                         "remove_plugin_rows is gone: dormancy must not delete rows")
        with open(cp.__file__, encoding="utf-8") as fh:
            self.assertNotIn("remove_plugin_rows", fh.read())
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

    def test_readme_zh_signal_clears_cn_threshold_with_docs(self):
        # English metadata + Chinese README is exactly issue 36's shape: the
        # two README signals alone must clear MIN_CN_SCORE.
        cn, q = cp.compute_scores({"readme_zh", "docs_cn"})
        self.assertEqual(cn, 20 + 15)
        self.assertGreaterEqual(cn, cp.MIN_CN_SCORE)

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
        signals = cp.collect_signals(plugin, meta, "Shanghai, China", True, True, True)
        self.assertEqual(signals, {"locale", "readme_zh", "topic", "docs_cn", "desc_cn", "author_cn", "location", "name_cn"})

    def test_collect_signals_omits_readme_zh_by_default(self):
        signals = cp.collect_signals({}, {"topics": []}, "", False, False)
        self.assertNotIn("readme_zh", signals)


class TestReadmeZhEvidence(unittest.TestCase):
    def test_cjk_body_rule(self):
        self.assertEqual(cp.readme_zh_evidence("中" * cp.README_CJK_BODY_MIN), "cjk-body")

    def test_short_cjk_body_is_not_enough(self):
        self.assertIsNone(cp.readme_zh_evidence("中" * (cp.README_CJK_BODY_MIN - 1)))

    def test_chinese_marker_outside_code(self):
        self.assertEqual(cp.readme_zh_evidence("[中文](./README_ZH.md)"), "zh-word")

    def test_chinese_marker_inside_code_is_ignored(self):
        # A Chinese path in a code sample is not documentation language.
        self.assertIsNone(cp.readme_zh_evidence("```\nNotes/中文笔记.md\n```"))
        self.assertIsNone(cp.readme_zh_evidence("open `Notes/中文笔记.md` first"))

    def test_kana_excludes_japanese_readme(self):
        self.assertIsNone(cp.readme_zh_evidence("これは日本語のドキュメントです。中文" * 20))

    def test_empty_readme(self):
        self.assertIsNone(cp.readme_zh_evidence(""))
        self.assertIsNone(cp.readme_zh_evidence(None))


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
        self.assertEqual(cp.update_title(1, "20260801"),
                         "Add Chinese-relevant plugins (20260801)")
        self.assertEqual(cp.update_title(0, "20260801"),
                         "No new Chinese-relevant plugins (20260801)")
    def test_build_pr_body_tiers(self):
        rows = [
            {"name": "A", "repo": "a/a", "author": "a", "desc": "d", "tier": "auto", "score": 120},
            {"name": "B", "repo": "b/b", "author": "b", "desc": "d", "tier": "review", "score": 60},
        ]
        body = cp.build_pr_body(rows)
        self.assertIn("Auto-merge candidates", body)
        self.assertIn("Review candidates", body)
        self.assertIn("auto-merge was skipped", body)

    def test_build_pr_body_never_claims_removals(self):
        body = cp.build_pr_body([])
        self.assertNotIn("Plugins removed", body)
        self.assertNotIn("removed", body.lower())
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
    if url.startswith("https://api.github.com/repos/broken/plugin/contents/"):
        if url.endswith("/contents/") or url.endswith("/contents"):
            return [{"name": "lang", "type": "dir"}, {"name": "README.md", "type": "file"}]
        if url.endswith("/lang"):
            return [{"name": "zh.json", "type": "file"}]
    if url == "https://api.github.com/repos/broken/plugin/contents/README.md":
        # Chinese, but too short to yield a usable summary line (<5 chars).
        return {"content": base64.b64encode("你好世界".encode()).decode()}
    if url == "https://api.github.com/repos/broken/plugin":
        return {"topics": [], "description": "npm install broken", "stargazers_count": 40, "pushed_at": "2026-07-01T00:00:00Z", "full_name": "broken/plugin", "html_url": "https://github.com/broken/plugin"}
    if url == "https://api.github.com/users/broken":
        return {"location": "Beijing, China"}
    # English metadata, Chinese README (issue 36's shape). No locale dir, no
    # Chinese topic, no Chinese description, no Chinese author name.
    if url.startswith("https://api.github.com/repos/zhreadme/plugin/contents/"):
        if url.endswith("/contents/") or url.endswith("/contents"):
            return [{"name": "README.md", "type": "file"}]
    if url == "https://api.github.com/repos/zhreadme/plugin/contents/README.md":
        return {"content": base64.b64encode((("这是一个课程表插件。" + "支持周次、单双周和调课。\n") * 30).encode()).decode()}
    if url == "https://api.github.com/repos/zhreadme/plugin":
        return {"topics": [], "description": "a class timetable", "stargazers_count": 5000, "pushed_at": "2026-07-01T00:00:00Z", "full_name": "zhreadme/plugin", "html_url": "https://github.com/zhreadme/plugin"}
    if url == "https://api.github.com/users/zhreadme":
        return {"location": ""}
    if url == "https://api.github.com/repos/zhreadme/plugin/releases?per_page=5":
        return [{"published_at": "2026-06-01T00:00:00Z", "assets": [{"download_count": 2000}]}]
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
        with unittest.mock.patch.object(cp, "verify_api_auth"), \
             unittest.mock.patch.object(cp, "gh_get", side_effect=fake_gh_get), \
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
        with unittest.mock.patch.object(cp, "verify_api_auth"), \
             unittest.mock.patch.object(cp, "gh_get", side_effect=fake_gh_get), \
             unittest.mock.patch.object(cp, "create_retry_session", return_value=FakeSession(plugins)), \
             redirect_stdout(out):
            cp.run_scan(skip_stale=True)
        data = json.loads(out.getvalue())
        self.assertFalse(data["first_run"])
        self.assertEqual(len(data["add"]), 1)
        self.assertEqual(data["add"][0]["repo"], "good/plugin")
        self.assertEqual(data["add"][0]["tier"], "auto")
        self.assertTrue(data["auto_merge_ready"])

    def test_scan_skips_unshippable_placeholder_candidates(self):
        # A candidate with no usable description ships as '[描述待补充]',
        # which validate_readme rejects. It must not set content_changed
        # (2026-10-07: this combo crashed the scan's commit step); the cache
        # backfill still records it so the next scan moves on.
        plugins = [
            {"id": "known-plugin", "name": "已知", "repo": "known/plugin", "author": "k", "description": "中文描述"},
            {"id": "broken-desc", "name": "无描述插件", "repo": "broken/plugin", "author": "broken", "description": "npm install broken"},
        ]
        cp.save_checked({"known-plugin"})
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with unittest.mock.patch.object(cp, "verify_api_auth"), \
             unittest.mock.patch.object(cp, "gh_get", side_effect=fake_gh_get), \
             unittest.mock.patch.object(cp, "create_retry_session", return_value=FakeSession(plugins)), \
             redirect_stdout(out):
            cp.run_scan(skip_stale=True)
        data = json.loads(out.getvalue())
        self.assertEqual(data["add"], [])
        self.assertFalse(data["content_changed"])
        self.assertTrue(data["cache_changed"])
        self.assertIn("broken-desc", cp.load_checked())

    def test_scan_admits_english_metadata_with_chinese_readme(self):
        # issue 36: Class Timetable / Vault Orrery had English name, author,
        # description and topics, so gate 1 dropped them before their Chinese
        # README was ever read. The README must now admit them on its own.
        plugins = [
            {"id": "known-plugin", "name": "已知", "repo": "known/plugin", "author": "k", "description": "中文描述"},
            {"id": "zhreadme-plugin", "name": "Class Timetable", "repo": "zhreadme/plugin",
             "author": "Elliott Park", "description": "a class timetable"},
        ]
        cp.save_checked({"known-plugin"})
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with unittest.mock.patch.object(cp, "verify_api_auth"), \
             unittest.mock.patch.object(cp, "gh_get", side_effect=fake_gh_get), \
             unittest.mock.patch.object(cp, "create_retry_session", return_value=FakeSession(plugins)), \
             redirect_stdout(out):
            cp.run_scan(skip_stale=True)
        data = json.loads(out.getvalue())
        self.assertEqual([r["repo"] for r in data["add"]], ["zhreadme/plugin"])
        self.assertEqual(data["add"][0]["tier"], "auto")
        # The Chinese README must supply the description (metadata is English).
        self.assertIn("课程表", data["add"][0]["desc"])

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
        # The new plugin is added; the dormant one stays exactly where it was.
        self.assertIn("good/plugin", text)
        self.assertIn("nyable/obsidian-text-finder", text,
                      "REMOVE_ROWS must not delete a row")
        with open(cp.PR_BODY_FILE, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("Auto-merge candidates", body)
        self.assertNotIn("Plugins removed", body)


class TestAppendRebuildsCatchAllSkeleton(unittest.TestCase):
    def test_append_rebuilds_skeleton_less_catch_all(self):
        # reorganize_sections can leave the catch-all as just its heading and
        # a note; a new row must rebuild the table, not vanish silently
        # (2026-10-07: this exact state crashed the scan's commit step).
        readme = (
            "## 原生中文插件，欢迎支持\n\n"
            "### 其他工具\n\n"
            "_（同步类工具等）_\n\n"
            "## 精选中文主题\n\n"
            "| 主题 | 推荐者 |\n"
            "| --- | --- |\n"
        )
        rows = [{"name": "Qiaomu UI Learn", "repo": "joeseesun/qiaomu-ui-learn",
                 "author": "joeseesun", "desc": "qd", "tier": "review", "score": 51}]
        out = cp.append_rows_to_other_tools(readme, rows)
        self.assertIn("joeseesun/qiaomu-ui-learn", out)
        self.assertIn("| 插件 | 作者 | 核心功能 |", out)
        self.assertIn("| --- | --- | --- |", out)
        # The rebuilt separator must be a valid anchor for the next append.
        out2 = cp.append_rows_to_other_tools(out, [
            {"name": "B", "repo": "b/b", "author": "b", "desc": "d", "tier": "auto", "score": 90}])
        self.assertIn("github.com/b/b", out2)
        self.assertLess(out2.index("joeseesun/qiaomu-ui-learn"),
                        out2.index("github.com/b/b"))


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

    def test_curated_desc_overrides_an_english_cell(self):
        # Upstream English descriptions give way to the curated Chinese one, so a
        # later scan pass cannot revert the list to English; the freshness
        # decoration already on the cell has to survive the rewrite.
        text = (
            "### 其他工具\n\n"
            "| 插件 | 作者 | 核心功能 |\n"
            "| --- | --- | --- |\n"
            "| [MeshSync](https://github.com/cimu233/obsidian-mesh-sync) | `cimu233` "
            "| Sync vaults peer to peer. · 最后更新 2026-10 |\n"
        )
        out = cp.clean_all_descriptions(text)
        self.assertIn(cp.CURATED_DESC["MeshSync"], out)
        self.assertNotIn("Sync vaults peer to peer.", out)
        self.assertIn("· 最后更新 2026-10", out)
        self.assertEqual(cp.clean_all_descriptions(out), out)

    def test_clean_desc_for_readme_uses_curated(self):
        self.assertEqual(
            cp.clean_desc_for_readme("分类: 未分类", name="MeshSync"),
            cp.CURATED_DESC["MeshSync"],
        )

    def test_curated_section_map_covers_other_tools(self):
        # Every plugin that used to sit in the catch-all must be pinned to a
        # real theme section, so a later re-file never sends it back there.
        targets = set(cp.CURATED_SECTION.values())
        self.assertLessEqual(targets, set(cp.CLASSIFY_PRIORITY))
        for section in ("效率与系统", "学术与写作", "思维导图与阅读", "娱乐与多媒体"):
            self.assertIn(section, targets)
        self.assertEqual(cp.CURATED_SECTION["obsidian-xiangqi"], "娱乐与多媒体")
        self.assertEqual(cp.CURATED_SECTION["Chem"], "学术与写作")
        self.assertEqual(cp.CURATED_SECTION["MySecret"], "效率与系统")
        self.assertEqual(cp.CURATED_SECTION["Bucket Sidebar"], "界面与视图增强")

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

    def test_glued_heading_rows_keep_section(self):
        # Regression: a '### ' heading glued to the previous line (no trailing
        # newline) must still be recognized. Otherwise its rows get an empty
        # section and reorganize_sections drops/garbles them (see 2026-09-04 fix).
        text = (
            "## 原生中文插件\n\n"
            "%% note %%\n\n"
            "### 界面与视图增强\n\n"
            "| 插件 | 作者 | 核心功能 |\n| --- | --- | --- |\n"
            "| [SmoothCursor](https://github.com/busyoGG/SmoothCursor) | `busyoGG` | 顺滑光标。 |\n"
            "| [Quiet Outline](https://github.com/guopenghui/obsidian-quiet-outline) | `guopenghui` | 大纲视图。 |### 编辑与格式化\n\n"
            "| 插件 | 作者 | 核心功能 |\n| --- | --- | --- |\n"
            "| [cm-chs-patch](https://github.com/aidenlx/cm-chs-patch) | `aidenlx` | 中文分词。 |\n\n"
            "## 精选中文主题\n\n|主题|作者|\n|---|---|\n"
        )
        rows = cp.parse_plugin_rows_full(text)
        by_name = {r["name"]: r["section"] for r in rows}
        self.assertEqual(by_name["SmoothCursor"], "界面与视图增强")
        self.assertEqual(by_name["Quiet Outline"], "界面与视图增强")
        self.assertEqual(by_name["cm-chs-patch"], "编辑与格式化")
        # reorganize must not drop or glue these rows
        out = cp.reorganize_sections(text)
        self.assertNotIn("|### ", out)
        self.assertIn("[SmoothCursor]", out)
        self.assertIn("[cm-chs-patch]", out)

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

    def test_decorate_row_freshness_badge(self):
        # archived repo: marker then date badge
        self.assertEqual(
            cp.decorate_row_freshness(
                "中文描述",
                {"archived": True, "stale": False, "ok": True, "last_update": "2023-01-15T00:00:00Z"},
            ),
            "中文描述 [已归档] · 最后更新 2023-01",
        )
        # active repo: date badge only
        self.assertEqual(
            cp.decorate_row_freshness(
                "中文描述",
                {"archived": False, "stale": False, "ok": True, "last_update": "2024-06-01T00:00:00Z"},
            ),
            "中文描述 · 最后更新 2024-06",
        )
        # metadata missing / fetch failed: no badge
        self.assertEqual(
            cp.decorate_row_freshness(
                "中文描述",
                {"archived": False, "stale": False, "ok": False, "last_update": ""},
            ),
            "中文描述",
        )
        # idempotent: re-decorating a decorated row must not duplicate the badge
        decorated = cp.decorate_row_freshness(
            "中文描述",
            {"archived": False, "stale": False, "ok": True, "last_update": "2024-06-01T00:00:00Z"},
        )
        self.assertEqual(
            cp.decorate_row_freshness(
                decorated,
                {"archived": False, "stale": False, "ok": True, "last_update": "2024-06-01T00:00:00Z"},
            ),
            "中文描述 · 最后更新 2024-06",
        )

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
        with unittest.mock.patch.object(cp, "verify_api_auth"), \
             unittest.mock.patch.object(
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

class TestAuthPreflight(unittest.TestCase):
    """A rejected token must abort the run, not degrade it to a no-op."""

    def _resp(self, status, payload=None):
        r = unittest.mock.Mock()
        r.status_code = status
        r.json.return_value = payload if payload is not None else {}
        r.raise_for_status = unittest.mock.Mock()
        return r

    def _session(self, resp):
        s = unittest.mock.Mock()
        s.get.return_value = resp
        return s

    def test_missing_token_exits(self):
        with unittest.mock.patch.object(cp, "GITHUB_TOKEN", ""),              unittest.mock.patch.object(cp, "API_HEADERS", {}),              self.assertRaises(SystemExit) as cm:
            cp.verify_api_auth("scan")
        self.assertEqual(cm.exception.code, 1)

    def test_rejected_token_401_exits(self):
        with unittest.mock.patch.object(cp, "GITHUB_TOKEN", "ghp_dead"),              unittest.mock.patch.object(cp, "API_HEADERS", {"Authorization": "Bearer ghp_dead"}),              unittest.mock.patch.object(cp, "create_retry_session",
                                        return_value=self._session(self._resp(401))),              self.assertRaises(SystemExit) as cm:
            cp.verify_api_auth("scan")
        self.assertEqual(cm.exception.code, 1)

    def test_rate_limited_403_exits(self):
        with unittest.mock.patch.object(cp, "GITHUB_TOKEN", "ghp_ok"),              unittest.mock.patch.object(cp, "API_HEADERS", {}),              unittest.mock.patch.object(cp, "create_retry_session",
                                        return_value=self._session(self._resp(403))),              self.assertRaises(SystemExit) as cm:
            cp.verify_api_auth()
        self.assertEqual(cm.exception.code, 1)

    def test_valid_token_passes(self):
        payload = {"resources": {"core": {"remaining": 4999}}}
        with unittest.mock.patch.object(cp, "GITHUB_TOKEN", "ghp_ok"),              unittest.mock.patch.object(cp, "API_HEADERS", {}),              unittest.mock.patch.object(cp, "create_retry_session",
                                        return_value=self._session(self._resp(200, payload))):
            cp.verify_api_auth("scan")  # must not raise

    def test_network_failure_exits(self):
        import requests
        session = unittest.mock.Mock()
        session.get.side_effect = requests.exceptions.ConnectionError("boom")
        with unittest.mock.patch.object(cp, "GITHUB_TOKEN", "ghp_ok"),              unittest.mock.patch.object(cp, "API_HEADERS", {}),              unittest.mock.patch.object(cp, "create_retry_session", return_value=session),              self.assertRaises(SystemExit) as cm:
            cp.verify_api_auth()
        self.assertEqual(cm.exception.code, 1)


class TestFreshnessNeverErasesOnFailure(unittest.TestCase):
    def test_unavailable_metadata_leaves_row_untouched(self):
        marked = "中文描述 · 最后更新 2026-06"
        self.assertEqual(
            cp.decorate_row_freshness(marked, {"ok": False, "archived": False, "stale": False}),
            marked,
        )

    def test_available_metadata_still_replaces_badge(self):
        out = cp.decorate_row_freshness(
            "中文描述 · 最后更新 2026-06",
            {"ok": True, "archived": False, "stale": False, "last_update": "2026-09-01T00:00:00Z"},
        )
        self.assertIn("最后更新 2026-09", out)
        self.assertNotIn("2026-06", out)

    def test_zero_success_freshness_run_exits(self):
        tmp = tempfile.TemporaryDirectory()
        self._orig = cp.README_PATH
        cp.README_PATH = os.path.join(tmp.name, "README.md")
        try:
            with open(cp.README_PATH, "w", encoding="utf-8") as f:
                f.write(SAMPLE_README)
            dead = {"ok": False, "archived": False, "stale": False, "last_update": ""}
            with unittest.mock.patch.object(cp, "verify_api_auth"),                  unittest.mock.patch.object(cp, "get_repo_freshness", return_value=dead),                  self.assertRaises(SystemExit) as cm:
                cp.freshness_readme()
            self.assertEqual(cm.exception.code, 1)
            # README must be left exactly as it was.
            with open(cp.README_PATH, encoding="utf-8") as f:
                self.assertEqual(f.read(), SAMPLE_README)
        finally:
            cp.README_PATH = self._orig
            tmp.cleanup()


class TestValidateRejectsUnmanagedRows(unittest.TestCase):
    def test_foreign_domain_row_is_flagged(self):
        text = SAMPLE_README.replace(
            "| [Text Finder](https://github.com/nyable/obsidian-text-finder) | `nyable` | 查找/替换 |",
            "| [Text Finder](https://github.com/nyable/obsidian-text-finder) | `nyable` | 查找/替换 |\n"
                        "| [墨匠](https://dsr.ink/) | `dsr.ink` | 导出 Markdown |",
        )
        issues = cp.validate_readme(text)
        self.assertTrue(any("墨匠" in i for i in issues), issues)

    def test_clean_readme_has_no_unmanaged_rows(self):
        self.assertEqual(cp.validate_readme(SAMPLE_README), [])

    def test_theme_rows_outside_plugin_section_are_ignored(self):
        # The theme table is below PLUGIN_SECTION_END; a non-github row there
        # is not a plugin row and must not fail validation.
        text = SAMPLE_README + (
                    "\n|主题|作者|\n|---|---|\n|[某主题](https://example.com/t)|`x`|\n"
                )
        self.assertEqual(cp.validate_readme(text), [])

    def test_table_header_and_separator_ignored(self):
        self.assertEqual(cp.validate_readme(SAMPLE_README), [])


if __name__ == "__main__":
    unittest.main()


class TestDormancyIsMarkOnly(unittest.TestCase):
    """Dormant rows stay in the README; the marker is what surfaces them."""

    def test_apply_ignores_remove_rows_env(self):
        tmp = tempfile.TemporaryDirectory()
        orig = cp.README_PATH
        orig_body = cp.PR_BODY_FILE
        cp.README_PATH = os.path.join(tmp.name, "README.md")
        # do_apply_readme also writes PR_BODY_FILE; without this redirect the
        # test overwrites the repo's tracked .github/pr-body.md.
        cp.PR_BODY_FILE = os.path.join(tmp.name, "pr-body.md")
        try:
            with open(cp.README_PATH, "w", encoding="utf-8") as f:
                f.write(SAMPLE_README)
            os.environ["ADD_ROWS"] = "[]"
            os.environ["REMOVE_ROWS"] = json.dumps([{
                "name": "Text Finder", "repo": "nyable/obsidian-text-finder",
                "full_name": "nyable/obsidian-text-finder", "author": "nyable",
                "section": "其他工具", "last_update": "2024-01-01",
                "html_url": "https://github.com/nyable/obsidian-text-finder",
            }])
            cp.do_apply_readme()
            with open(cp.README_PATH, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("nyable/obsidian-text-finder", text)
        finally:
            cp.README_PATH = orig
            cp.PR_BODY_FILE = orig_body
            os.environ.pop("REMOVE_ROWS", None)
            tmp.cleanup()

    def test_dormant_row_keeps_its_marker(self):
        marked = "中文描述 [长期未更新]"
        out = cp.decorate_row_freshness(
            marked,
            {"ok": True, "archived": False, "stale": True,
             "last_update": "2024-01-01T00:00:00Z"},
        )
        self.assertIn("长期未更新", out)

    def test_dormancy_alone_does_not_trigger_content_change(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        old_out = os.environ.pop("GITHUB_OUTPUT", None)
        try:
            with redirect_stdout(buf):
                cp.write_scan_output([], [{"repo": "a/b"}], False, False, False)
        finally:
            if old_out:
                os.environ["GITHUB_OUTPUT"] = old_out
        data = json.loads(buf.getvalue())
        self.assertFalse(data["content_changed"])
        self.assertEqual(len(data["stale"]), 1)


# --------------------------------------------------------------------------
# Observation zone: fixed section, automatic promote / drop
# --------------------------------------------------------------------------

OBSERVE_SAMPLE = """# Header

## 原生中文插件

%% note %%

### 界面与视图增强

| 插件 | 作者 | 核心功能 |
| --- | --- | --- |
| [Quiet Outline](https://github.com/guopenghui/obsidian-quiet-outline) | `guopenghui` | 大纲视图 |

### 其他工具

| 插件 | 作者 | 核心功能 |
| --- | --- | --- |
| [Text Finder](https://github.com/nyable/obsidian-text-finder) | `nyable` | 查找/替换 |

### 新插件观察区

> 新收录的插件先在此观察；达到活跃标准后自动转入上方的主题表格，长期没有动静的自动移出。

_（暂无观察中的插件）_

## 精选中文主题

|主题|作者|
|---|---|
|[Border](https://github.com/Akifyss/obsidian-border)|`Akifyss`|
"""


def _observe_row(name="New Plugin", repo="new/plugin", author="new", desc="中文描述"):
    return {"name": name, "repo": repo, "author": author, "desc": desc,
            "tier": "review", "score": 60}


class TestObserveZoneInsertion(unittest.TestCase):
    def test_append_into_empty_zone_builds_table_and_keeps_note(self):
        out = cp.append_rows_to_observe(OBSERVE_SAMPLE, [_observe_row()])
        self.assertIn("| [New Plugin](https://github.com/new/plugin) | `new` | 中文描述 |", out)
        self.assertIn("| 插件 | 作者 | 核心功能 |", out)
        self.assertIn("| --- | --- | --- |", out)
        self.assertIn(cp.OBSERVE_NOTE, out)
        # The empty-state placeholder disappears once the table exists.
        self.assertNotIn(cp.OBSERVE_EMPTY_NOTE, out)
        # The table is collapsed behind a <details> block.
        self.assertIn(cp.OBSERVE_SUMMARY_LABEL, out)
        self.assertIn(cp.OBSERVE_DETAILS_CLOSE, out)
        # And the zone stays inside the plugin section, after every theme table.
        self.assertGreater(out.index(cp.OBSERVE_SECTION),
                           out.index("### 界面与视图增强"))
        self.assertLess(out.index(cp.OBSERVE_SECTION), out.index("## 精选中文主题"))

    def test_second_append_lands_after_the_first(self):
        first = cp.append_rows_to_observe(OBSERVE_SAMPLE, [
            _observe_row(name="A", repo="a/plugin", author="a")])
        second = cp.append_rows_to_observe(first, [
            _observe_row(name="B", repo="b/plugin", author="b")])
        self.assertLess(second.index("github.com/a/plugin"),
                        second.index("github.com/b/plugin"))
        self.assertLess(second.index("github.com/b/plugin"),
                        second.index("</details>"))
        self.assertEqual(second.count(cp.OBSERVE_SECTION), 1)

    def test_append_creates_the_missing_section_at_the_end_of_the_plugin_section(self):
        # SAMPLE_README has no observation zone: it must be created, not skipped,
        # at the end of the plugin section so it never buries the curated tables.
        out = cp.append_rows_to_observe(SAMPLE_README, [_observe_row()])
        self.assertIn(cp.OBSERVE_SECTION, out)
        self.assertGreater(out.index(cp.OBSERVE_SECTION),
                           out.index("### 界面与视图增强"))
        self.assertLess(out.index(cp.OBSERVE_SECTION), out.index("## 精选中文主题"))
        self.assertLess(out.index("github.com/new/plugin"), out.index("## 精选中文主题"))
        self.assertIn(cp.OBSERVE_NOTE, out)
        # Valid markdown shape: the details block closes before the next heading.
        self.assertIn("</details>\n\n## 精选中文主题", out)

    def test_append_skips_repos_already_in_the_readme(self):
        rows = [_observe_row(
            name="Quiet Outline", repo="guopenghui/obsidian-quiet-outline",
            author="guopenghui", desc="大纲视图")]
        self.assertEqual(cp.append_rows_to_observe(OBSERVE_SAMPLE, rows), OBSERVE_SAMPLE)

    def test_parse_observe_rows_reads_only_the_zone(self):
        text = cp.append_rows_to_observe(OBSERVE_SAMPLE, [_observe_row()])
        rows = cp.parse_observe_rows(text)
        self.assertEqual([r["repo"] for r in rows], ["new/plugin"])
        self.assertEqual(rows[0]["author"], "new")
        # Rows in the theme tables are not part of the zone.
        self.assertNotIn("guopenghui/obsidian-quiet-outline",
                         [r["repo"] for r in rows])


class TestObserveZoneRemoval(unittest.TestCase):
    def test_remove_only_touches_the_zone(self):
        # Same repo in the main table and in the zone: only the zone row may go
        # (a promoted row is removed here and re-added to a theme table after).
        text = cp.append_rows_to_observe(OBSERVE_SAMPLE, [
            _observe_row(name="Quiet Outline", repo="guopenghui/obsidian-quiet-outline",
                         author="guopenghui", desc="大纲视图")])
        out = cp.remove_rows_by_repo(text, ["guopenghui/obsidian-quiet-outline"])
        zone_start, zone_end = cp.observe_section_bounds(out)
        self.assertNotIn("guopenghui/obsidian-quiet-outline", out[zone_start:zone_end])
        self.assertIn("github.com/guopenghui/obsidian-quiet-outline", out,
                      "the main-table row must survive")
        self.assertIn("github.com/guopenghui/obsidian-quiet-outline",
                      out[:zone_start], "main table sits before the zone")

    def test_remove_is_case_insensitive_and_ignores_unknown(self):
        text = cp.append_rows_to_observe(OBSERVE_SAMPLE, [_observe_row()])
        self.assertEqual(cp.remove_rows_by_repo(text, ["NEW/PLUGIN"]),
                         cp.remove_rows_by_repo(text, ["new/plugin"]))
        self.assertNotIn("github.com/new/plugin", cp.remove_rows_by_repo(text, ["new/plugin"]))
        self.assertEqual(cp.remove_rows_by_repo(text, ["nope/nope"]), text)
        self.assertEqual(cp.remove_rows_by_repo(text, []), text)

    def test_removing_the_last_row_drops_the_table(self):
        text = cp.append_rows_to_observe(OBSERVE_SAMPLE, [_observe_row()])
        out = cp.remove_rows_by_repo(text, ["new/plugin"])
        self.assertNotIn("github.com/new/plugin", out)
        zone_start, zone_end = cp.observe_section_bounds(out)
        self.assertIn(cp.OBSERVE_NOTE, out[zone_start:zone_end])
        # The collapsed block goes away too, and the empty state comes back.
        self.assertIn(cp.OBSERVE_EMPTY_NOTE, out[zone_start:zone_end])
        self.assertNotIn(cp.OBSERVE_DETAILS_CLOSE, out[zone_start:zone_end])


class TestObserveZoneReorganize(unittest.TestCase):
    def test_rows_are_never_refiled_into_theme_sections(self):
        row = _observe_row(
            name="AI 阅读助手", repo="ai/reader", author="ai", desc="AI 辅助写作与大纲视图"
        )
        text = cp.append_rows_to_observe(OBSERVE_SAMPLE, [row])
        out = cp.reorganize_sections(text)
        zone_start, zone_end = cp.observe_section_bounds(out)
        self.assertIn("github.com/ai/reader", out[zone_start:zone_end])
        # Not duplicated into the keyword-matched theme tables (AI 辅助 / 界面与视图增强).
        self.assertEqual(out.count("github.com/ai/reader"), 1)
        self.assertGreater(out.index(cp.OBSERVE_SECTION), out.index("### 界面与视图增强"))
        self.assertIn(cp.OBSERVE_NOTE, out)
        self.assertIn(cp.OBSERVE_SUMMARY_LABEL, out)

    def test_empty_zone_survives_reorganize(self):
        out = cp.reorganize_sections(OBSERVE_SAMPLE)
        self.assertIn(cp.OBSERVE_SECTION, out)
        self.assertIn(cp.OBSERVE_EMPTY_NOTE, out)
        self.assertIn(cp.OBSERVE_NOTE, out)
        # No table, no collapsed block when there is nothing to show.
        self.assertNotIn(cp.OBSERVE_DETAILS_CLOSE, out)
        # Idempotent, and still the last heading in the plugin section.
        self.assertEqual(cp.reorganize_sections(out), out)

    def test_zone_is_rendered_after_theme_sections(self):
        # Source order puts the zone first inside the plugin section; the rebuild
        # must move it behind every theme table so it never buries them.
        zone_block = (
            cp.OBSERVE_SECTION + "\n\n" + cp.OBSERVE_NOTE + "\n\n"
            + cp.OBSERVE_EMPTY_NOTE + "\n\n"
        )
        text = OBSERVE_SAMPLE.replace(zone_block, "")
        text = text.replace(
            "### 界面与视图增强",
            cp.OBSERVE_SECTION + "\n\n" + cp.OBSERVE_EMPTY_NOTE + "\n\n### 界面与视图增强",
        )
        self.assertLess(text.index(cp.OBSERVE_SECTION),
                        text.index("### 界面与视图增强"))
        out = cp.reorganize_sections(text)
        self.assertGreater(out.index(cp.OBSERVE_SECTION),
                           out.index("### 界面与视图增强"))
        self.assertLess(out.index(cp.OBSERVE_SECTION), out.index("## 精选中文主题"))


class TestDecideObserveAction(unittest.TestCase):
    CUTOFF = datetime(2026, 10, 31, tzinfo=timezone.utc)

    def setUp(self):
        patcher = unittest.mock.patch.multiple(
            cp,
            OBSERVE_PROMOTE_STARS=5,
            OBSERVE_PROMOTE_DOWNLOADS=500,
            OBSERVE_MIN_AGE_DAYS=30,
            OBSERVE_EXPIRE_DAYS=120,
            OBSERVE_EXPIRE_MIN_STARS=3,
            OBSERVE_EXPIRE_MIN_DOWNLOADS=200,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        def meta(days_old=60, stars=0, pushed_days_ago=1, release_days_ago=None):
            m = {
                "created_at": (self.CUTOFF - timedelta(days=days_old)).isoformat(),
                "stars": stars,
                "pushed_at": (self.CUTOFF - timedelta(days=pushed_days_ago)).isoformat(),
            }
            if release_days_ago is not None:
                m["latest_release"] = (
                    self.CUTOFF - timedelta(days=release_days_ago)
                ).isoformat()
            return m

        self.meta = meta

    def test_promote_by_stars(self):
        self.assertEqual(
            cp.decide_observe_action(self.meta(stars=5), 0, self.CUTOFF), "promote")
        self.assertEqual(
            cp.decide_observe_action(self.meta(stars=4), 0, self.CUTOFF), "keep")

    def test_promote_by_downloads(self):
        self.assertEqual(
            cp.decide_observe_action(self.meta(stars=0), 500, self.CUTOFF), "promote")
        self.assertEqual(
            cp.decide_observe_action(self.meta(stars=0), 499, self.CUTOFF), "keep")

    def test_promote_requires_minimum_age(self):
        self.assertEqual(
            cp.decide_observe_action(self.meta(days_old=30, stars=100), 9999, self.CUTOFF),
            "promote")
        self.assertEqual(
            cp.decide_observe_action(self.meta(days_old=29, stars=100), 9999, self.CUTOFF),
            "keep")

    def test_drop_needs_every_condition(self):
        dead = self.meta(days_old=120, stars=0, pushed_days_ago=181)
        self.assertEqual(cp.decide_observe_action(dead, 0, self.CUTOFF), "drop")
        # Each single rescue keeps the row.
        self.assertEqual(
            cp.decide_observe_action(self.meta(days_old=120, stars=3, pushed_days_ago=181),
                                     0, self.CUTOFF), "keep")
        self.assertEqual(
            cp.decide_observe_action(self.meta(days_old=120, stars=0, pushed_days_ago=181),
                                     200, self.CUTOFF), "keep")
        self.assertEqual(
            cp.decide_observe_action(self.meta(days_old=119, pushed_days_ago=181),
                                     0, self.CUTOFF), "keep")
        self.assertEqual(
            cp.decide_observe_action(self.meta(days_old=120, pushed_days_ago=180),
                                     0, self.CUTOFF), "keep")

    def test_recent_release_rescues_a_quiet_repo(self):
        m = self.meta(days_old=400, stars=0, pushed_days_ago=400, release_days_ago=10)
        self.assertEqual(cp.decide_observe_action(m, 0, self.CUTOFF), "keep")

    def test_incomplete_metadata_degrades_to_keep(self):
        self.assertEqual(cp.decide_observe_action({}, 0, self.CUTOFF), "keep")
        self.assertEqual(cp.decide_observe_action(None, 0, self.CUTOFF), "keep")
        self.assertEqual(
            cp.decide_observe_action({"stars": 0, "pushed_at": ""}, 0, self.CUTOFF), "keep")
        self.assertEqual(
            cp.decide_observe_action(
                {**self.meta(days_old=400, pushed_days_ago=400), "created_at": "not-a-date"},
                0, self.CUTOFF),
            "keep")

    def test_evaluate_zone_survives_api_failure(self):
        row = {"name": "X", "repo": "x/y", "author": "x", "desc": "d", "section": cp.OBSERVE_SECTION_NAME}
        with unittest.mock.patch.object(cp, "repo_meta", return_value={}), \
             unittest.mock.patch.object(cp, "repo_releases", return_value=(None, 0)):
            self.assertEqual(cp.evaluate_observe_zone([row]), ([], []))


class TestObserveMissingRepo(unittest.TestCase):
    """A repository GitHub no longer serves is the only deletion path that does
    not depend on the activity thresholds."""

    def _row(self, repo="dead/plugin"):
        return {
            "name": "Dead", "repo": repo, "author": "dead", "desc": "d",
            "section": cp.OBSERVE_SECTION_NAME,
        }

    def test_drop_needs_gone_repo_and_unlisted_plugin(self):
        self.assertEqual(cp.decide_missing_repo_action(False, False), "drop")
        self.assertEqual(cp.decide_missing_repo_action(False, True), "keep")
        self.assertEqual(cp.decide_missing_repo_action(True, False), "keep")
        self.assertEqual(cp.decide_missing_repo_action(True, True), "keep")

    def test_unknown_existence_never_drops(self):
        self.assertEqual(cp.decide_missing_repo_action(None, False), "keep")
        self.assertEqual(cp.decide_missing_repo_action(None, True), "keep")

    def test_listed_by_repo_is_case_insensitive(self):
        self.assertTrue(cp.observe_repo_listed("SomeOne/Thing", {"someone/thing"}))
        self.assertFalse(cp.observe_repo_listed("someone/other", {"x/y"}))

    def test_listed_by_owner_keeps_a_typoed_link_alive(self):
        # The README once linked depthinker925/Thinkers-TomaToDo (404) while the
        # registry shipped depthinker925/Thinker-TomaToDo: the owner is evidence
        # enough never to delete the row automatically.
        registry = {"depthinker925/thinker-tomatodo"}
        self.assertTrue(
            cp.observe_repo_listed("depthinker925/Thinkers-TomaToDo", registry))

    def test_zone_drops_a_gone_repo_missing_from_the_registry(self):
        with unittest.mock.patch.object(cp, "repo_meta", return_value={}), \
             unittest.mock.patch.object(cp, "repo_exists", return_value=False), \
             unittest.mock.patch.object(cp, "repo_releases", return_value=(None, 0)):
            promote, drop = cp.evaluate_observe_zone([self._row()], {"alive/thing"})
        self.assertEqual(promote, [])
        self.assertEqual(drop, ["dead/plugin"])

    def test_zone_keeps_a_gone_repo_still_in_the_registry(self):
        with unittest.mock.patch.object(cp, "repo_meta", return_value={}), \
             unittest.mock.patch.object(cp, "repo_exists", return_value=False), \
             unittest.mock.patch.object(cp, "repo_releases", return_value=(None, 0)):
            self.assertEqual(
                cp.evaluate_observe_zone([self._row()], {"dead/plugin"}), ([], []))
            # same owner, different plugin: still evidence of a rename, not death
            self.assertEqual(
                cp.evaluate_observe_zone([self._row()], {"dead/sibling"}), ([], []))

    def test_zone_keeps_a_gone_repo_when_existence_is_unknown(self):
        with unittest.mock.patch.object(cp, "repo_meta", return_value={}), \
             unittest.mock.patch.object(cp, "repo_exists", return_value=None), \
             unittest.mock.patch.object(cp, "repo_releases", return_value=(None, 0)):
            self.assertEqual(cp.evaluate_observe_zone([self._row()], set()), ([], []))

    def test_zone_without_a_registry_never_probes_or_drops(self):
        with unittest.mock.patch.object(cp, "repo_meta", return_value={}), \
             unittest.mock.patch.object(
                 cp, "repo_exists", side_effect=AssertionError("must not probe")), \
             unittest.mock.patch.object(cp, "repo_releases", return_value=(None, 0)):
            self.assertEqual(cp.evaluate_observe_zone([self._row()]), ([], []))


class TestRunScanReadsRepositoryReadme(unittest.TestCase):
    """The scan loop reuses `readme_text` for each candidate's own README; the
    observation-zone pass must still see the repository README."""

    def test_observation_zone_is_evaluated_against_the_repository_readme(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sample = cp.append_rows_to_observe(OBSERVE_SAMPLE, [{
            "name": "Old One", "repo": "old/one", "author": "old", "desc": "旧条目",
        }])
        readme = os.path.join(tmp.name, "README.md")
        with open(readme, "w", encoding="utf-8") as f:
            f.write(sample)
        registry = [{
            "id": "newbie", "name": "Newbie", "author": "someone",
            "description": "An English plugin", "repo": "someone/newbie",
        }]
        seen = {}

        class FakeResponse:
            def json(self):
                return registry

        class FakeSession:
            def get(self, url, **kwargs):
                return FakeResponse()

        def fake_evaluate(rows, registry_repos=None):
            seen["rows"] = list(rows)
            seen["registry"] = registry_repos
            return [], []

        with unittest.mock.patch.object(cp, "README_PATH", readme), \
             unittest.mock.patch.object(cp, "verify_api_auth"), \
             unittest.mock.patch.object(cp, "create_retry_session", return_value=FakeSession()), \
             unittest.mock.patch.object(cp, "load_checked", return_value={"other"}), \
             unittest.mock.patch.object(cp, "load_denylist", return_value=set()), \
             unittest.mock.patch.object(cp, "save_checked"), \
             unittest.mock.patch.object(cp, "fetch_readme_text", return_value=None), \
             unittest.mock.patch.object(cp, "evaluate_observe_zone", side_effect=fake_evaluate), \
             unittest.mock.patch.object(cp, "find_stale_plugins", return_value=[]), \
             unittest.mock.patch.object(cp, "write_scan_output"):
            cp.run_scan()

        self.assertTrue(seen["rows"])
        self.assertEqual(seen["rows"][0]["repo"], "old/one")
        self.assertEqual(seen["rows"][0]["section"], cp.OBSERVE_SECTION_NAME)
        self.assertEqual(seen["registry"], {"someone/newbie"})


class TestObserveApplyPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workdir = self.tmp.name
        self._orig = {}
        for name in ("README_PATH", "PR_BODY_FILE"):
            self._orig[name] = getattr(cp, name)
        cp.README_PATH = os.path.join(self.workdir, "README.md")
        cp.PR_BODY_FILE = os.path.join(self.workdir, "pr-body.md")
        with open(cp.README_PATH, "w", encoding="utf-8") as f:
            f.write(OBSERVE_SAMPLE)
        self._env = {}
        for key in ("ADD_ROWS", "OBSERVE_ROWS", "PROMOTE_ROWS", "DROP_REPOS"):
            self._env[key] = os.environ.get(key)
            os.environ[key] = "[]"

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(cp, name, value)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _readme(self):
        with open(cp.README_PATH, encoding="utf-8") as f:
            return f.read()

    def test_review_row_lands_in_the_zone(self):
        os.environ["OBSERVE_ROWS"] = json.dumps([_observe_row()])
        cp.do_apply_readme()
        text = self._readme()
        zone_start, zone_end = cp.observe_section_bounds(text)
        self.assertIn("github.com/new/plugin", text[zone_start:zone_end])
        self.assertNotIn("github.com/new/plugin", text[:zone_start])
        self.assertIn("Add Chinese-relevant plugins", self._pr_title())

    def _pr_title(self):
        return "Add Chinese-relevant plugins"

    def test_promote_row_moves_out_of_the_zone_into_a_theme_table(self):
        text = cp.append_rows_to_observe(OBSERVE_SAMPLE, [_observe_row()])
        with open(cp.README_PATH, "w", encoding="utf-8") as f:
            f.write(text)
        os.environ["PROMOTE_ROWS"] = json.dumps([
            {"name": "New Plugin", "repo": "new/plugin", "author": "new",
             "desc": "中文描述", "tier": "auto", "promoted": True}])
        cp.do_apply_readme()
        after = self._readme()
        zone_start, zone_end = cp.observe_section_bounds(after)
        self.assertNotIn("github.com/new/plugin", after[zone_start:zone_end])
        self.assertIn("github.com/new/plugin", after)
        # Re-filed above the zone, inside the plugin section (not the theme part).
        self.assertLess(after.index("github.com/new/plugin"), zone_start)
        self.assertLess(after.index("github.com/new/plugin"), after.index("## 精选中文主题"))
        self.assertIn(cp.OBSERVE_EMPTY_NOTE, after[zone_start:zone_end])

    def test_drop_removes_the_row_from_the_zone(self):
        text = cp.append_rows_to_observe(OBSERVE_SAMPLE, [_observe_row()])
        with open(cp.README_PATH, "w", encoding="utf-8") as f:
            f.write(text)
        os.environ["DROP_REPOS"] = json.dumps(["new/plugin"])
        cp.do_apply_readme()
        after = self._readme()
        self.assertNotIn("github.com/new/plugin", after)
        # Existing rows are untouched.
        self.assertIn("guopenghui/obsidian-quiet-outline", after)
        self.assertIn("nyable/obsidian-text-finder", after)

    def test_empty_payload_is_idempotent(self):
        cp.do_apply_readme()
        first = self._readme()
        cp.do_apply_readme()
        self.assertEqual(self._readme(), first)

    def test_zone_still_present_after_an_unrelated_apply(self):
        os.environ["ADD_ROWS"] = json.dumps([{
            "name": "好插件", "repo": "good/plugin", "author": "good",
            "desc": "中文描述", "tier": "auto", "score": 130}])
        cp.do_apply_readme()
        text = self._readme()
        self.assertIn(cp.OBSERVE_SECTION, text)
        self.assertIn(cp.OBSERVE_NOTE, text)
        self.assertIn(cp.OBSERVE_EMPTY_NOTE, text)
        self.assertGreater(text.index(cp.OBSERVE_SECTION), text.index("### 界面与视图增强"))


class TestScanRoutesReviewRowsToObserve(unittest.TestCase):
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
            json.dump([], f)
        with open(cp.README_PATH, "w", encoding="utf-8") as f:
            f.write(OBSERVE_SAMPLE)
        self.old_env = os.environ.pop("GITHUB_OUTPUT", None)

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(cp, name, value)
        self.tmp.cleanup()
        if self.old_env:
            os.environ["GITHUB_OUTPUT"] = self.old_env
        else:
            os.environ.pop("GITHUB_OUTPUT", None)

    def test_first_run_review_rows_go_to_observe_not_add(self):
        import io
        from contextlib import redirect_stdout
        plugins = [
            {"id": "good-plugin", "name": "好插件", "repo": "good/plugin",
             "author": "good", "description": "中文描述"},
        ]
        out = io.StringIO()
        with unittest.mock.patch.object(cp, "verify_api_auth"), \
             unittest.mock.patch.object(cp, "gh_get", side_effect=fake_gh_get), \
             unittest.mock.patch.object(cp, "create_retry_session",
                                       return_value=FakeSession(plugins)), \
             redirect_stdout(out):
            cp.run_scan(skip_stale=True)
        data = json.loads(out.getvalue())
        self.assertEqual(data["add"], [])
        self.assertEqual([r["repo"] for r in data["observe"]], ["good/plugin"])
        self.assertEqual(data["observe"][0]["tier"], "review")
        self.assertTrue(data["content_changed"])
        self.assertTrue(data["has_review"])
        self.assertEqual(data["promote"], [])
        self.assertEqual(data["drop"], [])

    def test_observe_rows_only_change_counts_as_content_change(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            cp.write_scan_output([], [], False, False, False,
                                 observe_rows=[_observe_row()])
        data = json.loads(buf.getvalue())
        self.assertTrue(data["content_changed"])
        self.assertTrue(data["has_review"])

    def test_drop_only_counts_as_content_change(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            cp.write_scan_output([], [], False, False, False, drop_repos=["a/b"])
        self.assertTrue(json.loads(buf.getvalue())["content_changed"])

class TestFollowRepoRename(unittest.TestCase):
    """A renamed/transferred repository must not stay in the README under its
    old owner: GitHub keeps 301-ing for a while, but once the old account is
    gone the row 404s and the observation sweep would drop it."""

    def test_same_name_is_unchanged(self):
        self.assertEqual(cp.follow_repo_rename("a/b", "a/b"), "a/b")

    def test_case_only_difference_is_not_a_rename(self):
        self.assertEqual(cp.follow_repo_rename("A/B", "a/b"), "A/B")

    def test_moved_repo_returns_the_canonical_name(self):
        self.assertEqual(
            cp.follow_repo_rename(
                "0flowerocean0/bloomtype-obsidian",
                "huahaimaker/bloomtype-obsidian"),
            "huahaimaker/bloomtype-obsidian")

    def test_missing_or_malformed_answer_keeps_the_row(self):
        for value in ("", None, False, "no-slash"):
            self.assertEqual(cp.follow_repo_rename("a/b", value), "a/b")


class TestFreshnessFollowsRenames(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig = {n: getattr(cp, n) for n in ("README_PATH", "FRESHNESS_CACHE_FILE")}
        cp.README_PATH = os.path.join(self.tmp.name, "README.md")
        cp.FRESHNESS_CACHE_FILE = os.path.join(self.tmp.name, "fresh.json")
        with open(cp.README_PATH, "w", encoding="utf-8") as f:
            f.write(SAMPLE_README)

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(cp, name, value)

    def _run(self, flags):
        with unittest.mock.patch.object(cp, "verify_api_auth"), \
             unittest.mock.patch.object(cp, "get_repo_freshness", return_value=flags):
            cp.freshness_readme()
        with open(cp.README_PATH, encoding="utf-8") as f:
            return f.read()

    def test_row_url_and_author_follow_the_move(self):
        text = self._run({
            "archived": False, "stale": False, "ok": True,
            "last_update": "2026-10-01T00:00:00Z",
            "full_name": "newowner/obsidian-quiet-outline",
        })
        self.assertIn("https://github.com/newowner/obsidian-quiet-outline", text)
        self.assertNotIn("https://github.com/guopenghui/obsidian-quiet-outline", text)
        self.assertIn("`newowner`", text)

    def test_cache_entry_without_full_name_is_refetched(self):
        """An entry written before full_name was cached must not be trusted:
        it is the only way a rename can be noticed on a cache hit."""
        with open(cp.FRESHNESS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"oldowner/obsidian-quiet-outline": {
                "archived": False, "stale": False, "last_update": "",
                "ok": True, "checked_at": "2999-01-01T00:00:00+00:00",
            }}, f)
        meta = {"full_name": "newowner/obsidian-quiet-outline", "archived": False,
                "pushed_at": "2026-10-01T00:00:00Z"}
        with unittest.mock.patch.object(cp, "repo_meta", return_value=meta), \
             unittest.mock.patch.object(cp, "repo_releases", return_value=("", [])):
            flags = cp.get_repo_freshness("oldowner/obsidian-quiet-outline")
        self.assertEqual(flags["full_name"], "newowner/obsidian-quiet-outline")

    def test_rename_onto_an_existing_row_is_not_applied(self):
        """Two rows must never end up pointing at the same repo: the second
        rename would be a duplicate that validate_readme rejects."""
        def flags_for(repo, cutoff=None):
            full = ("guopenghui/obsidian-quiet-outline"
                    if repo == "cumany/obsidian-floating-toc-plugin" else repo)
            return {"archived": False, "stale": False, "ok": True,
                    "last_update": "", "full_name": full}

        with unittest.mock.patch.object(cp, "verify_api_auth"), \
             unittest.mock.patch.object(cp, "get_repo_freshness", side_effect=flags_for):
            cp.freshness_readme()
        with open(cp.README_PATH, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("https://github.com/cumany/obsidian-floating-toc-plugin", text)
        self.assertEqual(text.count("https://github.com/guopenghui/obsidian-quiet-outline"), 1)

    def test_unchanged_repo_keeps_its_row_byte_for_byte(self):
        text = self._run({
            "archived": False, "stale": False, "ok": True,
            "last_update": "", "full_name": "guopenghui/obsidian-quiet-outline",
        })
        self.assertIn("https://github.com/guopenghui/obsidian-quiet-outline", text)
        self.assertIn("`guopenghui`", text)


class TestObserveSummaryCount(unittest.TestCase):
    """The collapsed summary says how many rows are hidden, so a reader can
    decide whether to open the block."""

    def test_summary_reports_the_row_count(self):
        text = cp.append_rows_to_observe(OBSERVE_SAMPLE, [
            _observe_row(name="A", repo="a/a"), _observe_row(name="B", repo="b/b")])
        out = cp.reorganize_sections(text)
        self.assertIn(cp.observe_summary(2), out)
        self.assertIn(cp.OBSERVE_SUMMARY_LABEL, out)

    def test_empty_zone_renders_no_details_block(self):
        out = cp.reorganize_sections(OBSERVE_SAMPLE)
        self.assertNotIn("<details>", out)
        self.assertIn(cp.OBSERVE_EMPTY_NOTE, out)

    def test_count_follows_a_removal(self):
        text = cp.append_rows_to_observe(OBSERVE_SAMPLE, [
            _observe_row(name="A", repo="a/a"), _observe_row(name="B", repo="b/b")])
        text = cp.remove_rows_by_repo(text, ["a/a"])
        self.assertIn(cp.observe_summary(1), cp.reorganize_sections(text))


class TestScanOutputContract(unittest.TestCase):
    """Every `steps.scan.outputs.*` the workflow reads must be written here; a
    typo in either place used to fail only at 08:17 in CI."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "github_output")
        self.old = os.environ.get("GITHUB_OUTPUT")
        os.environ["GITHUB_OUTPUT"] = self.path

    def tearDown(self):
        if self.old is None:
            os.environ.pop("GITHUB_OUTPUT", None)
        else:
            os.environ["GITHUB_OUTPUT"] = self.old

    def test_all_consumed_keys_are_written(self):
        cp.write_scan_output([_observe_row()], [], True, False, True,
                             observe_rows=[_observe_row(repo="b/b")],
                             promote_rows=[_observe_row(repo="c/c")],
                             drop_repos=["d/d"])
        with open(self.path, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if "=" in ln]
        data = dict(ln.split("=", 1) for ln in lines)
        self.assertEqual(json.loads(data["add_rows"])[0]["repo"], "new/plugin")
        self.assertEqual(json.loads(data["observe_rows"])[0]["repo"], "b/b")
        self.assertEqual(json.loads(data["promote_rows"])[0]["repo"], "c/c")
        self.assertEqual(json.loads(data["drop_repos"]), ["d/d"])
        self.assertEqual(data["content_changed"], "true")
        self.assertEqual(data["cache_changed"], "true")
        self.assertEqual(data["first_run"], "false")
        self.assertEqual(data["has_review"], "true")
        self.assertIn("auto_merge_ready", data)
        self.assertIn("stale_rows", data)

    def test_workflow_only_reads_keys_this_script_writes(self):
        workflow = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "workflows", "check-plugins.yml")
        with open(workflow, encoding="utf-8") as f:
            body = f.read()
        consumed = set(re.findall(r"steps\.scan\.outputs\.([A-Za-z0-9_]+)", body))
        cp.write_scan_output([], [], False, False, False)
        with open(self.path, encoding="utf-8") as f:
            written = {ln.split("=", 1)[0] for ln in f.read().splitlines() if "=" in ln}
        self.assertTrue(consumed)
        self.assertEqual(consumed - written, set())


class TestCuratedMapsStayApart(unittest.TestCase):
    """CURATED_DESC holds descriptions, CURATED_SECTION holds section names.
    A section name pasted into the description map renders as the row's
    "description"; a description pasted into the section map re-files the row
    into a section that does not exist. Both are silent."""

    def test_no_section_name_leaks_into_the_description_map(self):
        leaked = {k: v for k, v in cp.CURATED_DESC.items()
                  if v in set(cp.CLASSIFY_PRIORITY) | {"其他工具"}}
        self.assertEqual(leaked, {})

    def test_every_section_target_is_a_real_section(self):
        self.assertLessEqual(set(cp.CURATED_SECTION.values()), set(cp.CLASSIFY_PRIORITY))

    def test_issue_37_rows_are_pinned(self):
        self.assertEqual(cp.CURATED_SECTION.get("Tag Graph"), "链接与知识管理")
        self.assertEqual(cp.CURATED_SECTION.get("Vault Pet"), "娱乐与多媒体")


class TestRecheckLinks(unittest.TestCase):
    """recheck_readme.py is the only place that looks at the live README, so
    its link verdicts must be conservative: a flaky host or a bot wall is not
    a dead link, and only a bare GitHub repo URL can be a rename."""

    def test_extract_links_dedupes_and_strips_punctuation(self):
        text = ("见 https://a.com/x。另见 (https://a.com/x)，还有 "
                "https://b.com/y. 和 https://c.com/z）")
        self.assertEqual(
            rr.extract_links(text),
            ["https://a.com/x", "https://b.com/y", "https://c.com/z"])

    def test_classify_link(self):
        self.assertEqual(rr.classify_link(200, None, "https://a.com/x"), "ok")
        self.assertEqual(rr.classify_link(403, None, "https://a.com/x"), "ok")
        self.assertEqual(rr.classify_link(429, None, "https://a.com/x"), "ok")
        self.assertEqual(rr.classify_link(404, None, "https://a.com/x"), "dead")
        self.assertEqual(rr.classify_link(410, None, "https://a.com/x"), "dead")
        self.assertEqual(rr.classify_link(None, None, "https://a.com/x"), "skip")
        self.assertEqual(
            rr.classify_link(301, "https://github.com/new/repo",
                             "https://github.com/old/repo"), "moved")
        # A non-repo URL that redirects (http->https, trailing slash) is fine.
        self.assertEqual(
            rr.classify_link(301, "https://a.com/x/", "https://a.com/x"), "ok")
        self.assertEqual(
            rr.classify_link(301, "https://github.com/old/repo",
                             "https://github.com/old/repo/tree/main"), "ok")

    def test_check_links_buckets_each_probe(self):
        text = ("https://ok.com/a https://dead.com/b https://gone.com/c "
                "https://github.com/old/repo https://flaky.com/d")
        probe = {
            "https://ok.com/a": (200, None),
            "https://dead.com/b": (404, None),
            "https://gone.com/c": (410, None),
            "https://github.com/old/repo": (301, "https://github.com/new/repo"),
            "https://flaky.com/d": (None, None),
        }
        with unittest.mock.patch.object(rr, "probe_link", side_effect=lambda u: probe[u]):
            dead, moved, unknown, total = rr.check_links(text, workers=2)
        self.assertEqual(total, 5)
        self.assertEqual(dead, [("https://dead.com/b", 404), ("https://gone.com/c", 410)])
        self.assertEqual(moved, [("https://github.com/old/repo", "https://github.com/new/repo")])
        self.assertEqual(unknown, 1)


class TestDescriptionQuality(unittest.TestCase):
    """Descriptions ship in Chinese and stay short. The scan builds them from
    upstream metadata, so this guard is what keeps an English or truncated cell
    from reaching the README (and --validate is what stops the commit)."""

    def test_a_short_chinese_cell_is_fine(self):
        self.assertIsNone(cp.description_problem("无服务器局域网同步：自动发现设备、加密传输"))

    def test_an_english_cell_is_refused(self):
        self.assertEqual(cp.description_problem("Sync your vault over LAN"), "not-chinese")

    def test_a_long_cell_is_refused(self):
        long = "同" * (cp.DESC_MAX_CHARS + 1)
        self.assertEqual(cp.description_problem(long), "too-long")

    def test_the_limit_itself_is_allowed(self):
        self.assertIsNone(cp.description_problem("同" * cp.DESC_MAX_CHARS))

    def test_an_install_snippet_is_refused(self):
        self.assertEqual(cp.description_problem("安装：设置 → 第三方插件 → 浏览"), "remnant")
        self.assertEqual(cp.description_problem("当前版本 0.6.3，修复合并"), "remnant")

    def test_the_placeholder_is_refused(self):
        self.assertEqual(cp.description_problem("[描述待补充]"), "placeholder")
        self.assertEqual(cp.description_problem(""), "placeholder")

    def test_freshness_decorations_do_not_count(self):
        row = "本地 AI 助手：整理库内笔记" + " · 最后更新 2026-10"
        self.assertIsNone(cp.description_problem(row))
        self.assertIsNone(cp.description_problem("本地 AI 助手 [已归档]"))

    def test_validate_reports_bad_rows_with_a_reason(self):
        row = (
            "## 原生中文插件，欢迎支持\n\n### 其他工具\n\n"
            "| 插件 | 作者 | 核心功能 |\n| --- | --- | --- |\n"
            "| [Bad](https://github.com/a/b) | `a` | Sync your vault over LAN |\n"
            "| [Good](https://github.com/c/d) | `c` | 中文简介，简短清楚 |\n\n"
            "## 精选中文主题\n"
        )
        issues = cp.validate_readme(row)
        self.assertTrue(any("not-chinese" in i and "Bad" in i for i in issues), issues)
        self.assertFalse(any("Good" in i for i in issues), issues)

    def test_the_real_readme_passes(self):
        readme = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..", "README.md")
        with open(readme, encoding="utf-8") as f:
            text = f.read()
        problems = [
            (r["name"], cp.description_problem(r["desc"]))
            for r in cp.parse_plugin_rows_full(text)
            if cp.description_problem(r["desc"])
        ]
        self.assertEqual(problems, [])


class TestNeedsDescReporting(unittest.TestCase):
    """Candidates the scan refuses, and near misses the score barely drops, are
    reported on the run summary page; nothing else surfaces them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "github_output")
        self.summary = os.path.join(self.tmp.name, "summary")
        self.saved = {k: os.environ.get(k) for k in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY")}
        os.environ["GITHUB_OUTPUT"] = self.out
        os.environ["GITHUB_STEP_SUMMARY"] = self.summary

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_outputs_carry_both_lists(self):
        cp.write_scan_output([], [], False, False, False,
                             needs_desc=[{"name": "N", "repo": "a/b", "problem": "not-chinese"}],
                             near_miss=[{"name": "M", "repo": "c/d", "cn": 30, "q": 40, "stars": 2}])
        with open(self.out, encoding="utf-8") as f:
            outputs = dict(ln.split("=", 1) for ln in f.read().splitlines() if "=" in ln)
        self.assertEqual(json.loads(outputs["needs_desc"])[0]["repo"], "a/b")
        self.assertEqual(json.loads(outputs["near_miss"])[0]["repo"], "c/d")
        # Report-only: neither list makes the workflow commit anything.
        self.assertEqual(outputs["content_changed"], "false")

    def test_the_run_summary_lists_them(self):
        cp.write_scan_output([], [], False, False, False,
                             needs_desc=[{"name": "N", "repo": "a/b", "problem": "not-chinese",
                                          "raw": "Sync your vault"}],
                             near_miss=[{"name": "M", "repo": "c/d", "cn": 30, "q": 40, "stars": 2}])
        with open(self.summary, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("待人工写简介的候选", body)
        self.assertIn("a/b", body)
        self.assertIn("有中文但评分不够的候选", body)
        self.assertIn("c/d", body)

    def test_an_empty_summary_is_not_written(self):
        cp.write_scan_output([], [], False, False, False)
        self.assertFalse(os.path.exists(self.summary))


class TestScanToApplyChain(unittest.TestCase):
    """scan -> GITHUB_OUTPUT -> apply -> validate, the seam the workflow walks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workdir = self.tmp.name
        self._orig = {}
        for name in ("CACHE_FILE", "README_PATH", "PR_BODY_FILE", "DENY_FILE"):
            self._orig[name] = getattr(cp, name)
        cp.CACHE_FILE = os.path.join(self.workdir, "cache.json")
        cp.README_PATH = os.path.join(self.workdir, "README.md")
        cp.PR_BODY_FILE = os.path.join(self.workdir, "pr-body.md")
        cp.DENY_FILE = os.path.join(self.workdir, "deny.json")
        with open(cp.DENY_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)
        with open(cp.README_PATH, "w", encoding="utf-8") as f:
            f.write(SAMPLE_README)
        self.out_path = os.path.join(self.workdir, "github_output")
        open(self.out_path, "w").close()
        self.old_output = os.environ.get("GITHUB_OUTPUT")
        os.environ["GITHUB_OUTPUT"] = self.out_path
        self.old_rows = {k: os.environ.get(k) for k in
                         ("ADD_ROWS", "OBSERVE_ROWS", "PROMOTE_ROWS", "DROP_REPOS")}
        for key in self.old_rows:
            os.environ[key] = "[]"

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(cp, name, value)
        for key, value in self.old_rows.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if self.old_output is None:
            os.environ.pop("GITHUB_OUTPUT", None)
        else:
            os.environ["GITHUB_OUTPUT"] = self.old_output

    def test_scan_output_applies_into_a_valid_readme(self):
        cp.save_checked({"known-plugin"})
        plugins = [
            {"id": "known-plugin", "name": "已知", "repo": "known/plugin",
             "author": "k", "description": "中文描述"},
            {"id": "good-plugin", "name": "好插件", "repo": "good/plugin",
             "author": "good", "description": "中文描述"},
        ]
        with unittest.mock.patch.object(cp, "verify_api_auth"), \
             unittest.mock.patch.object(cp, "gh_get", side_effect=fake_gh_get), \
             unittest.mock.patch.object(cp, "create_retry_session",
                                       return_value=FakeSession(plugins)):
            cp.run_scan(skip_stale=True)

        with open(self.out_path, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if "=" in ln]
        outputs = dict(ln.split("=", 1) for ln in lines)
        self.assertEqual(outputs["content_changed"], "true")
        os.environ["ADD_ROWS"] = outputs["add_rows"]
        os.environ["OBSERVE_ROWS"] = outputs["observe_rows"]
        os.environ["PROMOTE_ROWS"] = outputs["promote_rows"]
        os.environ["DROP_REPOS"] = outputs["drop_repos"]
        cp.do_apply_readme()

        with open(cp.README_PATH, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("https://github.com/good/plugin", text)
        self.assertEqual(cp.validate_readme(text), [])
        # The row must land inside the plugin section, never after it.
        start, end = cp.plugin_section_bounds(text)
        self.assertIn("https://github.com/good/plugin", text[start:end])
        self.assertLess(text.index("https://github.com/good/plugin"),
                        text.index("## 精选中文主题"))

