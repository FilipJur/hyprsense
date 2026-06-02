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
from pathlib import Path

from config import Config, DEFAULT_LAYERS
from analog import TriggerState, stick_to_direction, StickDirection
from layers import LayerManager, Layer
from input_reader import InputReader, enumerate_dualsense
from device_monitor import DeviceMonitor
from dispatcher import run_action
from feedback import LightbarFeedback

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
        self._feedback = LightbarFeedback(
            hidraw_path=self.config.device.hidraw_path,
        )
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

        # Start monitoring for hotplug
        asyncio.create_task(self._monitor.monitor(self._on_device_change))

        # Start event processing
        await self._process_events()

    async def stop(self) -> None:
        """Graceful shutdown."""
        logger.info("Shutting down...")
        self._running = False

        if self._reader:
            await self._reader.stop()

        if self._feedback:
            self._feedback.disable()

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
        self._reset_state()

    async def _on_device_change(self, device_info: dict | None) -> None:
        """Handle device connect/disconnect."""
        if device_info is None:
            logger.info("Device disconnected")
            await self._disconnect_device()
            self._set_lightbar(Layer.BASE)
        else:
            logger.info(f"New device: {device_info['path']}")
            await self._connect_device(device_info)

    def _reset_state(self) -> None:
        """Reset all state on disconnect."""
        self._axis_state.clear()
        self._button_state.clear()
        self._dpad_state = (0, 0)
        self._stick_was_centered = True
        if self._trigger_state:
            self._trigger_state.reset()
        if self._layer_manager:
            self._layer_manager.reset()

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
        if layer == Layer.L2 or layer == Layer.L2_R2:
            # Resize mode
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

    async def _process_triggers(self) -> None:
        """Process L2/R2 trigger values for layer switching."""
        if not self._trigger_state or not self._layer_manager:
            return

        l2_val = self._axis_state.get("l2", 0)
        r2_val = self._axis_state.get("r2", 0)

        l2_active, r2_active = self._trigger_state.update(l2_val, r2_val)
        new_layer = self._layer_manager.update(l2_active, r2_active)

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

        # PS button always does the same thing
        if button == "ps":
            await self._run_layer_action("base", button)
            return

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
            Layer.L2_R2: self.config.lightbar.l2_r2,
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
