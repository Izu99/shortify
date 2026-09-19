#!/usr/bin/env bash
# Per-user install: no root, no dpkg, nothing written outside $HOME.
# Run again after a git pull to refresh; --uninstall removes everything it added.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor"

uninstall() {
  rm -f "$BIN/shortify" "$APPS/shortify.desktop"
  for n in 16 24 32 48 64 128 256; do
    rm -f "$ICONS/${n}x${n}/apps/shortify.png"
  done
  update-desktop-database "$APPS" 2>/dev/null || true
  gtk-update-icon-cache -f -t "$ICONS" 2>/dev/null || true
  echo "Shortify removed. Your settings in ~/.config/shortify were left alone."
  exit 0
}

[ "${1:-}" = "--uninstall" ] && uninstall

# --- dependency check, so failures are legible rather than a traceback ---
missing=()
command -v ffmpeg  >/dev/null || missing+=("ffmpeg")
command -v ffprobe >/dev/null || missing+=("ffmpeg (ffprobe)")
python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" 2>/dev/null \
  || missing+=("python3-gi gir1.2-gtk-3.0")
python3 -c "import cairo" 2>/dev/null || missing+=("python3-gi-cairo")
if [ ${#missing[@]} -gt 0 ]; then
  echo "Missing dependencies: ${missing[*]}"
  echo "Install them with:  sudo apt install ffmpeg python3-gi python3-gi-cairo gir1.2-gtk-3.0"
  exit 1
fi

if [ ! -x "$SRC/whisper.cpp/build/bin/whisper-cli" ]; then
  echo "Speech engine not built yet. Run ./setup.sh first."
  exit 1
fi

mkdir -p "$BIN" "$APPS"

# --- launcher: runs from the source tree, so a git pull updates the app ---
cat > "$BIN/shortify" <<LAUNCH
#!/usr/bin/env bash
exec python3 "$SRC/gui/ui.py" "\$@"
LAUNCH
chmod +x "$BIN/shortify"

# --- icons ---
for n in 16 24 32 48 64 128 256; do
  mkdir -p "$ICONS/${n}x${n}/apps"
  cp "$SRC/packaging/icons/shortify-${n}.png" "$ICONS/${n}x${n}/apps/shortify.png"
done

# --- menu entry ---
cat > "$APPS/shortify.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Shortify
GenericName=Caption Burner
Comment=Burn one-word-at-a-time captions onto vertical video
Exec=$BIN/shortify %f
Icon=shortify
Terminal=false
Categories=AudioVideo;Video;AudioVideoEditing;
MimeType=video/mp4;video/quicktime;video/x-matroska;video/webm;video/x-msvideo;
Keywords=caption;subtitle;shorts;video;transcribe;
StartupNotify=true
DESK

update-desktop-database "$APPS" 2>/dev/null || true
gtk-update-icon-cache -f -t "$ICONS" 2>/dev/null || true

echo "Installed."
echo "  command : shortify [video]"
echo "  menu    : Shortify (under Sound & Video)"
echo "  files   : $BIN/shortify, $APPS/shortify.desktop"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo; echo "NOTE: $BIN is not on your PATH. Add it to ~/.zshrc:"; echo "  export PATH=\"\$HOME/.local/bin:\$PATH\"";;
esac
