#!/bin/sh
# Open Feedback Forms — remove the whole install. No backup, no prompt.
#
#   sh -c "$(curl -fsSL https://raw.githubusercontent.com/cloudit24/open-feedback-forms/main/clean.sh)"
#
# Stops and deletes everything Docker holds for this project, permanently:
# containers, networks, volumes (config.json, uploads, the bundled database
# — every form, submission and admin account) and the built images. Nothing
# is rebuilt. The checkout itself — this folder, .env, any backup/ — is left
# alone. Use hard-reset.sh instead if you want a backup taken first.
set -e

die() { echo "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "Docker is required: https://docs.docker.com/get-docker/"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required."
[ -f ./docker-compose.yml ] || die "No docker-compose.yml here — cd into your open-feedback-forms checkout first."

# --remove-orphans also catches the bundled db container when the profile
# that owns it isn't active in .env; --rmi all drops the images too (an image
# another project still uses is left in place, with a warning).
docker compose down -v --remove-orphans --rmi all

echo
echo "Removed — every container, volume and image for this project is gone."
echo "This folder (including .env and any backup/) was left untouched."
