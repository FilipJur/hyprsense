"""
Device enumeration and hotplug detection for DualSense controllers.
Monitors /dev/input/ for connect/disconnect events.
"""

import asyncio
import logging
import os
from pathlib import Path

from input_reader import enumerate_dualsense

logger = logging.getLogger("hyprsense")

INOTIFY_AVAILABLE = False
try:
    import inotify_simple
    INOTIFY_AVAILABLE = True
except ImportError:
    pass


class DeviceMonitor:
    """Watches for DualSense connect/disconnect events."""

    def __init__(self, match_name: str = "DualSense", match_vendor: str = "054c",
                 preferred_uniq: str | None = None):
        self.match_name = match_name
        self.match_vendor = match_vendor
        self.preferred_uniq = preferred_uniq
        self._current_device: dict | None = None
        self._callback: callable | None = None

    def find_device(self) -> dict | None:
        """Find a connected DualSense gamepad. Returns device info dict or None."""
        devices = enumerate_dualsense(self.match_name, self.match_vendor)
        if not devices:
            return None

        # Prefer device matching preferred_uniq
        if self.preferred_uniq:
            for d in devices:
                if d["uniq"] == self.preferred_uniq:
                    logger.debug(f"Found preferred device at {d['path']} (uniq={d['uniq']})")
                    return d

        # Fall back to first available
        logger.debug(f"Found device at {devices[0]['path']} ({devices[0]['name']})")
        return devices[0]

    async def wait_for_device(self, timeout: float = 5.0,
                              interval: float = 1.0) -> dict | None:
        """Poll until a DualSense is found or timeout expires."""
        elapsed = 0.0
        while elapsed < timeout:
            dev = self.find_device()
            if dev:
                return dev
            await asyncio.sleep(interval)
            elapsed += interval
        return None

    async def monitor(self, callback: callable, interval: float = 2.0) -> None:
        """
        Continuously monitor for device connect/disconnect.
        Calls callback(device_info | None) on change.
        Uses inotify if available, falls back to polling.
        """
        self._callback = callback

        if INOTIFY_AVAILABLE:
            await self._monitor_inotify()
        else:
            await self._monitor_poll(interval)

    async def _monitor_inotify(self) -> None:
        """Monitor /dev/input using inotify."""
        import inotify_simple
        IN_CREATE = inotify_simple.flags.CREATE
        IN_DELETE = inotify_simple.flags.DELETE
        IN_MOVED_TO = inotify_simple.flags.MOVED_TO

        inotify = inotify_simple.INotify()
        watch_flags = IN_CREATE | IN_DELETE | IN_MOVED_TO
        inotify.add_watch("/dev/input/", watch_flags)

        loop = asyncio.get_event_loop()
        logger.info("Device monitor: using inotify on /dev/input/")

        last_check = 0.0
        debounce = 0.5  # 500ms debounce for device enumeration

        while True:
            try:
                events = await loop.run_in_executor(None, inotify.read, 1)
                if events:
                    now = asyncio.get_event_loop().time()
                    if now - last_check < debounce:
                        continue
                    last_check = now

                    dev = self.find_device()
                    if dev != self._current_device:
                        self._current_device = dev
                        await self._callback(dev)
            except asyncio.CancelledError:
                inotify.close()
                raise
            except OSError:
                await asyncio.sleep(1)

    async def _monitor_poll(self, interval: float) -> None:
        """Poll for device changes."""
        logger.info("Device monitor: polling every %.1fs", interval)
        while True:
            try:
                dev = self.find_device()
                if dev != self._current_device:
                    self._current_device = dev
                    await self._callback(dev)
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
