"""M8 secret management: no credentials committed to the repository, .env stays ignored, and responses/logs do not echo secrets."""
import ast
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERNS = {
    "aws_key": r"AKIA[0-9A-Z]{16}",
    "private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY",
    "github_token": r"gh[pousr]_[A-Za-z0-9]{30,}",
    "slack_token": r"xox[abprs]-[0-9A-Za-z-]{20,}",
    "api_key": r"sk-[A-Za-z0-9]{32,}",
    "discord_bot_token": r"[MN][A-Za-z0-9_-]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}",
    "dsn_with_password": r"postgres(?:ql)?://[^:/@\s]+:[^@\s$]{3,}@",
}
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".json", ".md", ".sh", ".yml", ".yaml", ".toml", ".plist", ".html", ".example", ".txt", ".sql"}
SECRET_NAME = re.compile(r"(password|passwd|secret|token|api_?key)", re.I)
THIS_FILE = Path(__file__).resolve()


def listed_files():
    """Tracked files from git; in a checkout without .git (the landing tool's clean copy) every file outside the ignored folders."""
    git = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True)
    if git.returncode == 0:
        return git.stdout.split("\0")
    skip = {".venv", "node_modules", ".git", "dist", "__pycache__", ".pytest_cache", ".vite", "coverage"}
    return [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and not skip & set(p.relative_to(ROOT).parts) and p.name != ".env" and not p.name.startswith(".env.") or p.name == ".env.example"]


def tracked_files():
    names = listed_files()
    return [ROOT / n for n in names if n and (ROOT / n).suffix in TEXT_SUFFIXES and (ROOT / n).is_file() and (ROOT / n).resolve() != THIS_FILE]


def test_no_credentials_in_tracked_files():
    found = []
    for path in tracked_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for label, pattern in SECRET_PATTERNS.items():
            if re.search(pattern, text):
                found.append(f"{path.relative_to(ROOT)}: {label}")
    assert not found, found


def test_env_files_are_ignored_and_never_committed():
    rules = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
    assert {".env", ".env.*", "!.env.example"} <= set(rules)
    tracked = listed_files()
    assert [n for n in tracked if re.search(r"(^|/)\.env(\..+)?$", n) and not n.endswith(".example")] == []
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.startswith("#") and SECRET_NAME.search(key):
            assert value.strip() in {"", '""', "changeme", "<set-me>"} or value.strip().startswith(("<", "$")), f".env.example carries a value for {key}"


def test_python_sources_have_no_hardcoded_secret_literals():
    """A secret-named variable or keyword must not be assigned a non-trivial string literal in non-test Python."""
    found = []
    for path in tracked_files():
        if path.suffix != ".py" or "tests" in path.relative_to(ROOT).parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            names, value = [], None
            if isinstance(node, ast.Assign):
                names, value = [t.id for t in node.targets if isinstance(t, ast.Name)], node.value
            elif isinstance(node, ast.keyword) and node.arg:
                names, value = [node.arg], node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str) and len(value.value) >= 12 \
                    and any(SECRET_NAME.search(n) for n in names) and re.fullmatch(r"[\x21-\x7e]+", value.value) and not re.fullmatch(r"[A-Za-z_./:-]+", value.value):
                found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not found, found


def test_server_reads_its_secrets_from_the_environment():
    text = (ROOT / "web_api" / "server.py").read_text(encoding="utf-8")
    assert 'INTERNAL_TOKEN = os.environ.get("INAZINA_INTERNAL_TOKEN"' in text
    assert "hmac.compare_digest" in text


def _outside_calls(node):
    """Walk an argument expression without descending into nested calls (a password handed to a function is not echoed)."""
    yield node
    if isinstance(node, ast.Call):
        return
    for child in ast.iter_child_nodes(node):
        yield from _outside_calls(child)


def test_responses_and_logs_never_interpolate_secret_values():
    """No send_json/logging/print argument may name a password, token or secret variable or payload key."""
    tree = ast.parse((ROOT / "web_api" / "server.py").read_text(encoding="utf-8"))
    leaks = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", getattr(node.func, "id", ""))
        if name not in {"send_json", "info", "warning", "error", "exception", "debug", "print"}:
            continue
        for arg in [*node.args, *[k.value for k in node.keywords]]:
            for inner in _outside_calls(arg):
                if isinstance(inner, ast.Name) and inner.id in {"INTERNAL_TOKEN", "password", "token"}:
                    leaks.append(f"{name}:{node.lineno}:{inner.id}")
                if isinstance(inner, ast.Subscript) and isinstance(inner.slice, ast.Constant) and inner.slice.value == "password":
                    leaks.append(f"{name}:{node.lineno}:['password']")
                if isinstance(inner, ast.Call) and getattr(inner.func, "attr", "") == "get" and inner.args \
                        and isinstance(inner.args[0], ast.Constant) and inner.args[0].value == "password":
                    leaks.append(f"{name}:{node.lineno}:get('password')")
    assert not leaks, leaks
