#!/bin/sh
# Open Feedback Forms — one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/cloudit24/open-feedback-forms/main/install.sh | sh
#
# Clones the repo (or reuses the current one, if you're already inside it),
# asks how you want to handle the database, writes .env accordingly, then
# builds and starts the containers. The admin account itself is always
# created afterward through the web UI (http://localhost:8080/admin) —
# never over a script prompt.
set -e

REPO_URL="https://github.com/cloudit24/open-feedback-forms.git"
DIR="open-feedback-forms"

command -v docker >/dev/null 2>&1 || { echo "Docker is required: https://docs.docker.com/get-docker/"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is required (bundled with recent Docker Desktop/Engine)."; exit 1; }

# Reuse the current checkout if install.sh is already being run from inside
# the cloned repo (has server.py as a sibling); otherwise clone fresh. Either
# way, an existing checkout gets pulled to latest — this is also how you
# update an install: just re-run this same one-liner.
if [ -f "./server.py" ] && [ -f "./docker-compose.yml" ]; then
    echo "Running from an existing checkout — pulling latest changes..."
    if command -v git >/dev/null 2>&1; then
        git pull --ff-only || echo "Could not fast-forward automatically — pull manually, then re-run this script."
    fi
else
    command -v git >/dev/null 2>&1 || { echo "git is required: https://git-scm.com/downloads"; exit 1; }
    if [ -d "$DIR" ]; then
        echo "$DIR already exists — pulling latest changes..."
        (cd "$DIR" && git pull --ff-only) || echo "Could not fast-forward automatically — pull manually, then re-run this script."
    else
        git clone "$REPO_URL" "$DIR"
    fi
    cd "$DIR"
fi

if [ -f .env ]; then
    echo ".env already exists — leaving it as-is. Delete it first to re-run this wizard."
else
    # When run as `curl ... | sh`, stdin IS the script — a plain `read` would
    # swallow the next lines of this file instead of waiting for the keyboard.
    # Always prompt via the terminal directly; fall back to "skip" when there
    # is no terminal at all (CI, or a fully unattended run).
    if [ -r /dev/tty ]; then
        ask() { printf "%s" "$1" > /dev/tty; read -r "$2" < /dev/tty; }
        ask_secret() {
            printf "%s" "$1" > /dev/tty
            stty -echo < /dev/tty 2>/dev/null
            read -r "$2" < /dev/tty
            stty echo < /dev/tty 2>/dev/null
            echo > /dev/tty
        }
    else
        echo "No terminal available — skipping the database wizard (set it up from /admin later)."
        ask() { eval "$2=''"; }
        ask_secret() { eval "$2=''"; }
    fi

    echo
    echo "Database setup:"
    echo "  1) Install a bundled MariaDB container for me (easiest)"
    echo "  2) Connect to a database I already have"
    echo "  3) Skip for now — I'll set it up later from the admin panel"
    ask "Choose [1/2/3]: " db_choice

    cp .env.example .env

    case "$db_choice" in
        1)
            gen_pw() { python3 -c "import secrets; print(secrets.token_urlsafe(18))" 2>/dev/null || \
                       od -An -tx1 -N18 /dev/urandom | tr -d ' \n'; }
            DB_PASSWORD=$(gen_pw)
            DB_ROOT_PASSWORD=$(gen_pw)
            {
                echo "COMPOSE_PROFILES=bundled-db"
                echo "DB_NAME=open_feedback_forms"
                echo "DB_USER=off_app"
                echo "DB_PASSWORD=$DB_PASSWORD"
                echo "DB_ROOT_PASSWORD=$DB_ROOT_PASSWORD"
                echo "OFF_WAIT_FOR_DB_HOST=db"
                echo "OFF_WAIT_FOR_DB_PORT=3306"
                echo "OFF_DB_HOST=db"
                echo "OFF_DB_PORT=3306"
                echo "OFF_DB_USER=off_app"
                echo "OFF_DB_PASSWORD=$DB_PASSWORD"
                echo "OFF_DB_NAME=open_feedback_forms"
            } >> .env
            echo "Generated a random database password — saved in .env (keep that file private)."
            NEEDS_BOOTSTRAP=1
            ;;
        2)
            ask "Database host: " db_host
            ask "Port [3306]: " db_port
            db_port=${db_port:-3306}
            ask "Database name: " db_name
            ask "Username: " db_user
            ask_secret "Password: " db_pass
            {
                echo "OFF_DB_HOST=$db_host"
                echo "OFF_DB_PORT=$db_port"
                echo "OFF_DB_USER=$db_user"
                echo "OFF_DB_PASSWORD=$db_pass"
                echo "OFF_DB_NAME=$db_name"
            } >> .env
            NEEDS_BOOTSTRAP=1
            ;;
        *)
            echo "Skipping — configure the database later from Configuration → Database connection in /admin."
            NEEDS_BOOTSTRAP=0
            ;;
    esac
fi

echo
echo "Building and starting containers..."
docker compose up -d --build

if [ "${NEEDS_BOOTSTRAP:-0}" = "1" ]; then
    echo "Saving the database connection to config.json..."
    docker compose run --rm app python docker/bootstrap.py
    docker compose restart app
fi

echo
echo "Done. Open http://localhost:8080/admin to create your admin account and get started."
