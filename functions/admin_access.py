import os

# Accounts that are administrators before the database says so (first start). The default keeps the original account;
# INAZINA_BOOTSTRAP_ADMIN_IDS (comma separated) replaces it. An empty value is ignored so a typo cannot lock everyone out.
DEFAULT_BOOTSTRAP_ADMIN_IDS = "1076861147560542278"


def bootstrap_admin_ids(environ=os.environ):
    raw = (environ.get("INAZINA_BOOTSTRAP_ADMIN_IDS") or "").strip()
    return frozenset(item.strip() for item in (raw or DEFAULT_BOOTSTRAP_ADMIN_IDS).split(",") if item.strip())


BOOTSTRAP_ADMIN_IDS = bootstrap_admin_ids()


def resolve_user_role(user_id, user_data):
    """Resolve the live SQL role, with a fixed bootstrap-admin override."""
    if str(user_id) in BOOTSTRAP_ADMIN_IDS:
        return "admin"
    try:
        # sqlite3.Row supports key lookup but ``"role" in row`` checks its
        # values, not its column names. Read by key and fall back for legacy rows.
        role = user_data["role"] if user_data is not None else "user"
    except (KeyError, IndexError, TypeError):
        role = "user"
    return str(role or "user").strip().lower()
