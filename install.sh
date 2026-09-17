#!/bin/sh
# Open Feedback Forms — one-line installer AND updater.
#
#   sh -c "$(curl -fsSL https://raw.githubusercontent.com/cloudit24/open-feedback-forms/main/install.sh)"
#
# The same link works both ways, from any directory:
#   - No install on this server yet -> clone, ask the database questions,
#     build and start.
#   - Already installed -> find it (wherever it lives), take a safety backup,
#     pull the latest code and rebuild. .env, config.json, uploads and the
#     database are kept exactly as they are; no questions are asked.
# The admin account is always created through the web UI, never over a
# script prompt.
set -e

REPO_URL="https://github.com/cloudit24/open-feedback-forms.git"
DIR="open-feedback-forms"
DEFAULT_PROJECT="open-feedback-forms"
KEEP_BACKUPS=5

die() { echo "$*" >&2; exit 1; }
as_root() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi; }
vol_exists() { docker volume inspect "$1" >/dev/null 2>&1; }
env_get() { sed -n "s/^$1=//p" .env 2>/dev/null | tr -d "'\"" | tail -n 1; }

command -v docker >/dev/null 2>&1 || die "Docker is required: https://docs.docker.com/get-docker/"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (bundled with recent Docker Desktop/Engine)."

# ---- find an existing install -------------------------------------------------
# Checked in order: the current directory; the folder Docker recorded for this
# project's containers (so running the link from a different directory still
# finds it); ./open-feedback-forms; ~/open-feedback-forms.
find_existing() {
    if [ -f ./server.py ] && [ -f ./docker-compose.yml ]; then pwd; return; fi
    wd=$(docker ps -a --filter "label=com.docker.compose.project=$DEFAULT_PROJECT" \
            --format '{{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null | head -n 1)
    if [ -n "$wd" ] && [ -f "$wd/docker-compose.yml" ]; then echo "$wd"; return; fi
    for d in "./$DIR" "$HOME/$DIR"; do
        if [ -f "$d/docker-compose.yml" ] && [ -f "$d/server.py" ]; then (cd "$d" && pwd); return; fi
    done
}

PULL_OK=1
EXISTING_DIR=$(find_existing)
if [ -n "$EXISTING_DIR" ]; then
    cd "$EXISTING_DIR"
    echo "Found Open Feedback Forms in $EXISTING_DIR — pulling latest code..."
    if command -v git >/dev/null 2>&1 && [ -d .git ]; then
        git pull --ff-only || PULL_OK=0
    else
        PULL_OK=0
    fi
else
    command -v git >/dev/null 2>&1 || die "git is required: https://git-scm.com/downloads"
    [ -e "$DIR" ] && die "./$DIR exists but isn't an Open Feedback Forms checkout — move it aside and re-run."
    git clone "$REPO_URL" "$DIR"
    cd "$DIR"
fi

# Compose names volumes <project>_off_data / <project>_off_db_data. The
# project is pinned to "open-feedback-forms" in docker-compose.yml, but an
# older install may have run from a folder with another name — keep using
# that install's volumes rather than starting empty ones.
PROJECT=$(env_get COMPOSE_PROJECT_NAME)
[ -n "$PROJECT" ] || PROJECT=$(sed -n 's/^name:[[:space:]]*//p' docker-compose.yml | head -n 1)
[ -n "$PROJECT" ] || PROJECT=$DEFAULT_PROJECT
PIN_PROJECT=""
BASE=$(basename "$(pwd)")
if [ "$BASE" != "$PROJECT" ] && vol_exists "${BASE}_off_data" && ! vol_exists "${PROJECT}_off_data"; then
    PROJECT=$BASE
    PIN_PROJECT=$BASE
fi
HAS_APP_DATA=0; vol_exists "${PROJECT}_off_data" && HAS_APP_DATA=1
HAS_DB_DATA=0;  vol_exists "${PROJECT}_off_db_data" && HAS_DB_DATA=1

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
    # Ctrl-C in the middle of a password prompt must not leave the terminal
    # with echo switched off. INT/TERM still have to stop the script — a trap
    # that doesn't exit would let the install carry on after Ctrl-C.
    trap 'stty echo </dev/tty 2>/dev/null || true' EXIT
    trap 'exit 130' INT TERM
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

# ---- existing data, lost .env ------------------------------------------------
# The database volume exists but .env doesn't (checkout deleted or moved).
# Starting the wizard here would write NEW database passwords that don't match
# the existing database. config.json in the app volume already holds the real
# connection details, so rebuild .env from it instead.
recover_env() {
    vol_exists "${PROJECT}_off_data" || return 1
    echo "Existing data found but no .env — rebuilding .env from the saved settings..."
    recovered=$(docker run --rm -i -v "${PROJECT}_off_data":/data:ro python:3.13-slim python - <<'PY'
import json, secrets, sys
try:
    with open("/data/config.json", encoding="utf-8") as f:
        d = json.load(f).get("db") or {}
except Exception:
    sys.exit(1)
out = {}
if d.get("host"):
    out.update(OFF_DB_HOST=d["host"], OFF_DB_PORT=str(d.get("port") or 3306),
               OFF_DB_USER=d.get("user", ""), OFF_DB_PASSWORD=d.get("password", ""),
               OFF_DB_NAME=d.get("database", ""))
    if d["host"] == "db":
        # MariaDB ignores these once its data exists; they only need to be
        # present. The real user/password/database come from config.json.
        out.update(COMPOSE_PROFILES="bundled-db", DB_NAME=d.get("database", ""),
                   DB_USER=d.get("user", ""), DB_PASSWORD=d.get("password", ""),
                   DB_ROOT_PASSWORD=secrets.token_urlsafe(18),
                   OFF_WAIT_FOR_DB_HOST="db", OFF_WAIT_FOR_DB_PORT="3306")
for k, v in out.items():
    if "'" in v:
        sys.exit(2)
    print("%s='%s'" % (k, v))
PY
    ) || return 1
    { cat .env.example; [ -n "$recovered" ] && printf "%s\n" "$recovered"; } > .env
    echo "Rebuilt .env from config.json."
}

# ---- safety backup before an update ----------------------------------------------
# Updating never deletes data, but a copy costs seconds and makes any surprise
# recoverable. Keeps the newest $KEEP_BACKUPS; older ones are removed.
backup_before_update() {
    dest="backup/pre-update-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$dest"
    echo "Safety backup -> $dest"
    if vol_exists "${PROJECT}_off_data"; then
        docker run --rm -v "${PROJECT}_off_data":/from:ro -v "$(pwd)/$dest":/to alpine \
            sh -c "mkdir -p /to/off_data && cp -a /from/. /to/off_data/ && chown -R $(id -u):$(id -g) /to" \
            && echo "  settings + uploads saved" \
            || echo "  WARNING: could not copy settings/uploads (continuing — the update doesn't touch them)"
    fi
    if [ -n "$(docker compose ps -q --status running db 2>/dev/null)" ]; then
        if docker compose exec -T db sh -c \
                'exec mariadb-dump -u"$MARIADB_USER" -p"$MARIADB_PASSWORD" --single-transaction "$MARIADB_DATABASE"' \
                </dev/null >"$dest/database.sql" 2>"$dest/database-dump.log"; then
            rm -f "$dest/database-dump.log"
            echo "  database saved"
        else
            echo "  WARNING: database dump failed, see $dest/database-dump.log (continuing — the update doesn't touch the database)"
        fi
    fi
    ls -1d backup/pre-update-* 2>/dev/null | sort -r | tail -n +$((KEEP_BACKUPS + 1)) |
        while read -r old; do rm -rf "$old"; done
}

# ---- decide: new install, or update --------------------------------------------
MODE=new
[ ! -f .env ] && [ "$HAS_APP_DATA" = 1 ] && recover_env || true
[ ! -f .env ] && [ "$HAS_DB_DATA" = 1 ] && die "
Existing database data was found (volume ${PROJECT}_off_db_data), but no .env and
no saved settings to rebuild it from. Stopping without changing anything, so the
existing data is safe. Restore your .env into $(pwd), then run this again."

if [ -f .env ]; then
    if [ -n "$(env_get OFF_DB_HOST)" ] || [ "$HAS_DB_DATA" = 1 ]; then
        MODE=update
    elif [ "$TTY" = 1 ]; then
        # Installed earlier with the database skipped, and no bundled
        # database data exists — setting one up now can't overwrite anything.
        ask "Installed, but no database is configured yet. Set one up now? [Y/n]: " yn
        case "$yn" in [nN]*) MODE=update ;; *) MODE=configure ;; esac
    else
        MODE=update
    fi
