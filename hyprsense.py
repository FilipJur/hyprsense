#!/usr/bin/env python3
"""
HyprSense — DualSense Gamepad → Hyprland Navigation Daemon.

Translates PS5 DualSense controller input into Hyprland window management
commands using three modifier layers (base, L2, R2) with lightbar feedback.

Usage:
    hyprsense.py [--config PATH] [--debug]

Signals:
    SIGHUP  — reload config
    SIGINT  — graceful shutdown
    SIGTERM — graceful shutdown
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from evdev import UInput, ecodes as evdev_ecodes
from pathlib import Path

from config import Config, DEFAULT_LAYERS
from analog import TriggerState, stick_to_direction, StickDirection
from layers import LayerManager, Layer
from input_reader import InputReader, enumerate_dualsense, find_dualsense_hidraw
from device_monitor import DeviceMonitor
from dispatcher import run_action
from feedback import LightbarFeedback
from game_detector import GameDetector

PID_FILE = os.path.expanduser("~/.local/state/hyprsense.pid")
LOG_FILE = os.path.expanduser("~/.local/state/hyprsense.log")

logger = logging.getLogger("hyprsense")


class HyprSenseDaemon:
    """Main daemon orchestrating input reading, layer logic, and dispatching."""

    def __init__(self, config_path: str | None = None):
        self.config = Config.load(config_path)
        self.config_path = config_path

        self._reader: InputReader | None = None
        self._trigger_state: TriggerState | None = None
        self._layer_manager: LayerManager | None = None
        self._feedback: LightbarFeedback | None = None
        self._monitor: DeviceMonitor | None = None

        self._queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._running = False

        # State tracking
        self._axis_state: dict[str, int] = {}
        self._button_state: dict[str, int] = {}
        self._dpad_state: tuple[int, int] = (0, 0)  # (hat_x, hat_y)

        # Stick focus cooldown
        self._last_stick_move = 0.0
        self._stick_was_centered = True
        # Right stick cooldown
        self._last_right_stick_move = 0.0
        # Scroll wheel (right stick base layer)
        self._scroll_uinput = None
        self._right_stick_was_centered = True
        # Scroll target (updated by input, consumed by scroll loop)
        self._scroll_target_x = 0.0  # normalized -1.0 to 1.0
        self._scroll_target_y = 0.0
        # Scroll accumulator (fractional remainder for smooth emission)
        self._scroll_accum_x = 0.0
        self._scroll_accum_y = 0.0
        # Scroll loop task handle
        self._scroll_task = None


        # Pause state (game mode)
        self._paused = False
        self._auto_paused = False  # True when auto-detection triggered pause

        # Game detector (auto-detection)
        self._detector: GameDetector | None = None
        # Write PID file
        self._write_pid()

    def _write_pid(self) -> None:
        os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))

    def _remove_pid(self) -> None:
        try:
            os.unlink(PID_FILE)
        except OSError:
            pass

    async def start(self) -> None:
        """Initialize and start all subsystems."""
        self._running = True

        # Init subsystems
        self._trigger_state = TriggerState(
            threshold=self.config.daemon.trigger_threshold,
            hysteresis=self.config.daemon.trigger_hysteresis,
        )
        self._layer_manager = LayerManager()
        self._feedback = LightbarFeedback()
        self._monitor = DeviceMonitor(
            match_name=self.config.device.match_name,
            match_vendor=self.config.device.match_vendor,
        )

        # Set initial lightbar
        self._set_lightbar(Layer.BASE)

        logger.info("HyprSense daemon starting")

        # Find and connect to device
        device = await self._monitor.wait_for_device(timeout=10)
        if device is None:
            logger.warning("No DualSense found, waiting for hotplug...")
        else:
            await self._connect_device(device)
            self._monitor._current_device = device
            self._reset_triggers()

        # Start monitoring for hotplug
        asyncio.create_task(self._monitor.monitor(self._on_device_change))

        # Start automatic game detection (if enabled)
        gd_config = self.config.game_detection
        if gd_config.enabled:
            self._detector = GameDetector(
                on_game_start=self._on_game_start,
                on_game_stop=self._on_game_stop,
                extra_game_classes=gd_config.extra_game_classes,
                deny_classes=gd_config.deny_classes,
            )
            asyncio.create_task(self._detector.run())

        # Start scroll emission loop
        self._scroll_task = asyncio.create_task(self._scroll_loop())

        # Start event processing
        await self._process_events()

    async def stop(self) -> None:
        """Graceful shutdown."""
        logger.info("Shutting down...")
        self._running = False

        if self._scroll_task:
            self._scroll_task.cancel()
            try:
                await self._scroll_task
            except asyncio.CancelledError:
                pass

        if self._reader:
            await self._reader.stop()

        if self._feedback:
            self._feedback.disable()

        if self._scroll_uinput:
            self._scroll_uinput.close()
            self._scroll_uinput = None
        if self._detector:
            await self._detector.stop()

        self._remove_pid()
        logger.info("HyprSense stopped")


    def reload_config(self) -> None:
        """Reload config from disk (SIGHUP handler)."""
        logger.info("Reloading config")
        self.config.reload(self.config_path)
        if self._trigger_state:
            self._trigger_state.threshold = self.config.daemon.trigger_threshold
            self._trigger_state.hysteresis = self.config.daemon.trigger_hysteresis
        logger.info("Config reloaded")

    async def _connect_device(self, device_info: dict) -> None:
        """Connect to and start reading from a DualSense device."""
        if self._reader:
            await self._reader.stop()

        self._reader = InputReader(device_info["path"], self._queue)
        if await self._reader.open():
            await self._reader.start()
            logger.info(f"Connected to {device_info['name']} ({device_info['uniq']})")

    async def _disconnect_device(self) -> None:
        """Disconnect from current device."""
        if self._reader:
            await self._reader.stop()
            self._reader = None
        if self._monitor:
            self._monitor._current_device = None
        self._reset_state()

    async def _on_device_change(self, device_info: dict | None) -> None:
        """Handle device connect/disconnect."""
        if device_info is None:
            logger.info("Device disconnected")
            await self._disconnect_device()
            self._set_lightbar(Layer.BASE)
        else:
            # Skip if already connected to this exact device
            if self._reader and self._reader.device_path == device_info["path"]:
                return
            logger.info(f"New device: {device_info['path']}")
            await self._connect_device(device_info)

    def _reset_state(self) -> None:
        """Reset all state on disconnect."""
        self._axis_state.clear()
        self._button_state.clear()
        self._dpad_state = (0, 0)
        self._stick_was_centered = True
        self._right_stick_was_centered = True
        self._last_right_stick_move = 0.0
        self._scroll_target_x = 0.0
        self._scroll_target_y = 0.0
        self._scroll_accum_x = 0.0
        self._scroll_accum_y = 0.0
        if self._trigger_state:
            self._trigger_state.reset()
        if self._layer_manager:
            self._layer_manager.reset()


    def _enter_game_mode(self, source: str = "manual") -> None:
        """Release controller for gaming (game detected or PS button pressed)."""
        if self._paused:
            return
        self._paused = True
        logger.info(f"Game mode ON ({source}) — releasing controller")
        if self._reader:
            self._reader.pause()
        if self._feedback:
            self._feedback.set_color(0, 0, 0)

    def _exit_game_mode(self, source: str = "manual") -> None:
        """Re-acquire controller for desktop navigation."""
        if not self._paused:
            return
        self._paused = False
        self._auto_paused = False
        logger.info(f"Game mode OFF ({source}) — resuming desktop navigation")
        if self._reader:
            self._reader.resume()
        self._reset_state()
        layer = self._layer_manager.current if self._layer_manager else Layer.BASE
        self._set_lightbar(layer)
        self._reset_triggers()

    def _reset_triggers(self) -> None:
        """Reset DualSense adaptive triggers to off state via hidraw.

        Games often set trigger effects (continuous resistance, auto-trigger,
        feedback) and don't clear them on exit.  The hid-playstation driver
        caches the last effect — the hardware stays locked until a zeroed
        output report resets it.
        """
        hidraw = find_dualsense_hidraw()
        if hidraw is None:
            logger.debug("No hidraw device found for trigger reset")
            return

        # USB output report 0x02, 47-byte payload.
        # Byte 0: report ID.  Byte 1: flags — 0x06 = right (bit 1) + left (bit 2)
        # trigger effect enabled.  Bytes 2-20: rumble/audio (ignored — flags
        # not set).  Bytes 21-31: right trigger (all zero = off).  Bytes 32-42:
        # left trigger (all zero = off).  Bytes 43-47: LED (ignored).
        report = bytearray(48)
        report[0] = 0x02          # report ID
        report[1] = 0x06          # flags: right + left trigger sections valid
        # Bytes 2..47 already zero — triggers off, other sections ignored.

        try:
            fd = os.open(hidraw, os.O_WRONLY | os.O_NONBLOCK)
            os.write(fd, bytes(report))
            os.close(fd)
            logger.info("Adaptive triggers reset to off state")
        except OSError as e:
            logger.warning(f"Trigger reset failed ({hidraw}): {e}")

    def pause_for_osk(self) -> None:
        """Pause for on-screen keyboard (SIGUSR1 handler)."""
        self._enter_game_mode("osk")

    def resume_from_osk(self) -> None:
        """Resume after on-screen keyboard (SIGUSR2 handler)."""
        self._exit_game_mode("osk")

    def _on_game_start(self) -> None:
        """Callback: game window detected by GameDetector."""
        self._auto_paused = True
        self._enter_game_mode("auto-detect")

    def _on_game_stop(self) -> None:
        """Callback: game window closed, detected by GameDetector."""
        self._exit_game_mode("auto-detect")

    async def _process_events(self) -> None:
        """Main event processing loop."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                await self._handle_event(event)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def _handle_event(self, event) -> None:
        """Process a single normalized gamepad event."""
        if self._paused:
            return

        if event.type == "axis":
            await self._handle_axis(event.name, event.value)
        elif event.type == "button":
            await self._handle_button(event.name, event.value)

    async def _handle_axis(self, name: str, value: int) -> None:
        """Handle an axis event."""
        self._axis_state[name] = value

        # Process left stick
        if name == "stick_lx" or name == "stick_ly":
            await self._process_stick()
            return

        # Process right stick
        if name == "stick_rx" or name == "stick_ry":
            await self._process_right_stick()
            return

        # Process triggers
        if name == "l2" or name == "r2":
            await self._process_triggers()
            return

        # Process D-pad
        if name == "dpad_x":
            self._dpad_state = (value, self._dpad_state[1])
            await self._process_dpad()
            return
        if name == "dpad_y":
            self._dpad_state = (self._dpad_state[0], value)
            await self._process_dpad()
            return

    async def _handle_button(self, name: str, value: int) -> None:
        """Handle a button event (0 = release, 1 = press)."""
        prev = self._button_state.get(name, 0)
        self._button_state[name] = value

        # Only act on press (rising edge)
        if value == 1 and prev == 0:
            await self._dispatch_button(name)

    async def _process_stick(self) -> None:
        """Process left stick for focus movement."""
        x = self._axis_state.get("stick_lx", 128)
        y = self._axis_state.get("stick_ly", 128)

        direction = stick_to_direction(
            x, y, center=128,
            deadzone_fraction=self.config.daemon.stick_deadzone,
            cardinal=self.config.daemon.stick_cardinal_snap,
        )

        if direction == StickDirection.NONE:
            self._stick_was_centered = True
            return

        # Rate limit stick moves
        now = time.monotonic()
        cooldown_s = self.config.daemon.cooldown_ms / 1000.0

        if not self._stick_was_centered and now - self._last_stick_move < cooldown_s:
            return

        self._stick_was_centered = False
        self._last_stick_move = now

        # Map direction to focus or resize based on layer
        layer = self._layer_manager.current if self._layer_manager else Layer.BASE
        await self._dispatch_stick(direction, layer)

    async def _dispatch_stick(self, direction: StickDirection, layer: Layer) -> None:
        """Dispatch stick direction based on current layer."""
        if layer == Layer.L2:
            resize_map = {
                StickDirection.UP: "dispatch resizeactive 0 -30",
                StickDirection.DOWN: "dispatch resizeactive 0 30",
                StickDirection.LEFT: "dispatch resizeactive -30 0",
                StickDirection.RIGHT: "dispatch resizeactive 30 0",
            }
            action = resize_map.get(direction)
        else:
            # Focus mode (base and R2)
            focus_map = {
                StickDirection.UP: "dispatch movefocus u",
                StickDirection.DOWN: "dispatch movefocus d",
                StickDirection.LEFT: "dispatch movefocus l",
                StickDirection.RIGHT: "dispatch movefocus r",
            }
            action = focus_map.get(direction)

        if action:
            await run_action(action)

    async def _process_right_stick(self) -> None:
        x = self._axis_state.get("stick_rx", 128)
        y = self._axis_state.get("stick_ry", 128)

        layer = self._layer_manager.current if self._layer_manager else Layer.BASE

        if layer == Layer.BASE:
            self._scroll_update_target(x, y)
        else:
            # Window movement: keep existing cardinal-snap + cooldown
            direction = stick_to_direction(
                x, y, center=128,
                deadzone_fraction=self.config.daemon.stick_deadzone,
                cardinal=self.config.daemon.stick_cardinal_snap,
            )
            if direction == StickDirection.NONE:
                self._right_stick_was_centered = True
                return
            now = time.monotonic()
            cooldown_s = self.config.daemon.cooldown_ms / 1000.0
            if not self._right_stick_was_centered and now - self._last_right_stick_move < cooldown_s:
                return
            self._right_stick_was_centered = False
            self._last_right_stick_move = now
            await self._dispatch_right_stick(direction)

    def _ensure_scroll_device(self) -> UInput:
        """Lazily create a uinput device for scroll wheel events."""
        if self._scroll_uinput is None:
            self._scroll_uinput = UInput({
                evdev_ecodes.EV_REL: [
                    evdev_ecodes.REL_WHEEL,
                    evdev_ecodes.REL_HWHEEL,
                    evdev_ecodes.REL_WHEEL_HI_RES,
                    evdev_ecodes.REL_HWHEEL_HI_RES,
                ],
            }, name="HyprSense Scroll Wheel")
            logger.info("Scroll wheel device created")
        return self._scroll_uinput
    def _scroll_update_target(self, x: int, y: int) -> None:
        """Update scroll target from raw stick position.

        Just stores normalized values; the background scroll loop handles emission.
        """
        CENTER = 128
        HALF_RANGE = 128
        deadzone_frac = self.config.daemon.stick_deadzone
        deadzone = int(HALF_RANGE * deadzone_frac)
        usable = HALF_RANGE - deadzone
        if usable <= 0:
            self._scroll_target_x = 0.0
            self._scroll_target_y = 0.0
            return

        def remap(raw_axis: int) -> float:
            """0-255 → [-1.0, 1.0] with deadzone, linear curve."""
            offset = raw_axis - CENTER
            if offset == 0:
                return 0.0
            sign = 1 if offset > 0 else -1
            mag = abs(offset)
            if mag <= deadzone:
                return 0.0
            t = (mag - deadzone) / usable
            if t > 1.0:
                t = 1.0
            return sign * t

        self._scroll_target_x = remap(x)
        self._scroll_target_y = remap(y)

    async def _scroll_loop(self) -> None:
        """Fixed-rate scroll emission loop.

        Reads current stick target and emits scroll events at a consistent rate,
        decoupled from input event timing. This is the xboxdrv approach adapted
        to asyncio.

        Emits REL_WHEEL_HI_RES for smooth scroll + REL_WHEEL for discrete fallback.
        """
        TICK = 1.0 / 60.0  # 60Hz emission rate
        MAX_RATE = 1200.0   # v120 units per second at full deflection (10 notches/sec)

        while self._running:
            try:
                await asyncio.sleep(TICK)
            except asyncio.CancelledError:
                break

            nx = self._scroll_target_x
            ny = self._scroll_target_y

            # Reset accumulator when stick is centered (immediate stop, no drift)
            if nx == 0.0 and ny == 0.0:
                self._scroll_accum_x = 0.0
                self._scroll_accum_y = 0.0
                continue

            # Skip if paused
            if self._paused:
                continue

            # Check layer — only emit scroll on BASE layer
            layer = self._layer_manager.current if self._layer_manager else Layer.BASE
            if layer != Layer.BASE:
                self._scroll_accum_x = 0.0
                self._scroll_accum_y = 0.0
                continue

            # Accumulate: deflection × rate × fixed_dt
            self._scroll_accum_x += nx * MAX_RATE * TICK
            self._scroll_accum_y -= ny * MAX_RATE * TICK  # invert Y

            # Extract integer v120 amounts
            emit_x = int(self._scroll_accum_x)
            emit_y = int(self._scroll_accum_y)

            if emit_x == 0 and emit_y == 0:
                continue

            # Subtract emitted amount (preserve fractional remainder)
            self._scroll_accum_x -= emit_x
            self._scroll_accum_y -= emit_y

            dev = self._ensure_scroll_device()

            # Emit HI_RES (smooth scroll for modern apps)
            if emit_x:
                dev.write(evdev_ecodes.EV_REL, evdev_ecodes.REL_HWHEEL_HI_RES, emit_x)
                # Discrete fallback: emit REL_WHEEL ±1 for every 120 v120 units
                sign_x = 1 if emit_x > 0 else -1
                legacy_x = abs(emit_x)
                while legacy_x >= 120:
                    dev.write(evdev_ecodes.EV_REL, evdev_ecodes.REL_HWHEEL, sign_x)
                    legacy_x -= 120

            if emit_y:
                dev.write(evdev_ecodes.EV_REL, evdev_ecodes.REL_WHEEL_HI_RES, emit_y)
                sign_y = 1 if emit_y > 0 else -1
                legacy_y = abs(emit_y)
                while legacy_y >= 120:
                    dev.write(evdev_ecodes.EV_REL, evdev_ecodes.REL_WHEEL, sign_y)
                    legacy_y -= 120

            dev.syn()

    async def _dispatch_right_stick(self, direction: StickDirection) -> None:
        """Dispatch right stick direction as window movement."""
        move_map = {
            StickDirection.UP: "dispatch movewindow u",
            StickDirection.DOWN: "dispatch movewindow d",
            StickDirection.LEFT: "dispatch movewindow l",
            StickDirection.RIGHT: "dispatch movewindow r",
        }
        action = move_map.get(direction)
        if action:
            await run_action(action)

    async def _process_triggers(self) -> None:
        """Process L2/R2 trigger clicks for layer toggling."""
        if not self._trigger_state or not self._layer_manager:
            return

        l2_val = self._axis_state.get("l2", 0)
        r2_val = self._axis_state.get("r2", 0)

        l2_clicked, r2_clicked = self._trigger_state.update(l2_val, r2_val)
        new_layer = self._layer_manager.update(l2_clicked, r2_clicked)

        if new_layer is not None:
            self._set_lightbar(new_layer)

    async def _process_dpad(self) -> None:
        """Process D-pad hat for button-like actions."""
        hat_x, hat_y = self._dpad_state

        if hat_x == 0 and hat_y == 0:
            # Centered: clear dpad button states for next press
            for btn in ("dpad_up", "dpad_down", "dpad_left", "dpad_right"):
                self._button_state.pop(btn, None)
            return
        # Map to button name — clear other dpad states for diagonal transitions
        dpad_buttons = ("dpad_up", "dpad_down", "dpad_left", "dpad_right")
        if hat_y == -1:
            button = "dpad_up"
        elif hat_y == 1:
            button = "dpad_down"
        elif hat_x == -1:
            button = "dpad_left"
        elif hat_x == 1:
            button = "dpad_right"
        else:
            return

        # Clear other dpad button states (handles diagonal transitions)
        for btn in dpad_buttons:
            if btn != button:
                self._button_state.pop(btn, None)

        # Fire as if button press (D-pad is stateful, fire once per engage)
        prev = self._button_state.get(button, 0)
        self._button_state[button] = 1
        if prev == 0:
            await self._dispatch_button(button)

    async def _dispatch_button(self, button: str) -> None:
        """Dispatch a button press based on current layer."""
        layer = self._layer_manager.current if self._layer_manager else Layer.BASE
        layer_name = layer.name.lower()


        # Check current layer first, fall back to base
        action = self._get_layer_action(layer_name, button)
        if action is None:
            action = self._get_layer_action("base", button)

        if action:
            logger.debug(f"[{layer_name}] {button} → {action}")
            await run_action(action)

    def _get_layer_action(self, layer: str, button: str) -> str | None:
        """Get the mapped action for a button in a layer."""
        layers = self.config.layers if self.config.layers else DEFAULT_LAYERS
        layer_config = layers.get(layer, {})
        return layer_config.get(button)

    async def _run_layer_action(self, layer: str, button: str) -> None:
        """Run an action specifically from a named layer (ignoring current layer)."""
        action = self._get_layer_action(layer, button)
        if action:
            await run_action(action)

    def _set_lightbar(self, layer: Layer) -> None:
        """Update lightbar color based on layer."""
        if not self._feedback:
            return

        color_map = {
            Layer.BASE: self.config.lightbar.base,
            Layer.L2: self.config.lightbar.l2,
            Layer.R2: self.config.lightbar.r2,
        }
        color = color_map.get(layer, (0, 0, 0))
        self._feedback.set_color(*color)

