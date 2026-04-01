#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Claude Status — installer ==="
echo ""

# ── System packages ────────────────────────────────────────────────────────────
echo "Installing system packages…"
sudo apt-get install -y \
    gir1.2-appindicator3-0.1 \
    gir1.2-gtk-3.0 \
    python3-gi \
    python3-gi-cairo \
    python3-cairo \
    libappindicator3-1

# ── Enable AppIndicator GNOME extension (required on GNOME 40+) ───────────────
echo ""
echo "Enabling AppIndicator GNOME extension…"
# Try the Ubuntu/Pop!_OS extension ID first, then the community one
for ext_id in \
    "ubuntu-appindicators@ubuntu.com" \
    "appindicatorsupport@rgcjonas.gmail.com"; do
    if gnome-extensions list 2>/dev/null | grep -q "$ext_id"; then
        gnome-extensions enable "$ext_id" 2>/dev/null && \
            echo "  Enabled: $ext_id" && break
    fi
done

# ── Make executable ────────────────────────────────────────────────────────────
chmod +x "$SCRIPT_DIR/claude_status.py"

# ── Autostart entry ────────────────────────────────────────────────────────────
echo ""
read -rp "Add to GNOME autostart (runs on login)? [Y/n] " answer
if [[ "${answer,,}" != "n" ]]; then
    mkdir -p ~/.config/autostart
    cat > ~/.config/autostart/claude-status.desktop << EOF
[Desktop Entry]
Type=Application
Name=Claude Status
Comment=Claude usage limits in the GNOME panel
Exec=python3 $SCRIPT_DIR/claude_status.py
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
EOF
    echo "  Created ~/.config/autostart/claude-status.desktop"
fi

# ── Done ───────────────────────────────────────────────────────────────────────
echo ""
echo "=== Done! ==="
echo ""
echo "  Start now:   python3 $SCRIPT_DIR/claude_status.py"
echo "  Debug mode:  python3 $SCRIPT_DIR/claude_status.py --debug"
echo "  Config file: ~/.config/claude-status/config.json"
echo ""
echo "If the icon doesn't appear, log out and back in after enabling"
echo "the AppIndicator extension."
echo ""
echo "─── Authentication ──────────────────────────────────────────────────"
echo "  Auth is read from ~/.claude/.credentials.json (Claude Code OAuth)."
echo "  If you get an auth error, run 'claude' to refresh the token."
echo ""