fi

NEEDS_BOOTSTRAP=0
if [ "$MODE" = new ] || [ "$MODE" = configure ]; then
    [ -f .env ] && cp .env ".env.bak.$(date +%Y%m%d-%H%M%S)"
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
    # never leaves a half-written .env behind.
    { cat .env.example; printf "%s" "$ENV_EXTRA"; } > .env
    echo "Saved .env (keep that file private — it holds the database password)."
else
    echo "Existing install — updating only. Your forms, submissions, settings and database are kept."
    backup_before_update
fi

if [ -n "$PIN_PROJECT" ] && [ -z "$(env_get COMPOSE_PROJECT_NAME)" ]; then
    printf "COMPOSE_PROJECT_NAME='%s'\n" "$PIN_PROJECT" >> .env
fi

# ---- build & start -----------------------------------------------------------
# `up --build` replaces containers only; named volumes (the data) are never
# removed by it.
echo
echo "Building and starting containers..."
docker compose up -d --build --remove-orphans

if [ "$NEEDS_BOOTSTRAP" = 1 ]; then
    echo "Saving the database connection to config.json..."
    # `compose run` attaches stdin by default — under `curl | sh` that IS
    # this script, and it would swallow the rest of it. Give it nothing.
    docker compose run --rm -T app python docker/bootstrap.py </dev/null
    docker compose restart app
