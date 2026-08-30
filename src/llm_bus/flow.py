"""Flows: declarative multi-agent loops with bus-enforced routing.

A flow file (TOML) names the agents (nodes), how each is run (runner CLI + model, optionally in
its own git worktree) and the routes between them. `flow up` materializes it into ordinary
llm-bus agents (`<flow>.<node>`), a project/group, and worktrees. Each node's spawn command is

    <runner cmd>; llm-bus -c <flow>.<node> flow done --rc $?

so when the LLM process exits, THE BUS decides who runs next: the node's `next` list by default,
or `on.<signal>` if the agent called `llm-bus -c NAME flow signal <signal>` before exiting.
Targets are started as screen sessions (spawn.py). A `[supervisor]` node is woken every N
handoffs (and on the `blocked` signal) and may rewrite the flow file with `flow add-agent`,
`flow route`, ... — `flow up` is idempotent, so edits are applied by re-running it.

    name = "ml_loop"
    project = "ml"                 # bus project; the group is the flow name
    repo = "~/projects/ml"         # worktrees under <repo>/.llm_bus_worktrees/
    entry = "implementer"
    max_turns = 200

    [runners.claude]
    cmd = "claude --dangerously-skip-permissions --model {model} -p {prompt}"

    [agents.implementer]
    runner = "claude"
    model = "claude-opus-5"
    role = "iterate on the model"
    worktree = true
    next = ["reviewer"]
    [agents.implementer.on]
    blocked = ["supervisor"]

    [supervisor]
    runner = "claude"
    model = "claude-sonnet-5"
    every = 5
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import spawn as spawner
from .config import (
    _toml_str,
    agents_dir,
    check_name,
    create_agent,
    load_agent,
)
from .db import BusError
from .spawn import Spawn

FLOW_FILE = "flow.toml"
WORKTREES_DIR = ".llm_bus_worktrees"
SUPERVISOR = "supervisor"
SIGNAL_KEY = "flow_signal"

DEFAULT_RUNNERS = {
    "claude": {
        "cmd": "claude --dangerously-skip-permissions --model {model} -p {prompt}",
        "model": "claude-opus-5",
    },
    "codex": {
        "cmd": "codex exec --dangerously-bypass-approvals-and-sandbox --model {model} {prompt}",
        "model": "gpt-5",
    },
    "kimi": {
        "cmd": "kimi --model {model} -p {prompt}",
        "model": "kimi-k2",
    },
}

EXAMPLE = """\
name = "ml_loop"
project = "ml"
repo = "~/projects/ml"          # optional: enables per-agent git worktrees
entry = "implementer"           # receives `llm-bus flow run` tasks
max_turns = 200                 # safety stop; supervisor can raise it

# Runner templates: {model} and {prompt} are substituted, {prompt} is already shell-quoted.
# Built-ins exist for claude/codex/kimi; override or add your own here.
[runners.kimi]
cmd = "kimi --model {model} -p {prompt}"
model = "kimi-k2"

[agents.implementer]
runner = "claude"
model = "claude-opus-5"
role = "implement the current idea on the ML model; then answer review comments: fix them, or argue why it is already good"
worktree = true                 # own git worktree + branch llm-bus/ml_loop/implementer
next = ["reviewer"]

[agents.reviewer]
runner = "codex"
model = "gpt-5"
role = "review the implementer's latest change (and its replies to your comments). No comments → signal approve immediately; comments → just exit; implementer pushed back and you still object → signal dispute"
worktree = "implementer"        # share implementer's worktree
next = ["implementer"]          # comments → implementer fixes or pushes back
[agents.reviewer.on]
approve = ["ideas"]             # good enough → next idea
dispute = ["resolver"]          # implementer disagrees and you still object → escalate

[agents.resolver]
runner = "claude"
model = "claude-sonnet-5"
role = "arbiter: read the implementer/reviewer dispute and rule on it; you never write code"
next = []                       # must signal
[agents.resolver.on]
ok = ["ideas"]                  # `llm-bus -c ml_loop.resolver flow signal ok`
fix = ["implementer"]           # the reviewer is right; implementer must fix

[agents.ideas]
runner = "kimi"
role = "propose the next improvement to try"
next = ["implementer"]
[agents.ideas.on]
dry = ["scout"]                 # no ideas left → go look outside

[agents.scout]
runner = "claude"
role = "search the web for new approaches relevant to our problem and summarize them"
next = ["ideas"]

