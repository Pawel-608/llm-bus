"""On-demand agents: a GNU screen session per agent, started when someone needs it.

An agent's config.toml may carry a [spawn] table:

    [spawn]
    cmd     = "claude --dangerously-skip-permissions 'You are {name}. Run `llm-bus -c {name} whoami` and act.'"
    session = "bob"        # screen session name (default: agent name)
    cwd     = "/repo"      # working dir (default: agent folder)
    idle_timeout = 30      # minutes; `llm-bus reap` kills sessions idle longer than this

Liveness == "a screen session with that name exists". Spawning == `screen -dmS SESSION sh -c CMD`.
The `cmd` string may use {name}, {session}, {dir} placeholders.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

SPAWN_LOCK = ".spawn.lock"


@dataclass
class Spawn:
    session: str
    cmd: str | None = None
    cwd: str | None = None
    idle_timeout: float | None = (
        None  # minutes without bus activity before `reap` kills it
    )

    @classmethod
    def from_config(cls, name: str, data: dict | None) -> Spawn:
        data = data or {}
        it = data.get("idle_timeout")
        return cls(
            session=str(data.get("session") or name),
            cmd=data.get("cmd") or None,
            cwd=data.get("cwd") or None,
            idle_timeout=float(it) if it is not None else None,
        )


def _screen() -> str:
    exe = os.environ.get("LLM_BUS_SCREEN") or shutil.which("screen")
    if not exe or not Path(exe).is_file():
        raise RuntimeError(
            f"`screen` not found ({exe or 'not in PATH'}; install GNU screen)"
        )
    return exe


def list_sessions() -> dict[str, list[str]]:
    """{session_name: [pid, ...]} from `screen -ls` (exit code is unreliable; parse output)."""
    out = subprocess.run(
        [_screen(), "-ls"], capture_output=True, text=True, check=False
    ).stdout
    found: dict[str, list[str]] = {}
    for m in re.finditer(r"^\s+(\d+)\.(\S+)\s+\(", out, re.MULTILINE):
        found.setdefault(m.group(2), []).append(m.group(1))
    return found


def is_alive(sp: Spawn) -> bool:
    return sp.session in list_sessions()


def start(agent_name: str, agent_dir: Path, sp: Spawn) -> dict:
    """Start the agent's screen session. Returns status: spawned | alive | no-cmd."""
    if not sp.cmd:
        return {"agent": agent_name, "session": sp.session, "status": "no-cmd"}
    lock = agent_dir / SPAWN_LOCK
    try:  # serialize concurrent spawners (two senders DMing a dead agent at once)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return {"agent": agent_name, "session": sp.session, "status": "alive"}
    try:
        os.close(fd)
        if is_alive(sp):
            return {"agent": agent_name, "session": sp.session, "status": "alive"}
        cmd = sp.cmd.format(name=agent_name, session=sp.session, dir=str(agent_dir))
        cwd = Path(sp.cwd).expanduser() if sp.cwd else agent_dir
        subprocess.run(
            [_screen(), "-dmS", sp.session, "sh", "-c", cmd],
            cwd=cwd,
            check=True,
        )
        return {
            "agent": agent_name,
            "session": sp.session,
            "status": "spawned",
            "cmd": cmd,
            "cwd": str(cwd),
        }
    finally:
        lock.unlink(missing_ok=True)


def kill(sp: Spawn) -> bool:
    """Quit the session; True if one was running."""
    pids = list_sessions().get(sp.session)
    if not pids:
        return False
    for pid in pids:
        subprocess.run(
            [_screen(), "-S", f"{pid}.{sp.session}", "-X", "quit"],
            capture_output=True,
            check=False,
        )
    return True
