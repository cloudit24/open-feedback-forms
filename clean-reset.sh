#!/bin/sh
# Open Feedback Forms — clean reset, NO backup.
#
#   curl -fsSL https://raw.githubusercontent.com/cloudit24/open-feedback-forms/main/clean-reset.sh | sh
#
# Straight to `docker compose down -v` (deletes every container AND volume
# for this project — every form, submission and admin account, permanently)
# then rebuilds from scratch. Nothing is backed up first.
#
# Use hard-reset.sh instead if you want a backup taken before the wipe.
set -e

command -v docker >/dev/null 2>&1 || { echo "Docker is required: https://docs.docker.com/get-docker/"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is required."; exit 1; }
[ -f "./docker-compose.yml" ] || {
    echo "No docker-compose.yml in the current directory."
    echo "cd into your open-feedback-forms checkout first, then re-run this script."
    exit 1
}

echo "About to run:  docker compose down -v   (deletes ALL containers AND volumes"
echo "for this project — every form, submission and admin account, permanently,"
echo "with NO backup taken first) — then rebuild and start fresh."
echo

if [ -r /dev/tty ]; then
    printf "Type YES to continue, anything else to abort: " > /dev/tty
    read -r CONFIRM < /dev/tty
else
    echo "No terminal available to confirm — aborting. Re-run interactively."
    exit 1
fi

if [ "$CONFIRM" != "YES" ]; then
    echo "Aborted — nothing was touched."
    exit 0
fi

echo "Tearing down and rebuilding..."
docker compose down -v
docker compose up -d --build

echo
echo "Done. A fresh container is starting — open http://localhost:8080/admin"
echo "to go through setup again (admin account, then database connection)."
