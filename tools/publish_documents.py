"""Publish the public documents the console changed.

The official site (GitHub Pages) shows apps/site/public/links/*: the terms, the privacy policy, their announced revisions (*.next.txt) and
pending.json. The console edits those files on this machine only, so after a save, a cancellation or the effective date swapping a text in,
they must be committed and pushed for the public site to follow. This commits just those files (never other work in the tree) through
tools/land_commit.py, which tests the commit in a clean checkout and pushes; the push redeploys the site.

    python tools/publish_documents.py        (documents.publish_async() starts it when INAZINA_PUBLISH_DOCUMENTS=1)
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINKS = "apps/site/public/links"


def changed_paths(root=ROOT):
    """Document files that differ from the last commit: modified, deleted, or new (including announced revisions)."""
    result = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all", "--", LINKS], cwd=root, check=True, capture_output=True, text=True)
    return sorted(line[3:].strip().strip('"') for line in result.stdout.splitlines() if line.strip())


def publish(root=ROOT, runner=subprocess.run):
    paths = changed_paths(root)
    if not paths:
        return 0
    command = [sys.executable, str(Path(root) / "tools" / "land_commit.py"), "docs: publish the terms and policies changed in the console",
               "--files", *paths, "--verify", ".venv/bin/python -m pytest tests/test_documents.py -q"]
    return runner(command, cwd=root).returncode


if __name__ == "__main__":
    sys.exit(publish())
