#!/bin/sh
# Open Feedback Forms — backup-first hard reset.
#
#   sh -c "$(curl -fsSL https://raw.githubusercontent.com/cloudit24/open-feedback-forms/main/hard-reset.sh)"
#
# Backs up config.json, uploaded logos, and (if the bundled MariaDB
# container is in use) a full SQL dump into ./backup/<timestamp>/ — THEN
# tears down every container and volume (`docker compose down -v`, which
# deletes off_data and off_db_data permanently) and rebuilds from scratch.
#
# Only ever run this from inside the checkout that already has your
# docker-compose.yml and .env — it operates on whatever project is in the
# current directory.
set -e

command -v docker >/dev/null 2>&1 || { echo "Docker is required: https://docs.docker.com/get-docker/"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is required."; exit 1; }
[ -f "./docker-compose.yml" ] || {
    echo "No docker-compose.yml in the current directory."
    echo "cd into your open-feedback-forms checkout first, then re-run this script."
    exit 1
}

STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="./backup/$STAMP"
mkdir -p "$BACKUP_DIR"

echo "Backing up to $BACKUP_DIR ..."

# config.json + uploaded logos live on the off_data volume — copy them out
# via a throwaway container rather than assuming a bind mount.
VOL_DATA="$(basename "$(pwd)")_off_data"
if docker volume inspect "$VOL_DATA" >/dev/null 2>&1; then
    mkdir -p "$BACKUP_DIR/off_data"
    docker run --rm -v "$VOL_DATA":/from -v "$(pwd)/$BACKUP_DIR/off_data":/to alpine \
        sh -c "cp -a /from/. /to/ 2>/dev/null || true"
    echo "  config.json + uploads -> $BACKUP_DIR/off_data/"
else
    echo "  (no $VOL_DATA volume found — nothing to copy there)"
fi

# Full SQL dump, only possible if this install uses the bundled db service
# (an external database is the admin's own to back up).
if [ -f .env ]; then
    . ./.env 2>/dev/null || true
fi
if docker compose ps db >/dev/null 2>&1 && [ "$(docker compose ps -q db)" != "" ]; then
    DUMP_PASS="${DB_ROOT_PASSWORD:-}"
    if [ -n "$DUMP_PASS" ]; then
        if docker compose exec -T db mariadb-dump -uroot -p"$DUMP_PASS" --all-databases \
                > "$BACKUP_DIR/database.sql" 2>"$BACKUP_DIR/database-dump.log"; then
            echo "  database dump -> $BACKUP_DIR/database.sql"
        else
            echo "  database dump FAILED — see $BACKUP_DIR/database-dump.log (continuing anyway)"
        fi
    else
        echo "  DB_ROOT_PASSWORD not found in .env — skipping SQL dump."
        echo "  (the raw table data is still in off_data's absence noted above; dump it yourself"
        echo "   with 'docker compose exec db mariadb-dump ...' before continuing if you need it)"
    fi
else
    echo "  bundled 'db' service isn't running — if you're on an external database,"
    echo "  back it up yourself (this script only covers the bundled one)."
fi

echo
echo "Backup step done. Everything above this line is non-destructive."
echo
echo "About to run:  docker compose down -v   (deletes ALL containers AND volumes"
echo "for this project — every form, submission and admin account, permanently)"
echo "then rebuild and start fresh."
echo

if [ -r /dev/tty ]; then
    printf "Type YES to continue, anything else to abort: " > /dev/tty
    read -r CONFIRM < /dev/tty
else
    echo "No terminal available to confirm — aborting. Re-run interactively, or edit"
    echo "this script to skip the prompt if you're sure."
    exit 1
fi

if [ "$CONFIRM" != "YES" ]; then
    echo "Aborted — nothing was torn down. Your backup is still in $BACKUP_DIR."
    exit 0
fi

echo "Tearing down and rebuilding..."
docker compose down -v
docker compose up -d --build

echo
echo "Done. A fresh container is starting — open http://localhost:8080/admin"
echo "to go through setup again (admin account, then database connection)."
echo "Your backup is in $BACKUP_DIR if you need to recover anything from it."
