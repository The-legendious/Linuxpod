#!/usr/bin/env bash
# Builds Linuxpod-x86_64.AppImage from this source tree.
# Run on Ubuntu 24.04 (or newer) with: python3.12 python3-tk python3-pil
# python3-pil.imagetk (or pip Pillow) python3-mutagen libgpod-dev pkg-config build-essential,
# plus appimagetool on PATH (or APPIMAGETOOL=/path/to/appimagetool).
# The AppImage bundles Python, Tk, Pillow, mutagen and the iPod helper;
# ffmpeg/AtomicParsley (video), udisks2 and zenity stay optional host tools.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-/usr/bin/python3.12}"
PYVER="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
BUILD="${BUILD_DIR:-$ROOT/build/appimage}"
APPDIR="$BUILD/Linuxpod.AppDir"
OUT="${OUT:-$ROOT/build/Linuxpod-x86_64.AppImage}"
TOOL="${APPIMAGETOOL:-appimagetool}"

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib" "$APPDIR/usr/share" "$APPDIR/opt/linuxpod/helper/lib"

echo "== Python $PYVER =="
cp "$PY" "$APPDIR/usr/bin/python$PYVER"
rsync -a --exclude test --exclude idlelib --exclude lib2to3 --exclude ensurepip \
      --exclude '__pycache__' --exclude 'config-*' --exclude 'site-packages' --exclude 'dist-packages' \
      "/usr/lib/python$PYVER/" "$APPDIR/usr/lib/python$PYVER/"
SITE="$APPDIR/usr/lib/python3/dist-packages"
mkdir -p "$SITE"
"$PY" - "$SITE" <<'PYEOF'
import os, shutil, sys
import PIL, mutagen
from PIL import ImageTk  # noqa: F401 - album art needs it; fail the build if missing (Ubuntu: python3-pil.imagetk)
dest = sys.argv[1]
for mod in (PIL, mutagen):
    src = os.path.dirname(mod.__file__)
    shutil.copytree(src, os.path.join(dest, os.path.basename(src)),
                    ignore=shutil.ignore_patterns("__pycache__"))
libs = os.path.join(os.path.dirname(os.path.dirname(PIL.__file__)), "pillow.libs")
if os.path.isdir(libs):
    shutil.copytree(libs, os.path.join(dest, "pillow.libs"))
PYEOF

