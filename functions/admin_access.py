BOOTSTRAP_ADMIN_IDS = frozenset({"1076861147560542278"})


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
