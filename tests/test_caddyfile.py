import re
from pathlib import Path

TEXT = (Path(__file__).resolve().parents[1] / "infra" / "Caddyfile").read_text()
LIVE = "\n".join(line for line in TEXT.splitlines() if not line.lstrip().startswith("#"))
D = "{$INAZINA_DOMAIN:inazina.localhost}"


def _body(text, start):
    """Text between the brace that opens at `start` and its matching close."""
    depth, i = 0, start
    while i < len(text):
        depth += text[i] == "{"
        depth -= text[i] == "}"
        if depth == 0:
            return text[start + 1:i]
        i += 1
    raise AssertionError("unbalanced braces")


def blocks(text):
    """Top-level site label -> body (placeholders like {$X} are skipped by looking for ' {' at line end)."""
    out = {}
    for m in re.finditer(r"^(\S[^\n]*?) \{$", text, re.M):
        if text.count("\n", 0, m.start()) and text[:m.start()].count("{") != text[:m.start()].count("}"):
            continue
        out[m.group(1).strip()] = _body(text, m.end() - 1)
    return out


def handlers(body):
    """Matcher (or '' for the catch-all) -> inner text of each `handle` block directly inside a site body."""
    out = {}
    for m in re.finditer(r"^\thandle( @\w+)? \{$", body, re.M):
        out[(m.group(1) or "").strip()] = _body(body, m.end() - 1)
    return out


SITES = blocks(LIVE)


def test_no_public_api_or_internal_gateway():
    assert "4313" not in LIVE
    assert not re.search(r"^api\.", LIVE, re.M)
    assert "{$INAZINA_TLS" in LIVE


def test_sites_present():
    assert set(SITES) == {
        "(common)", D, f"www.{D}", f"corp.{D}", f"id.{D}", f"naru.{D}", f"console.{D}", "{$WL_DOMAIN:wl.kr.localhost}",
    }


def test_simple_hosts_proxy_to_their_port():
    for label, port in {f"www.{D}": "4314", f"corp.{D}": "4314", "{$WL_DOMAIN:wl.kr.localhost}": "4315"}.items():
        assert re.search(rf"^\treverse_proxy 127\.0\.0\.1:{port}$", SITES[label], re.M), label


def test_apex_redirects_permanently_to_www():
    assert f"redir https://www.{D}{{uri}} permanent" in SITES[D]
    assert "reverse_proxy" not in SITES[D]


def test_wl_is_its_own_domain_not_under_inazina():
    label = "{$WL_DOMAIN:wl.kr.localhost}"
    assert label in SITES and "INAZINA_DOMAIN" not in label


def test_id_host_exposes_only_oidc_paths():
    body = SITES[f"id.{D}"]
    assert "@idpaths path /oidc/* /login /register /reset" in body
    found = handlers(body)
    assert "reverse_proxy 127.0.0.1:4312" in found["@idpaths"]
    assert found[""].strip() == "respond 404"
    assert "reverse_proxy" not in body.replace(found["@idpaths"], "")


def test_app_hosts_split_api_from_frontend_and_block_internal():
    for host, port in (("naru", "4311"), ("console", "4310")):
        body = SITES[f"{host}.{D}"]
        assert "@internal path /api/internal*" in body and "@api path /api/*" in body
        found = handlers(body)
        assert found["@internal"].strip() == "respond 404"
        assert found["@api"].strip() == "reverse_proxy 127.0.0.1:4312"
        assert found[""].strip() == f"reverse_proxy 127.0.0.1:{port}"
        # The internal block must come before the generic /api/* handler (first handle wins).
        assert body.index("handle @internal") < body.index("handle @api")
