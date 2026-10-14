#!/usr/bin/env bash
# Control the Schwab Statement Analyzer web app on 0.0.0.0:9388.
#
#   ./run.sh start | stop | restart | status | logs | initdb | inventory
#   ./run.sh ingest [--days N] [--dry-run]   pull new trade confirmations
#   ./run.sh monitor [--dry-run]             alert on positions past a price band
#   ./run.sh cron-install                    print the crontab line to add
#
# The password is read from $APP_PASSWORD, else from .env, else from
# .app_password (created on first start). Statements contain account numbers and
# a home address, so the app refuses to serve anything without it.
set -euo pipefail

cd "$(dirname "$0")"

MONITOR_PYTHON=python3
if [[ -x .venv/bin/python3 ]]; then
    MONITOR_PYTHON="$PWD/.venv/bin/python3"
fi

PORT=9388
PID_FILE=.run/app.pid
LOG_FILE=.run/app.log
LOCK_FILE=.run/ingest.lock
MONITOR_LOCK=.run/monitor.lock
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

# Cron fires this every ten minutes through the US session. Its own lock file, not
# the ingest one: the two passes are independent, and a slow quote round trip must
# not make an ingest tick skip.
monitor() {
    load_env
    mkdir -p "$(dirname "$MONITOR_LOCK")"
    flock -n "$MONITOR_LOCK" "$MONITOR_PYTHON" -u -m schwab monitor "$@" 2>&1 \
        | awk '{ print strftime("%Y-%m-%d %H:%M:%S"), $0; fflush() }'
}

news() {
    local argument log=.run/news.log
    for argument in "$@"; do
        if [[ "$argument" == --dry-run ]]; then
            load_env
            "$MONITOR_PYTHON" -B -u -m schwab news "$@" 2>&1 \
                | awk '{ print strftime("%Y-%m-%d %H:%M:%S"), $0; fflush() }'
            return
        fi
    done
    umask 077
    if [[ -L .run || ( -e .run && ! -d .run ) || -L "$log" || ( -e "$log" && ! -f "$log" ) ]]; then
        printf '%s\n' 'News failed: runtime directory and log must not be symlinks or special files.' >&2
        return 1
    fi
    mkdir -p .run
    : >> "$log"
    chmod 600 "$log"
    # Cron already redirects stdout here; do not append the same lines twice.
    if [[ /dev/fd/1 -ef "$log" ]]; then log=; fi
    { load_env; "$MONITOR_PYTHON" -B -u -m schwab news "$@"; } 2>&1 \
        | awk -v log_path="$log" '{
            line = strftime("%Y-%m-%d %H:%M:%S") " " $0
            if (log_path != "") { print line >> log_path; fflush(log_path) }
            print line; fflush()
        }'
}

cron_install() {
    echo "Add these lines with 'crontab -e' (not installed automatically):"
    echo
    echo "# Trade confirmations, every five minutes"
    echo "*/5 * * * * cd $PWD && ./run.sh ingest >> .run/ingest.log 2>&1"
    echo
    echo "# Price-band alerts, every ten minutes through the US session."
    echo "# The session runs 21:30-04:00 local, so it crosses midnight: the evening half"
    echo "# is Mon-Fri and the morning half is Tue-Sat, which is the same five sessions."
    echo "30,40,50 21 * * 1-5 cd $PWD && ./run.sh monitor >> .run/monitor.log 2>&1"
    echo "*/10 22,23 * * 1-5 cd $PWD && ./run.sh monitor >> .run/monitor.log 2>&1"
    echo "*/10 0-3 * * 2-6 cd $PWD && ./run.sh monitor >> .run/monitor.log 2>&1"
    echo "0 4 * * 2-6 cd $PWD && ./run.sh monitor >> .run/monitor.log 2>&1"
    echo
    echo "Both run under flock, so overlapping ticks exit immediately."
    echo "ingest notifies for every confirmation processed and stays silent on a tick"
    echo "that finds nothing new. monitor mails a position only when it reaches a price"
    echo "band it has not reported before, so most ticks send nothing."
    echo
    echo "The window is written for daylight saving time. When New York moves to EST in"
    echo "November the session is 22:30-05:00 local, so shift the hours by one."
    echo
    echo "# Daily Sonus news, including weekends; requires verified news-profile login."
    echo "# Scheduler timezone must be Asia/Shanghai (not merely command-level TZ)."
    echo "# If using CRON_TZ, restore its previous scope before other jobs."
    echo "# schwab-news-daily"
    printf '0 20 * * * umask 077; "%s/run" news >> "%s/.run/news.log" 2>&1\n' "$PWD" "$PWD"
    echo "News emails only; it keeps a lock and delivery timestamp, not report copies."
    echo "Existing news-profile login is reused unattended; research consumes Qoder usage/credits."
    echo "No monetary cap. Activate only after isolated Sonus/web-only runtime verification."
}

