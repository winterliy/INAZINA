"""Set a new Wide-Learning (port 4315) password from the terminal when the current one is not known.

    python tools/reset_access_password.py wl-entry   # entry password
    python tools/reset_access_password.py wl-code    # login-attempt log code (digits)

The 4312 and 4313 pages no longer have a password of their own: they take an administrator's dashboard id and
password. The new value is typed twice without echo and stored salted in database/wl-security.json (git-ignored).
It applies at once (no restart). Needs local access to this folder, which is the point.
"""
import getpass
import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ITERATIONS = 210_000
WL_STORE = Path(os.environ.get("INAZINA_WL_SECURITY_PATH", ROOT / "database" / "wl-security.json"))


def reset_wl(kind, secret):
    if kind == "wl-entry" and not 8 <= len(secret) <= 64:
        raise ValueError("입장 비밀번호는 8~64자로 입력해주세요.")
    if kind == "wl-code" and not (secret.isdigit() and 4 <= len(secret) <= 8):
        raise ValueError("확인 코드는 숫자 4~8자리로 입력해주세요.")
    try:
        data = json.loads(WL_STORE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), bytes.fromhex(salt), ITERATIONS).hex()
    stored = f"pbkdf2${ITERATIONS}${salt}${digest}"
    if kind == "wl-entry":
        data["entry"] = {"hash": stored, "updatedAt": stamp}
    else:
        data["logCode"] = {"hash": stored, "length": len(secret), "updatedAt": stamp}
    WL_STORE.parent.mkdir(parents=True, exist_ok=True)
    temp = WL_STORE.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, WL_STORE)


def main(argv):
    targets = {"wl-entry", "wl-code"}
    if len(argv) != 2 or argv[1] not in targets:
        print(__doc__)
        return 2
    target = argv[1]
    first = getpass.getpass("새 값: ")
    if first != getpass.getpass("한 번 더: "):
        print("두 값이 다릅니다. 아무것도 바꾸지 않았습니다.")
        return 1
    try:
        reset_wl(target, first)
    except ValueError as error:
        print(error)
        return 1
    print(f"{target}: 바꿨습니다. 바로 적용됩니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
