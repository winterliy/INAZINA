import os
import re
import shutil
import subprocess
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
    """Top-level site label -> body for every `label {` line outside another block."""
    out = {}
    for m in re.finditer(r"^(\S[^\n]*?) \{$", text, re.M):
        before = text[:m.start()]
        if before.count("{") != before.count("}"):  # still inside an earlier block
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
WL = "wl.localhost"


def test_names_are_inazina_localhost_only_and_there_is_no_public_api_or_internal_gateway():
    assert "4313" not in LIVE
    assert not re.search(r"^api\.", LIVE, re.M)
    assert ".kr" not in LIVE and "INAZINA_TLS" not in LIVE and "WL_DOMAIN" not in LIVE, "the public-domain plan was dropped"


def test_every_site_uses_caddys_own_certificates():
    assert "tls internal" in SITES["(common)"]
    for label, body in SITES.items():
        if label != "(common)":
            assert "tls " not in body, label  # only (common) says how


def test_sites_present():
    assert set(SITES) == {
        "(common)", "(frontend_dev)", "(frontend_static)", "(frontend_)", "(wl_proxy)", "(wl_off)", "(wl_)",
        D, f"www.{D}", f"corp.{D}", f"id.{D}", f"naru.{D}", f"console.{D}", WL,
    }


def test_wiki_viewer_site_is_chosen_by_INAZINA_WL_and_has_the_one_name_wl_localhost():
    assert "wl.inazina.localhost" not in LIVE
    body = SITES[WL]
    assert "import wl_{$INAZINA_WL:proxy}" in body
    assert "reverse_proxy 127.0.0.1:4315" in SITES["(wl_proxy)"]
    assert "respond" in SITES["(wl_off)"] and "4315" not in SITES["(wl_off)"]


def test_apex_redirects_permanently_to_www():
    assert f"redir https://www.{D}{{uri}} permanent" in SITES[D]
    assert "reverse_proxy" not in SITES[D]


def test_id_host_exposes_only_oidc_paths():
    body = SITES[f"id.{D}"]
    assert "@idpaths path /oidc/* /login /register /reset" in body
    found = handlers(body)
    assert "reverse_proxy 127.0.0.1:4312" in found["@idpaths"]
    assert found[""].strip() == "respond 404"
    assert "reverse_proxy" not in body.replace(found["@idpaths"], "")


def test_app_hosts_split_api_from_frontend_and_block_internal():
    for host, app in (("naru", "hts"), ("console", "dashboard")):
        body = SITES[f"{host}.{D}"]
        assert "@internal path /api/internal*" in body and "@api path /api/*" in body
        found = handlers(body)
        assert found["@internal"].strip() == "respond 404"
        assert found["@api"].strip() == "reverse_proxy 127.0.0.1:4312"
        port = {"naru": "4311", "console": "4310"}[host]
        assert found[""].strip() == f"import frontend_{{$INAZINA_FRONTEND:dev}} {port} {{$INAZINA_ROOT:.}}/apps/{app}/dist"
        # The internal block must come before the generic /api/* handler (first handle wins).
        assert body.index("handle @internal") < body.index("handle @api")


def test_site_hosts_send_only_the_contact_form_to_the_api():
    for host in ("www", "corp"):
        body = SITES[f"{host}.{D}"]
        assert "@feedback path /api/site/feedback" in body
        found = handlers(body)
        assert found["@feedback"].strip() == "reverse_proxy 127.0.0.1:4312"
        assert found[""].strip() == "import frontend_{$INAZINA_FRONTEND:dev} 4314 {$INAZINA_ROOT:.}/apps/site/dist"


# ---- What Caddy makes of it (skipped when Caddy is not installed) ----
import json

import pytest

needs_caddy = pytest.mark.skipif(shutil.which("caddy") is None, reason="caddy is not installed")
HOSTS = {"inazina.localhost", "www.inazina.localhost", "corp.inazina.localhost", "id.inazina.localhost", "naru.inazina.localhost",
         "console.inazina.localhost", "wl.localhost"}


