#!/usr/bin/env python3
"""End-to-end check of the two-person approval against a REAL Python data server running on a COPY database.

    createdb inazina_m8_e2e && pg_restore --no-owner -d inazina_m8_e2e <a pg_dump of production>
    INAZINA_PG_DSN=postgresql:///inazina_m8_e2e python tools/m8_dual_approval_e2e.py [--port 5441] [--keep]

Refuses any database whose name does not start with `inazina_m8_`. Creates three test accounts (two administrators, one member) in the
copy, starts web_api/server.py on the given port with a random throw-away internal token, walks the whole flow over HTTP (switch on,
high-risk write answered 409, own approval refused, approval by the other administrator, resend succeeds, second use refused, payload
change refused, switch off needs approval) and kills only the server process it started. Prints one JSON report after a `---REPORT---` line (the server's start-up prints come before it); exit 1 on any failure.
"""
import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ADM1, ADM2, MEMBER = "90000000000000001", "90000000000000002", "90000000000000003"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=5441)
    parser.add_argument("--keep", action="store_true", help="leave the server running (prints its PID) for a browser check")
    args = parser.parse_args()
    from psycopg.conninfo import conninfo_to_dict
    dsn = os.environ.get("INAZINA_PG_DSN", "")
    name = conninfo_to_dict(dsn).get("dbname", "") if dsn else ""
    if not name.startswith("inazina_m8_"):
        raise SystemExit("INAZINA_PG_DSN must name a copy database whose name starts with inazina_m8_")
    token = secrets.token_urlsafe(32)
    os.environ["INAZINA_INTERNAL_TOKEN"] = token
    folder = Path(tempfile.mkdtemp(prefix="inazina-m8-e2e-"))
    os.environ["INAZINA_DB_PATH"] = str(folder / "inazina.db")
    os.environ["INAZINA_PYTHON_PORT"] = str(args.port)
    from web_api import server
    from database import DatabaseManager
    from database.db_manager import DBManager
    DBManager.database_dir = folder
    db = DatabaseManager()
    for user_id, nick, role in ((ADM1, "e2e-adm1", "admin"), (ADM2, "e2e-adm2", "admin"), (MEMBER, "e2e-member", "user")):
        server.save_password(user_id, "E2e-password-12345", create=True, nickname=nick, country_id="낙원")
        db.users.update_role(user_id, role)
    db.close()
    proc = subprocess.Popen([sys.executable, "-m", "web_api.server"], cwd=ROOT, env=os.environ.copy(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results, failed = [], []

    def call(method, path, user, body=None):
        request = urllib.request.Request(f"http://127.0.0.1:{args.port}{path}", method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"x-inazina-internal-token": token, "x-inazina-user-id": user, "content-type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def step(label, got, expect_status, check=lambda body: True):
        status, body = got
        ok = status == expect_status and bool(check(body))
        results.append({"step": label, "status": status, "ok": ok, "error": body.get("error") if isinstance(body, dict) else None})
        if not ok:
            failed.append(label)
        return body

    try:
        for _ in range(120):
            try:
                if call("GET", "/internal/admin/approvals", ADM1)[0] == 200:
                    break
            except Exception:
                time.sleep(1)
        else:
            raise SystemExit("server did not start")
        edit = f"/internal/admin/user/{MEMBER}"
        change = {"changes": {"role": "admin", "role_reason": "e2e grant"}}
        step("switch on", call("POST", "/internal/admin/approvals", ADM1, {"action": "set_switch", "enabled": True}), 200, lambda b: b["enabled"] is True)
        body = step("high-risk write is held", call("POST", edit, ADM1, change), 409, lambda b: b["error"] == "approval_required")
        request_id = body["approvalRequestId"]
        step("member is still a member", call("GET", f"/internal/admin/user/{MEMBER}", ADM1), 200, lambda b: b["user"]["role"] == "user")
        step("own approval refused", call("POST", "/internal/admin/approvals", ADM1, {"action": "approve", "requestId": request_id}), 400,
             lambda b: "requester_cannot_decide" in b["error"])
        step("resend without approval refused", call("POST", edit, ADM1, {**change, "approvalId": request_id}), 400, lambda b: "not_granted" in b["error"])
        step("other administrator approves", call("POST", "/internal/admin/approvals", ADM2, {"action": "approve", "requestId": request_id}), 200)
        step("approval for another payload refused", call("POST", edit, ADM1, {"changes": {"role": "admin", "role_reason": "different"}, "approvalId": request_id}),
             400, lambda b: "does_not_match" in b["error"])
        step("resend with approval succeeds", call("POST", edit, ADM1, {**change, "approvalId": request_id}), 200)
        step("member is now an administrator", call("GET", f"/internal/admin/user/{MEMBER}", ADM1), 200, lambda b: b["user"]["role"] == "admin")
        step("second use refused", call("POST", edit, ADM1, {**change, "approvalId": request_id}), 400, lambda b: "already_used" in b["error"])
        status, listing = call("GET", "/internal/admin/approvals", ADM2)
        row = next((item for item in listing["requests"] if item["requestId"] == request_id), {})
        results.append({"step": "approval list shows it used", "status": status, "ok": row.get("status") == "used" and row.get("approver") == ADM2, "error": None})
        if not results[-1]["ok"]:
            failed.append("approval list")
        controls = call("GET", "/internal/admin/controls", ADM2)
        results.append({"step": "controls overview carries alerts", "status": controls[0], "ok": controls[0] == 200 and len(controls[1].get("anomalies", [])) == 3, "error": None})
        if not results[-1]["ok"]:
            failed.append("controls overview")
        off = step("switch off is held", call("POST", "/internal/admin/approvals", ADM1, {"action": "set_switch", "enabled": False}), 409)
        step("other administrator approves switch off", call("POST", "/internal/admin/approvals", ADM2, {"action": "approve", "requestId": off["approvalRequestId"]}), 200)
        step("switch off succeeds", call("POST", "/internal/admin/approvals", ADM1, {"action": "set_switch", "enabled": False, "approvalId": off["approvalRequestId"]}), 200,
             lambda b: b["enabled"] is False)
        step("write is direct again", call("POST", edit, ADM1, {"changes": {"role": "user"}}), 200)
        if args.keep:
            print(json.dumps({"keep_pid": proc.pid, "port": args.port, "token": token, "adm1": ADM1, "adm2": ADM2}))
    finally:
        if not args.keep:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
    print("---REPORT---")
    print(json.dumps({"database": name, "ok": not failed, "failed": failed, "steps": results}, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