fi

ADMIN_PORT=$(env_get ADMIN_PORT)
URL="http://localhost:${ADMIN_PORT:-8080}/admin"

# Don't just say "done" — ask the running app what state it's actually in.
DB_STATE=unknown
if command -v curl >/dev/null 2>&1; then
    printf "Checking the app"
    i=0
    while [ $i -lt 30 ]; do
        status=$(curl -fs "$URL/status" 2>/dev/null || true)
        case "$status" in
            *'"dbConnected": true'*)   DB_STATE=connected; break ;;
            *'"dbConfigured": false'*) DB_STATE=unconfigured; break ;;
            *'"dbConfigured": true'*)  DB_STATE=unreachable ;;   # keep trying — it may still be starting
        esac
        printf "."; sleep 1; i=$((i + 1))
    done
    echo
    case "$DB_STATE" in
        connected)
            echo "Database connected." ;;
        unconfigured)
            echo "The app is running but has NO database configured."
            echo "Set it in $URL → Configuration → Database connection, or re-run this script." ;;
        unreachable)
            echo "The app is up and has a database configured, but can't reach it. See why with:  docker compose logs app"
            echo "(For a MariaDB on this server: it must listen on more than 127.0.0.1 — see the note above.)" ;;
        *)
            echo "The app isn't answering on $URL yet. Check:  docker compose ps   and   docker compose logs app" ;;
    esac
fi

echo
case "$MODE" in
    update)
        if [ "$PULL_OK" = 1 ]; then
            echo "Updated. Existing data kept. $URL"
        else
            echo "Rebuilt, but the code was NOT updated: git couldn't fast-forward in $(pwd)"
            echo "(usually a file there was edited by hand — check with: git -C \"$(pwd)\" status)."
            echo "Existing data kept. $URL"
        fi ;;
    *)
        if [ "$NEEDS_BOOTSTRAP" = 1 ]; then
            echo "Done. Open $URL to create your admin account."
        else
            echo "Done. Open $URL to create your admin account and connect the database."
        fi ;;
esac
