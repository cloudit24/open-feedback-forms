#!/bin/sh
# Open Feedback Forms — one-line installer and updater.
#
#   curl -fsSL https://raw.githubusercontent.com/cloudit24/open-feedback-forms/main/install.sh | sh
#
# First run: clones the repo, asks how you want to handle the database,
# writes .env, then builds and starts the containers. Every run after that:
# pulls the latest code and rebuilds — .env, config.json and the database
# are never touched unless you say so at the prompt. The admin account is
# always created afterward through the web UI, never over a script prompt.
set -e

REPO_URL="https://github.com/cloudit24/open-feedback-forms.git"
DIR="open-feedback-forms"

die() { echo "$*" >&2; exit 1; }
as_root() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi; }

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
# Database/user names go into SQL and .env — letters, digits and _ only.
ask_ident() {
    while :; do
        ask "$1" "$2"
        eval "_v=\${$2:-$3}"
        case "$_v" in
            *[!A-Za-z0-9_]*) echo "  (letters, digits and _ only)" >/dev/tty ;;
            *) eval "$2=\$_v"; return 0 ;;
        esac
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

# ---- MariaDB already installed on this server --------------------------------
# Creates the database + user there and points the container at it. Inside a
# container "localhost" is the container itself, so the host is reached as
# host.docker.internal (docker-compose.yml maps that to the host's gateway).

# Run SQL as MariaDB root: as this user if that already works (root with
# unix-socket auth), else via sudo, else with the root password.
sql_root() {
    if [ -z "$SQL_ROOT" ]; then
        CLI=$(command -v mariadb || command -v mysql || true)
        [ -n "$CLI" ] || die "No mariadb/mysql client found on this server — is MariaDB installed here? (Otherwise choose 'Connect to a database I already have'.)"
        if "$CLI" -uroot -e "SELECT 1" >/dev/null 2>&1; then
            SQL_ROOT="$CLI -uroot"
        elif [ "$(id -u)" != 0 ] && sudo -n "$CLI" -uroot -e "SELECT 1" >/dev/null 2>&1; then
            SQL_ROOT="sudo -n $CLI -uroot"
        elif [ "$(id -u)" != 0 ] && [ "$TTY" = 1 ] && sudo -v && sudo "$CLI" -uroot -e "SELECT 1" >/dev/null 2>&1; then
            SQL_ROOT="sudo $CLI -uroot"
        else
            ask_secret "MariaDB root password: " root_pw
            MYSQL_PWD="$root_pw"; export MYSQL_PWD
            "$CLI" -uroot -e "SELECT 1" >/dev/null 2>&1 || die "Could not log in to MariaDB as root with that password."
            SQL_ROOT="$CLI -uroot"
        fi
    fi
    $SQL_ROOT
}

