"""
Layer state machine for HyprSense.
Manages toggle-based transitions between base, L2, and R2 layers.
Mutually exclusive: L2 and R2 cannot be active simultaneously.
"""

from enum import Enum, auto


class Layer(Enum):
    BASE = auto()
    L2 = auto()
    R2 = auto()


class LayerManager:
    """State machine for modifier layers with toggle-on-rising-edge.

    Each trigger click toggles its layer on or off. Layers are mutually
    exclusive — activating one deactivates the other. Clicking the
    currently active trigger returns to base.
    """

    def __init__(self):
        self._current = Layer.BASE

    def update(self, l2_clicked: bool, r2_clicked: bool) -> Layer | None:
        """
        Process trigger click events and update layer state.
        Returns new Layer if state changed, else None.
        """
        if l2_clicked and r2_clicked:
            # Both simultaneously: safe default to BASE
            if self._current != Layer.BASE:
                self._current = Layer.BASE
                return Layer.BASE
            return None

        if l2_clicked:
            if self._current == Layer.BASE:
                self._current = Layer.L2
                return Layer.L2
            elif self._current == Layer.L2:
                self._current = Layer.BASE
                return Layer.BASE
            else:  # R2
                self._current = Layer.L2
                return Layer.L2

        if r2_clicked:
            if self._current == Layer.BASE:
                self._current = Layer.R2
                return Layer.R2
            elif self._current == Layer.R2:
                self._current = Layer.BASE
                return Layer.BASE
            else:  # L2
                self._current = Layer.R2
                return Layer.R2

        return None

    def reset(self) -> None:
        self._current = Layer.BASE

    @property
    def current(self) -> Layer:
        return self._current

    @property
    def layer_name(self) -> str:
        return self._current.name.lower()