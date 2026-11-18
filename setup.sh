#!/usr/bin/env bash
# Spejari setup for Debian, Ubuntu, Kali and Raspberry Pi OS.
#
#   ./setup.sh                      install packages, create .venv, install Python deps
#   ./setup.sh --no-apt             skip apt (only create the venv)
#   ./setup.sh --no-gui             CLI only: skip Tk / CustomTkinter (headless Pi Lite)
#   ./setup.sh --no-bluetooth       skip the Bluetooth packages (bleak, bluez)
#   ./setup.sh --adsb               also install RTL-SDR tools and an ADS-B decoder (readsb / dump1090)
#   ./setup.sh --configure-gpsd     additionally point gpsd at a detected u-blox dongle
#   ./setup.sh --configure-gpsd /dev/ttyACM0
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

DO_APT=1
WITH_GUI=1
WITH_BT=1
WITH_ADSB=0
CONFIGURE_GPSD=0
GPS_DEV=""
while [ $# -gt 0 ]; do
    case "$1" in
        --no-apt) DO_APT=0 ;;
        --no-gui) WITH_GUI=0 ;;
        --no-bluetooth) WITH_BT=0 ;;
        --adsb) WITH_ADSB=1 ;;
        --configure-gpsd)
            CONFIGURE_GPSD=1
            if [ $# -gt 1 ] && [[ "$2" == /dev/* ]]; then GPS_DEV="$2"; shift; fi
            ;;
        -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

say() { printf '\033[1;36m[spejari]\033[0m %s\n' "$*"; }
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null || { echo "Run as root or install sudo." >&2; exit 1; }
    SUDO="sudo"
fi

if ! command -v python3 >/dev/null; then
    echo "python3 not found" >&2; exit 1
fi
python3 - <<'PY' || { echo "Python 3.9 or newer is required." >&2; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 9) else 1)
PY

install_adsb_decoder() {
    # Debian / Ubuntu / Kali ship different decoders depending on the release; try them in order.
    for pkg in readsb dump1090-fa dump1090-mutability dump1090; do
        if apt-cache show "$pkg" >/dev/null 2>&1; then
            say "Installing ADS-B decoder package: $pkg"
            if $SUDO apt-get install -y "$pkg"; then
                # the package may start its own background service that would grab the dongle
                $SUDO systemctl disable --now "$pkg" >/dev/null 2>&1 || true
                return 0
            fi
        fi
    done
    say "No ADS-B decoder package in your apt sources - building readsb from source into ./tools/"
    $SUDO apt-get install -y git build-essential pkg-config libusb-1.0-0-dev librtlsdr-dev \
        libncurses-dev zlib1g-dev libzstd-dev
    mkdir -p tools
    rm -rf tools/readsb-src
    git clone --depth 1 https://github.com/wiedehopf/readsb tools/readsb-src
    make -C tools/readsb-src -j"$(nproc)" RTLSDR=yes
    cp tools/readsb-src/readsb tools/readsb
    say "Built ./tools/readsb (Spejari finds it automatically)"
}

if [ "$DO_APT" -eq 1 ]; then
    if command -v apt-get >/dev/null; then
        say "Installing system packages (iw, gpsd, python3-venv, ...)"
        $SUDO apt-get update
        $SUDO apt-get install -y python3 python3-venv python3-pip iw rfkill \
            wireless-regdb usbutils gpsd gpsd-clients
        if [ "$WITH_GUI" -eq 1 ]; then
            say "Installing GUI packages (python3-tk, xhost)"
            $SUDO apt-get install -y python3-tk x11-xserver-utils
        fi
        if [ "$WITH_BT" -eq 1 ]; then
            say "Installing Bluetooth packages (bluez) and enabling the bluetooth service"
            if $SUDO apt-get install -y bluez; then
                # Kali and some minimal images ship BlueZ disabled: without it every BLE scan fails with
                # "Unit dbus-org.bluez.service not found".
                command -v rfkill >/dev/null 2>&1 && $SUDO rfkill unblock bluetooth || true
                $SUDO systemctl enable --now bluetooth >/dev/null 2>&1 \
                    || say "Could not enable the bluetooth service - run: sudo systemctl enable --now bluetooth"
            else
                say "bluez could not be installed - Bluetooth LE war-driving will not work"
            fi
        fi
        if [ "$WITH_ADSB" -eq 1 ]; then
            say "Installing RTL-SDR tools"
            $SUDO apt-get install -y rtl-sdr librtlsdr0 usbutils
            # the DVB-T kernel driver claims the dongle before librtlsdr can use it
            printf 'blacklist dvb_usb_rtl28xxu\nblacklist rtl2832\nblacklist rtl2830\n' \
                | $SUDO tee /etc/modprobe.d/spejari-rtlsdr.conf >/dev/null
            $SUDO rmmod dvb_usb_rtl28xxu rtl2832 2>/dev/null || true
            install_adsb_decoder
        fi
    else
        say "apt-get not found - install iw, gpsd, python3-venv (and bluez / rtl-sdr / readsb if wanted) manually."
    fi
fi

say "Creating virtual environment (.venv)"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip >/dev/null
grep -v '^#' requirements.txt > .requirements-install.tmp
[ "$WITH_GUI" -eq 1 ] || { grep -vi customtkinter .requirements-install.tmp > .req2.tmp; mv .req2.tmp .requirements-install.tmp; }
[ "$WITH_BT" -eq 1 ] || { grep -vi bleak .requirements-install.tmp > .req2.tmp; mv .req2.tmp .requirements-install.tmp; }
.venv/bin/python -m pip install -r .requirements-install.tmp
rm -f .requirements-install.tmp

chmod +x run.sh setup.sh main.py
mkdir -p exports

if [ "$CONFIGURE_GPSD" -eq 1 ]; then
    if [ -z "$GPS_DEV" ]; then
        for dev in /dev/ttyACM* /dev/ttyUSB*; do
            [ -e "$dev" ] || continue
            vid="$(udevadm info -q property -n "$dev" 2>/dev/null | sed -n 's/^ID_VENDOR_ID=//p')"
            if [ "$vid" = "1546" ]; then GPS_DEV="$dev"; break; fi
        done
        [ -n "$GPS_DEV" ] || GPS_DEV="$(ls /dev/ttyACM* 2>/dev/null | head -n1 || true)"
    fi
    if [ -z "$GPS_DEV" ]; then
        say "No GPS device found - plug in the u-blox dongle and re-run with --configure-gpsd"
    else
        say "Configuring gpsd for $GPS_DEV"
        [ -f /etc/default/gpsd ] && $SUDO cp /etc/default/gpsd /etc/default/gpsd.spejari.bak
        $SUDO tee /etc/default/gpsd >/dev/null <<GPSD
# Written by Spejari setup.sh
START_DAEMON="true"
GPSD_OPTIONS="-n"
DEVICES="$GPS_DEV"
USBAUTO="true"
GPSD
        $SUDO systemctl enable gpsd.socket >/dev/null 2>&1 || true
        $SUDO systemctl restart gpsd.socket gpsd >/dev/null 2>&1 || true
        say "gpsd restarted. Check the fix with: gpsmon  or  cgps -s"
    fi
fi

say "Done. Start Spejari with:  ./run.sh   (--cli for the terminal, --gui for the window)"
say "No hardware? Try:  ./run.sh --simulate"
say "Tip: ./run.sh --list-adapters, --list-bluetooth and --list-sdr show what Spejari can see."
say "Uploads: ./run.sh --keys  stores your WiGLE / WDGWars API keys, then  ./run.sh --upload both"
