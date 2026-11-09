#!/usr/bin/env bash
# Installs everything Linuxpod needs, then compiles the iPod helper.
set -e

echo "== Linuxpod setup =="

if command -v apt >/dev/null 2>&1; then
    echo "-- Installing system packages (apt, needs sudo) --"
    sudo apt update
    sudo apt install -y \
        python3 python3-pip python3-tk \
        libgpod-dev libgpod-common pkg-config build-essential libglib2.0-dev \
        udisks2 util-linux ffmpeg atomicparsley zenity
    echo ""
    echo "zenity gives the app your desktop's own native file/folder picker"
    echo "instead of Tkinter's generic one. If you're on KDE Plasma, kdialog"
    echo "(usually already installed) is used automatically instead - no"
    echo "extra setup needed either way. Without either, file dialogs still"
    echo "work, just with Tkinter's plainer look."
    echo ""
    echo "libgpod-common carries a udev rule that automatically reads a"
    echo "newer iPod's hardware ID (Nano video/3G and later, Classic, Touch)"
    echo "into a file on the device the first time it's plugged in after"
    echo "install - without it, syncing to those models fails with"
    echo "\"Couldn't find the iPod firewire ID\". If you installed this"
    echo "*after* already plugging your iPod in, unplug and replug it once"
    echo "so the udev rule can run."
else
    echo "This script only automates apt (Debian/Ubuntu/Mint/etc)."
    echo "On another distro, install the equivalents of:"
    echo "  python3, python3-pip, python3-tk (usually already present),"
    echo "  libgpod (dev headers), pkg-config, a C compiler, glib2 (dev headers)."
    echo "e.g. Fedora:  sudo dnf install python3-tkinter libgpod-devel pkgconf-pkg-config gcc glib2-devel ffmpeg atomicparsley zenity"
    echo "     Arch:    sudo pacman -S tk libgpod pkgconf base-devel glib2 ffmpeg atomicparsley zenity"
    echo ""
    echo "If you have a newer iPod (Nano video/3G+, Classic, Touch), also make"
    echo "sure your distro's libgpod package includes its udev rule and the"
    echo "ipod-read-sysinfo-extended tool (sometimes split into a separate"
    echo "package, e.g. Debian/Ubuntu's libgpod-common) - without it, syncing"
    echo "to those models fails with \"Couldn't find the iPod firewire ID\"."
    echo "See the README's Known Limitations for the manual fix if needed."
fi

echo "-- Installing Python packages --"
pip3 install --user -r "$(dirname "$0")/requirements.txt"

echo "-- Compiling the iPod helper --"
# -B: always rebuild. unzip keeps the zip's own file timestamps, which
# can look older than a helper built from a previous zip, so plain make
# could keep running the old helper against new Python code.
make -B -C "$(dirname "$0")/helper"

echo ""
echo "Setup complete. Run the app with:"
echo "    python3 $(dirname "$0")/src/main.py"
