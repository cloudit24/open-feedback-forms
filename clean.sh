#!/bin/sh
# Open Feedback Forms — remove the whole install.
#
#   sh -c "$(curl -fsSL https://raw.githubusercontent.com/cloudit24/open-feedback-forms/main/clean.sh)"
#
# Works from any folder: finds the install the same way install.sh does.
# Stops and deletes everything Docker holds for this project, permanently:
# containers, networks, volumes (config.json, uploads, the bundled database
# — every form, submission and admin account) and the built images. Also
# deletes .env and any .env.bak.* copies, so the next install.sh starts from
# a clean slate and asks the database questions again.
#
# Nothing is rebuilt, and nothing else in the folder is touched — a backup/
# directory is left alone. Use hard-reset.sh instead if you want a backup
# taken before the wipe.
set -e

SCRIPT_URL="https://raw.githubusercontent.com/cloudit24/open-feedback-forms/main/clean.sh"
PROJECT="open-feedback-forms"

die() { echo "$*" >&2; exit 1; }

# Run this same script again under sudo — at most once.
rerun_as_root() {
    [ "$(id -u)" = 0 ] && return 1
    [ -n "${OFF_RERUN:-}" ] && return 1
    command -v sudo >/dev/null 2>&1 || return 1
    echo "$1 — running again with sudo..."
    if [ -f "$0" ] && grep -q "Open Feedback Forms" "$0" 2>/dev/null; then
        exec sudo env OFF_RERUN=1 sh "$0"
    fi
    script=$(curl -fsSL "$SCRIPT_URL" 2>/dev/null || wget -qO- "$SCRIPT_URL" 2>/dev/null) || return 1
    exec sudo env OFF_RERUN=1 sh -c "$script"
}

command -v docker >/dev/null 2>&1 || die "Docker isn't installed — there's nothing to remove."
if ! docker info >/dev/null 2>&1; then
    case "$(docker info 2>&1 || true)" in
        *[Pp]ermission\ denied*) rerun_as_root "This user isn't allowed to use Docker" || die "No permission to use Docker — run this with sudo." ;;
        *) [ "$(id -u)" = 0 ] || rerun_as_root "Docker isn't running" || die "Docker isn't running. Start it with: sudo systemctl start docker"
           systemctl start docker 2>/dev/null || service docker start 2>/dev/null || true; sleep 5
           docker info >/dev/null 2>&1 || die "Docker isn't working: $(docker info 2>&1 | tail -n 2)" ;;
    esac
fi
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required."

# Find the install: this folder, the folder Docker recorded for the running
# containers, then ./open-feedback-forms and the home folder(s).
if ! { [ -f ./docker-compose.yml ] && [ -f ./server.py ]; }; then
    user_home=$HOME
    [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ] && user_home=$(eval echo "~$SUDO_USER")
    wd=$(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" \
            --format '{{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null | head -n 1)
    for d in "$wd" "./$PROJECT" "$HOME/$PROJECT" "$user_home/$PROJECT"; do
        if [ -n "$d" ] && [ -f "$d/docker-compose.yml" ] && [ -f "$d/server.py" ]; then cd "$d"; break; fi
    done
fi

if [ -f ./docker-compose.yml ]; then
    where="the install in $(pwd)"
else
    # The folder itself is gone, but Docker may still hold the containers and data.
    ids=$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")
    vols=$(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT")
    [ -n "$ids$vols" ] || die "No Open Feedback Forms install found on this server — nothing to remove."
    where="the install's containers and data (its folder is already gone)"
fi

echo "This deletes $where:"
echo "every container, volume, image and .env — every form, submission and admin account, permanently."

# Under `curl | sh` stdin is the script itself, so ask the terminal directly.
# No terminal (CI/unattended) means no way to confirm — go ahead.
if ( : </dev/tty ) 2>/dev/null; then
    printf "Press Enter to proceed, or 'n' to cancel: " >/dev/tty
    read -r reply </dev/tty
    case "$reply" in
        [nN]*) echo "Cancelled — nothing was touched."; exit 0 ;;
    esac
fi

if [ -f ./docker-compose.yml ]; then
    # --remove-orphans also catches the bundled db container when its profile
    # isn't active in .env; --rmi all drops the images too. Runs before .env
    # is deleted, since compose reads it.
    docker compose down -v --remove-orphans --rmi all
    rm -f .env .env.bak.*
else
    [ -n "$ids" ] && docker rm -f $ids >/dev/null
    [ -n "$vols" ] && docker volume rm $vols >/dev/null
    docker network rm "${PROJECT}_default" >/dev/null 2>&1 || true
    docker rmi open-feedback-forms:latest >/dev/null 2>&1 || true
fi

echo
echo "Removed — every container, volume and image for this project is gone,"
echo "along with .env. Run install.sh to set it up again from scratch."
