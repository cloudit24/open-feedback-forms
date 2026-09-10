#!/bin/sh
# Thin wrapper around the real command (`python server.py`). Its only job:
# when compose starts a bundled MariaDB alongside the app, give the database
# a moment to finish its own first-boot initialization before the app makes
# its one connection attempt at startup — server.py itself handles an
# unreachable database gracefully (logs it, admin can retry from /admin),
# this just avoids that being the common case on a fresh `docker compose up`.
set -e

if [ -n "$OFF_WAIT_FOR_DB_HOST" ]; then
    echo "waiting for database at $OFF_WAIT_FOR_DB_HOST:${OFF_WAIT_FOR_DB_PORT:-3306}..."
    python - <<'PYEOF'
import os, socket, time
host = os.environ["OFF_WAIT_FOR_DB_HOST"]
port = int(os.environ.get("OFF_WAIT_FOR_DB_PORT", "3306"))
deadline = time.time() + 60
while time.time() < deadline:
    try:
        with socket.create_connection((host, port), timeout=2):
            break
    except OSError:
        time.sleep(1)
else:
    print("database still unreachable after 60s — starting anyway, fix it from /admin")
PYEOF
fi

exec "$@"
