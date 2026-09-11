#!/bin/sh
# Open Feedback Forms — remove the whole install.
#
#   sh -c "$(curl -fsSL https://raw.githubusercontent.com/cloudit24/open-feedback-forms/main/clean.sh)"
#
# Stops and deletes everything Docker holds for this project, permanently:
# containers, networks, volumes (config.json, uploads, the bundled database
# — every form, submission and admin account) and the built images. Also
# deletes .env and any .env.bak.* copies, so the next install.sh starts from
# a clean slate and asks the database questions again.
#
# Nothing is rebuilt, and nothing else in this folder is touched — a
# backup/ directory from hard-reset.sh is left alone. Use hard-reset.sh
# instead if you want a backup taken before the wipe.
set -e

die() { echo "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "Docker is required: https://docs.docker.com/get-docker/"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required."
[ -f ./docker-compose.yml ] || die "No docker-compose.yml here — cd into your open-feedback-forms checkout first."

echo "This deletes every container, volume, image and .env for this project."
echo "Every form, submission and admin account goes with it, permanently."

# Under `curl | sh` stdin is the script itself, so ask the terminal directly.
# No terminal (CI/unattended) means no way to confirm — go ahead.
if [ -r /dev/tty ]; then
    printf "Press Enter to proceed, or 'n' to cancel: " >/dev/tty
    read -r reply </dev/tty
    case "$reply" in
        [nN]*) echo "Cancelled — nothing was touched."; exit 0 ;;
    esac
fi

# --remove-orphans also catches the bundled db container when the profile
# that owns it isn't active in .env; --rmi all drops the images too (an image
# another project still uses is left in place, with a warning). This runs
# before .env is deleted — compose reads it for COMPOSE_PROFILES and the
# db service's required DB_ROOT_PASSWORD.
docker compose down -v --remove-orphans --rmi all

rm -f .env
rm -f .env.bak.*

echo
echo "Removed — every container, volume and image for this project is gone,"
echo "along with .env. Run install.sh to set it up again from scratch."
