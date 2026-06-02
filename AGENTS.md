# HyprSense — DualSense Gamepad → Hyprland Navigation Daemon

A Python daemon that reads PS5 DualSense input and translates it into Hyprland
window management commands with three modifier layers (base, L2, R2) and
lightbar color feedback.

## Hardware

```
DualSense Controller ←Bluetooth→ Raspberry Pi Pico 2W (DS5 Bridge) ←USB→ PC
```

The Pico 2W runs [DS5 Bridge](https://github.com/SundayMoments/DS5_Bridge)
firmware. The host sees a standard DualSense (VID 054c, PID 0ce6) via the
`hid-playstation` kernel driver.

## Files

| File | Purpose |
|------|---------|
| `hyprsense.py` | Entry point, asyncio event loop, signal handling |
| `config.py` | YAML config loader + default mappings |
| `input_reader.py` | evdev async reader (gamepad only) |
| `analog.py` | Stick deadzone, cardinal snap, trigger threshold |
| `layers.py` | Layer state machine (base/L2/R2) |
| `dispatcher.py` | hyprctl dispatch calls |
| `feedback.py` | Lightbar RGB via hidraw |
| `device_monitor.py` | Device enumeration + hotplug detection |
| `config/hyprsense.yaml` | Default mapping config |
| `install.sh` | Symlink to `~/.local/bin/` + systemd service |
| `hyprsense.service` | systemd user service |

## Layer Model

- **Base**: window nav (focus move, workspaces, floating, layouts)
- **L2**: window management (resize, move-to-workspace, grouping, pin)
- **R2**: media / system controls (volume, brightness, media keys)
- **L2+R2**: both held (combines L2 + R2)

## Controls

### Base Layer

| Input | Action |
|-------|--------|
| Left Stick | Move focus (cardinal snap) |
| L1 / R1 | Prev/next workspace |
| Cross | Terminal |
| Circle | Close window |
| Triangle | Toggle scratchpad |
| Square | Toggle floating |
| D-pad | Master layout controls |
| L3 | Fullscreen |
| R3 | App launcher (rofi) |
| Options | Window switcher |
| Create | Empty workspace |
| PS | Lock screen |

### L2 Layer (hold L2)

| Input | Action |
|-------|--------|
| Left Stick | Resize window |
| L1 / R1 | Move window to prev/next workspace |
| Cross | Pin window |
| Circle | Toggle group |
| D-pad ←→ | Change group active |
| R3 | Screenshot snip |
| Options | Clipboard history |

### R2 Layer (hold R2)

| Input | Action |
|-------|--------|
| L1 / R1 | Volume down/up |
| D-pad ↑↓ | Brightness up/down |
| D-pad ←→ | Previous/next track |
| L3 | Toggle mute |
| R3 | Play/pause |
| Cross | System monitor |
| Square | Color picker |
| Triangle | Emoji picker |
| Options | Browser |

## Installation

```bash
cd ~/projects/hyprsense
./install.sh
```

This symlinks `hyprsense.py` to `~/.local/bin/` and installs a systemd user
service. Add to Hyprland config:

```
exec-once = systemctl --user start hyprsense
```

## Usage

```bash
# Manual start
hyprsense.py

# With debug logging
hyprsense.py --debug

# Custom config path
hyprsense.py --config ~/.config/custom-hyprsense.yaml

# Reload config without restart
pkill -SIGHUP -f hyprsense.py
```

## Config

Copy the default config to customize:

```bash
cp ~/projects/hyprsense/config/hyprsense.yaml ~/.config/hyprsense.yaml
```

## Dependencies

- python-evdev (evdev input reading)
- pyyaml (config parsing)
- hyprctl (in PATH)
- brightnessctl, playerctl, pactl (for R2 layer media controls — optional)

## Logs

`~/.local/state/hyprsense.log`
