"""Agent identity folders and project files. Nothing is auto-discovered: both are passed explicitly.

Agent:   ~/.llm_bus/agents/<name>/   (-c NAME | -c PATH-TO-FOLDER | -c PATH-TO-config.toml)
           config.toml   name, role
           CONTEXT.md    free-form knowledge / instructions for the agent
           NOTES.md      memory the agent appends to (`llm-bus -c NAME remember ...`)
           state.json    CLI-managed state: read cursors per project/group
           [spawn] table in config.toml: how to start this agent on demand (see spawn.py)
Project: .llm_bus_project TOML file, usually in the repo   (-p PATH | -p NAME)
           project = "demo"
           group   = "dev"     # optional default group
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .spawn import Spawn

CONFIG_FILE = "config.toml"
CONTEXT_FILE = "CONTEXT.md"
NOTES_FILE = "NOTES.md"
STATE_FILE = "state.json"
PROJECT_FILE = ".llm_bus_project"


def bus_home() -> Path:
    return Path(os.environ.get("LLM_BUS_HOME") or Path.home() / ".llm_bus")


def agents_dir() -> Path:
    return bus_home() / "agents"


NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def check_name(kind: str, value: str) -> str:
    """Agent/project/group/session names: safe in paths, cursor keys, DM group ids, screen, sh."""
    if not NAME_RE.match(value or ""):
        raise ValueError(
            f"invalid {kind} name {value!r}: use letters, digits, '.', '_', '-' (must start alphanumeric)"
        )
    return value


def _toml_str(v: str) -> str:
    return json.dumps(v, ensure_ascii=False)  # valid TOML basic string


# --- agent -------------------------------------------------------------
DM_PROJECT = "_dm"
HUB_NAME = "hub"
HUB_CONTEXT = """# hub — the agent directory

You are the central directory of this llm-bus. You know every agent (name, role, context) —
your `whoami` prints the full DIRECTORY. Other agents DM you questions like
"I want to do X, is there an agent that can help?" (they use `llm-bus -c NAME ask "..."`).

Your job:
- Answer with the best-matching agent name(s), their role, and how to reach them
  (`llm-bus -c NAME dm <agent> "..."`). Say clearly if nobody fits.