create_local_db() {   # name user password
    # Inside an SQL string only \ and ' are special; ' is already refused.
    esc_pw=$(printf '%s' "$3" | sed 's/\\/\\\\/g')
    echo "Creating database '$1' and user '$2' on this server's MariaDB..."
    sql_root <<SQL
CREATE DATABASE IF NOT EXISTS \`$1\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '$2'@'%' IDENTIFIED BY '$esc_pw';
ALTER USER '$2'@'%' IDENTIFIED BY '$esc_pw';
GRANT ALL PRIVILEGES ON \`$1\`.* TO '$2'@'%';
FLUSH PRIVILEGES;
SQL
    echo "Database ready."
}

# Debian/Ubuntu ship MariaDB bound to 127.0.0.1 only — unreachable from a
# container. Detect that and offer to fix it.
ensure_db_reachable() {
    command -v ss >/dev/null 2>&1 || return 0
    listens=$(ss -ltnH 2>/dev/null | awk '$4 ~ /:3306$/ {print $4}')
    if [ -z "$listens" ]; then
        echo "Note: nothing is listening on port 3306 — if MariaDB has 'skip-networking' set, the container won't reach it."
        return 0
    fi
    printf '%s\n' "$listens" | grep -qvE '^(127\.0\.0\.1|\[::1\]):' && return 0

    CNF=/etc/mysql/mariadb.conf.d/50-server.cnf
    echo
    echo "MariaDB only listens on 127.0.0.1, so the app container can't reach it."
    if [ "$TTY" = 1 ] && [ -f "$CNF" ]; then
        ask "Set 'bind-address = 0.0.0.0' in $CNF and restart MariaDB now? [Y/n]: " yn
        case "$yn" in
            [nN]*) ;;
            *)
                as_root sed -i 's/^[[:space:]]*bind-address[[:space:]]*=.*/bind-address = 0.0.0.0/' "$CNF"
                if grep -q '^bind-address = 0.0.0.0' "$CNF"; then
                    as_root systemctl restart mariadb && echo "MariaDB restarted." && return 0
                fi
                ;;
        esac
    fi
    echo "Fix by hand: set 'bind-address = 0.0.0.0' in MariaDB's server config (usually $CNF),"
    echo "restart MariaDB, and make sure your firewall only allows port 3306 from Docker."
}

# ---- database wizard ---------------------------------------------------------
NEEDS_BOOTSTRAP=0
RUN_WIZARD=1
if [ -f .env ]; then
    RUN_WIZARD=0
    if [ "$TTY" = 1 ]; then
        ask ".env already exists. Run the database wizard again? [y/N]: " redo
        case "$redo" in
            [yY]*)
                cp .env ".env.bak.$(date +%Y%m%d-%H%M%S)"
                echo "Old .env kept as a .env.bak.* copy."
                RUN_WIZARD=1 ;;
            *) echo "Keeping .env." ;;
        esac
    else
        echo ".env already exists — keeping it."
    fi
fi

if [ "$RUN_WIZARD" = 1 ]; then
    echo
    echo "Database setup:"
    echo "  1) Install a bundled MariaDB container for me (easiest)"
    echo "  2) Use the MariaDB already installed on this server — create the database and user for me"
    echo "  3) Connect to a database I already have (already created, here or elsewhere)"
    echo "  4) Skip for now — I'll set it up later from the admin panel"
    ask "Choose [1/2/3/4]: " db_choice

    case "$db_choice" in
        1)
            echo
            ask_ident "Database name [open_feedback_forms]: " db_name open_feedback_forms
            ask_ident "Database user [off_app]: " db_user off_app
            ask_secret "Database password (blank = generate one): " db_pass
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
            ask_ident "Database name [open_feedback_forms]: " db_name open_feedback_forms
            ask_ident "Database user [off_app]: " db_user off_app
            ask_secret "Database password (blank = generate one): " db_pass
            [ -n "$db_pass" ] || db_pass=$(gen_pw)
            case "$db_pass" in *"'"*) die "Sorry — the password can't contain a single quote (')." ;; esac

            create_local_db "$db_name" "$db_user" "$db_pass"
            ensure_db_reachable

            env_set OFF_DB_HOST host.docker.internal
            env_set OFF_DB_PORT 3306
            env_set OFF_DB_USER "$db_user"
            env_set OFF_DB_PASSWORD "$db_pass"
            env_set OFF_DB_NAME "$db_name"
            NEEDS_BOOTSTRAP=1
            ;;
        3)
            echo
            echo "(If the database is on this same server, use host.docker.internal as the host.)"
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

# Don't just say "done" — confirm the app actually reached the database.
DB_OK=0
if [ "$NEEDS_BOOTSTRAP" = 1 ] && command -v curl >/dev/null 2>&1; then
    printf "Checking the database connection"
    i=0
    while [ $i -lt 20 ]; do
        case "$(curl -fs "$URL/status" 2>/dev/null || true)" in
            *'"dbConnected": true'*) DB_OK=1; break ;;
        esac
        printf "."; sleep 1; i=$((i + 1))
    done
    echo
    if [ "$DB_OK" = 1 ]; then
        echo "Database connected."
    else
        echo "The app is up but hasn't reached the database yet. See why with:  docker compose logs app"
        echo "(For a MariaDB on this server: it must listen on more than 127.0.0.1 — see the note above.)"
    fi
fi

echo
if [ "$RUN_WIZARD" = 0 ]; then
    echo "Updated. $URL"
elif [ "$NEEDS_BOOTSTRAP" = 1 ]; then
    echo "Done. Open $URL to create your admin account."
else
    echo "Done. Open $URL to create your admin account and connect the database."
fi
