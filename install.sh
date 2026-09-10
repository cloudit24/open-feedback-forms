#!/bin/sh
# Open Feedback Forms — one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/mhdanc58/open-feedback-forms/main/install.sh | sh
#
# Clones the repo (or reuses the current one, if you're already inside it),
# asks how you want to handle the database, writes .env accordingly, then
# builds and starts the containers. The admin account itself is always
# created afterward through the web UI (http://localhost:8080/admin) —
# never over a script prompt.
set -e

REPO_URL="https://github.com/mhdanc58/open-feedback-forms.git"
DIR="open-feedback-forms"

command -v docker >/dev/null 2>&1 || { echo "Docker is required: https://docs.docker.com/get-docker/"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is required (bundled with recent Docker Desktop/Engine)."; exit 1; }

# Reuse the current checkout if install.sh is already being run from inside
# the cloned repo (has server.py as a sibling); otherwise clone fresh.
if [ -f "./server.py" ] && [ -f "./docker-compose.yml" ]; then
    echo "Running from an existing checkout — skipping clone."
else
    command -v git >/dev/null 2>&1 || { echo "git is required: https://git-scm.com/downloads"; exit 1; }
    if [ -d "$DIR" ]; then
        echo "$DIR already exists — reusing it."
    else
        git clone "$REPO_URL" "$DIR"
    fi
    cd "$DIR"
fi

if [ -f .env ]; then
    echo ".env already exists — leaving it as-is. Delete it first to re-run this wizard."
else
    echo
    echo "Database setup:"
    echo "  1) Install a bundled MariaDB container for me (easiest)"
    echo "  2) Connect to a database I already have"
    echo "  3) Skip for now — I'll set it up later from the admin panel"
    printf "Choose [1/2/3]: "
    read -r db_choice

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
            printf "Database host: "; read -r db_host
            printf "Port [3306]: "; read -r db_port
            db_port=${db_port:-3306}
            printf "Database name: "; read -r db_name
            printf "Username: "; read -r db_user
            printf "Password: "; stty -echo 2>/dev/null; read -r db_pass; stty echo 2>/dev/null; echo
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
