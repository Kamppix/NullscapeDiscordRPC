#!/usr/bin/env bash
# Linux build: output/<APP_NAME>-<version>-<arch>.AppImage
# Needs Python 3 with venv; the build tools go in a virtual environment in output/venv-linux, and
# appimagetool is downloaded to output/tools/ the first time.
set -euo pipefail
cd "$(dirname "$0")"
root="$PWD"

# Don't leave __pycache__ folders next to the sources
export PYTHONDONTWRITEBYTECODE=1

# Everything the build produces goes in output/ (scratch files, including the downloaded game icon,
# in output/temp/, deleted at the end)
out="$root/output"
build_temp="$out/temp"
venv="$out/venv-linux"
tools="$out/tools"
python="${PYTHON:-python3}"

# Names and version come from src/common.py (single source of truth)
mapfile -t names < <(PYTHONPATH="$root/src" "$python" -c \
    "import common as c; print(c.APP_NAME, c.LINUX_NAME, c.APP_ID, c.APP_VERSION, sep='\n')")
app_name=${names[0]} linux_name=${names[1]} app_id=${names[2]} version=${names[3]}

# Architecture of the Python you build with (the program inherits it), as AppImage names it
case "$(uname -m)" in
    x86_64 | amd64) arch=x86_64 ;;
    aarch64 | arm64) arch=aarch64 ;;
    *) arch=$(uname -m) ;;
esac

appimage="$app_name-$version-$arch.AppImage"
echo "Building version $version ($arch) -> output/$appimage"

cleanup() { rm -rf "$build_temp"; find "$root/src" -name __pycache__ -type d -prune -exec rm -rf {} +; }
trap 'echo; echo "Build failed - see the messages above."; cleanup' ERR
mkdir -p "$out" "$tools"
rm -rf "$build_temp"
mkdir -p "$build_temp"

echo "Installing build dependencies..."
[[ -x "$venv/bin/python" ]] || "$python" -m venv "$venv"
"$venv/bin/python" -m pip install --quiet --upgrade pip pyinstaller pypresence psutil requests

appimagetool="$tools/appimagetool-$arch.AppImage"
if [[ ! -x $appimagetool ]]; then
    echo "Downloading appimagetool..."
    "$venv/bin/python" -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" \
        "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$arch.AppImage" \
        "$appimagetool.part"
    chmod 755 "$appimagetool.part"
    mv "$appimagetool.part" "$appimagetool"
fi

echo "Fetching the latest game icon..."
# Only needed during the build (it goes into the AppImage); use fetch_icon.sh to get a copy to keep
(cd "$build_temp" && "$venv/bin/python" "$root/src/main.py" --fetch-icon)

echo "Building $linux_name (the app)..."
# Not --onefile: inside the AppImage the program runs straight from the mounted image, nothing is
# unpacked anywhere
"$venv/bin/python" -m PyInstaller --noconfirm --clean --log-level WARN --name "$linux_name" \
    --distpath "$build_temp/dist" --workpath "$build_temp/build" --specpath "$build_temp" \
    --exclude-module tkinter --exclude-module PIL "$root/src/main.py"

echo "Packing the AppImage..."
appdir="$build_temp/AppDir"
mkdir -p "$appdir/usr/lib"
cp -a "$build_temp/dist/$linux_name" "$appdir/usr/lib/$linux_name"
cat > "$appdir/AppRun" <<EOF
#!/bin/sh
# Starts the program inside the AppImage
HERE="\$(dirname "\$(readlink -f "\$0")")"
exec "\$HERE/usr/lib/$linux_name/$linux_name" "\$@"
EOF
chmod 755 "$appdir/AppRun"
install -m 644 "$build_temp/icon.png" "$appdir/$app_id.png"
ln -s "$app_id.png" "$appdir/.DirIcon"
# The same menu entry --integrate writes, but naming the program instead of an AppImage path
PYTHONPATH="$root/src" "$venv/bin/python" -c \
    "import sys, system; sys.stdout.write(system.desktop_entry([sys.argv[1]]))" "$linux_name" \
    > "$appdir/$app_id.desktop"
rm -f "$out/$appimage"
# Extract-and-run: appimagetool is an AppImage itself, and this also works without FUSE (containers)
ARCH="$arch" APPIMAGE_EXTRACT_AND_RUN=1 "$appimagetool" --no-appstream "$appdir" "$out/$appimage"

echo "Cleaning up..."
trap - ERR
cleanup

echo
echo "Build finished successfully!  >>  output/$appimage"
