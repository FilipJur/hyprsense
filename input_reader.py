"""
Async evdev input reader for DualSense gamepad.
Reads button/axis events from /dev/input/event* and publishes via asyncio queues.
"""

import asyncio
import evdev
from evdev import InputDevice, ecodes, AbsInfo
import logging

logger = logging.getLogger("hyprsense")


# Button event codes to logical names
BUTTON_MAP = {
    ecodes.BTN_SOUTH: "cross",
    ecodes.BTN_EAST: "circle",
    ecodes.BTN_NORTH: "triangle",
    ecodes.BTN_WEST: "square",
    ecodes.BTN_TL: "l1",
    ecodes.BTN_TR: "r1",
    ecodes.BTN_TL2: "l2",
    ecodes.BTN_TR2: "r2",
    ecodes.BTN_SELECT: "create",
    ecodes.BTN_START: "options",
    ecodes.BTN_THUMBL: "l3",
    ecodes.BTN_THUMBR: "r3",
    ecodes.BTN_MODE: "ps",
}

# Axis event codes to logical names
AXIS_MAP = {
    ecodes.ABS_X: "stick_lx",
    ecodes.ABS_Y: "stick_ly",
    ecodes.ABS_Z: "l2",       # analog trigger
    ecodes.ABS_RX: "stick_rx",
    ecodes.ABS_RY: "stick_ry",
    ecodes.ABS_RZ: "r2",      # analog trigger
    ecodes.ABS_HAT0X: "dpad_x",
    ecodes.ABS_HAT0Y: "dpad_y",
}


class GamepadEvent:
    """Normalized gamepad event."""
    __slots__ = ("type", "name", "value", "timestamp")

    def __init__(self, etype: str, name: str, value: int, timestamp: float):
        self.type = etype     # "button" or "axis"
        self.name = name      # logical name: "cross", "stick_lx", etc.
        self.value = value    # button: 0/1, axis: raw value
        self.timestamp = timestamp


class InputReader:
    """Async reader for DualSense evdev gamepad node."""

    def __init__(self, device_path: str, queue: asyncio.Queue):
        self.device_path = device_path
        self.queue = queue
        self._device: InputDevice | None = None
        self._running = False
        self._task: asyncio.Task | None = None

    async def open(self) -> bool:
        """Open the evdev device and start reading."""
        try:
            self._device = InputDevice(self.device_path)
            # Grab exclusive to prevent events reaching other consumers
            self._device.grab()
            logger.info(f"Opened {self.device_path} ({self._device.name})")
            return True
        except (OSError, PermissionError) as e:
            logger.error(f"Cannot open {self.device_path}: {e}")
            return False

    async def start(self) -> None:
        """Start async event reading loop."""
        if self._device is None:
            raise RuntimeError("Device not opened")
        self._running = True
        self._task = asyncio.create_task(self._read_loop())
        logger.info("Input reader started")

    async def stop(self) -> None:
        """Stop reading and release device."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._device:
            try:
                self._device.ungrab()
                self._device.close()
            except OSError:
                pass
            self._device = None
        logger.info("Input reader stopped")

    async def _read_loop(self) -> None:
        """Main async read loop. Feeds events into the queue."""
        loop = asyncio.get_event_loop()
        while self._running and self._device is not None:
            try:
                # Read events without blocking the event loop
                events = await loop.run_in_executor(None, self._read_batch)
                for event in events:
                    gamepad_event = self._normalize(event)
                    if gamepad_event:
                        await self.queue.put(gamepad_event)
            except asyncio.CancelledError:
                break
            except OSError as e:
                if self._running:
                    logger.error(f"Read error: {e}")
                    await asyncio.sleep(0.5)
                else:
                    break

    def _read_batch(self) -> list:
        """Read a batch of pending events. Blocking, run in executor."""
        if self._device is None:
            return []
        try:
            return list(self._device.read())
        except BlockingIOError:
            return []

    def _normalize(self, event) -> GamepadEvent | None:
        """Convert raw evdev event to normalized GamepadEvent."""
        if event.type == ecodes.EV_KEY:
            name = BUTTON_MAP.get(event.code)
            if name:
                return GamepadEvent("button", name, event.value, event.timestamp())
        elif event.type == ecodes.EV_ABS:
            name = AXIS_MAP.get(event.code)
            if name:
                return GamepadEvent("axis", name, event.value, event.timestamp())
        return None


def enumerate_dualsense(match_name: str = "DualSense",
                        match_vendor: str = "054c") -> list[dict]:
    """
    Find all DualSense evdev gamepad nodes.
    Returns list of dicts with 'path', 'name', 'phys', 'uniq'.
    """
    import evdev
    devices = []
    vendor_id = int(match_vendor, 16)
    for path in evdev.list_devices():
        try:
            dev = InputDevice(path)
            info = dev.info
            if (info.vendor == vendor_id
                    and match_name.lower() in dev.name.lower()
                    and ecodes.EV_ABS in dev.capabilities()):
                # Check it's the gamepad node (has BTN_SOUTH)
                caps = dev.capabilities()
                keys = caps.get(ecodes.EV_KEY, [])
                if ecodes.BTN_SOUTH in keys:
                    devices.append({
                        "path": path,
                        "name": dev.name,
                        "phys": dev.phys,
                        "uniq": info.uniq,
                    })
            dev.close()
        except (OSError, PermissionError):
            continue
    return devices
