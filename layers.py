"""
Layer state machine for HyprSense.
Manages transitions between base, L2, and R2 layers.
Layer priority: L2+R2 > L2 > R2 > base.
"""

from enum import Enum, auto
import time


class Layer(Enum):
    BASE = auto()
    L2 = auto()
    R2 = auto()
    L2_R2 = auto()


class LayerManager:
    """State machine for modifier layers with persistence-based debounce.

    A layer transition only fires after the target state has been
    consistently requested for `debounce_ms` milliseconds. This
    prevents flicker from noisy trigger readings.
    """

    def __init__(self, debounce_ms: int = 50):
        self.debounce_ms = debounce_ms
        self._current = Layer.BASE
        self._pending_target: Layer | None = None
        self._pending_since: float = 0.0

    def update(self, l2_active: bool, r2_active: bool) -> Layer | None:
        """
        Process trigger states, apply persistence debounce.
        Returns new layer if a debounced transition completed, else None.
        """
        now = time.monotonic()

        target = Layer.BASE
        if l2_active and r2_active:
            target = Layer.L2_R2
        elif l2_active:
            target = Layer.L2
        elif r2_active:
            target = Layer.R2

        if target == self._current:
            # Returned to current layer — reset any in-progress debounce
            self._pending_target = None
            return None

        # Different target than current — check debounce
        if target != self._pending_target:
            # New target direction: start debounce timer
            self._pending_target = target
            self._pending_since = now
            return None

        # Same pending target — check if debounce period elapsed
        if (now - self._pending_since) * 1000.0 < self.debounce_ms:
            return None

        # Debounce satisfied — commit transition
        self._current = target
        self._pending_target = None
        return target

    def reset(self) -> None:
        self._current = Layer.BASE
        self._pending_target = None
        self._pending_since = 0.0

    @property
    def current(self) -> Layer:
        return self._current

    @property
    def layer_name(self) -> str:
        return self._current.name.lower()

