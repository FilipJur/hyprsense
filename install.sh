#!/bin/bash
# HyprSense install script
# Symlinks to ~/.local/bin/ and sets up systemd user service.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"
STATE_DIR="$HOME/.local/state"

echo "=== HyprSense Install ==="

# Create directories
mkdir -p "$BIN_DIR"
mkdir -p "$STATE_DIR"
mkdir -p "$HOME/.config/systemd/user"

# Symlink daemon script
echo "→ Linking hyprsense.py → $BIN_DIR/hyprsense.py"
ln -sf "$SCRIPT_DIR/hyprsense.py" "$BIN_DIR/hyprsense.py"

# Install systemd user service
echo "→ Installing systemd user service"
cp "$SCRIPT_DIR/hyprsense.service" "$HOME/.config/systemd/user/hyprsense.service"
systemctl --user daemon-reload
systemctl --user enable hyprsense.service

# Install default config if none exists
if [ ! -f "$HOME/.config/hyprsense.yaml" ]; then
    echo "→ Installing default config to ~/.config/hyprsense.yaml"
    mkdir -p "$HOME/.config"
    cp "$SCRIPT_DIR/config/hyprsense.yaml" "$HOME/.config/hyprsense.yaml"
else
    echo "→ Config already exists at ~/.config/hyprsense.yaml (not overwriting)"
fi

echo ""
echo "=== Install complete ==="
echo "Start the daemon:"
echo "  systemctl --user start hyprsense"
echo ""
echo "Or add to Hyprland config:"
echo "  exec-once = systemctl --user start hyprsense"
echo ""
echo "Logs: journalctl --user -u hyprsense -f"
