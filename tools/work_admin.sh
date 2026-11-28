#!/bin/zsh
# Work accounts from the Mac (the work MySQL must be running):
#   tools/work_admin.sh create-admin <id> [name]     the first administrator (asks for the password)
#   tools/work_admin.sh reset-password <id>          when an administrator forgot theirs
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[[ -f "$ROOT/.env" ]] && { set -a; source "$ROOT/.env"; set +a; }
cd "$ROOT"
exec npm run --silent admin --workspace @inazina/work -- "$@"
