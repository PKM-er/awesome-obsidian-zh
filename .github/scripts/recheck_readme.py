#!/usr/bin/env python3
"""Read-only periodic recheck for PKM-er/awesome-obsidian-zh README.

- Fetches the live README.md from main (no local repo checkout needed).
- Runs validate_readme(): detects duplicate plugin repos and broken/placeholder
  descriptions (pure, no network).
- Computes what `cleanup_readme()` WOULD change on a throwaway temp copy, so we
  see drift (misclassification / duplicates / TOC / sort) WITHOUT touching the
  real repo or pushing anything.
- Prints a report and exits. Safe to run on a schedule.
"""
import os
import sys
import io
import tempfile
import difflib
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check_plugins as cp  # noqa: E402

REPO = "PKM-er/awesome-obsidian-zh"
RAW = f"https://raw.githubusercontent.com/{REPO}/main/README.md"


def fetch_readme():
    req = urllib.request.Request(RAW, headers={"User-Agent": "recheck"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def main():
    text = fetch_readme()
    print(f"Fetched live README ({len(text)} chars) from {REPO}@main\n")

    # 1) validate (read-only)
    print("=== validate_readme ===")
    issues = cp.validate_readme(text)
    if issues:
        for i in issues:
            print(" - " + i)
        print(f"  -> {len(issues)} issue(s) found.")
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

    print("\n(recheck is read-only: nothing was modified on the repo or pushed.)")


if __name__ == "__main__":
    main()
