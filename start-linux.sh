#!/usr/bin/env bash
# Also accept `sh start-linux.sh` for compatibility with the original launcher.
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail

if [[ "$(uname -s)" != Linux ]]; then
    echo "此脚本用于 Linux；Windows 请运行 start-windows.bat。" >&2
    exit 1
fi

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd -- "$APP_DIR"
VENV_PYTHON="$APP_DIR/.venv/bin/python"
DEPENDENCY_STAMP="$APP_DIR/.venv/.hwmonitor-pyproject.toml"
# Keep launcher state separate from application settings and systemd state.
STATE_DIR="$APP_DIR/data/launcher"
PID_FILE="$STATE_DIR/hwmonitor.pid"
ARGS_FILE="$STATE_DIR/arguments"
STARTUP_LOG="$STATE_DIR/startup.log"
ACTION=start
case "${1:-}" in
    start|stop|status|restart|logs) ACTION="$1"; shift ;;
    --*|-h|'') ;;
    *) echo "用法：sh start-linux.sh [start|stop|status|restart|logs] [服务参数]" >&2; exit 1 ;;
esac
if [[ "$ACTION" == stop || "$ACTION" == status || "$ACTION" == logs ]] && (( $# )); then
    echo "$ACTION 不接受服务参数。" >&2
    exit 1
fi

ensure_environment() {
    local python_bin="${PYTHON:-python3}"
    if [[ ! -x "$VENV_PYTHON" ]]; then
        if ! command -v "$python_bin" >/dev/null 2>&1; then
            echo "请安装 Python 3.11+、venv 和 pip，或通过 PYTHON 指定解释器。" >&2
            return 1
        fi
        "$python_bin" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "需要 Python 3.11 或更高版本")'
        echo "首次启动：创建虚拟环境 .venv …"
        if ! "$python_bin" -m venv "$APP_DIR/.venv"; then
            echo "Ubuntu / Debian 可先执行：sudo apt install python3-venv python3-pip" >&2
            return 1
        fi
    fi
    if ! cmp -s "$APP_DIR/pyproject.toml" "$DEPENDENCY_STAMP"; then
        echo "正在安装或更新运行依赖（需要联网）…"
        "$VENV_PYTHON" -m pip install -e "$APP_DIR"
        cp "$APP_DIR/pyproject.toml" "$DEPENDENCY_STAMP"
    fi
}

# Diagnostics/help remain foreground commands and do not affect a running server.
for arg in "$@"; do
    if [[ "$arg" == --check || "$arg" == --help || "$arg" == -h ]]; then
        ensure_environment
        exec "$VENV_PYTHON" -m hwmonitor "$@"
    fi
done

umask 077
mkdir -p -- "$STATE_DIR"
if [[ "$ACTION" == logs ]]; then
    touch "$STARTUP_LOG"
    exec tail -n 50 -F "$STARTUP_LOG"
fi
if ! command -v flock >/dev/null 2>&1; then
    echo "未找到 flock，请先安装 util-linux。" >&2
    exit 1
fi
exec 9>"$STATE_DIR/control.lock"
if ! flock -n 9; then
    echo "另一个管理命令正在执行，请稍后重试。" >&2
    exit 1
fi

is_app_process() {
    local pid="$1" cwd executable index
    local -a argv
    [[ "$pid" =~ ^[1-9][0-9]*$ && "$pid" != 1 ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    cwd="$(readlink -f -- "/proc/$pid/cwd" 2>/dev/null)" || return 1
    [[ "$cwd" == "$APP_DIR" && -r "/proc/$pid/cmdline" ]] || return 1
    mapfile -d '' -t argv < "/proc/$pid/cmdline" 2>/dev/null || return 1
    (( ${#argv[@]} >= 3 )) || return 1
    executable="${argv[0]}"
    [[ "$executable" == /* ]] || executable="$cwd/$executable"
    [[ "$(readlink -f -- "$executable")" == "$(readlink -f -- "$VENV_PYTHON")" ]] || return 1
    for (( index=1; index < ${#argv[@]}-1; index++ )); do
        case "${argv[index]}" in
            -u|-B|-E|-s|-S|-I|-O|-OO) ;;
            -m) [[ "${argv[index+1]}" == hwmonitor ]]; return $? ;;
            *) return 1 ;;
        esac
    done
    return 1
}

find_app_pid() {
    local pid proc
    if [[ -f "$PID_FILE" ]]; then
        read -r pid < "$PID_FILE" || true
        if is_app_process "${pid:-}"; then printf '%s\n' "$pid"; return 0; fi
    fi
    # Recover after losing the PID file, including an old foreground launch.
    for proc in /proc/[0-9]*; do
        pid="${proc##*/}"
        if is_app_process "$pid"; then printf '%s\n' "$pid"; return 0; fi
    done
    return 1
}

stop_app() {
    local pid attempt
    if ! pid="$(find_app_pid)"; then
        rm -f -- "$PID_FILE"
        echo "硬件监控未运行。"
        return 0
    fi
    printf '%s\n' "$pid" > "$PID_FILE"
    echo "正在停止硬件监控，PID：$pid"
    kill -TERM "$pid"
    for (( attempt=0; attempt<30; attempt++ )); do
        if ! is_app_process "$pid"; then
            rm -f -- "$PID_FILE"
            echo "硬件监控已停止。"
            return 0
        fi
        sleep 1
    done
    echo "30 秒后仍未退出，已保留 PID；请检查 $STARTUP_LOG" >&2
    return 1
}

start_app() {
    local pid attempt option skip=false
    local -a saved=()
    if pid="$(find_app_pid)"; then
        printf '%s\n' "$pid" > "$PID_FILE"
        echo "硬件监控已运行，PID：$pid；无需重复启动。"
        return 0
    fi
    ensure_environment
    command -v nohup >/dev/null 2>&1 || { echo "未找到 nohup，请安装 coreutils。" >&2; return 1; }
    [[ ! -f "$STARTUP_LOG" ]] || mv -f -- "$STARTUP_LOG" "$STARTUP_LOG.previous"
    : > "$STARTUP_LOG"
    # The data endpoint is a setting now: a legacy --data-port is passed once, the service
    # moves it into settings.json, and it is not replayed so the management page stays in control.
    for option in "$@"; do
        if [[ "$skip" == true ]]; then skip=false; continue; fi
        case "$option" in
            --data-port) skip=true ;;
            --data-port=*) ;;
            *) saved+=("$option") ;;
        esac
    done
    # NUL separators preserve spaces in options; restart reuses the last options.
    printf '%s\0' "${saved[@]}" > "$ARGS_FILE"
    # Close the management lock in the child so later stop/status can acquire it.
    nohup "$VENV_PYTHON" -u -m hwmonitor --headless "$@" \
        >> "$STARTUP_LOG" 2>&1 < /dev/null 9>&- &
    pid=$!
    printf '%s\n' "$pid" > "$PID_FILE"
    echo "正在等待服务启动，PID：$pid …"
    for (( attempt=0; attempt<150; attempt++ )); do
        if grep -Fq '[HWMONITOR_READY]' "$STARTUP_LOG" && is_app_process "$pid"; then
            tail -n 5 "$STARTUP_LOG"
            echo "硬件监控已在后台运行，关闭终端后继续运行。"
            echo "查看日志：sh \"$APP_DIR/start-linux.sh\" logs"
            return 0
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            wait "$pid" 2>/dev/null || true
            rm -f -- "$PID_FILE"
            tail -n 50 "$STARTUP_LOG" >&2
            echo "启动失败，请检查上述日志。" >&2
            return 1
        fi
        sleep 0.2
    done
    tail -n 50 "$STARTUP_LOG" >&2
    echo "30 秒内未收到就绪提示，进程可能仍在初始化；请用 status 和 logs 检查。" >&2
    return 1
}

case "$ACTION" in
    stop) stop_app ;;
    status)
        if pid="$(find_app_pid)"; then
            printf '%s\n' "$pid" > "$PID_FILE"
            echo "硬件监控正在运行，PID：$pid"
            if [[ -r "/proc/$pid/cmdline" ]]; then
                mapfile -d '' -t process_args < "/proc/$pid/cmdline" || true
                printf '运行命令：'; printf ' %q' "${process_args[@]}"; printf '\n'
            fi
            echo "日志：$STARTUP_LOG"
        else
            rm -f -- "$PID_FILE"
            echo "硬件监控未运行。"
            exit 1
        fi
        ;;
    start|restart)
        options=(--host 0.0.0.0 --port 7076 "$@")
        if [[ "$ACTION" == restart ]]; then
            if (( $# == 0 )) && [[ -f "$ARGS_FILE" ]]; then
                mapfile -d '' -t options < "$ARGS_FILE"
            fi
            stop_app
        fi
        start_app "${options[@]}"
        ;;
esac