[supervisor]
runner = "claude"
model = "claude-sonnet-5"
every = 5                       # woken every 5 handoffs (and on the `blocked` signal)
"""


@dataclass
class Node:
    name: str
    runner: str = "claude"
    model: str | None = None
    role: str | None = None
    context: str | None = None
    worktree: bool | str = False  # True = own worktree; "other" = share other's
    cwd: str | None = None
    next: list[str] = field(default_factory=list)
    on: dict[str, list[str]] = field(default_factory=dict)
    idle_timeout: float | None = None


@dataclass
class Flow:
    name: str
    project: str
    path: Path
    entry: str | None = None
    repo: str | None = None
    max_turns: int = 100
    runners: dict[str, dict] = field(default_factory=dict)
    agents: dict[str, Node] = field(default_factory=dict)
    supervisor: Node | None = None
    every: int = 5

    @property
    def group(self) -> str:
        return self.name

    def agent_name(self, node: str) -> str:
        return f"{self.name}.{node}"

    def nodes(self) -> dict[str, Node]:
        d = dict(self.agents)
        if self.supervisor:
            d[SUPERVISOR] = self.supervisor
        return d

    def runner(self, node: Node) -> dict:
        r = {
            **DEFAULT_RUNNERS.get(node.runner, {}),
            **self.runners.get(node.runner, {}),
        }
        if not r.get("cmd"):
            raise BusError(
                f"{self.path}: unknown runner {node.runner!r} for agent {node.name!r}"
                f" (define [runners.{node.runner}] with a cmd)"
            )
        return r


# --- file I/O ------------------------------------------------------------------
def _node(name: str, d: dict) -> Node:
    if not isinstance(d, dict):
        raise BusError(f"agent {name!r}: expected a table")
    on = d.get("on") or {}
    if not isinstance(on, dict) or not all(isinstance(v, list) for v in on.values()):
        raise BusError(f"agent {name!r}: `on` must map signal -> list of agents")
    return Node(
        name=name,
        runner=str(d.get("runner") or "claude"),
        model=d.get("model"),
        role=d.get("role"),
        context=d.get("context"),
        worktree=d.get("worktree", False),
        cwd=d.get("cwd"),
        next=[str(x) for x in (d.get("next") or [])],
        on={str(k): [str(x) for x in v] for k, v in on.items()},
        idle_timeout=d.get("idle_timeout"),
    )


def load_flow(ref: str) -> Flow:
    p = Path(ref).expanduser()
    if p.is_dir():
        p = p / FLOW_FILE
    if not p.is_file():
        raise BusError(f"flow file not found: {p} (see `llm-bus flow example`)")
    try:
        data = tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as e:
        raise BusError(f"{p}: invalid TOML: {e}") from None
    name = data.get("name")
    if not name:
        raise BusError(f"{p}: missing required key 'name'")
    try:
        check_name("flow", name)
    except ValueError as e:
        raise BusError(str(e)) from None
    agents = {}
    for n, d in (data.get("agents") or {}).items():
        try:
            check_name("agent", n)
        except ValueError as e:
            raise BusError(f"{p}: {e}") from None
        if n == SUPERVISOR:
            raise BusError(
                f"{p}: '{SUPERVISOR}' is reserved; use the [supervisor] table"
            )
        agents[n] = _node(n, d)
    sup_d = data.get("supervisor")
    sup = _node(SUPERVISOR, sup_d) if sup_d else None
    flow = Flow(
        name=name,
        project=str(data.get("project") or name),
        path=p.resolve(),
        entry=data.get("entry"),
        repo=data.get("repo"),
        max_turns=int(data.get("max_turns", 100)),
        runners=dict(data.get("runners") or {}),
        agents=agents,
        supervisor=sup,
        every=int((sup_d or {}).get("every", 5)),
    )
    _validate(flow)
    return flow


def _validate(flow: Flow) -> None:
    nodes = flow.nodes()
    if flow.entry and flow.entry not in nodes:
        raise BusError(f"{flow.path}: entry {flow.entry!r} is not an agent")
    for n in nodes.values():
        for t in n.next + [t for ts in n.on.values() for t in ts]:
            if t not in nodes:
                raise BusError(f"{flow.path}: agent {n.name!r} routes to unknown {t!r}")
        if isinstance(n.worktree, str) and n.worktree not in flow.agents:
            raise BusError(
                f"{flow.path}: agent {n.name!r} shares worktree of unknown {n.worktree!r}"
            )
        if n.worktree and not flow.repo:
            raise BusError(
                f"{flow.path}: agent {n.name!r} wants a worktree but no `repo` set"
            )
        flow.runner(n)


def _toml_list(xs: list[str]) -> str:
    return "[" + ", ".join(_toml_str(x) for x in xs) + "]"


def _node_lines(n: Node, header: str) -> list[str]:
    out = [f"[{header}]", f"runner = {_toml_str(n.runner)}"]
    if n.model:
        out.append(f"model = {_toml_str(n.model)}")
    if n.role:
        out.append(f"role = {_toml_str(n.role)}")
    if n.context:
        out.append(f"context = {_toml_str(n.context)}")
    if n.worktree:
        out.append(
            "worktree = true"
            if n.worktree is True
            else f"worktree = {_toml_str(n.worktree)}"
        )
    if n.cwd:
        out.append(f"cwd = {_toml_str(n.cwd)}")
    if n.idle_timeout is not None:
        out.append(f"idle_timeout = {n.idle_timeout:g}")
    if n.name != SUPERVISOR:
        out.append(f"next = {_toml_list(n.next)}")
    if n.on:
        out.append(f"[{header}.on]")
        out += [f"{_toml_str(k)} = {_toml_list(v)}" for k, v in n.on.items()]
    return out


def dump_flow(flow: Flow) -> str:
    lines = [f"name = {_toml_str(flow.name)}", f"project = {_toml_str(flow.project)}"]
    if flow.repo:
        lines.append(f"repo = {_toml_str(flow.repo)}")
    if flow.entry:
        lines.append(f"entry = {_toml_str(flow.entry)}")
    lines.append(f"max_turns = {flow.max_turns}")
    for rname, r in flow.runners.items():
        lines += ["", f"[runners.{rname}]"]
        lines += [f"{k} = {_toml_str(str(v))}" for k, v in r.items()]
    for n in flow.agents.values():
        lines += [""] + _node_lines(n, f"agents.{n.name}")
    if flow.supervisor:
        s = flow.supervisor
        lines += [""] + _node_lines(s, SUPERVISOR)
        # `every` must sit before any sub-table of [supervisor]
        idx = lines.index(f"[{SUPERVISOR}]") + 1
        lines.insert(idx, f"every = {flow.every}")
    return "\n".join(lines) + "\n"


def save_flow(flow: Flow) -> None:
    _validate(flow)
    tmp = flow.path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(dump_flow(flow))
    os.replace(tmp, flow.path)


# --- materialization -----------------------------------------------------------
def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def ensure_worktree(flow: Flow, node: str) -> Path:
    repo = Path(flow.repo).expanduser().resolve()
    if _git(repo, "rev-parse", "--is-inside-work-tree").returncode != 0:
        raise BusError(f"{repo} is not a git repository (flow `repo`)")
    path = repo / WORKTREES_DIR / f"{flow.name}-{node}"
    if path.is_dir():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    branch = f"llm-bus/{flow.name}/{node}"
    exists = (
        _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode
        == 0
    )
    r = _git(
        repo,
        "worktree",
        "add",
        *([] if exists else ["-b", branch]),
        str(path),
        *([branch] if exists else []),
    )
    if r.returncode != 0:
        raise BusError(f"git worktree add failed for {node}: {r.stderr.strip()}")
    return path


def node_cwd(flow: Flow, node: Node, worktrees: dict[str, Path]) -> Path | None:
    if node.cwd:
        return Path(node.cwd).expanduser()
    if node.worktree is True:
        return worktrees[node.name]
    if isinstance(node.worktree, str):
        return worktrees[node.worktree]
    if flow.repo:
        return Path(flow.repo).expanduser()
    return None


def _routes_text(flow: Flow, n: Node) -> str:
    lines = [
        f"- default (just exit): → {', '.join(n.next) or '(nobody; the loop pauses)'}"
    ]
    for sig, ts in n.on.items():
        lines.append(
            f"- `llm-bus -c {flow.agent_name(n.name)} flow signal {sig}` then exit: → {', '.join(ts) or '(nobody)'}"
        )
    if flow.supervisor and "blocked" not in n.on:
        lines.append(
            f"- `llm-bus -c {flow.agent_name(n.name)} flow signal blocked` then exit: → supervisor"
        )
    return "\n".join(lines)


def graph_text(flow: Flow) -> str:
    """Compact routing table:

    implementer  next → reviewer          (claude/opus; role)
    reviewer     next → implementer       (codex/gpt-5; role)
                 on approve → ideas
    """
    nodes = flow.nodes()
    width = max((len(n) for n in nodes), default=0)
    out = [
        (
            f"flow {flow.name}  project={flow.project} group={flow.group}  entry={flow.entry or '-'}"
            f"  max_turns={flow.max_turns}"
        )
    ]
    for n in flow.agents.values():
        model = n.model or flow.runner(n).get("model") or "?"
        wt = (
            "own worktree"
            if n.worktree is True
            else (f"worktree of {n.worktree}" if n.worktree else "")
        )
        role = (n.role or "").split(". ")[0].split(";")[0]
        role = role[:57] + "…" if len(role) > 58 else role
        info = "; ".join(x for x in (f"{n.runner}/{model}", wt, role) if x)
        rows = [("next", n.next)] + [(f"on {sig}", ts) for sig, ts in n.on.items()]
        for i, (label, ts) in enumerate(rows):
            name = n.name if i == 0 else ""
            line = f"  {name:<{width}}  {label} → {', '.join(ts) or '–'}"
            out.append(f"{line:<{width + 30}}  ({info})" if i == 0 else line)
    if flow.supervisor:
        s = flow.supervisor
        model = s.model or flow.runner(s).get("model") or "?"
        out.append(
            f"  {SUPERVISOR:<{width}}  every {flow.every} handoffs, on blocked, on errors"
            f"  ({s.runner}/{model})"
        )
    return "\n".join(out)


def _agent_context(flow: Flow, n: Node, cwd: Path | None) -> str:
    me = flow.agent_name(n.name)
    A = f"llm-bus -c {me} -p {flow.project}"
    peers = ", ".join(
        f"{k} ({v.role})" if v.role else k
        for k, v in flow.agents.items()
        if k != n.name
    )
    return f"""# {me} — node `{n.name}` of flow `{flow.name}`

