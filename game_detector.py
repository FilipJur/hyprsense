"""Automatic game detection via Hyprland IPC events.

Connects to Hyprland's .socket2.sock, reads activewindow/openwindow/closewindow
events, classifies windows as game/not-game, and invokes callbacks on game state
transitions with debouncing.
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("hyprsense")

# Hardcoded regex patterns that cover 95%+ of PC games.
#   steam_app_       — all Steam/Proton games (app IDs like 12345, fallback "default" from Lutris)
#   .*\.exe$        — Proton/Wine .exe windows (e.g. cs2.exe)
#   ^gamescope$     — Gamescope microcompositor sessions
GAME_PATTERNS = [
    re.compile(r"steam_app_"),
    re.compile(r"^steam$"),          # Steam client / Big Picture
    re.compile(r".*\.exe$"),
    re.compile(r"^gamescope$"),
]

# Window classes that MUST NOT be treated as games even if matched.
DEFAULT_DENY = {"steamwebhelper"}


def _get_hyprland_socket_path() -> str | None:
    """Construct path to Hyprland event socket.

    Uses $XDG_RUNTIME_DIR and $HYPRLAND_INSTANCE_SIGNATURE. Falls back to
    enumerating the first signature directory under $XDG_RUNTIME_DIR/hypr/.
    """
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000")
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not sig:
        hypr_dir = os.path.join(xdg_runtime, "hypr")
        if os.path.isdir(hypr_dir):
            for entry in sorted(os.listdir(hypr_dir)):
                sock = os.path.join(hypr_dir, entry, ".socket2.sock")
                if os.path.exists(sock):
                    sig = entry
                    break
    if not sig:
        return None
    return os.path.join(xdg_runtime, "hypr", sig, ".socket2.sock")


async def _get_active_window_class() -> str:
    """Query hyprctl for the current active window's class string."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "hyprctl", "-j", "activewindow",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout:
            data = json.loads(stdout)
            return data.get("class", "")
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"hyprctl activewindow failed: {e}")
    return ""