usage() {
    printf '%s\n' \
        "Usage: $0 [command]" \
        "       $0 {-h|--help|help [command]}" \
        "       $0 <command> {-h|--help}" \
        "" \
        "Commands (default: start):" \
        "  start         Start the password-protected web app on port $PORT." \
        "  stop          Stop the web app and remove its PID file." \
        "  restart       Stop, then start the web app." \
        "  status        Check whether the web app is running." \
        "  logs          Follow the web app log (last 100 lines first)." \
        "  initdb        Create the application database and schema." \
        "  inventory     Display stored data and reported alert bands." \
        "  ingest        Fetch and store trade confirmations; may send email." \
        "  monitor       Refresh position quotes and send new alerts." \
        "  news          Email a daily Sonus briefing without saving report copies." \
        "  cron-install  Print suggested crontab entries; install nothing." \
        "" \
        "Use help <command> for usage, options and effects." \
        "Help requires no credentials and does not load .env or change state."
}

usage_error() {
    printf 'Error: %s\n' "$1" >&2
    usage >&2
    exit 2
}

command_help() {
    local command="$1" effects
    case "$command" in
        start)
            effects="Start the web app on 0.0.0.0:$PORT. Loads .env, secures credential files,
creates a password if needed, and writes .run PID/log files. Firewall the port."
            ;;
        stop)
            effects="Stop the web app (SIGTERM, then SIGKILL if needed) and remove its PID file."
            ;;
        restart)
            effects="Stop, then start the web app. Loads .env, secures credential files,
creates a password if needed, and updates .run PID/log files."
            ;;
        status)
            effects="Check the PID file and process; change nothing. Exit 1 if not running."
            ;;
        logs)
            effects="Show the last 100 web app log lines, then follow new lines until Ctrl-C.
Reads .run/app.log; does not start the app."
            ;;
        initdb)
            effects="Load .env (chmod 600) and create the application's database and schema."
            ;;
        inventory)
            effects="Load .env (chmod 600) and read stored data and reported alert bands
from the application database; does not start the app."
            ;;
        cron-install)
            effects="Print suggested ingest/monitor/news crontab entries and scheduling notes.
Does not install or modify cron, load .env, or start any job."
            ;;
        ingest|monitor|news)
            printf 'Usage: %s %s [options]\n' "$0" "$command"
            if [[ "$command" == ingest ]]; then
                printf '%s\n' 'Fetch trade confirmations. Normal runs load .env (chmod 600), create' \
                    '.run, acquire the ingest lock, write to the database, refresh quotes' \
                    'and may send email. Help performs none of these actions.'
            elif [[ "$command" == news ]]; then
                printf '%s\n' 'News runs load .env (chmod 600), read positions, and use paid Sonus web' \
                    'research with underlying tickers, position categories and watchlist symbols.' \
                    'Briefings are emailed to GMAIL_TO without report copies. Help does none of this.'
            else
                printf '%s\n' 'Monitor positions. Normal runs load .env (chmod 600), create .run,' \
                    'acquire the monitor lock, fetch quotes, write to the database and' \
                    'may send email. Help performs none of these actions.'
            fi
            if [[ "$command" == monitor || "$command" == news ]]; then
                "$MONITOR_PYTHON" -B -m schwab "$command" --help
            else
                python3 -B -m schwab ingest --help
            fi
            return
            ;;
        *) usage_error "Unknown command: $command" ;;
    esac
    printf 'Usage: %s %s\n\n%s\n\nOptions: -h, --help (show help only). No other arguments accepted.\n' \
        "$0" "$command" "$effects"
}

# Help must bypass credentials, locks, database and process-control side effects.
command="${1-start}"
case "$command" in
    -h|--help)
        [[ $# -eq 1 ]] || usage_error "Help accepts no extra arguments."
        usage
        exit 0
        ;;
    help)
        [[ $# -le 2 ]] || usage_error "Use help [command]."
        if [[ $# -eq 1 ]]; then usage; else command_help "$2"; fi
        exit 0
        ;;
    start|stop|restart|status|logs|initdb|inventory|cron-install)
        if [[ $# -eq 2 && ( "$2" == -h || "$2" == --help ) ]]; then
            command_help "$command"
            exit 0
        fi
        [[ $# -le 1 ]] || usage_error "$command accepts no arguments (except -h/--help)."
        ;;
    ingest|monitor|news)
        for argument in "${@:2}"; do
            if [[ "$argument" == -h || "$argument" == --help ]]; then
                command_help "$command"
                exit 0
            fi
        done
        ;;
    *) usage_error "Unknown command: $command" ;;
esac

case "$command" in
    start) start ;;
    stop) stop ;;
    restart) stop; start ;;
    status) status ;;
    logs) tail -n 100 -f "$LOG_FILE" ;;
    initdb) initdb ;;
    inventory) inventory ;;
    ingest) shift; ingest "$@" ;;
    monitor) shift; monitor "$@" ;;
    news) shift; news "$@" ;;
    cron-install) cron_install ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs|initdb|inventory|ingest|monitor|news|cron-install}" >&2
        exit 2
        ;;
esac
