"""
Config loader + defaults for HyprSense.
Loads YAML config from ~/.config/hyprsense.yaml, falls back to defaults.
"""

import os
import yaml
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DaemonConfig:
    cooldown_ms: int = 300
    trigger_threshold: int = 128
    trigger_hysteresis: int = 10
    stick_deadzone: float = 0.20
    stick_cardinal_snap: bool = True


@dataclass
class DeviceConfig:
    match_name: str = "DualSense"
    match_vendor: str = "054c"


@dataclass
class LightbarConfig:
    base: tuple = (255, 255, 255)
    l2: tuple = (0, 100, 255)
    r2: tuple = (0, 255, 100)

@dataclass
class GameDetectionConfig:
    enabled: bool = True
    extra_game_classes: list[str] = field(default_factory=list)
    deny_classes: list[str] = field(default_factory=lambda: ["steamwebhelper"])

@dataclass
class Config:
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    device: DeviceConfig = field(default_factory=DeviceConfig)
    lightbar: LightbarConfig = field(default_factory=LightbarConfig)
    game_detection: GameDetectionConfig = field(default_factory=GameDetectionConfig)
    layers: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        """Load config from YAML file, falling back to defaults."""
        if path is None:
            path = os.path.expanduser("~/.config/hyprsense.yaml")

        config = cls()

        if os.path.exists(path):
            with open(path) as f:
                data = yaml.safe_load(f)

            if data is None:
                return config

            if "daemon" in data:
                d = data["daemon"]
                config.daemon = DaemonConfig(
                    cooldown_ms=d.get("cooldown_ms", 300),
                    trigger_threshold=d.get("trigger_threshold", 128),
                    trigger_hysteresis=d.get("trigger_hysteresis", 10),
                    stick_deadzone=d.get("stick_deadzone", 0.20),
                    stick_cardinal_snap=d.get("stick_cardinal_snap", True),
                )

            if "device" in data:
                d = data["device"]
                config.device = DeviceConfig(
                    match_name=d.get("match_name", "DualSense"),
                    match_vendor=d.get("match_vendor", "054c"),
                )

            if "lightbar" in data:
                lb = data["lightbar"]
                config.lightbar = LightbarConfig(
                    base=tuple(lb.get("base", [255, 255, 255])),
                    l2=tuple(lb.get("l2", [0, 100, 255])),
                    r2=tuple(lb.get("r2", [0, 255, 100])),
                )

            if "game_detection" in data:
                gd = data["game_detection"]
                config.game_detection = GameDetectionConfig(
                    enabled=gd.get("enabled", True),
                    extra_game_classes=gd.get("extra_game_classes", []),
                    deny_classes=gd.get("deny_classes", ["steamwebhelper"]),
                )

            if "layers" in data:
                config.layers = data["layers"]

        return config

    def reload(self, path: Optional[str] = None) -> "Config":
        """Reload config in-place, return self for chaining."""
        new = Config.load(path)
        self.daemon = new.daemon
        self.device = new.device
        self.lightbar = new.lightbar
        self.game_detection = new.game_detection
        self.layers = new.layers
        return self


DEFAULT_LAYERS = {
    "base": {
        "cross": "dispatch exec steam",
        "circle": "dispatch killactive",
        "triangle": "dispatch togglespecialworkspace",
        "square": "dispatch togglefloating",
        "dpad_up": "dispatch swapactiveworkspaces",
        "dpad_down": "dispatch togglesplit",
        "dpad_left": "dispatch mfact -0.05",
        "dpad_right": "dispatch mfact 0.05",
        "l1": "dispatch workspace r-1",
        "r1": "dispatch workspace r+1",
        "l3": "dispatch fullscreen",
        "r3": "dispatch exec rofi -show drun",
        "options": "dispatch exec rofi -show window",
        "create": "dispatch workspace empty",
    },
    "l2": {
        "cross": "dispatch pin",
        "circle": "dispatch togglegroup",
        "triangle": "dispatch focusmaster",
        "square": "dispatch layoutmsg orientationcenter",
        "dpad_down": "dispatch movetoworkspace special",
        "dpad_left": "dispatch changegroupactive b",
        "dpad_right": "dispatch changegroupactive f",
        "l1": "dispatch movetoworkspace r-1",
        "r1": "dispatch movetoworkspace r+1",
        "l3": "dispatch fullscreen",
        "r3": "dispatch exec slurp | grim -g -",
    },
    "r2": {
        "cross": "dispatch exec steam",
        "circle": "dispatch exec lutris",
        "triangle": "dispatch exec zen",
        "square": "dispatch exec steam -bigpicture",
        "l1": "dispatch exec pactl set-sink-volume @DEFAULT_SINK@ -5%",
        "r1": "dispatch exec pactl set-sink-volume @DEFAULT_SINK@ +5%",
    },
}