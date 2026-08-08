#!/usr/bin/env bash
# Control the Schwab Statement Analyzer web app on 0.0.0.0:9388.
#
#   ./run.sh start | stop | restart | status | logs | initdb | inventory
#   ./run.sh ingest [--days N] [--dry-run]   pull new trade confirmations
#   ./run.sh cron-install                    print the crontab line to add
#
# The password is read from $APP_PASSWORD, else from .env, else from
# .app_password (created on first start). Statements contain account numbers and
# a home address, so the app refuses to serve anything without it.
set -euo pipefail

cd "$(dirname "$0")"

PORT=9388
PID_FILE=.run/app.pid
LOG_FILE=.run/app.log
LOCK_FILE=.run/ingest.lock
PASS_FILE=.app_password
ENV_FILE=.env

# DATABASE_URL and APP_PASSWORD live in .env so they never reach the command
# line or the process list of other users.
load_env() {
    [[ -f "$ENV_FILE" ]] || return 0
    chmod 600 "$ENV_FILE"
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
}

load_password() {
    if [[ -n "${APP_PASSWORD:-}" ]]; then
        return
    fi
    if [[ ! -f "$PASS_FILE" ]]; then
        umask 077
        python3 -c 'import secrets; print(secrets.token_urlsafe(18))' > "$PASS_FILE"
        echo "Generated a password in $PASS_FILE:"
        echo "  $(cat "$PASS_FILE")"
    fi
    chmod 600 "$PASS_FILE"
    APP_PASSWORD="$(cat "$PASS_FILE")"
    export APP_PASSWORD
}

running_pid() {
    [[ -f "$PID_FILE" ]] || return 1
    local pid
    pid="$(cat "$PID_FILE")"
    kill -0 "$pid" 2>/dev/null || return 1
    echo "$pid"
}

start() {
    if pid="$(running_pid)"; then
        echo "Already running (pid $pid) on port $PORT."
        return 0
    fi
    load_env
    load_password
    mkdir -p "$(dirname "$PID_FILE")"
    nohup streamlit run app.py >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 2
    if pid="$(running_pid)"; then
        echo "Started (pid $pid): http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT"
        echo "Reachable from any host that can route to this machine. Firewall the port."
    else
        echo "Failed to start. Last lines of $LOG_FILE:"
        tail -n 20 "$LOG_FILE"
        return 1
    fi
}

stop() {
    if ! pid="$(running_pid)"; then
        echo "Not running."
        rm -f "$PID_FILE"
        return 0
    fi
    kill "$pid"
    for _ in $(seq 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "Did not exit on SIGTERM, sending SIGKILL."
        kill -9 "$pid"
    fi
    rm -f "$PID_FILE"
    echo "Stopped."
}

status() {
    if pid="$(running_pid)"; then
        echo "Running (pid $pid) on port $PORT."
    else
        echo "Not running."
        return 1
    fi
}

initdb() {
    load_env
    python3 -m schwab initdb
}

inventory() {
    load_env
    python3 -m schwab inventory
}

# Cron fires this every few minutes. flock keeps a slow IMAP round trip from
# overlapping the next tick, which would try to store the same confirm twice.
ingest() {
    load_env
    mkdir -p "$(dirname "$LOCK_FILE")"
    # Cron appends to .run/ingest.log, so each line carries its own timestamp -
    # a bare summary line is otherwise indistinguishable from the tick before it.
    # pipefail is set, so ingest's exit code still reaches cron.
    flock -n "$LOCK_FILE" python3 -u -m schwab ingest "$@" 2>&1 \
        | awk '{ print strftime("%Y-%m-%d %H:%M:%S"), $0; fflush() }'
}

cron_install() {
    echo "Add this line with 'crontab -e' (not installed automatically):"
    echo
    echo "*/5 * * * * cd $PWD && ./run.sh ingest >> .run/ingest.log 2>&1"
    echo
    echo "It runs under flock, so overlapping ticks exit immediately."
    echo "A notification is sent for every confirmation processed, and stays silent"
    echo "on a tick that finds nothing new."
}

case "${1:-start}" in
    start) start ;;
    stop) stop ;;
    restart) stop; start ;;
    status) status ;;
    logs) tail -n 100 -f "$LOG_FILE" ;;
    initdb) initdb ;;
    inventory) inventory ;;
    ingest) shift; ingest "$@" ;;
    cron-install) cron_install ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs|initdb|inventory|ingest|cron-install}" >&2
        exit 2
        ;;
esac
