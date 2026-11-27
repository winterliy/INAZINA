"""Credential patterns shared by the secret-hygiene test and the rotation check tool."""
SECRET_PATTERNS = {
    "aws_key": r"AKIA[0-9A-Z]{16}",
    "private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY",
    "github_token": r"gh[pousr]_[A-Za-z0-9]{30,}",
    "slack_token": r"xox[abprs]-[0-9A-Za-z-]{20,}",
    "api_key": r"sk-[A-Za-z0-9]{32,}",
    "discord_bot_token": r"[MN][A-Za-z0-9_-]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}",
    "dsn_with_password": r"postgres(?:ql)?://[^:/@\s]+:[^@\s$]{3,}@",
}
