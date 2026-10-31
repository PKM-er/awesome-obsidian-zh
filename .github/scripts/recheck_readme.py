#!/usr/bin/env python3
"""Read-only periodic recheck for PKM-er/awesome-obsidian-zh README.

- Fetches the live README.md from main (no local repo checkout needed).
- Runs validate_readme(): detects duplicate plugin repos and broken/placeholder
  descriptions (pure, no network).
- Computes what `cleanup_readme()` WOULD change on a throwaway temp copy, so we
  see drift (misclassification / duplicates / TOC / sort) WITHOUT touching the
  real repo or pushing anything.
- Probes every external link: 404/410 fails the run, a GitHub repository that
  moved is reported so the row can follow the rename.
- Prints a report and exits. Safe to run on a schedule.
"""
import concurrent.futures
import difflib
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check_plugins as cp  # noqa: E402

REPO = "PKM-er/awesome-obsidian-zh"
RAW = f"https://raw.githubusercontent.com/{REPO}/main/README.md"

UA = "awesome-obsidian-zh-recheck"
LINK_TIMEOUT = 15
LINK_WORKERS = 8
# CJK punctuation also terminates a URL: prose can put a full stop right
# after the link, and those characters are not part of any URL we ship.
LINK_RE = re.compile(r"https?://[^\s)\]<>\"'`。，、；：！？（）【】「」『』]+")
# Only a bare repository URL can be rewritten by the row pipeline; other 3xx
# (http->https, trailing slash) are normal and must not be reported as moves.
GITHUB_REPO_RE = re.compile(r"^https://github\.com/([^/]+)/([^/?#]+)/?$")


def extract_links(text):
    """Unique http(s) URLs in document order (trailing punctuation stripped)."""
    seen, out = set(), []
    for m in LINK_RE.finditer(text):
        url = m.group(0).rstrip(".,;:!?。，、；：！？）】」』")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def classify_link(status, location, url):
    """Map one probe to 'ok' / 'dead' / 'moved' / 'skip'.

    Only 404/410 mean the link is gone: 403/429 mean the site refuses
    automated probes (github.com rate limits, bot walls), which says nothing
    about whether a reader can open it. An unreachable host (status None) is
    'skip' for the same reason -- a flaky network must not fail the run."""
    if status is None:
        return "skip"
    if status in (404, 410):
        return "dead"
    if location and GITHUB_REPO_RE.match(url):
        return "moved"
    return "ok"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def probe_link(url, timeout=LINK_TIMEOUT):
    """Return (status, location); status None when the host never answered."""
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                return resp.status, None
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                return e.code, e.headers.get("Location")
            if method == "HEAD" and e.code in (403, 405, 501):
                continue          # HEAD refused: ask again with GET
            return e.code, None
        except Exception:
            if method == "GET":
                return None, None
    return None, None


def check_links(text, workers=LINK_WORKERS):
    """Probe every external link. Returns (dead, moved, unknown, total)."""
    urls = extract_links(text)
    dead, moved, unknown = [], [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for url, (status, location) in zip(urls, ex.map(probe_link, urls)):
            verdict = classify_link(status, location, url)
            if verdict == "dead":
                dead.append((url, status))
            elif verdict == "moved":
                moved.append((url, location))
            elif verdict == "skip":
                unknown += 1
    return dead, moved, unknown, len(urls)


def fetch_readme():
    req = urllib.request.Request(RAW, headers={"User-Agent": "recheck"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def main():
    text = fetch_readme()
    print(f"Fetched live README ({len(text)} chars) from {REPO}@main\n")
    problems = []

    # 1) validate (read-only)
    print("=== validate_readme ===")
    issues = cp.validate_readme(text)
    if issues:
        for i in issues:
            print(" - " + i)
        print(f"  -> {len(issues)} issue(s) found.")
        problems.append(f"{len(issues)} validation issue(s)")
    else:
        print(" OK: no duplicate plugin repos, no broken descriptions.")

    # 2) compute cleanup proposal on a temp copy (read-only w.r.t. the repo)
    print("\n=== cleanup_readme proposal (what would change) ===")
    tmp = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
    tmp.write(text)
    tmp.close()
    saved = cp.README_PATH
    cp.README_PATH = tmp.name
    try:
        cp.cleanup_readme()
        with open(tmp.name, encoding="utf-8") as f:
            cleaned = f.read()
    except Exception as e:
        print(f" cleanup could not run (error: {e}); skipping diff.")
        cleaned = text
    finally:
        cp.README_PATH = saved
        os.unlink(tmp.name)

    if cleaned == text:
        print(" OK: cleanup would make no changes (README is already tidy).")
    else:
        diff = list(difflib.unified_diff(
            text.splitlines(), cleaned.splitlines(),
            fromfile="current", tofile="after-cleanup", lineterm=""))
        changed = [d for d in diff if d[:1] in "+-" and d[:3] not in ("+++", "---")]
        print(f" cleanup would change {len(changed)} line(s). First 40 diff lines:")
        for line in diff[:40]:
            print("   " + line)
        problems.append(f"cleanup drift: {len(changed)} line(s) would change")

    # 3) external links: dead ones fail the run, renamed repos are reported
    if "--no-links" in sys.argv:
        print("\n=== external links: skipped (--no-links) ===")
    else:
        print("\n=== external links ===")
        dead, moved, unknown, total = check_links(text)
        print(f" checked {total} unique link(s): {total - len(dead) - len(moved) - unknown} ok, "
              f"{len(dead)} dead, {len(moved)} moved, {unknown} unreachable")
        for url, status in dead:
            print(f"   DEAD  {url} (HTTP {status})")
        for url, location in moved:
            print(f"   MOVED {url} -> {location}")
        if dead:
            problems.append(f"{len(dead)} dead link(s)")
        if moved:
            print("   (a moved GitHub repo is fixed by the freshness pass; "
                  "check that the next scan followed it)")

    print("\n(recheck is read-only: nothing was modified on the repo or pushed.)")

    # A scheduled run must be able to fail loudly: exit non-zero when the live
    # README has validation issues, cleanup drift or dead links, so the red run
    # on the Actions tab is the alarm instead of a log nobody reads.
    if problems:
        print("recheck FAILED: " + "; ".join(problems))
        sys.exit(1)
    print("recheck OK.")


if __name__ == "__main__":
    main()