def _check_single_instance() -> bool:
    """Check if another instance is already running via PID file."""
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                pid = int(f.read().strip())
            # Check if process is still alive
            os.kill(pid, 0)
            logger.error(f"Another instance is running (PID {pid})")
            return False
        except (ValueError, OSError):
            # Stale PID file
            pass
    return True


def setup_logging(debug: bool = False) -> None:
    """Configure logging to file and console."""
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

    level = logging.DEBUG if debug else logging.INFO
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(fmt)
    file_handler.setLevel(level)

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(fmt)
    console_handler.setLevel(level)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console_handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="HyprSense — DualSense Nav Daemon")
    parser.add_argument("--config", "-c", help="Path to config YAML")
    parser.add_argument("--debug", "-d", action="store_true", help="Debug logging")
    args = parser.parse_args()

    setup_logging(args.debug)

    if not _check_single_instance():
        sys.exit(1)

    daemon = HyprSenseDaemon(config_path=args.config)

    # Signal handlers
    loop = asyncio.new_event_loop()

    def _sig_handler():
        logger.info("Received shutdown signal")
        asyncio.ensure_future(daemon.stop(), loop=loop)

    def _hup_handler():
        daemon.reload_config()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _sig_handler)
    loop.add_signal_handler(signal.SIGHUP, _hup_handler)
    loop.add_signal_handler(signal.SIGUSR1, lambda: daemon.pause_for_osk())
    loop.add_signal_handler(signal.SIGUSR2, lambda: daemon.resume_from_osk())

    try:
        loop.run_until_complete(daemon.start())
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()

    logger.info("HyprSense exited")


if __name__ == "__main__":
    main()