- Keep the directory accurate: when you learn what an agent actually does, `remember` it.
- Loop: `llm-bus -c hub dm --wait` → answer with `llm-bus -c hub dm <asker> "..."` → repeat.
"""


def dm_group(a: str, b: str) -> str:
    return "~".join(sorted((a, b)))


@dataclass
class Agent:
    name: str
    role: str | None
    dir: Path
    hub: bool = False
    state: dict = field(default_factory=dict)
    spawn: Spawn = field(default_factory=lambda: Spawn(session=""))

    @property
    def config_path(self) -> Path:
        return self.dir / CONFIG_FILE

    @property
    def context_path(self) -> Path:
        return self.dir / CONTEXT_FILE

    @property
    def notes_path(self) -> Path:
        return self.dir / NOTES_FILE

    @property
    def state_path(self) -> Path:
        return self.dir / STATE_FILE

    def context(self) -> str:
        return self.context_path.read_text() if self.context_path.is_file() else ""

    def notes(self) -> str:
        return self.notes_path.read_text() if self.notes_path.is_file() else ""

    def remember(self, text: str) -> None:
        with self.notes_path.open("a") as f:
            f.write(text.rstrip("\n") + "\n")

    # cursors: last message id this agent has consumed, per "project/group"
    def cursor(self, project: str, group: str) -> int:
        return int(self.state.get("cursors", {}).get(f"{project}/{group}", 0))

    def set_cursor(self, project: str, group: str, mid: int) -> None:
        cur = self.state.setdefault("cursors", {})
        key = f"{project}/{group}"
        if mid > int(cur.get(key, 0)):
            cur[key] = mid
            self._save_state()

    def _save_state(self) -> None:
        """Merge with what's on disk (another process as the same agent may have advanced
        cursors meanwhile), then write atomically via tmp + os.replace."""
        with open(self.dir / ".state.lock", "a+") as lk:
            fcntl.flock(
                lk, fcntl.LOCK_EX
            )  # serialize read-merge-write across processes
            try:
                self._merge_and_write()
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)

    def _merge_and_write(self) -> None:
        disk = _read_json(self.state_path)
        merged = dict(disk)
        merged.update({k: v for k, v in self.state.items() if k != "cursors"})
        cursors = dict(disk.get("cursors", {}))
        for k, v in self.state.get("cursors", {}).items():
            cursors[k] = max(int(v), int(cursors.get(k, 0)))
        merged["cursors"] = cursors
        self.state["cursors"] = dict(cursors)
        tmp = self.state_path.with_name(f".{STATE_FILE}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(merged, indent=2) + "\n")
        os.replace(tmp, self.state_path)


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def resolve_agent_dir(ref: str) -> Path:
    p = Path(ref).expanduser()
    if p.is_dir():
        return p
    if p.is_file():
        return p.parent
    if "/" in ref or ref.endswith(".toml"):
        return p.parent if ref.endswith(".toml") else p
    return agents_dir() / ref


def load_agent(ref: str) -> Agent:
    d = resolve_agent_dir(ref)
    cfg = d / CONFIG_FILE
    if not cfg.is_file():
        raise ValueError(f"agent not found: {cfg} (create with `llm-bus init NAME`)")
    try:
        data = tomllib.loads(cfg.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{cfg}: invalid TOML: {e}") from None
    if not data.get("name"):
        raise ValueError(f"{cfg}: missing required key 'name'")
    try:
        check_name(
            "agent", data["name"]
        )  # hand-edited configs must obey the same rules
    except ValueError as e:
        raise ValueError(f"{cfg}: {e}") from None
    state = _read_json(d / STATE_FILE)
    return Agent(
        name=data["name"],
        role=data.get("role"),
        dir=d,
        hub=bool(data.get("hub", False)),
        state=state,
        spawn=Spawn.from_config(data["name"], data.get("spawn")),
    )


def create_agent(
    name: str,
    role: str | None,
    context: str | None,
    dir: Path | None = None,
    force=False,
    hub=False,
    spawn: Spawn | None = None,
) -> Agent:
    check_name("agent", name)
    if spawn and spawn.session:
        check_name("session", spawn.session)
    d = dir or agents_dir() / name
    cfg = d / CONFIG_FILE
    if cfg.exists() and not force:
        raise ValueError(f"{cfg} already exists (use --force to overwrite)")
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"name = {_toml_str(name)}"]
    if role:
        lines.append(f"role = {_toml_str(role)}")
    if hub:
        lines.append("hub = true")
    if spawn and spawn.cmd:
        lines += ["", "[spawn]", f"cmd = {_toml_str(spawn.cmd)}"]
        if spawn.session and spawn.session != name:
            lines.append(f"session = {_toml_str(spawn.session)}")
        if spawn.cwd:
            lines.append(f"cwd = {_toml_str(spawn.cwd)}")
        if spawn.idle_timeout is not None:
            lines.append(f"idle_timeout = {spawn.idle_timeout:g}")
    cfg.write_text("\n".join(lines) + "\n")
    ctx = d / CONTEXT_FILE
    if context is not None or not ctx.exists():
        ctx.write_text(
            (
                context
                or f"# {name}\n\nRole: {role or '(unspecified)'}\n\n"
                "<!-- Describe this agent's responsibilities, conventions, who it talks to. -->\n"
            ).rstrip("\n")
            + "\n"
        )
    (d / NOTES_FILE).touch()
    return load_agent(str(d))


def list_agents() -> list[dict]:
    out = []
    if agents_dir().is_dir():
        for d in sorted(agents_dir().iterdir()):
            if (d / CONFIG_FILE).is_file():
                try:
                    a = load_agent(str(d))
                    out.append(
                        {"name": a.name, "role": a.role, "hub": a.hub, "dir": str(d)}
                    )
                except ValueError as e:
                    out.append({"name": d.name, "error": str(e), "dir": str(d)})
    return out


def directory(query: str | None = None) -> list[dict]:
    """All agents with role, context, notes; optionally filtered by a case-insensitive substring."""
    out = []
    q = (query or "").lower()
    for a in list_agents():
        if "error" in a:
            continue
        ag = load_agent(a["dir"])
        entry = {
            "name": ag.name,
            "role": ag.role,
            "hub": ag.hub,
            "context": ag.context().strip(),
            "notes": ag.notes().strip(),
        }
        hay = " ".join(str(v) for v in entry.values()).lower()
        if not q or q in hay:
            out.append(entry)
    return out


# --- project -------------------------------------------------------------
@dataclass
class ProjectRef:
    name: str
    group: str | None = None
    path: Path | None = None


def load_project(ref: str) -> ProjectRef:
    """`ref` is a path to a .llm_bus_project file (or a dir containing one), else a bare name."""
    p = Path(ref).expanduser()
    if p.is_dir():
        p = p / PROJECT_FILE
    if p.is_file():
        try:
            data = tomllib.loads(p.read_text())
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"{p}: invalid TOML: {e}") from None
        if not data.get("project"):
            raise ValueError(f"{p}: missing required key 'project'")
        return ProjectRef(name=data["project"], group=data.get("group"), path=p)
    if "/" in ref or ref.startswith("."):
        raise ValueError(f"project file not found: {p}")
    return ProjectRef(name=ref)


def write_project_file(path: Path, name: str, group: str | None, force=False) -> Path:
    check_name("project", name)
    if group:
        check_name("group", group)
    if path.is_dir():
        path = path / PROJECT_FILE
    if path.exists() and not force:
        raise ValueError(f"{path} already exists (use --force to overwrite)")
    lines = [f"project = {_toml_str(name)}"]
    if group:
        lines.append(f"group = {_toml_str(group)}")
    path.write_text("\n".join(lines) + "\n")
    return path