Role: {n.role or "(unspecified)"}

You are one step in an automated loop. You are started by the bus when it is your turn, do your
work, report it, and EXIT. Routing is enforced by the bus after you exit — you never start
other agents yourself.

## Every run
1. `{A} whoami` — your context, notes, unread counts.
2. `{A} read --unread` — the shared channel `{flow.project}/{flow.group}`; the last handoff
   line tells you why you were started. `{A} read -n 40` for more history.
3. Do your job{f" in `{cwd}`" if cwd else ""}. Other agents only see what you post.
4. `{A} send "..."` — a concrete report (what you did, results, what the next agent should do).
   Use `{A} send --stdin` for long text. `llm-bus -c {me} remember "..."` for things to keep.
5. Choose the route and exit (the runner process ending IS the handoff):
{_routes_text(flow, n)}

Peers in this flow: {peers or "-"}
Flow file (read-only for you; the supervisor edits it): {flow.path}
{("" if not n.context else chr(10) + "## Instructions" + chr(10) + n.context)}"""


def _supervisor_context(flow: Flow) -> str:
    me = flow.agent_name(SUPERVISOR)
    A = f"llm-bus -c {me} -p {flow.project}"
    F = "llm-bus flow"
    return f"""# {me} — supervisor of flow `{flow.name}`

