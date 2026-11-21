import re
from pathlib import Path

TEXT = (Path(__file__).resolve().parents[1] / "infra" / "Caddyfile").read_text()
LIVE = "\n".join(l for l in TEXT.splitlines() if not l.lstrip().startswith("#"))


def blocks():
    """Map site label -> body for top-level live site blocks."""
    out = {}
    for m in re.finditer(r"^(\S.*?) \{\n(.*?)^\}", LIVE, re.S | re.M):
        out[m.group(1).strip()] = m.group(2)
    return out


def block(label):
    return blocks()[label]


def test_no_public_api_or_internal_gateway():
    assert "4313" not in LIVE
    assert not re.search(r"^api\.", LIVE, re.M)
    assert "{$INAZINA_DOMAIN:inazina.localhost}" in LIVE
    assert "{$INAZINA_TLS" in LIVE


def test_host_map():
    d = "{$INAZINA_DOMAIN:inazina.localhost}"
    expect = {
        f"id.{d}": "4312",
        f"naru.{d}": "4311",
        f"console.{d}": "4310",
        f"www.{d}": "4314",
        f"corp.{d}": "4314",
        f"wl.kr.{d}": "4315",
    }
    for label, port in expect.items():
        assert f"127.0.0.1:{port}" in block(label), label
    assert "redir" in block(d) and f"www.{d}" in block(d)


def test_id_host_exposes_only_oidc_paths():
    b = block("id.{$INAZINA_DOMAIN:inazina.localhost}")
    assert "@idpaths path /oidc/* /login /register /reset" in b
    assert b.rstrip().endswith("respond 404")


def test_api_internal_blocked():
    d = "{$INAZINA_DOMAIN:inazina.localhost}"
    for h in ("naru", "console"):
        b = block(f"{h}.{d}")
        assert "path /api/internal*" in b
        assert "respond @internal 404" in b or "respond @internal 403" in b
        assert "reverse_proxy @api 127.0.0.1:4312" in b
