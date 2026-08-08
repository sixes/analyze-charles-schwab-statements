#!/usr/bin/env bash
# Control the Schwab Statement Analyzer web app on 0.0.0.0:9388.
#
#   ./run.sh start | stop | restart | status | logs | initdb
#
# The password is read from $APP_PASSWORD, else from .env, else from
# .app_password (created on first start). Statements contain account numbers and
# a home address, so the app refuses to serve anything without it.
set -euo pipefail

cd "$(dirname "$0")"

PORT=9388
PID_FILE=.run/app.pid
LOG_FILE=.run/app.log
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
    python3 store.py initdb
}

inventory() {
    load_env
    python3 store.py inventory
}

case "${1:-start}" in
    start) start ;;
    stop) stop ;;
    restart) stop; start ;;
    status) status ;;
    logs) tail -n 100 -f "$LOG_FILE" ;;
    initdb) initdb ;;
    inventory) inventory ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs|initdb|inventory}" >&2
        exit 2
        ;;
esac