You monitor this multi-agent loop and repair it. You are woken every {flow.every} handoffs,
whenever an agent signals `blocked`, and when the flow hits `max_turns`. Do your check, act,
report, and EXIT (you are not part of the routing graph; nothing runs after you unless you start it).

## Every run
1. `{A} whoami`, then `{A} read -n 60` — recent traffic on `{flow.project}/{flow.group}`.
2. `{F} status {flow.path}` — turn counter, status, which sessions are alive (also `llm-bus ps`).
3. Look for: agents going in circles, ignoring reviews, never signaling `done`/`dry`,
   crashes (`flow done --rc N` with N != 0 shows up in the handoff line), a missing role.
4. Fix the graph if needed — edits are applied immediately (idempotent `flow up`):
   - `{F} show {flow.path}`
   - `{F} add-agent {flow.path} NAME --runner claude --model M --role "..." --next a,b [--on sig=a,b] [--worktree]`
   - `{F} rm-agent {flow.path} NAME`
   - `{F} route {flow.path} FROM TO [--on SIGNAL]`   /   `{F} unroute {flow.path} FROM TO [--on SIGNAL]`
   - `{F} set {flow.path} max_turns 400`   (also `entry`, `every`)
   - `{F} run {flow.path} "instructions" --to NODE`   start a specific node with a message
   - `{F} stop {flow.path}` / `{F} resume {flow.path}`   pause/unpause routing
   - `llm-bus kill {flow.name}.NODE`   stop a runaway session
   New/changed agents' CONTEXT.md is regenerated by the bus; give role-specific guidance via
   `llm-bus -c {flow.name}.NODE remember "..."` (NOTES.md survives regeneration).
5. `{A} send "[supervisor] ..."` — say what you observed and changed, then exit.
   `llm-bus -c {me} remember "..."` for observations to keep across runs.