class GameDetector:
    """Detects game windows via Hyprland IPC socket events.

    Connects to Hyprland's .socket2.sock, reads activewindow/openwindow/
    closewindow events, classifies windows as game/not-game, and invokes
    callbacks on game state transitions.

    Debounce:
      - Enter game: 0.5 s delay; restarts on any class change.
      - Exit game:  2.0 s delay; cancelled if a game window re-appears.
    """

    def __init__(self, on_game_start, on_game_stop,
                 debounce_enter: float = 0.5,
                 debounce_exit: float = 2.0,
                 extra_game_classes: list[str] | None = None,
                 deny_classes: list[str] | None = None):
        self._on_game_start = on_game_start
        self._on_game_stop = on_game_stop
        self._debounce_enter = debounce_enter
        self._debounce_exit = debounce_exit
        self._running = False
        self._is_game = False

        # Compile patterns: hardcoded + user extras
        self._patterns = list(GAME_PATTERNS)
        for cls in (extra_game_classes or []):
            self._patterns.append(re.compile(re.escape(cls)))

        self._deny = DEFAULT_DENY | set(deny_classes or [])

        # Desktop-file game class cache (emulators, Lutris, Heroic, etc.)
        self._desktop_cache: dict[str, bool] = {}
        self._build_desktop_cache()

        # Debounce task handles
        self._enter_task: asyncio.Task | None = None
        self._exit_task: asyncio.Task | None = None

        # Window address → class (closewindow only carries address)
        self._window_class_cache: dict[str, str] = {}

        # Fullscreen state (for Big Picture / fullscreen launcher detection)
        self._fullscreen_active = False
        self._fullscreen_class: str | None = None

    # ------------------------------------------------------------------
    # Desktop-file cache
    # ------------------------------------------------------------------

    def _build_desktop_cache(self) -> None:
        """Scan .desktop files for game StartupWMClasses.

        Only files with Categories=Game and a StartupWMClass are cached.
        This catches emulators (RetroArch, PCSX2) and launcher-managed
        games (Lutris, Heroic) that set a proper WMClass.
        """
        desktop_dirs = [
            os.path.expanduser("~/.local/share/applications"),
            "/usr/share/applications",
            "/usr/local/share/applications",
        ]
        seen = 0
        for desk_dir in desktop_dirs:
            if not os.path.isdir(desk_dir):
                continue
            for entry in os.listdir(desk_dir):
                if not entry.endswith(".desktop"):
                    continue
                path = os.path.join(desk_dir, entry)
                wmclass = None
                is_game = False
                try:
                    with open(path) as f:
                        for line in f:
                            if line.startswith("Categories=") and "Game" in line:
                                is_game = True
                            if line.startswith("StartupWMClass="):
                                wmclass = line.split("=", 1)[1].strip()
                    if is_game and wmclass:
                        self._desktop_cache[wmclass] = True
                        seen += 1
                except OSError:
                    continue
        if seen:
            logger.info(f"Desktop cache: {seen} game window classes loaded")

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def _classify(self, window_class: str) -> bool:
        """Check if a window class string belongs to a game."""
        if not window_class:
            return False
        if window_class in self._deny:
            return False
        if window_class in self._desktop_cache:
            return True
        for pat in self._patterns:
            if pat.match(window_class):
                return True
        return False

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main loop: connect, read events, reconnect on failure."""
        self._running = True
        while self._running:
            try:
                await self._read_loop()
            except (OSError, ConnectionError, asyncio.IncompleteReadError) as e:
                logger.warning(f"Hyprland socket disconnected: {e}")
            except Exception as e:
                logger.error(f"Game detector error: {e}", exc_info=True)
            if self._running:
                logger.info("Reconnecting to Hyprland socket in 5 s …")
                await asyncio.sleep(5)

    async def _read_loop(self) -> None:
        socket_path = _get_hyprland_socket_path()
        if not socket_path:
            logger.warning("Cannot find Hyprland socket — game detection disabled")
            while self._running and not _get_hyprland_socket_path():
                await asyncio.sleep(5)
            return

        reader, writer = await asyncio.open_unix_connection(socket_path)
        logger.info(f"Game detector connected to {socket_path}")

        # Re-evaluate current window on (re)connect
        current_class = await _get_active_window_class()
        if current_class:
            self._evaluate(current_class)

        try:
            while self._running:
                try:
                    line = await reader.readline()
                except (OSError, ConnectionError):
                    raise

                if not line:
                    raise ConnectionError("EOF on socket")

                line = line.decode().strip()
                if not line or ">>" not in line:
                    continue

                event, _, data = line.partition(">>")

                if event == "activewindow":
                    parts = data.split(",", 1)
                    if parts:
                        self._evaluate(parts[0])

                elif event == "openwindow":
                    parts = data.split(",")
                    if len(parts) >= 3:
                        self._window_class_cache[parts[0]] = parts[2]

                elif event == "closewindow":
                    self._window_class_cache.pop(data, None)

                elif event == "fullscreen":
                    was_fullscreen = self._fullscreen_active
                    self._fullscreen_active = (data == "1")
                    if self._fullscreen_active != was_fullscreen:
                        current_class = await _get_active_window_class()
                        if current_class:
                            self._evaluate(current_class)
        finally:
            writer.close()
            await writer.wait_closed()

    # ------------------------------------------------------------------
    # Debounced transitions
    # ------------------------------------------------------------------

    def _evaluate(self, window_class: str) -> None:
        """Evaluate window class and trigger debounced transition if needed."""
        is_game = self._classify(window_class)

        if is_game and not self._is_game:
            # Potentially entering game mode — (re)start enter debounce.
            # Restart on any class change so splash→game flicker is safe.
            self._cancel_exit()
            self._cancel_enter()
            self._enter_task = asyncio.create_task(self._debounced_enter())

        elif not is_game and self._is_game:
            # Potentially exiting game mode — start exit debounce.
            # Do NOT cancel on subsequent non-game classes (just let the
            # timer run). Only a game-class window cancels the exit.
            if self._exit_task is None or self._exit_task.done():
                self._exit_task = asyncio.create_task(self._debounced_exit())

        elif is_game and self._is_game:
            # Still a game but class changed — cancel any lingering exit.
            self._cancel_exit()

    async def _debounced_enter(self) -> None:
        """Wait debounce period, then enter game mode."""
        await asyncio.sleep(self._debounce_enter)
        if self._is_game:
            return
        logger.info("Game window detected — entering game mode")
        self._is_game = True
        self._on_game_start()

    async def _debounced_exit(self) -> None:
        """Wait debounce period, then exit game mode."""
        await asyncio.sleep(self._debounce_exit)
        if not self._is_game:
            return
        logger.info("Game window gone — exiting game mode")
        self._is_game = False
        self._on_game_stop()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _cancel_enter(self) -> None:
        if self._enter_task and not self._enter_task.done():
            self._enter_task.cancel()
        self._enter_task = None

    def _cancel_exit(self) -> None:
        if self._exit_task and not self._exit_task.done():
            self._exit_task.cancel()
        self._exit_task = None

    async def stop(self) -> None:
        """Stop the detector and cancel pending timers."""
        self._running = False
        self._cancel_enter()
        self._cancel_exit()
