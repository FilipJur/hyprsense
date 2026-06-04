"""
Hyprctl dispatcher for HyprSense.
Executes window management commands via hyprctl.
"""

import asyncio
import os
import logging

logger = logging.getLogger("hyprsense")


async def dispatch(command: str, cooldown: float = 0.0) -> bool:
    """
    Execute a hyprctl dispatch command.
    cooldown: seconds to wait before executing (for rate-limiting).
    Returns True on success.
    """
    if cooldown > 0:
        await asyncio.sleep(cooldown)

    # Expand env vars in commands (e.g. $TERMINAL, $BROWSER)
    expanded = _expand_env(command)

    try:
        proc = await asyncio.create_subprocess_shell(
            f"hyprctl dispatch {expanded}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            logger.warning(f"hyprctl dispatch {command}: {err}")
            return False
        return True
    except OSError as e:
        logger.error(f"hyprctl dispatch failed: {e}")
        return False


async def exec_command(command: str) -> bool:
    """
    Execute an arbitrary shell command (for pactl, playerctl, etc.).
    command starts with 'exec ' already stripped by the dispatcher.
    """
    # Expand env vars
    expanded = _expand_env(command)

    try:
        proc = await asyncio.create_subprocess_shell(
            expanded,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0
    except OSError as e:
        logger.error(f"Command failed: {e}")
        return False


async def run_action(action: str, cooldown: float = 0.0) -> None:
    """
    Run a mapping action from the config.
    Actions are either 'dispatch <command>' or 'exec <command>'.
    """
    if not action:
        return

    if action.startswith("dispatch "):
        cmd = action[len("dispatch "):]
        await dispatch(cmd, cooldown)
    elif action.startswith("exec "):
        cmd = action[len("exec "):]
        await exec_command(cmd)
    else:
        logger.warning(f"Unknown action format: {action}")


def _expand_env(s: str) -> str:
    """Expand $VAR and ${VAR} references using os.environ."""
    # Simple env var expansion — handles $VAR style
    result = []
    i = 0
    while i < len(s):
        if s[i] == '$' and i + 1 < len(s):
            if s[i + 1] == '{':
                end = s.find('}', i + 2)
                if end != -1:
                    var = s[i + 2:end]
                    result.append(os.environ.get(var, ''))
                    i = end + 1
                    continue
            else:
                # Read variable name
                j = i + 1
                while j < len(s) and (s[j].isalnum() or s[j] == '_'):
                    j += 1
                var = s[i + 1:j]
                result.append(os.environ.get(var, ''))
                i = j
                continue
        result.append(s[i])
        i += 1
    return ''.join(result)
