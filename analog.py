"""
Analog stick and trigger processing.
Deadzone, cardinal snap, trigger threshold with hysteresis.
"""

import math
from enum import Enum, auto


class StickDirection(Enum):
    NONE = auto()
    UP = auto()
    DOWN = auto()
    LEFT = auto()
    RIGHT = auto()


def apply_deadzone(value: int, center: int = 128, fraction: float = 0.20) -> int:
    """Return 0 if value is within deadzone, else centered value retaining sign."""
    magnitude = abs(value - center)
    threshold = int(255 * fraction)
    if magnitude < threshold:
        return 0
    return value - center


def cardinal_snap(dx: int, dy: int) -> StickDirection:
    """Snap stick vector to nearest cardinal direction (aggressive — no diagonals)."""
    if dx == 0 and dy == 0:
        return StickDirection.NONE

    # atan2(dy, dx): angle from +x axis, counter-clockwise
    # We want: +x = right, +y = down (evdev convention, but we flip dy below)
    # Actually evdev: ABS_Y down is positive. For focus movement,
    # "up" on stick means lower ABS_Y value. We'll handle sign in dispatcher.
    angle = math.degrees(math.atan2(dy, dx))

    # Normalize to [0, 360)
    if angle < 0:
        angle += 360

    # Cardinal sectors (45° wide, centered on 0/90/180/270)
    # RIGHT: 315-45, DOWN: 45-135, LEFT: 135-225, UP: 225-315
    if angle >= 315 or angle < 45:
        return StickDirection.RIGHT
    elif 45 <= angle < 135:
        return StickDirection.DOWN
    elif 135 <= angle < 225:
        return StickDirection.LEFT
    else:
        return StickDirection.UP


def stick_to_direction(x: int, y: int, center: int = 128,
                       deadzone_fraction: float = 0.20,
                       cardinal: bool = True) -> StickDirection:
    """Process stick ABS_X/ABS_Y values (0-255) into a cardinal direction."""
    dx = apply_deadzone(x, center, deadzone_fraction)
    dy = apply_deadzone(y, center, deadzone_fraction)

    if dx == 0 and dy == 0:
        return StickDirection.NONE

    if cardinal:
        return cardinal_snap(dx, dy)

    # Non-cardinal mode: diagonal support (not currently used for focus)
    angle = math.degrees(math.atan2(dy, dx))
    if angle < 0:
        angle += 360
    if angle >= 315 or angle < 45:
        return StickDirection.RIGHT
    elif 45 <= angle < 135:
        return StickDirection.DOWN
    elif 135 <= angle < 225:
        return StickDirection.LEFT
    else:
        return StickDirection.UP


class TriggerState:
    """Tracks L2/R2 trigger state with hysteresis."""

    def __init__(self, threshold: int = 128, hysteresis: int = 10):
        self.threshold = threshold
        self.hysteresis = hysteresis
        self._l2_active = False
        self._r2_active = False

    def update(self, l2_value: int, r2_value: int) -> tuple[bool, bool]:
        """
        Update trigger states from raw values (0-255).
        Returns (l2_active, r2_active).
        """
        engage = self.threshold
        disengage = self.threshold - self.hysteresis

        # L2 hysteresis
        if l2_value >= engage:
            self._l2_active = True
        elif l2_value <= disengage:
            self._l2_active = False

        # R2 hysteresis
        if r2_value >= engage:
            self._r2_active = True
        elif r2_value <= disengage:
            self._r2_active = False

        return self._l2_active, self._r2_active

    def reset(self) -> None:
        self._l2_active = False
        self._r2_active = False

    @property
    def l2(self) -> bool:
        return self._l2_active

    @property
    def r2(self) -> bool:
        return self._r2_active
