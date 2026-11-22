#!/usr/bin/env python3
"""Land selected working-tree changes on master without taking anyone else's uncommitted work with them.

Several sessions edit this working tree at once, so a plain `git commit -a` would sweep another session's half-finished change into
yours. This tool builds the commit from HEAD plus only what you name, tests it in a clean checkout, and only then moves master and pushes.

    python tools/land_commit.py "message" --files a.py b.py --hunks web_api/server.py=alm,ALM docs/x.md=keyword \
        --verify ".venv/bin/python -m pytest -q -n 4 tests/test_x.py"

  --files   whole files (new files included). Use only for files nobody else is editing right now.
  --hunks   path=kw1,kw2: for a shared file, take only the diff hunks (3 lines of context) whose changed lines mention a keyword.
  --verify  a shell command run in a clean checkout of the candidate commit (cwd = that checkout; `.venv` and `node_modules` are linked in).
Changed .py files are always compiled first. One landing at a time (a lock), and if master moved meanwhile the candidate is rebuilt on top.
Never run `git stash`, `git checkout -- <file>` or `git reset --hard` in the shared tree: that destroys other sessions' work.
"""
import argparse, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = Path("/tmp/inazina-land.lock")
TRAILER = ("Co-Authored-By: Claude <noreply@anthropic.com>\nCo-authored-by: Codex <noreply@openai.com>\n"
           "Co-authored-by: winterliy <154217661+winterliy@users.noreply.github.com>\nCo-authored-by: jaehyun <ano701.main@gmail.com>")


def git(*args, env=None, input=None, check=True):
    result = subprocess.run(["git", *args], cwd=ROOT, env=env, input=input, capture_output=True, text=True)
    if check and result.returncode:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr.strip()[:400]}")
    return result.stdout


def take_lock(wait_seconds=1800):
    deadline = time.time() + wait_seconds
    while True:
        try:
            LOCK.mkdir()
            return
        except FileExistsError:
            if time.time() - LOCK.stat().st_mtime > 3600:          # a landing that died without cleaning up
                shutil.rmtree(LOCK, ignore_errors=True)
                continue
            if time.time() > deadline:
                raise SystemExit("another landing holds the lock for too long")
            time.sleep(3)


def build_candidate(message, files, hunks):
    old = git("rev-parse", "HEAD").strip()
    index = tempfile.mktemp(prefix="land-idx.")
    env = {**os.environ, "GIT_INDEX_FILE": index}
    try:
        git("read-tree", "HEAD", env=env)
        if files:
            git("add", "--", *files, env=env)
        for path, keywords in hunks.items():
            diff = git("diff", "-U3", "--", path)
            if not diff:
                raise SystemExit(f"{path}: no uncommitted changes to take")
            header, *parts = re.split(r"(?m)^(?=@@ )", diff)
            keep = [p for p in parts if any(k in "\n".join(l for l in p.splitlines()[1:] if l[:1] in "+-") for k in keywords)]
            print(f"{path}: taking {len(keep)} of {len(parts)} hunks")
            if not keep:
                raise SystemExit(f"{path}: no hunk mentions {keywords}")
            applied = subprocess.run(["git", "apply", "--cached", "--recount", "-"], cwd=ROOT, env=env, input=header + "".join(keep), text=True, capture_output=True)
            if applied.returncode:
                raise SystemExit(f"{path}: could not apply the selected hunks: {applied.stderr.strip()[:300]}")
        tree = git("write-tree", env=env).strip()
    finally:
        if os.path.exists(index):
            os.remove(index)
    if tree == git("rev-parse", "HEAD^{tree}").strip():
        return old, None, tree
    commit = git("commit-tree", tree, "-p", old, "-m", f"{message}\n\n{TRAILER}").strip()
    return old, commit, tree


def verify(commit, command):
    folder = Path(tempfile.mkdtemp(prefix="land-check-"))
    try:
        archive = subprocess.Popen(["git", "archive", commit], cwd=ROOT, stdout=subprocess.PIPE)
        subprocess.run(["tar", "-x", "-C", str(folder)], stdin=archive.stdout, check=True)
        archive.wait()
        for name in (".venv", "node_modules"):
            if (ROOT / name).exists():
                (folder / name).symlink_to(ROOT / name)
        for app in (ROOT / "apps").glob("*/node_modules"):
            (folder / "apps" / app.parent.name / "node_modules").symlink_to(app)
        # A deleted .py file is part of the diff but not of the checkout: only files that exist can be compiled.
        changed = [p for p in git("diff", "--name-only", f"{commit}^", commit).split() if p.endswith(".py") and (folder / p).exists()]
        if changed:
            compiled = subprocess.run([str(ROOT / ".venv/bin/python"), "-m", "py_compile", *changed], cwd=folder, capture_output=True, text=True)
            if compiled.returncode:
                raise SystemExit(f"candidate does not compile:\n{compiled.stderr[-600:]}")
            print(f"compiled {len(changed)} changed .py files")
        if command:
            run = subprocess.run(command, shell=True, cwd=folder, capture_output=True, text=True)
            tail = "\n".join((run.stdout + run.stderr).strip().splitlines()[-12:])
            print(tail)
            if run.returncode:
                raise SystemExit("verification failed; nothing was committed")
    finally:
        for link in [folder / ".venv", folder / "node_modules", *folder.glob("apps/*/node_modules")]:
            if link.is_symlink():
                link.unlink()
        shutil.rmtree(folder, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("message")
    parser.add_argument("--files", nargs="*", default=[])
    parser.add_argument("--hunks", nargs="*", default=[], metavar="PATH=KW,KW")
    parser.add_argument("--verify", default="")
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args()
    hunks = {item.partition("=")[0]: item.partition("=")[2].split(",") for item in args.hunks}
    if not args.files and not hunks:
        raise SystemExit("name something to land (--files and/or --hunks)")
    take_lock()
    try:
        for attempt in range(3):
            old, commit, _ = build_candidate(args.message, args.files, hunks)
            if commit is None:
                print("nothing to commit"); return
            verify(commit, args.verify)
            if git("rev-parse", "HEAD").strip() != old:
                print("master moved while verifying; rebuilding on top"); continue
            git("update-ref", "refs/heads/master", commit, old)
            git("reset", "-q", "--", *args.files, *hunks, check=False)     # the index follows HEAD for the paths just landed
            print(git("show", "--stat", "--format=%h %s", commit))
            if not args.no_push:
                git("push", "-q", "origin", "master")
                print("pushed")
            return
        raise SystemExit("master kept moving; try again")
    finally:
        shutil.rmtree(LOCK, ignore_errors=True)


if __name__ == "__main__":
    main()
