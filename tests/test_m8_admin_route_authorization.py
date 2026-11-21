"""M8 least privilege: every /internal/admin/* route in web_api/server.py must demand the admin role, and administrator
writes must leave an audit trail. Static (AST) checks, so a new route added without a guard fails here."""
import ast
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "web_api" / "server.py"
HANDLERS = {"do_GET", "do_POST", "do_PUT", "do_PATCH", "do_DELETE"}
# Calls that record the administrator's action: explicit audit rows, the audited admin scope, or the admin command layer.
AUDIT_MARKERS = ("audit", "admin_scope", "admin_control")
# Administrator POST routes whose audit evidence is not visible statically. Each needs a reason; shrink this list, never grow it quietly.
NO_STATIC_AUDIT_EVIDENCE = {
    "/internal/admin/dr/monitor/sample": "samples DR deviation state through DrDeviationMonitorManager; no admin_audit row (미비)",
}

SOURCE = SERVER.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
FUNCS = {node.name: node for node in TREE.body if isinstance(node, ast.FunctionDef)}


def strings(node):
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def requires_admin(statements):
    for statement in statements:
        for node in ast.walk(statement):
            if isinstance(node, ast.Compare) and any(isinstance(c, ast.Constant) and c.value == "admin" for c in [node.left, *node.comparators]):
                return True
    return False


def handler_functions():
    for cls in (n for n in ast.walk(TREE) if isinstance(n, ast.ClassDef)):
        for item in cls.body:
            if isinstance(item, ast.FunctionDef) and item.name in HANDLERS:
                yield item


def admin_routes(handler):
    """Yield (if-node, admin-guarded-by-ancestor) for each branch whose condition names an /internal/admin path."""
    def visit(node, guarded):
        if isinstance(node, ast.If) and any(s.startswith("/internal/admin") for s in strings(node.test)):
            yield node, guarded
            inner = guarded or requires_admin(node.body)
            for child in node.body:
                yield from visit(child, inner)
            for child in node.orelse:
                yield from visit(child, guarded)
            return
        for child in ast.iter_child_nodes(node):
            yield from visit(child, guarded)
    yield from visit(handler, False)


def route_name(branch):
    return next(s for s in strings(branch.test) if s.startswith("/internal/admin"))


def test_admin_routes_exist_for_get_and_post():
    counts = {h.name: sum(1 for _ in admin_routes(h)) for h in handler_functions()}
    assert counts.get("do_GET", 0) >= 30 and counts.get("do_POST", 0) >= 25, counts


def test_every_admin_route_requires_admin_role():
    unguarded = []
    for handler in handler_functions():
        for branch, guarded in admin_routes(handler):
            if not guarded and not requires_admin(branch.body):
                unguarded.append(f"{handler.name}:{branch.lineno} {route_name(branch)}")
    assert not unguarded, "admin route without an admin role check: " + "; ".join(unguarded)


def test_admin_routes_answer_admin_required_to_other_roles():
    for handler in handler_functions():
        for branch, guarded in admin_routes(handler):
            if guarded:
                continue
            assert "admin_required" in strings(ast.Module(body=branch.body, type_ignores=[])), f"{handler.name}:{branch.lineno} {route_name(branch)}"


def _calls(node):
    return {getattr(c.func, "id", None) for c in ast.walk(node) if isinstance(c, ast.Call)} - {None}


def _audited(names, depth=3, seen=None):
    seen = seen if seen is not None else set()
    for name in names:
        if name in seen or name not in FUNCS:
            continue
        seen.add(name)
        text = ast.get_source_segment(SOURCE, FUNCS[name]).lower()
        if any(marker in text for marker in AUDIT_MARKERS) or (depth and _audited(_calls(FUNCS[name]), depth - 1, seen)):
            return True
    return False


def test_admin_writes_leave_audit_evidence():
    post = next(h for h in handler_functions() if h.name == "do_POST")
    missing = {}
    for branch, _ in admin_routes(post):
        text = ast.get_source_segment(SOURCE, branch).lower()
        if any(marker in text for marker in AUDIT_MARKERS) or _audited(_calls(ast.Module(body=branch.body, type_ignores=[]))):
            continue
        missing[route_name(branch)] = branch.lineno
    assert set(missing) <= set(NO_STATIC_AUDIT_EVIDENCE), f"new admin write without audit evidence: {sorted(set(missing) - set(NO_STATIC_AUDIT_EVIDENCE))}"


def test_sensitive_admin_reads_are_audited():
    """Reads of user lists, funding and ALM record who looked (audit_admin_access). Login history, the security
    overview and the audit log itself are read without a trail: a known gap listed in docs/threat_model_laplace.md."""
    text = SOURCE
    for route in ("/internal/admin/users", "/internal/admin/funding-cashflow", "/internal/admin/funding", "/internal/admin/alm"):
        get = next(h for h in handler_functions() if h.name == "do_GET")
        branch = next(b for b, _ in admin_routes(get) if route_name(b) == route)
        assert "audit_admin_access" in ast.get_source_segment(text, branch), route