Current graph:
{graph_text(flow)}
"""


def _spawn_cmd(flow: Flow, n: Node, me: str) -> str:
    r = flow.runner(n)
    model = n.model or r.get("model")
    tmpl = str(r["cmd"])
    prompt = (
        f"You are llm-bus agent {me}. Run `llm-bus -c {me} -p {flow.project} whoami` and follow the"
        " CONTEXT it prints exactly: read unread, do your job, post a report, choose the route"
        " (signal or not), then exit."
    )
    runner = tmpl.replace("{model}", shlex.quote(model or "")).replace(
        "{prompt}", shlex.quote(prompt)
    )
    return f"{runner}; llm-bus -c {me} flow done --rc $?"


def flow_up(bus, flow: Flow) -> dict:
    """Create/refresh project, group, agents, worktrees. Idempotent."""
    bus.ensure_group(flow.project, flow.group)
    worktrees = {
        n: ensure_worktree(flow, n)
        for n, nd in flow.agents.items()
        if nd.worktree is True
    }
    created = []
    for n in flow.nodes().values():
        me = flow.agent_name(n.name)
        cwd = node_cwd(flow, n, worktrees)
        ctx = (
            _supervisor_context(flow)
            if n.name == SUPERVISOR
            else _agent_context(flow, n, cwd)
        )
        try:
            a = create_agent(
                me,
                n.role or ("flow supervisor" if n.name == SUPERVISOR else None),
                ctx,
                force=True,
                spawn=Spawn(
                    session=me,
                    cmd=_spawn_cmd(flow, n, me),
                    cwd=str(cwd) if cwd else None,
                    idle_timeout=n.idle_timeout,
                ),
            )
        except ValueError as e:
            raise BusError(str(e)) from None
        with a.config_path.open("a") as f:
            f.write(
                f"\n[flow]\nfile = {_toml_str(str(flow.path))}\nnode = {_toml_str(n.name)}\n"
            )
        bus.join_group(flow.project, flow.group, me, a.role)
        created.append({"agent": me, "node": n.name, "cwd": str(cwd) if cwd else None})
    # agents removed from the file: leave their folders, but make sure they're not running
    prefix = flow.name + "."
    for d in agents_dir().iterdir() if agents_dir().is_dir() else []:
        if d.name.startswith(prefix) and d.name[len(prefix) :] not in flow.nodes():
            try:
                spawner.kill(load_agent(str(d)).spawn)
            except Exception as e:  # noqa: BLE001 — best effort
                print(f"(could not stop {d.name}: {e})", file=sys.stderr)
    bus.flow_state(flow.name)
    return {
        "flow": flow.name,
        "project": flow.project,
        "group": flow.group,
        "agents": created,
        "worktrees": {k: str(v) for k, v in worktrees.items()},
    }


# --- runtime --------------------------------------------------------------------
def agent_flow(agent) -> tuple[Flow, str]:
    """(flow, node) for an agent created by `flow up`."""
    data = tomllib.loads(agent.config_path.read_text()).get("flow") or {}
    if not data.get("file"):
        raise BusError(
            f"{agent.name} is not a flow agent (no [flow] table in its config)"
        )
    return load_flow(data["file"]), str(data["node"])


def _start(bus, flow: Flow, node: str, args) -> dict:
    me = flow.agent_name(node)
    try:
        a = load_agent(me)
        r = spawner.start(a.name, a.dir, a.spawn)
    except Exception as e:  # noqa: BLE001 — routing must report, not crash
        r = {"agent": me, "status": "error", "error": f"{type(e).__name__}: {e}"}
    if r["status"] == "spawned":
        bus.touch(me, "spawned")
    if not args.json and r["status"] != "alive":
        print(
            f"({r['status']}: {me}{' — ' + r['error'] if 'error' in r else ''})",
            file=sys.stderr,
        )
    return r


def _post(bus, flow: Flow, sender: str, body: str) -> dict:
    m = bus.send(flow.project, flow.group, sender, body, None)
    try:
        load_agent(sender).set_cursor(flow.project, flow.group, m["id"])
    except ValueError:
        pass
    return m


def _wake_supervisor(bus, flow: Flow, args, reason: str) -> dict | None:
    if not flow.supervisor:
        return None
    _post(bus, flow, "bus", f"[flow:{flow.name}] waking supervisor: {reason}")
    return _start(bus, flow, SUPERVISOR, args)


def flow_done(bus, agent, rc: int, args) -> dict:
    flow, node = agent_flow(agent)
    signal = agent.state.get(SIGNAL_KEY) or None
    agent.state[SIGNAL_KEY] = (
        None  # state merge keeps disk keys, so overwrite rather than pop
    )
    agent._save_state()
    if node == SUPERVISOR:
        return {"flow": flow.name, "node": node, "routed": [], "signal": signal}
    n = flow.nodes()[node]
    st = bus.flow_state(flow.name)
    if st["status"] == "stopped":
        _post(
            bus,
            flow,
            agent.name,
            f"[flow:{flow.name}] {node} finished (rc={rc}) but the flow is stopped",
        )
        return {
            "flow": flow.name,
            "node": node,
            "routed": [],
            "signal": signal,
            "stopped": True,
        }
    if signal is not None and signal in n.on:
        targets = list(n.on[signal])
    elif signal == "blocked" and flow.supervisor:
        targets = [SUPERVISOR]
    elif signal is not None:
        targets = []  # unknown signal → pause here; the supervisor will notice
    else:
        targets = list(n.next)
    turn = bus.flow_next_turn(flow.name, f"{node} → {', '.join(targets) or '-'}")
    line = (
        f"[flow:{flow.name}] turn {turn}/{flow.max_turns}: {node} → {', '.join(targets) or '(nobody)'}"
        + (f"  signal={signal}" if signal else "")
        + (f"  rc={rc}" if rc else "")
    )
    if (
        signal is not None
        and signal not in n.on
        and not (signal == "blocked" and flow.supervisor)
    ):
        line += f"  (unknown signal for {node}; routing paused)"
    _post(bus, flow, agent.name, line)
    if turn > flow.max_turns:
        bus.set_flow_state(flow.name, status="stopped")
        _wake_supervisor(
            bus, flow, args, f"max_turns ({flow.max_turns}) reached; flow stopped"
        )
        return {
            "flow": flow.name,
            "node": node,
            "routed": [],
            "signal": signal,
            "turn": turn,
            "stopped": True,
        }
    results = {t: _start(bus, flow, t, args)["status"] for t in targets}
    if (
        flow.supervisor
        and SUPERVISOR not in targets
        and flow.every > 0
        and turn % flow.every == 0
    ):
        _wake_supervisor(
            bus, flow, args, f"periodic check (every {flow.every} handoffs)"
        )
    if flow.supervisor and rc and SUPERVISOR not in targets:
        _wake_supervisor(bus, flow, args, f"{node} exited with rc={rc}")
    bus.set_flow_state(flow.name, status="running")
    return {
        "flow": flow.name,
        "node": node,
        "signal": signal,
        "turn": turn,
        "routed": results,
    }


def flow_run(bus, flow: Flow, task: str, to: str | None, args) -> dict:
    node = to or flow.entry
    if not node:
        raise BusError("no `entry` in the flow file; pass --to NODE")
    if node not in flow.nodes():
        raise BusError(f"unknown agent {node!r} in flow {flow.name}")
    if to is None:
        bus.set_flow_state(flow.name, status="running", turns=0, last_handoff=None)
    else:
        bus.set_flow_state(flow.name, status="running")
    m = _post(bus, flow, "user", f"[flow:{flow.name}] task for {node}: {task}")
    r = _start(bus, flow, node, args)
    return {"flow": flow.name, "to": node, "message": m["id"], "spawn": r["status"]}


def flow_status(bus, flow: Flow) -> dict:
    st = bus.flow_state(flow.name)
    try:
        sessions = spawner.list_sessions()
    except RuntimeError:
        sessions = {}
    pres = bus.presence()
    nodes = []
    for n in flow.nodes():
        me = flow.agent_name(n)
        p = pres.get(me)
        nodes.append(
            {
                "node": n,
                "agent": me,
                "alive": me in sessions,
                "last_cmd": p and p["last_cmd"],
                "last_seen": p and p["last_seen"],
            }
        )
    return {**st, "max_turns": flow.max_turns, "nodes": nodes}


def flow_down(bus, flow: Flow, remove_worktrees: bool) -> dict:
    killed = []
    for n in flow.nodes():
        me = flow.agent_name(n)
        try:
            if spawner.kill(load_agent(me).spawn):
                killed.append(me)
        except (ValueError, RuntimeError):
            pass
    bus.set_flow_state(flow.name, status="stopped")
    removed = []
    if remove_worktrees and flow.repo:
        repo = Path(flow.repo).expanduser().resolve()
        for n, nd in flow.agents.items():
            if nd.worktree is True:
                path = repo / WORKTREES_DIR / f"{flow.name}-{n}"
                if path.is_dir():
                    r = _git(repo, "worktree", "remove", "--force", str(path))
                    if r.returncode == 0:
                        removed.append(str(path))
    return {"flow": flow.name, "killed": killed, "worktrees_removed": removed}


# --- editing (supervisor commands) ------------------------------------------------
def _parse_on(specs: list[str]) -> dict[str, list[str]]:
    out = {}
    for s in specs:
        if "=" not in s:
            raise BusError(f"--on expects SIGNAL=a,b got {s!r}")
        k, v = s.split("=", 1)
        out[k.strip()] = [x.strip() for x in v.split(",") if x.strip()]
    return out


def _csv(s: str | None) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def edit_add_agent(flow: Flow, args) -> Node:
    try:
        check_name("agent", args.name)
    except ValueError as e:
        raise BusError(str(e)) from None
    if args.name == SUPERVISOR:
        raise BusError("use the [supervisor] table for the supervisor")
    old = flow.agents.get(args.name)
    n = Node(
        name=args.name,
        runner=args.runner or (old.runner if old else "claude"),
        model=args.model or (old.model if old else None),
        role=args.role or (old.role if old else None),
        context=args.context or (old.context if old else None),
        worktree=(True if args.worktree == "true" else args.worktree)
        if args.worktree
        else (old.worktree if old else False),
        next=_csv(args.next) if args.next is not None else (old.next if old else []),
        on=_parse_on(args.on) if args.on else (old.on if old else {}),
    )
    flow.agents[n.name] = n
    save_flow(flow)
    return n


def edit_rm_agent(flow: Flow, name: str) -> None:
    if name not in flow.agents:
        raise BusError(f"no agent {name!r} in flow {flow.name}")
    del flow.agents[name]
    for n in flow.nodes().values():
        n.next = [t for t in n.next if t != name]
        n.on = {k: [t for t in v if t != name] for k, v in n.on.items()}
    if flow.entry == name:
        flow.entry = None
    save_flow(flow)


def edit_route(flow: Flow, src: str, dst: str, on: str | None, remove: bool) -> None:
    nodes = flow.nodes()
    if src not in nodes or dst not in nodes:
        raise BusError(f"unknown agent in route {src} → {dst}")
    n = nodes[src]
    lst = n.on.setdefault(on, []) if on else n.next
    if remove:
        if dst in lst:
            lst.remove(dst)
        if on and not lst:
            n.on.pop(on, None)
    elif dst not in lst:
        lst.append(dst)
    save_flow(flow)


def edit_set(flow: Flow, key: str, value: str) -> None:
    if key == "max_turns":
        flow.max_turns = int(value)
    elif key == "every":
        flow.every = int(value)
    elif key == "entry":
        flow.entry = value or None
    else:
        raise BusError("settable keys: max_turns, every, entry")
    save_flow(flow)


# --- CLI ---------------------------------------------------------------------------
def _emit(args, data, text):
    print(json.dumps(data, ensure_ascii=False) if args.json else text)


def _need_agent(args):
    if args.agent is None:
        raise BusError("agent required: pass -c NAME")
    return args.agent


def cmd_example(bus, args):
    print(EXAMPLE, end="")


def cmd_show(bus, args):
    flow = load_flow(args.file)
    _emit(
        args,
        {"file": str(flow.path), "graph": tomllib.loads(dump_flow(flow))},
        graph_text(flow),
    )


def cmd_up(bus, args):
    flow = load_flow(args.file)
    r = flow_up(bus, flow)
    _emit(
        args,
        r,
        f"flow {flow.name} ready: {len(r['agents'])} agents in {flow.project}/{flow.group}"
        + "".join(
            f"\n  {a['agent']}" + (f"  cwd={a['cwd']}" if a["cwd"] else "")
            for a in r["agents"]
        )
        + f'\nstart it: llm-bus flow run {args.file} "task"',
    )


def cmd_run(bus, args):
    flow = load_flow(args.file)
    flow_up(bus, flow)
    r = flow_run(bus, flow, args.task, args.to, args)
    _emit(args, r, f"flow {flow.name}: sent task to {r['to']} ({r['spawn']})")


def cmd_status(bus, args):
    flow = load_flow(args.file)
    r = flow_status(bus, flow)
    lines = [
        f"flow {flow.name}: {r['status']}  turns {r['turns']}/{r['max_turns']}"
        + (f"  last: {r['last_handoff']}" if r["last_handoff"] else "")
    ]
    for n in r["nodes"]:
        lines.append(
            f"  {'*' if n['alive'] else ' '} {n['agent']}"
            + (f"  ({n['last_cmd']})" if n["last_cmd"] else "")
        )
    _emit(args, r, "\n".join(lines))


def cmd_stop(bus, args):
    flow = load_flow(args.file)
    st = bus.set_flow_state(flow.name, status="stopped")
    _emit(
        args,
        st,
        f"flow {flow.name} stopped (running agents finish; no further routing)",
    )


def cmd_resume(bus, args):
    flow = load_flow(args.file)
    st = bus.set_flow_state(flow.name, status="running")
    _emit(args, st, f"flow {flow.name} resumed")


def cmd_down(bus, args):
    flow = load_flow(args.file)
    r = flow_down(bus, flow, args.worktrees)
    _emit(
        args,
        r,
        f"flow {flow.name} down: killed {', '.join(r['killed']) or 'nothing'}"
        + (
            f"; removed worktrees {', '.join(r['worktrees_removed'])}"
            if r["worktrees_removed"]
            else ""
        ),
    )


def cmd_signal(bus, args):
    a = _need_agent(args)
    flow, node = agent_flow(a)
    a.state[SIGNAL_KEY] = args.signal
    a._save_state()
    n = flow.nodes()[node]
    routes = n.on.get(args.signal) or (
        [SUPERVISOR] if args.signal == "blocked" and flow.supervisor else []
    )
    _emit(
        args,
        {"agent": a.name, "signal": args.signal, "routes_to": routes},
        f"signal {args.signal!r} set for {a.name}: on exit → {', '.join(routes) or '(nobody: unknown signal, routing pauses)'}",
    )


def cmd_done(bus, args):
    a = _need_agent(args)
    r = flow_done(bus, a, args.rc, args)
    _emit(
        args,
        r,
        f"{a.name} done → {', '.join(f'{k} ({v})' for k, v in (r.get('routed') or {}).items()) or 'nobody'}",
    )


def _reup(bus, flow: Flow, args, msg: str):
    flow = load_flow(str(flow.path))
    flow_up(bus, flow)
    _emit(
        args,
        {"file": str(flow.path), "graph": tomllib.loads(dump_flow(flow))},
        msg + "\n" + graph_text(flow),
    )


def cmd_add_agent(bus, args):
    flow = load_flow(args.file)
    n = edit_add_agent(flow, args)
    _reup(bus, flow, args, f"agent {n.name} saved")


def cmd_rm_agent(bus, args):
    flow = load_flow(args.file)
    edit_rm_agent(flow, args.name)
    _reup(bus, flow, args, f"agent {args.name} removed")


def cmd_route(bus, args):
    flow = load_flow(args.file)
    edit_route(flow, args.src, args.dst, args.on, remove=False)
    _reup(
        bus,
        flow,
        args,
        f"route {args.src} → {args.dst}" + (f" on {args.on}" if args.on else ""),
    )


def cmd_unroute(bus, args):
    flow = load_flow(args.file)
    edit_route(flow, args.src, args.dst, args.on, remove=True)
    _reup(
        bus,
        flow,
        args,
        f"removed route {args.src} → {args.dst}"
        + (f" on {args.on}" if args.on else ""),
    )


def cmd_set(bus, args):
    flow = load_flow(args.file)
    edit_set(flow, args.key, args.value)
    _reup(bus, flow, args, f"{args.key} = {args.value}")


def add_parsers(sub) -> None:
    fp = sub.add_parser(
        "flow", help="multi-agent loops with bus-enforced routing"
    ).add_subparsers(dest="sub", required=True)

    def with_file(name, help):
        s = fp.add_parser(name, help=help)
        s.add_argument(
            "file", help=f"flow TOML file (or a directory containing {FLOW_FILE})"
        )
        return s

    fp.add_parser("example", help="print an example flow file").set_defaults(
        fn=cmd_example
    )
    with_file("show", "print the graph").set_defaults(fn=cmd_show)
    with_file(
        "up", "create/refresh agents, group and worktrees (idempotent)"
    ).set_defaults(fn=cmd_up)
    s = with_file("run", "send a task to the entry agent (or --to NODE) and start it")
    s.add_argument("task")
    s.add_argument("--to", metavar="NODE")
    s.set_defaults(fn=cmd_run)
    with_file("status", "turns, status, alive sessions").set_defaults(fn=cmd_status)
    with_file("stop", "pause routing (running agents finish)").set_defaults(fn=cmd_stop)
    with_file("resume", "unpause routing").set_defaults(fn=cmd_resume)
    s = with_file("down", "kill all sessions of the flow")
    s.add_argument("--worktrees", action="store_true", help="also remove git worktrees")
    s.set_defaults(fn=cmd_down)

    s = fp.add_parser(
        "signal",
        help="(agent) choose the on.<signal> route for when I exit (-c required)",
    )
    s.add_argument("signal")
    s.set_defaults(fn=cmd_signal)
    s = fp.add_parser(
        "done", help="(runner wrapper) I exited: route to the next agents (-c required)"
    )
    s.add_argument("--rc", type=int, default=0, help="exit code of the runner")
    s.set_defaults(fn=cmd_done)

    s = with_file("add-agent", "add or update an agent (re-runs `up`)")
    s.add_argument("name")
    s.add_argument("--runner")
    s.add_argument("--model")
    s.add_argument("--role")
    s.add_argument("--context")
    s.add_argument("--next", help="comma-separated default targets")
    s.add_argument("--on", action="append", help="SIGNAL=a,b (repeatable)")
    s.add_argument(
        "--worktree",
        nargs="?",
        const="true",
        metavar="[NODE]",
        help="own worktree, or share NODE's",
    )
    s.set_defaults(fn=cmd_add_agent)
    s = with_file("rm-agent", "remove an agent and its routes (re-runs `up`)")
    s.add_argument("name")
    s.set_defaults(fn=cmd_rm_agent)
    for name, fn in (("route", cmd_route), ("unroute", cmd_unroute)):
        s = with_file(
            name,
            f"{'add' if name == 'route' else 'remove'} a route FROM → TO (default or --on SIGNAL)",
        )
        s.add_argument("src", metavar="FROM")
        s.add_argument("dst", metavar="TO")
        s.add_argument("--on", metavar="SIGNAL")
        s.set_defaults(fn=fn)
    s = with_file("set", "set max_turns | every | entry")
    s.add_argument("key")
    s.add_argument("value")
    s.set_defaults(fn=cmd_set)