# Libraries the host may not have. Core system libraries (glibc, X11,
# xcb, fontconfig, freetype, OpenSSL...) come from the host so fonts and
# the display work like any other app there.
EXCLUDE='^(libc|libm|libdl|librt|libpthread|libresolv|libutil|ld-linux.*|libX11|libX11-xcb|libxcb.*|libXau|libXdmcp|libXext|libfontconfig|libfreetype|libz|libstdc\+\+|libgcc_s|libGL.*|libssl|libcrypto|libcrypt|libdb-.*|libreadline|libncursesw|libpanelw|libtinfo|libharfbuzz|libglib-2\.0|libgobject-2\.0|libgio-2\.0|libgmodule-2\.0|libgdk_pixbuf-2\.0|libmount|libselinux|libblkid|libpcre2.*|libpng16|libjpeg|libbrotli.*|libusb-1\.0)\.so'
copy_deps() {  # copy_deps <dest dir> <elf files...>
    local dest="$1"; shift
    ldd "$@" 2>/dev/null | awk '/=> \//{print $3}' | sort -u | while read -r lib; do
        name="$(basename "$lib")"
        if [[ ! "$name" =~ $EXCLUDE ]] && [ ! -e "$dest/$name" ]; then
            cp -L "$lib" "$dest/$name"
        fi
    done
}
copy_deps "$APPDIR/usr/lib" "$APPDIR/usr/bin/python$PYVER" "$APPDIR/usr/lib/python$PYVER"/lib-dynload/*.so
copy_deps "$APPDIR/usr/lib" "$APPDIR/usr/lib"/*.so*   # second level (BLT, Xft...)
cp -r /usr/share/tcltk "$APPDIR/usr/share/tcltk"

# Child processes (zenity, ffmpeg, udisksctl...) must not inherit our
# library path.
cat > "$APPDIR/usr/lib/python$PYVER/sitecustomize.py" <<'PYEOF'
import os
_orig = os.environ.pop("MM_ORIG_LD_LIBRARY_PATH", None)
if os.environ.get("APPDIR"):
    if _orig:
        os.environ["LD_LIBRARY_PATH"] = _orig
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)
PYEOF

echo "== App =="
rsync -a --exclude __pycache__ "$ROOT/src/" "$APPDIR/opt/linuxpod/src/"
cp "$ROOT/README.md" "$APPDIR/opt/linuxpod/"

echo "== iPod helper =="
make -B -C "$ROOT/helper" >/dev/null
cp "$ROOT/helper/ipod_helper" "$APPDIR/opt/linuxpod/helper/ipod_helper.bin"
copy_deps "$APPDIR/opt/linuxpod/helper/lib" "$APPDIR/opt/linuxpod/helper/ipod_helper.bin"
cat > "$APPDIR/opt/linuxpod/helper/ipod_helper" <<'SHEOF'
#!/bin/sh
here="$(dirname "$(readlink -f "$0")")"
LD_LIBRARY_PATH="$here/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" exec "$here/ipod_helper.bin" "$@"
SHEOF
chmod +x "$APPDIR/opt/linuxpod/helper/ipod_helper"

echo "== Precompiling =="
"$APPDIR/usr/bin/python$PYVER" -s -m compileall -q -j0 "$APPDIR/usr/lib" "$APPDIR/opt/linuxpod/src" >/dev/null || true

cat > "$APPDIR/AppRun" <<SHEOF
#!/bin/sh
HERE="\$(dirname "\$(readlink -f "\$0")")"
export APPDIR="\${APPDIR:-\$HERE}"
export MM_ORIG_LD_LIBRARY_PATH="\${LD_LIBRARY_PATH:-}"
export LD_LIBRARY_PATH="\$HERE/usr/lib\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
export TCL_LIBRARY="\$HERE/usr/share/tcltk/tcl8.6"
export TK_LIBRARY="\$HERE/usr/share/tcltk/tk8.6"
exec "\$HERE/usr/bin/python$PYVER" -s -E "\$HERE/opt/linuxpod/src/main.py" "\$@"
SHEOF
chmod +x "$APPDIR/AppRun"

cat > "$APPDIR/linuxpod.desktop" <<'DEOF'
[Desktop Entry]
Type=Application
Name=Linuxpod
StartupWMClass=Linuxpod
Comment=Manage your music library and sync it to a click-wheel iPod
Exec=AppRun
Icon=linuxpod
Categories=AudioVideo;Audio;
Terminal=false
DEOF
"$PY" - "$APPDIR/linuxpod.png" <<'PYEOF'
import sys
from PIL import Image, ImageDraw
S = 4; W = 256 * S
img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
bw, bh = int(W * 0.6), int(W * 0.92)
x0, y0 = (W - bw) // 2, (W - bh) // 2
d.rounded_rectangle([x0, y0, x0 + bw, y0 + bh], int(bw * 0.16), fill=(229, 229, 234, 255),
                    outline=(174, 174, 178, 255), width=3 * S)
m = int(bw * 0.12)
d.rounded_rectangle([x0 + m, y0 + m, x0 + bw - m, y0 + m + int(bh * 0.36)], 6 * S, fill=(10, 103, 229, 255))
cx, cy, r = W // 2, y0 + int(bh * 0.71), int(bw * 0.33)
d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 255), outline=(199, 199, 204, 255), width=2 * S)
r2 = int(r * 0.38)
d.ellipse([cx - r2, cy - r2, cx + r2, cy + r2], fill=(199, 199, 204, 255))
img.resize((256, 256), Image.LANCZOS).save(sys.argv[1])
PYEOF
ln -sf linuxpod.png "$APPDIR/.DirIcon"

echo "== Packing =="
mkdir -p "$(dirname "$OUT")"
ARCH=x86_64 "$TOOL" --no-appstream "$APPDIR" "$OUT"
echo "Built $OUT"
