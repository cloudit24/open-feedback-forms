#!/bin/sh
# Open Feedback Forms — one-line installer and updater.
#
#   curl -fsSL https://raw.githubusercontent.com/cloudit24/open-feedback-forms/main/install.sh | sh
#
# First run: clones the repo, asks how you want to handle the database,
# writes .env, then builds and starts the containers. Every run after that:
# pulls the latest code and rebuilds — .env, config.json and the database
# are never touched. The admin account is always created afterward through
# the web UI, never over a script prompt.
set -e

REPO_URL="https://github.com/cloudit24/open-feedback-forms.git"
DIR="open-feedback-forms"

die() { echo "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "Docker is required: https://docs.docker.com/get-docker/"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (bundled with recent Docker Desktop/Engine)."

# ---- get the code ----------------------------------------------------------
# Already inside a checkout? Reuse it. Otherwise clone (or reuse ./$DIR).
# Either way an existing checkout is pulled to latest — re-running this
# one-liner is how you update.
pull() {
    command -v git >/dev/null 2>&1 || { echo "git not found — using the code already here."; return 0; }
    git pull --ff-only || echo "Could not fast-forward — pull manually, then re-run this script."
}
if [ -f ./server.py ] && [ -f ./docker-compose.yml ]; then
    echo "Existing checkout — pulling latest changes..."
    pull
else
    command -v git >/dev/null 2>&1 || die "git is required: https://git-scm.com/downloads"
    if [ -d "$DIR" ]; then
        echo "$DIR already exists — pulling latest changes..."
        (cd "$DIR" && pull)
    else
        git clone "$REPO_URL" "$DIR"
    fi
    cd "$DIR"
fi

# ---- prompting --------------------------------------------------------------
# Under `curl | sh`, stdin IS the script — a plain `read` would eat the
# script's own next lines. Always talk to the terminal directly. With no
# terminal at all (CI / unattended), every prompt answers "" so the wizard
# takes its defaults and skips the database.
if [ -r /dev/tty ]; then
    TTY=1
    ask() { printf "%s" "$1" >/dev/tty; read -r "$2" </dev/tty; }
    ask_secret() {
        printf "%s" "$1" >/dev/tty
        stty -echo </dev/tty 2>/dev/null
        read -r "$2" </dev/tty
        stty echo </dev/tty 2>/dev/null
        echo >/dev/tty
    }
    # Ctrl-C in the middle of a password prompt must not leave the
    # terminal with echo switched off.
    trap 'stty echo </dev/tty 2>/dev/null' EXIT INT TERM
else
    TTY=0
    ask() { eval "$2=''"; }
    ask_secret() { eval "$2=''"; }
fi
ask_required() {
    while :; do
        ask "$1" "$2"
        eval "_v=\$$2"
        [ -n "$_v" ] && return 0
        [ "$TTY" = 1 ] || die "No terminal to answer \"$1\" — run interactively, or set the database up from /admin later."
        echo "  (required)" >/dev/tty
    done
}
gen_pw() {
    python3 -c "import secrets; print(secrets.token_urlsafe(18))" 2>/dev/null ||
        od -An -tx1 -N18 /dev/urandom | tr -d ' \n'
}
# Compose reads .env itself, so values go in single quotes: everything
# inside is literal — $, #, spaces and quotes survive as typed. A single
# quote is the one character that can't be represented that way.
ENV_EXTRA=""
env_set() {
    case "$2" in *"'"*) die "Sorry — $1 can't contain a single quote (')." ;; esac
    ENV_EXTRA="$ENV_EXTRA$1='$2'
"
}

# ---- database wizard (first run only) ----------------------------------------
NEEDS_BOOTSTRAP=0
FIRST_RUN=0
if [ -f .env ]; then
    echo ".env already exists — keeping it. (Delete it to run the database wizard again.)"
else
    FIRST_RUN=1
    echo
    echo "Database setup:"
    echo "  1) Install a bundled MariaDB container for me (easiest)"
    echo "  2) Connect to a database I already have"
    echo "  3) Skip for now — I'll set it up later from the admin panel"
    ask "Choose [1/2/3]: " db_choice

    case "$db_choice" in
        1)
            echo
            ask "Database name [open_feedback_forms]: " db_name
            ask "Database user [off_app]: " db_user
            ask_secret "Database password (blank = generate one): " db_pass
            db_name=${db_name:-open_feedback_forms}
            db_user=${db_user:-off_app}
            [ -n "$db_pass" ] || db_pass=$(gen_pw)

            env_set COMPOSE_PROFILES bundled-db
            env_set DB_NAME "$db_name"
            env_set DB_USER "$db_user"
            env_set DB_PASSWORD "$db_pass"
            env_set DB_ROOT_PASSWORD "$(gen_pw)"
            env_set OFF_WAIT_FOR_DB_HOST db
            env_set OFF_WAIT_FOR_DB_PORT 3306
            env_set OFF_DB_HOST db
            env_set OFF_DB_PORT 3306
            env_set OFF_DB_USER "$db_user"
            env_set OFF_DB_PASSWORD "$db_pass"
            env_set OFF_DB_NAME "$db_name"
            NEEDS_BOOTSTRAP=1
            ;;
        2)
            echo
            ask_required "Database host: " db_host
            ask "Port [3306]: " db_port
            ask_required "Database name: " db_name
            ask_required "Username: " db_user
            ask_secret "Password: " db_pass

            env_set OFF_DB_HOST "$db_host"
            env_set OFF_DB_PORT "${db_port:-3306}"
            env_set OFF_DB_USER "$db_user"
            env_set OFF_DB_PASSWORD "$db_pass"
            env_set OFF_DB_NAME "$db_name"
            NEEDS_BOOTSTRAP=1
            ;;
        *)
            echo "Skipping — set the database up later from Configuration → Database connection in /admin."
            ;;
    esac

    # Written in one go, only once every answer is in — an aborted wizard
    # never leaves a half-written .env behind for the next run to "keep".
    { cat .env.example; printf "%s" "$ENV_EXTRA"; } > .env
    echo "Saved .env (keep that file private — it holds the database password)."
fi

# ---- build & start -----------------------------------------------------------
echo
echo "Building and starting containers..."
docker compose up -d --build --remove-orphans

if [ "$NEEDS_BOOTSTRAP" = 1 ]; then
    echo "Saving the database connection to config.json..."
    docker compose run --rm app python docker/bootstrap.py
    docker compose restart app
fi

ADMIN_PORT=$(sed -n "s/^ADMIN_PORT=//p" .env | tr -d "'\"" | tail -n 1)
URL="http://localhost:${ADMIN_PORT:-8080}/admin"
echo
if [ "$FIRST_RUN" = 0 ]; then
    echo "Updated. $URL"
elif [ "$NEEDS_BOOTSTRAP" = 1 ]; then
    echo "Done. Open $URL to create your admin account."
else
    echo "Done. Open $URL to create your admin account and connect the database."
fi
