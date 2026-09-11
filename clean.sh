#!/bin/sh
# Open Feedback Forms — remove everything, no backup, no confirmation.
#
#   curl -fsSL https://raw.githubusercontent.com/cloudit24/open-feedback-forms/main/clean.sh | sh
#
# Runs `docker compose down -v` immediately (deletes every container AND
# volume for this project — every form, submission and admin account,
# permanently) and stops there. Nothing is rebuilt or started back up,
# nothing is backed up, nothing is asked before it happens — only run this
# when you're sure.
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

docker compose down -v

echo
echo "Removed — every container and volume for this project is gone."
