# HyprSense — DualSense Gamepad → Hyprland Navigation Daemon

Translates PS5 DualSense controller input into Hyprland window management
commands using three toggle-based modifier layers with lightbar color feedback.

## Hardware Topology

```
DualSense Controller ←Bluetooth→ Raspberry Pi Pico 2W (DS5 Bridge) ←USB→ PC
```

The Pico 2W runs [DS5 Bridge](https://github.com/SundayMoments/DS5_Bridge)
firmware. The host sees a standard DualSense (VID 054c, PID 0ce6) via the
`hid-playstation` kernel driver — no pairing or OS Bluetooth stack required.

## Layer Model

Three layers, activated by **toggle** (not hold):

| Layer | Trigger | Lightbar | Purpose |
|-------|---------|----------|---------|
| **Base** | default | White | Window navigation |
| **L2** | Squeeze L2 to toggle | Blue | Window management |
| **R2** | Squeeze R2 to toggle | Green | Media & system controls |

Layers are mutually exclusive — activating one deactivates the other.
Squeeze again to return to base.

## Controls

### Base Layer

| Input | Action |
|-------|--------|
| Left Stick | Move focus (cardinal snap) |
| D-pad ↑/↓ | Swap active workspaces / Toggle split |
| D-pad ←/→ | Adjust master factor |
| L1 / R1 | Previous / next workspace |
| Cross (×) | Terminal (`$TERMINAL`) |
| Circle (○) | Close window |
| Triangle (△) | Toggle scratchpad |
| Square (□) | Toggle floating |
| L3 | Fullscreen |
| R3 | App launcher (rofi drun) |
| Options | Window switcher (rofi window) |
| Create | Empty workspace |

PS button toggles **Game Mode** on/off (see below).

### L2 Layer (toggle L2)

| Input | Action |
|-------|--------|
| Left Stick | Resize window |
| D-pad ↓ | Move to special workspace |
| D-pad ←/→ | Change group active |
| L1 / R1 | Move window to prev/next workspace |
| Cross (×) | Pin window |
| Circle (○) | Toggle group |
| Triangle (△) | Focus master |
| Square (□) | Layout orientation center |
| L3 | Fullscreen |
| R3 | Screenshot snip (slurp + grim) |

### R2 Layer (toggle R2)

| Input | Action |
|-------|--------|
| L1 / R1 | Volume down / up |
| D-pad ↑/↓ | Brightness up / down |
| D-pad ←/→ | Previous / next track |
| Cross (×) | System monitor (missioncenter) |
| Circle (○) | Clipboard history |
| Square (□) | Color picker |
| L3 | Toggle mute |
| R3 | Play / pause |
| Options | Browser (`$BROWSER`) |

## Game Mode

Press the **PS button** to toggle between desktop navigation and game mode.

| State | Lightbar | Controller |
|-------|----------|------------|
| Desktop nav | White (or layer color) | Exclusive — HyprSense processes all input |
| Game mode | Off | Released — games receive all input directly |

In game mode, the controller is released from HyprSense's exclusive grab.
Games see the DualSense as a normal gamepad via evdev. The PS button remains
active as the toggle back to desktop mode — no other buttons trigger actions.

### Automatic Game Detection

HyprSense monitors Hyprland window events to detect when a game window is
focused and automatically releases the controller grab:

- **Steam games** — detected via `steam_app_<id>` window class (all Steam
  games, Proton and native).
- **Proton/Wine games** — detected via `.exe` window class (e.g. `cs2.exe`).
- **Gamescope** — detected via `gamescope` window class.
- **Emulators and launchers** — detected by scanning `.desktop` files for
  `Categories=Game` entries with a `StartupWMClass`.

When a game launches: controller grab is released, lightbar turns off.
When the game exits: grab re-acquired, lightbar restored to current layer color.

**Debounce:** 500 ms before entering, 2 s before exiting — prevents flicker
during game startup transitions (launcher → splash → game).

**Manual override:** Press the PS button to manually switch modes at any time.
If the game was auto-detected, PS forces back to desktop. If you manually
entered game mode, auto-detection still resumes when the game exits.

**Configuration** (`~/.config/hyprsense.yaml`):

```yaml
game_detection:
  enabled: true
  extra_game_classes: []       # e.g. ["minecraft"]
  deny_classes: ["steamwebhelper"]  # never treat these as games
```

**What it does NOT catch:** non-Steam native Linux games without a desktop file.
Add their window class to `extra_game_classes`.

### Adaptive Triggers

The DS5 Bridge firmware applies default trigger resistance effects on L2/R2.
To disable them:
1. Connect the Pico 2W to a Windows PC
2. Open the DS5 Bridge companion app
3. Go to Triggers tab, set both L2 and R2 trigger mode to "Off" or minimal resistance
4. Save profile to Pico flash

There is currently no Linux tool to change these settings. The companion
protocol is open-source (`companion.cpp`) and could be reverse-engineered for
a future Linux utility.
## Lightbar

The lightbar uses the `hid-playstation` kernel driver's LED sysfs interface
(`/sys/class/leds/input*:rgb:indicator/multi_intensity`). Writing RGB values
to this file changes the lightbar color. Unlike raw HID output reports, sysfs
writes are forwarded correctly by the DS5 Bridge firmware.

A udev rule is required for non-root write access. The install script sets
this up automatically, or you can create it manually:

```bash
# /etc/udev/rules.d/99-dualsense-led.rules
SUBSYSTEM=="leds", KERNEL=="input*:rgb:indicator", RUN+="/bin/chmod 666 /sys%p/multi_intensity"
```
Then `sudo udevadm control --reload-rules && sudo udevadm trigger`.

## Installation

### Dependencies

```bash
pacman -S python-evdev python-pyyaml brightnessctl playerctl
```

### Install

```bash
cd ~/projects/hyprsense
./install.sh
```

This symlinks `hyprsense.py` to `~/.local/bin/`, copies the default config to
`~/.config/hyprsense.yaml` (if none exists), installs a systemd user service,
and sets up the udev rule for LED access.

### Autostart

Add to `~/.config/hypr/hyprland.conf`:

```
exec-once = systemctl --user start hyprsense
```

## Configuration

Config lives at `~/.config/hyprsense.yaml`. Copy the default to customize:

```bash
cp ~/projects/hyprsense/config/hyprsense.yaml ~/.config/hyprsense.yaml
```

Reload config without restarting:

```bash
pkill -SIGHUP -f hyprsense.py
```

Key settings:

| Field | Default | Description |
|-------|---------|-------------|
| `daemon.cooldown_ms` | 300 | Delay between repeated inputs |
| `daemon.trigger_threshold` | 128 | Trigger value (0–255) that counts as a squeeze |
| `daemon.trigger_hysteresis` | 10 | Dead band below threshold to prevent flicker |
| `daemon.stick_deadzone` | 0.20 | Stick deadzone as fraction of range |

## Gamepad Keyboard (Future Work)

There are no existing gamepad-optimized on-screen keyboards for Linux/Wayland.
Virtual keyboards (wf-osk, squeekboard, onboard) all require touch or mouse input
and cannot be navigated with a gamepad.

A viable approach reuses existing tools:

```
Gamepad → hyprsense → uinput (arrow keys) → rofi (character grid) → wtype (type char) → focused app
```

HyprSense would emit uinput keyboard events (arrow keys, enter, escape) to
navigate a rofi character grid, then inject the selected character with `wtype`.
Only the uinput bridge in hyprsense is new code — rofi and wtype already exist.

## Usage

```bash
# Manual start
hyprsense.py

# Debug logging
hyprsense.py --debug

# Custom config path
hyprsense.py --config ~/.config/custom-hyprsense.yaml

# View logs
journalctl --user -u hyprsense -f
```

## Troubleshooting

### Device not found

Check the Pico 2W is connected and shows as a DualSense:

```bash
ls /dev/input/by-id/ | grep DualSense
evtest /dev/input/event*  # check which event node has DualSense events
```

### Lightbar not changing

Check the LED sysfs path exists and is writable:

```bash
ls /sys/class/leds/*:rgb:indicator/multi_intensity
echo "255 0 0" > /sys/class/leds/input*:rgb:indicator/multi_intensity
```

If the command fails with "Permission denied", re-run the install script
to set up the udev rule, or create it manually (see Lightbar section above).

### Config not loading

Verify config file is valid YAML:

```bash

### Game not detecting controller

HyprSense grabs the DualSense exclusively. Normally, auto-detection releases
the controller when a game window is focused. If the controller is not
releasing automatically:

1. Press the **PS button** to manually enter game mode.
2. Verify auto-detection is enabled: check `game_detection.enabled: true` in
   `~/.config/hyprsense.yaml`.
3. If the game's window class is not caught by the hardcoded patterns, add it
   to `extra_game_classes` in the config.
4. Check the log: `journalctl --user -u hyprsense -f` for lines containing
   "Game detector" or "Game mode".
5. Verify the Hyprland socket is accessible:
   ```bash
   ls -la $XDG_RUNTIME_DIR/hypr/*/.socket2.sock
   ```

Press PS again to return to desktop navigation.

## Files

```
~/.local/bin/hyprsense.py              # Symlink → src/hyprsense.py
~/.config/hyprsense.yaml               # User config (customize this)
~/.config/systemd/user/hyprsense.service
/etc/udev/rules.d/99-dualsense-led.rules
~/.local/state/hyprsense.pid           # PID file for single-instance check
~/.local/state/hyprsense.log           # Application logs
```
