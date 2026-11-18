#!/usr/bin/env bash
# Launch Spejari with the right interpreter and, when needed, root privileges.
#   ./run.sh               choose CLI or GUI at start-up
#   ./run.sh --cli         terminal dashboard
#   ./run.sh --gui         desktop window (CustomTkinter)
#   ./run.sh --mode ble    Bluetooth LE only (also: wifi, adsb, wifi+ble, all)
#   ./run.sh --simulate    try it without any hardware (no root needed)
cd "$(dirname "$(readlink -f "$0")")" || exit 1
PY=".venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

no_root=0
want_cli=0
for arg in "$@"; do
    case "$arg" in
        --simulate|--simulate=*) no_root=1 ;;
        # helpers that need no special rights
        -h|--help|-V|--version|--list-gps|--list-sdr|--list-bluetooth|--keys|--test-keys|--upload|--upload=*|--recover)
            no_root=1 ;;
        --cli) want_cli=1 ;;
    esac
done

if [ "$(id -u)" -ne 0 ] && [ "$no_root" -eq 0 ]; then
    # Wi-Fi scanning needs root (BLE: root lets Spejari start BlueZ and power adapters on).
    # The GUI then runs as root: allow root to use this desktop session.
    if [ "$want_cli" -eq 0 ] && [ -n "${DISPLAY:-}" ] && command -v xhost >/dev/null 2>&1; then
        xhost +SI:localuser:root >/dev/null 2>&1 || true
    fi
    exec sudo --preserve-env=DISPLAY,XAUTHORITY,WAYLAND_DISPLAY,XDG_RUNTIME_DIR,DBUS_SESSION_BUS_ADDRESS,WIGLE_API_NAME,WIGLE_API_TOKEN,WIGLE_API_ENCODED,WDGWARS_API_KEY \
        "$PY" main.py "$@"
fi
exec "$PY" main.py "$@"
