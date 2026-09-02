#!/usr/bin/env bash
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN=""

for candidate in "$ROOT_DIR/backend/.venv/bin/python" "${PYTHON:-}" python3 python; do
  [[ -n "$candidate" ]] || continue
  if "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [[ -z "$PYTHON_BIN" ]]; then
  echo "Python 3.11 or newer is required." >&2
  exit 1
fi

if (( $# > 1 )); then
  echo "Usage: ./db.sh [check|migrate]" >&2
  exit 2
fi

case "${1:-}" in
  ""|migrate)
    exec "$PYTHON_BIN" "$ROOT_DIR/scripts/migrate_mysql.py"
    ;;
  check)
    exec "$PYTHON_BIN" "$ROOT_DIR/scripts/migrate_mysql.py" --check
    ;;
  *)
    echo "Usage: ./db.sh [check|migrate]" >&2
    exit 2
    ;;
esac