def _caddy(command, **env):
    merged = {key: value for key, value in os.environ.items() if not key.startswith("INAZINA_")}
    merged.update({"INAZINA_ROOT": "/srv/inazina", **env})
    return subprocess.run(["caddy", command, "--config", str(Path(__file__).resolve().parents[1] / "infra" / "Caddyfile"), "--adapter", "caddyfile"],
                          capture_output=True, text=True, env=merged, timeout=60)


def _adapt(**env):
    result = _caddy("adapt", **env)
    assert result.returncode == 0, result.stderr[-400:]
    return json.loads(result.stdout)


def _walk(node, found):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "dial":
                found["dials"].add(value)
            elif key == "root":
                found["roots"].add(value)
            elif key == "handler":
                found["handlers"].add(value)
            _walk(value, found)
    elif isinstance(node, list):
        for item in node:
            _walk(item, found)
    return found


def _site(config, host):
    for server in config["apps"]["http"]["servers"].values():
        for route in server["routes"]:
            if any(host in match.get("host", []) for match in route.get("match", [])):
                return _walk(route, {"dials": set(), "roots": set(), "handlers": set()})
    raise AssertionError(f"no site for {host}")


@needs_caddy
@pytest.mark.parametrize("env", [{}, {"INAZINA_FRONTEND": "static"}, {"INAZINA_WL": "off"}, {"INAZINA_FRONTEND": ""}, {"INAZINA_WL": ""}], ids=["dev", "static", "wl-off", "frontend-empty", "wl-empty"])
def test_caddy_accepts_the_file_in_every_mode(env):
    result = _caddy("validate", **env)
    assert result.returncode == 0, result.stderr[-400:]


@needs_caddy
def test_caddy_serves_exactly_the_planned_names_with_its_own_certificates():
    config = _adapt()
    hosts = {host for server in config["apps"]["http"]["servers"].values() for route in server["routes"] for match in route.get("match", []) for host in match.get("host", [])}
    assert hosts == HOSTS
    assert "internal" in json.dumps(config["apps"]["tls"]["automation"]), "certificates come from Caddy's local CA"


@needs_caddy
def test_development_mode_proxies_the_screens_to_the_vite_servers():
    config = _adapt()
    for host, port in (("naru.inazina.localhost", "4311"), ("console.inazina.localhost", "4310"), ("www.inazina.localhost", "4314"), ("corp.inazina.localhost", "4314"),
                       ("wl.localhost", "4315")):
        site = _site(config, host)
        assert f"127.0.0.1:{port}" in site["dials"], host
        assert "file_server" not in site["handlers"], host


@needs_caddy
def test_static_mode_serves_the_built_files_and_never_the_vite_servers_of_the_screens():
    config = _adapt(INAZINA_FRONTEND="static")
    for host, app in (("naru.inazina.localhost", "hts"), ("console.inazina.localhost", "dashboard"), ("www.inazina.localhost", "site"), ("corp.inazina.localhost", "site")):
        site = _site(config, host)
        assert site["roots"] == {f"/srv/inazina/apps/{app}/dist"}, host
        assert "file_server" in site["handlers"], host
        assert not site["dials"] & {"127.0.0.1:4310", "127.0.0.1:4311", "127.0.0.1:4314"}, host
    assert "127.0.0.1:4312" in _site(config, "naru.inazina.localhost")["dials"], "/api still goes to the API"
    assert "127.0.0.1:4312" in _site(config, "console.inazina.localhost")["dials"]
    assert _site(config, "www.inazina.localhost")["dials"] == {"127.0.0.1:4312"}, "the site only asks the API for its contact form"


@needs_caddy
def test_wiki_viewer_is_proxied_by_default_in_both_modes_and_off_on_request():
    for env in ({}, {"INAZINA_FRONTEND": "static"}):
        assert "127.0.0.1:4315" in _site(_adapt(**env), "wl.localhost")["dials"], env
    assert "127.0.0.1:4315" not in _site(_adapt(INAZINA_WL="off"), "wl.localhost")["dials"]


@needs_caddy
def test_static_mode_falls_back_to_the_app_for_client_side_routes_and_caches_hashed_files():
    text = json.dumps(_adapt(INAZINA_FRONTEND="static"))
    assert "try_files" in text or "rewrite" in text
    assert "immutable" in text
