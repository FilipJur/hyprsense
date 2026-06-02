"""
Lightbar RGB feedback via raw HID output reports.
Writes directly to /dev/hidraw* — no sudo needed for DualSense.
"""

import os
import logging

logger = logging.getLogger("hyprsense")

# USB output report for DualSense
REPORT_ID = 0x31
REPORT_SIZE = 63  # 64 bytes in USB mode (includes report ID at byte 0)
LIGHTBAR_OFFSET = 45  # RGB at bytes 45,46,47


class LightbarFeedback:
    """Sends RGB color commands to DualSense lightbar via hidraw."""

    def __init__(self, hidraw_path: str = "/dev/hidraw8", enabled: bool = True):
        self.hidraw_path = hidraw_path
        self._enabled = enabled
        self._disabled_silently = False
        self._fd: int | None = None

    def set_color(self, r: int, g: int, b: int) -> None:
        """Set lightbar to the given RGB color."""
        if self._disabled_silently:
            return

        if not self._enabled:
            return

        report = bytearray(REPORT_SIZE + 1)  # +1 for report ID
        report[0] = REPORT_ID
        report[LIGHTBAR_OFFSET] = r & 0xFF
        report[LIGHTBAR_OFFSET + 1] = g & 0xFF
        report[LIGHTBAR_OFFSET + 2] = b & 0xFF

        self._write_report(report)

    def disable(self) -> None:
        """Disable feedback (turn off lightbar)."""
        self.set_color(0, 0, 0)
        self._enabled = False
        self._close()

    def _write_report(self, report: bytes) -> None:
        """Write HID output report to hidraw device."""
        try:
            if self._fd is None:
                self._open()
            if self._fd is not None:
                written = os.write(self._fd, bytes(report))
                if written != len(report):
                    logger.debug(f"HID write: expected {len(report)}, wrote {written}")
        except (OSError, PermissionError) as e:
            logger.warning(f"Cannot write lightbar: {e}. Disabling feedback.")
            self._disabled_silently = True
            self._close()

    def _open(self) -> None:
        """Open hidraw device for writing."""
        try:
            self._fd = os.open(self.hidraw_path, os.O_WRONLY | os.O_NONBLOCK)
        except (OSError, PermissionError) as e:
            logger.warning(f"Cannot open {self.hidraw_path}: {e}")
            self._disabled_silently = True
            self._fd = None

    def _close(self) -> None:
        """Close hidraw device."""
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def __del__(self):
        self._close()
