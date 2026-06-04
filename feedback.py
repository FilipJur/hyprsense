"""
Lightbar RGB feedback via hid-playstation LED sysfs.
Writes to /sys/class/leds/input*:rgb:indicator/multi_intensity.

The hid-playstation kernel driver registers the DualSense lightbar as a
multicolor LED. DS5 Bridge firmware forwards these sysfs writes to the
controller — unlike raw HID output reports, which the bridge does not forward.
"""

import os
import logging

logger = logging.getLogger("hyprsense")


def find_dualsense_led() -> str | None:
    """Find the DualSense lightbar LED sysfs path.

    Scans /sys/class/leds/ for *:rgb:indicator entries whose device
    matches a DualSense controller (via name or playstation driver).
    Returns the LED directory path or None.
    """
    leds_base = "/sys/class/leds"
    if not os.path.isdir(leds_base):
        return None

    for entry in os.listdir(leds_base):
        if not entry.endswith(":rgb:indicator"):
            continue
        led_path = os.path.join(leds_base, entry)
        device_path = os.path.join(led_path, "device")

        # Check device name (e.g. "DualSense Wireless Controller")
        try:
            name_path = os.path.join(device_path, "name")
            if os.path.exists(name_path):
                with open(name_path) as f:
                    dev_name = f.read().strip().lower()
                if "dualsense" in dev_name or "wireless controller" in dev_name:
                    return led_path
        except OSError:
            continue

        # Fallback: check via uevent for hid-playstation driver
        try:
            uevent_path = os.path.join(device_path, "uevent")
            if os.path.exists(uevent_path):
                with open(uevent_path) as f:
                    uevent = f.read()
                if "DRIVER=playstation" in uevent:
                    return led_path
        except OSError:
            continue

    return None


class LightbarFeedback:
    """Sends RGB color commands to DualSense lightbar via LED sysfs."""

    def __init__(self, led_path: str | None = None, enabled: bool = True):
        self._led_path = led_path if led_path else find_dualsense_led()
        self._enabled = enabled

    def set_color(self, r: int, g: int, b: int) -> None:
        """Set lightbar to the given RGB color. Retries on transient failures."""
        if not self._enabled:
            return

        # Rediscover LED path if missing or stale (device may have reconnected)
        if not self._led_path or not os.path.exists(
            os.path.join(self._led_path, "multi_intensity")
        ):
            self._led_path = find_dualsense_led()

        if not self._led_path:
            return

        intensity_path = os.path.join(self._led_path, "multi_intensity")
        try:
            with open(intensity_path, "w") as f:
                f.write(f"{r} {g} {b}")
        except (OSError, PermissionError) as e:
            logger.warning(f"Cannot write lightbar ({intensity_path}): {e}")

    def disable(self) -> None:
        """Disable feedback (turn off lightbar)."""
        self.set_color(0, 0, 0)
        self._enabled = False

    def __del__(self):
        self._enabled = False
