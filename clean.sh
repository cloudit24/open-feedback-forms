#!/bin/sh
# Open Feedback Forms — clean reset, no backup, no confirmation.
#
#   curl -fsSL https://raw.githubusercontent.com/cloudit24/open-feedback-forms/main/clean.sh | sh
#
# Runs `docker compose down -v` immediately (deletes every container AND
# volume for this project — every form, submission and admin account,
# permanently) then rebuilds from scratch. Nothing is backed up and nothing
# is asked before it happens — only run this when you're sure.
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

echo "Tearing down and rebuilding..."
docker compose down -v
docker compose up -d --build

echo
echo "Done. A fresh container is starting — open http://localhost:8080/admin"
echo "to go through setup again (admin account, then database connection)."
