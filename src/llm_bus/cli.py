"""llm-bus: a tiny message bus CLI for agents."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from . import flow as flowmod
from . import spawn as spawner
from .config import (
    DM_PROJECT,
    HUB_CONTEXT,
    HUB_NAME,
    PROJECT_FILE,
    Agent,
    ProjectRef,
    check_name,
    create_agent,
    directory,
    dm_group,
    list_agents,
    load_agent,
    load_project,
    write_project_file,
)
from .db import Bus, BusError
from .guide import guide_text
from .spawn import Spawn


# --- resolution helpers ---------------------------------------------------
def _agent(args) -> Agent:
    if args.agent is None:
        raise BusError(
            "agent required: pass -c NAME (create one with `llm-bus init NAME`)"
        )
    return args.agent


def _project(args) -> ProjectRef:
    if args.project is None:
        raise BusError(
            f"project required: pass -p {PROJECT_FILE}|NAME (see `llm-bus project init`)"
        )
    return args.project


def _group(
    args, pos: list[str], n_tail: int, tail_name: str = "message body"
) -> tuple[str, str, list[str]]:
    """Resolve (project, group, tail) from -p plus positionals `[GROUP] *tail`."""
    pr = _project(args)
    extra = len(pos) - n_tail
    if extra < 0:
        raise BusError(
            f"{tail_name} required"
            + (" (or --stdin)" if tail_name == "message body" else "")
        )
    if extra == 1:
        return pr.name, pos[0], pos[1:]
    if extra == 0 and pr.group:
        return pr.name, pr.group, pos
    raise BusError(
        f"group required: pass GROUP (or set a default `group` in {PROJECT_FILE})"
    )


def _emit(args, data, text: str) -> None:
    print(json.dumps(data, ensure_ascii=False) if args.json else text)


def _who(name: str, role: str | None) -> str:
    return f"{name} ({role})" if role else name


def _fmt_msg(m: dict) -> str:
    ts = m["created_at"][:19].replace("T", " ")
    reply = f" ↳#{m['reply_to']}" if m.get("reply_to") else ""
    return f"#{m['id']}{reply} [{ts}] {_who(m['sender'], m.get('role'))}: {m['body']}"


MENTION_RE = re.compile(r"(?<![\w.])@([A-Za-z0-9_.-]+)")


def _mentions(body: str, me: str) -> list[str]:
    known = {a["name"] for a in list_agents() if "error" not in a}
    seen: list[str] = []
    for n in MENTION_RE.findall(body):
        n = n.rstrip(".")
        if n in known and n != me and n not in seen:
            seen.append(n)
    return seen


def _all_targets(bus, a: Agent) -> list[tuple[str, str]]:
    return [(m["project"], m["group"]) for m in bus.memberships(a.name)]


def _label(a: Agent, project: str, group: str) -> str:
    return (
        f"dm:{_dm_peer(a.name, group)}"
        if project == DM_PROJECT
        else f"{project}/{group}"
    )


def _fmt_members(ms: list[dict]) -> str:
    return ", ".join(_who(m["agent"], m["role"]) for m in ms) or "-"


def _fmt_msgs(msgs: list[dict], empty: str) -> str:
    return "\n".join(_fmt_msg(m) for m in msgs) or empty


def _dm_peer(me: str, group: str) -> str:
    return group.replace(me, "", 1).strip("~")


def _wait_loop(
    bus, a: Agent, targets, timeout, interval, args, include_self=False, what="messages"
):
    """Poll (project, group) pairs (or a callable producing them) until unread messages appear."""
    deadline = None if timeout <= 0 else time.monotonic() + timeout
    while True:
        hits = []
        for project, group in targets() if callable(targets) else targets:
            new = bus.unread(project, group, a.cursor(project, group))
            if new:
                a.set_cursor(project, group, new[-1]["id"])
            hits += [m for m in new if include_self or m["sender"] != a.name]
        if hits:
            hits.sort(key=lambda m: m["id"])
            _emit(args, hits, _fmt_msgs(hits, ""))
            return 0
        if deadline is not None and time.monotonic() >= deadline:
            _emit(args, [], f"(timeout: no new {what})")
            return 2
        time.sleep(interval)


# --- agent commands ---------------------------------------------------------
def cmd_init(bus, args):
    try:
        a = create_agent(
            args.name,
            args.role,
            args.context,
            Path(args.dir) if args.dir else None,
            args.force,
            spawn=Spawn(
                session=args.session or args.name,
                cmd=args.cmd,
                cwd=args.cwd,
                idle_timeout=args.idle_timeout,
            ),
        )
    except ValueError as e:
        raise BusError(str(e)) from None
    _emit(
        args,
        {"name": a.name, "role": a.role, "dir": str(a.dir), "spawn": a.spawn.cmd},
        f"created agent {_who(a.name, a.role)} at {a.dir}",
    )


def cmd_agents(bus, args):
    agents = list_agents()
    lines = [
        f"{a['name']}  (invalid: {a['error']})"
        if "error" in a
        else f"{_who(a['name'], a['role'])}{'  [hub]' if a.get('hub') else ''}  {a['dir']}"
        for a in agents
    ]
    _emit(args, agents, "\n".join(lines) or "(no agents)")


def cmd_remember(bus, args):
    a = _agent(args)
    text = sys.stdin.read().strip() if args.stdin else args.text
    if not text:
        raise BusError("nothing to remember")
    a.remember(text)
    _emit(
        args, {"agent": a.name, "notes": str(a.notes_path)}, f"noted in {a.notes_path}"
    )


def _fmt_directory(entries: list[dict], me: str | None = None) -> str:
    if not entries:
        return "(no agents)"
    out = []
    for e in entries:
        out.append(
            f"* {_who(e['name'], e['role'])}"
            + ("  [hub]" if e["hub"] else "")
            + ("  (you)" if e["name"] == me else "")
        )
        for label, text in (("context", e["context"]), ("notes", e["notes"])):
            if text:
                out.append(f"    {label}: " + " ".join(text.split())[:300])
    return "\n".join(out)


def cmd_directory(bus, args):
    entries = directory(args.query)
    _emit(
        args,
        entries,
        _fmt_directory(entries, me=args.agent.name if args.agent else None),
    )


def cmd_hub_init(bus, args):
    try:
        a = create_agent(
            HUB_NAME,
            "agent directory / router",
            HUB_CONTEXT,
            force=args.force,
            hub=True,
        )
    except ValueError as e:
        raise BusError(str(e)) from None
    _emit(
        args,
        {"name": a.name, "dir": str(a.dir)},
        f"created hub agent at {a.dir}\nSpawn it with: llm-bus -c hub whoami",
    )


def cmd_whoami(bus, args):
    a = _agent(args)
    pr = args.project
    unread, dms = [], []
    for m in bus.memberships(a.name):
        n = bus.count_after(
            m["project"], m["group"], a.cursor(m["project"], m["group"])
        )
        if m["project"] == DM_PROJECT:
            dms.append({"peer": _dm_peer(a.name, m["group"]), "unread": n})
        else:
            unread.append(
                {**m, "unread": n, "cursor": a.cursor(m["project"], m["group"])}
            )
    data = {
        "name": a.name,
        "role": a.role,
        "hub": a.hub,
        "dir": str(a.dir),
        "context": a.context(),
        "notes": a.notes(),
        "project": (
            {
                "name": pr.name,
                "group": pr.group,
                "file": str(pr.path) if pr.path else None,
            }
            if pr
            else None
        ),
        "memberships": unread,
        "dms": dms,
        "directory": directory() if a.hub else None,
    }
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
        return
    out = [
        f"YOU ARE: {_who(a.name, a.role)}"
        + ("  [HUB — see DIRECTORY below]" if a.hub else ""),
        f"AGENT DIR: {a.dir}",
    ]
    if pr:
        out.append(
            f"PROJECT: {pr.name}"
            + (f"  default group: {pr.group}" if pr.group else "")
            + (f"  ({pr.path})" if pr.path else "")
        )
    else:
        out.append(f"PROJECT: (none — pass -p {PROJECT_FILE})")
    out.append("MEMBERSHIPS:")
    out += [
        f"  {m['project']}/{m['group']}  unread: {m['unread']}" for m in unread
    ] or ["  (none — use `join`)"]
    if dms:
        out.append("DMS:")
        out += [f"  {d['peer']}  unread: {d['unread']}" for d in dms]
    ctx, notes = a.context().strip(), a.notes().strip()
    out.append(f"\n--- CONTEXT ({a.context_path}) ---\n{ctx or '(empty)'}")
    out.append(f"\n--- NOTES ({a.notes_path}) ---\n{notes or '(empty)'}")
    if a.hub:
        out.append(
            "\n--- DIRECTORY ---\n" + _fmt_directory(data["directory"], me=a.name)
        )
    out.append(
        "\nNext: `llm-bus guide` for commands. Read unread with `read --unread`, block with `wait`."
    )
    print("\n".join(out))


# --- on-demand agents (screen sessions) ---------------------------------------
def _spawn_if_dead(args, peer: str) -> dict | None:
    """Start PEER's screen session if it has a spawn cmd and isn't running. Never raises."""
    if getattr(args, "no_spawn", False):
        return None
    try:
        a = load_agent(peer)
        if not a.spawn.cmd:
            return None
        r = spawner.start(a.name, a.dir, a.spawn)
    except Exception as e:  # noqa: BLE001 — a failed spawn must never break the send itself
        r = {"agent": peer, "status": "error", "error": f"{type(e).__name__}: {e}"}
    if r["status"] == "spawned":
        args.bus.touch(peer, "spawned")
        if not args.json:
            print(
                f"(spawned {peer} in screen session '{r['session']}')", file=sys.stderr
            )
    elif r["status"] == "error" and not args.json:
        print(f"(could not spawn {peer}: {r['error']})", file=sys.stderr)
    return r


def _load_named(name: str) -> Agent:
    try:
        return load_agent(name)
    except ValueError as e:
        raise BusError(str(e)) from None


def cmd_ps(bus, args):
    try:
        sessions = spawner.list_sessions()
    except RuntimeError as e:
        raise BusError(str(e)) from None
    rows = _ps_rows(bus, sessions)
    _emit(
        args,
        rows,
        "\n".join(
            f"{'*' if r['alive'] else ' '} {_who(r['name'], r['role'])}"
            f"  session={r['session']}"
            + ("" if r["spawnable"] else "  (no spawn cmd)")
            + (
                f"  last: {_ago(r['idle_s'])} ({r['last_cmd']})"
                if r["last_seen"]
                else "  last: never"
            )
            + (f"  idle_timeout={r['idle_timeout']:g}m" if r["idle_timeout"] else "")
            for r in rows
        )
        or "(no agents)",
    )


def _ago(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)}m ago"
    return f"{seconds / 3600:.1f}h ago"


def _ps_rows(bus, sessions) -> list[dict]:
    pres = bus.presence()
    now = time.time()
    rows = []
    for a in list_agents():
        if "error" in a:
            continue
        ag = load_agent(a["dir"])
        pr = pres.get(ag.name)
        rows.append(
            {
                "name": ag.name,
                "role": ag.role,
                "session": ag.spawn.session,
                "alive": ag.spawn.session in sessions,
                "spawnable": bool(ag.spawn.cmd),
                "last_seen": pr["last_seen"] if pr else None,
                "last_cmd": pr["last_cmd"] if pr else None,
                "idle_s": (now - pr["last_seen"]) if pr else None,
                "idle_timeout": ag.spawn.idle_timeout,
            }
        )
    return rows


def cmd_reap(bus, args):
    """Kill alive sessions whose agent hasn't touched the bus for longer than its idle_timeout."""
    try:
        sessions = spawner.list_sessions()
    except RuntimeError as e:
        raise BusError(str(e)) from None
    reaped = []
    for r in _ps_rows(bus, sessions):
        limit = args.idle if args.idle is not None else r["idle_timeout"]
        if not r["alive"] or not limit:
            continue
        idle_s = r["idle_s"] if r["idle_s"] is not None else float("inf")
        if idle_s < limit * 60:
            continue
        entry = {
            "agent": r["name"],
            "session": r["session"],
            "idle_s": idle_s,
            "killed": False,
        }
        if not args.dry_run:
            entry["killed"] = spawner.kill(load_agent(r["name"]).spawn)
        reaped.append(entry)
    _emit(
        args,
        reaped,
        "\n".join(
            f"{'would kill' if args.dry_run else ('killed' if e['killed'] else 'kill failed')}:"
            f" {e['agent']} (idle {_ago(e['idle_s']) if e['idle_s'] != float('inf') else 'never active'})"
            for e in reaped
        )
        or "(nothing to reap)",
    )


def cmd_kill(bus, args):
    a = _load_named(args.name)
    try:
        killed = spawner.kill(a.spawn)
    except RuntimeError as e:
        raise BusError(str(e)) from None
    _emit(
        args,
        {"agent": a.name, "session": a.spawn.session, "killed": killed},
        f"{'killed' if killed else 'not running'}: {a.name} (session '{a.spawn.session}')",
    )


# --- dm ------------------------------------------------------------------------
def _dm_target(bus, me: Agent, peer: str) -> str:
    if peer == me.name:
        raise BusError("cannot DM yourself")
    peer_info = next((a for a in list_agents() if a["name"] == peer), None)
    if peer_info is None:
        raise BusError(f"unknown agent '{peer}' (see `llm-bus agents`)")
    g = dm_group(me.name, peer)
    bus.ensure_group(DM_PROJECT, g)
    bus.join_group(DM_PROJECT, g, me.name, me.role)
    bus.join_group(
        DM_PROJECT, g, peer, peer_info.get("role")
    )  # both sides see the conversation
    return g


def _dm_targets(bus, a: Agent):
    return [
        (m["project"], m["group"])
        for m in bus.memberships(a.name)
        if m["project"] == DM_PROJECT
    ]


def cmd_dm(bus, args):
    a = _agent(args)
    if args.peer is None:
        if args.wait:
            return _wait_loop(
                bus,
                a,
                lambda: _dm_targets(bus, a),
                args.timeout,
                args.interval,
                args,
                what="DMs",
            )
        convs = [
            {
                "peer": _dm_peer(a.name, g),
                "unread": bus.count_after(p, g, a.cursor(p, g)),
            }
            for p, g in _dm_targets(bus, a)
        ]
        _emit(
            args,
            convs,
            "\n".join(f"{c['peer']}  unread: {c['unread']}" for c in convs)
            or "(no DMs)",
        )
        return 0
    g = _dm_target(bus, a, args.peer)
    body = sys.stdin.read().strip() if args.stdin else args.body
    if args.stdin and not body:
        raise BusError("empty message")
    if body:
        m = bus.send(DM_PROJECT, g, a.name, body, a.role, reply_to=args.reply)
        a.set_cursor(DM_PROJECT, g, m["id"])
        m["spawn"] = _spawn_if_dead(args, args.peer)
        _emit(args, m, f"dm #{m['id']} → {args.peer}")
        return 0
    if args.wait:
        return _wait_loop(
            bus, a, [(DM_PROJECT, g)], args.timeout, args.interval, args, what="DMs"
        )
    if args.all:
        msgs = bus.latest(DM_PROJECT, g, limit=args.limit)
    else:
        msgs = bus.unread(DM_PROJECT, g, a.cursor(DM_PROJECT, g), limit=args.limit)
    if msgs:
        a.set_cursor(DM_PROJECT, g, msgs[-1]["id"])
    _emit(args, msgs, _fmt_msgs(msgs, "(no new DMs)"))
    return 0


def cmd_ask(bus, args):
    a = _agent(args)
    g = _dm_target(bus, a, HUB_NAME)
    m = bus.send(DM_PROJECT, g, a.name, args.question, a.role)
    a.set_cursor(DM_PROJECT, g, m["id"])
    _spawn_if_dead(args, HUB_NAME)
    if not args.json:
        print(
            f"asked hub (#{m['id']}), waiting up to {args.timeout:g}s...",
            file=sys.stderr,
        )
    return _wait_loop(
        bus,
        a,
        [(DM_PROJECT, g)],
        args.timeout,
        args.interval,
        args,
        what="reply from hub",
    )


# --- project / group commands --------------------------------------------------
def _checked(kind: str, value: str) -> str:
    try:
        return check_name(kind, value)
    except ValueError as e:
        raise BusError(str(e)) from None


def cmd_project_create(bus, args):
    _checked("project", args.name)
    p = bus.create_project(args.name)
    _emit(args, p, f"created project '{p['name']}'")


def cmd_project_init(bus, args):
    _checked("project", args.name)
    if args.group:
        _checked("group", args.group)
    try:
        bus.create_project(args.name)
        created = True
    except BusError:
        created = False
    try:
        path = write_project_file(Path(args.file), args.name, args.group, args.force)
    except ValueError as e:
        raise BusError(str(e)) from None
    if args.group:
        bus.ensure_group(args.name, args.group)
    _emit(
        args,
        {
            "project": args.name,
            "group": args.group,
            "file": str(path),
            "created": created,
        },
        f"{'created' if created else 'using existing'} project '{args.name}', wrote {path}",
    )


def cmd_project_list(bus, args):
    ps = bus.list_projects()
    _emit(
        args,
        ps,
        "\n".join(
            f"{p['name']}  (groups: {p['groups']}, members: {p['members']})" for p in ps
        )
        or "(no projects)",
    )


def cmd_group_create(bus, args):
    pr = _project(args)
    g = bus.create_group(pr.name, _checked("group", args.name))
    _emit(args, g, f"created group '{pr.name}/{g['name']}'")


def cmd_group_list(bus, args):
    pr = _project(args)
    gs = bus.list_groups(pr.name)
    _emit(
        args,
        gs,
        "\n".join(
            f"{pr.name}/{g['name']}  (members: {g['members']}, messages: {g['messages']})"
            for g in gs
        )
        or "(no groups)",
    )


def cmd_join(bus, args):
    a = _agent(args)
    pr = _project(args)
    group = args.group or pr.group
    if group:
        bus.join_group(pr.name, group, a.name, a.role)
        members = bus.group_members(pr.name, group)
        target = f"{pr.name}/{group}"
    else:
        bus.join_project(pr.name, a.name, a.role)
        members = bus.project_members(pr.name)
        target = pr.name
    _emit(
        args,
        {"agent": a.name, "role": a.role, "target": target, "members": members},
        f"{_who(a.name, a.role)} joined {target}  members: {_fmt_members(members)}",
    )


def cmd_members(bus, args):
    pr = _project(args)
    group = args.group or pr.group
    members = (
        bus.group_members(pr.name, group) if group else bus.project_members(pr.name)
    )
    _emit(
        args,
        members,
        "\n".join(_who(m["agent"], m["role"]) for m in members) or "(no members)",
    )


# --- message commands ------------------------------------------------------------
def cmd_send(bus, args):
    a = _agent(args)
    project, group, tail = _group(args, args.pos, 0 if args.stdin else 1)
    body = sys.stdin.read().strip() if args.stdin else tail[0]
    if not body:
        raise BusError("empty message")
    m = bus.send(project, group, a.name, body, a.role, reply_to=args.reply)
    a.set_cursor(project, group, m["id"])  # own message counts as read
    spawned = {}
    for peer in _mentions(body, a.name):
        r = _spawn_if_dead(args, peer)
        if r:
            spawned[peer] = r["status"]
    m["spawned"] = spawned
    _emit(args, m, f"sent #{m['id']} to {project}/{group}")


def cmd_read(bus, args):
    if args.thread is not None:
        msgs = bus.thread(args.thread)
        _emit(args, msgs, _fmt_msgs(msgs, "(empty thread)"))
        return
    project, group, _ = _group(args, args.pos, 0)
    if args.unread:
        after = max(args.after, _agent(args).cursor(project, group))
        msgs = bus.unread(project, group, after, limit=args.limit)
    else:
        msgs = bus.latest(project, group, limit=args.limit, after=args.after)
    if msgs and args.agent is not None and (args.unread or args.mark):
        args.agent.set_cursor(project, group, msgs[-1]["id"])
    _emit(args, msgs, _fmt_msgs(msgs, "(no messages)"))


def cmd_wait(bus, args):
    a = _agent(args)
    if args.all:  # every group I'm in + every DM, in one blocking call
        return _wait_loop(
            bus,
            a,
            lambda: _all_targets(bus, a),
            args.timeout,
            args.interval,
            args,
            args.include_self,
        )
    project, group, _ = _group(args, args.pos, 0)
    if (
        args.after is not None
    ):  # explicit start point overrides the stored cursor for this call
        a.state.setdefault("cursors", {})[f"{project}/{group}"] = args.after
    return _wait_loop(
        bus, a, [(project, group)], args.timeout, args.interval, args, args.include_self
    )


# --- Claude Code Stop hook: push delivery ------------------------------------------
def _collect_unread(bus, a: Agent, per_target: int = 20) -> list[dict]:
    """Unread messages from others across all groups + DMs; advances cursors."""
    hits = []
    for project, group in _all_targets(bus, a):
        new = bus.unread(project, group, a.cursor(project, group), limit=per_target)
        if new:
            a.set_cursor(project, group, new[-1]["id"])
        for m in new:
            if m["sender"] != a.name:
                m["where"] = _label(a, project, group)
                hits.append(m)
    hits.sort(key=lambda m: m["id"])
    return hits


def cmd_hook(bus, args):
    """Stop hook for Claude Code: block the stop and hand over unread messages, if any."""
    a = _agent(args)
    if not sys.stdin.isatty():
        try:
            sys.stdin.read()  # consume the hook payload; we don't need it
        except OSError:
            pass
    hits = _collect_unread(bus, a)
    if not hits:
        return 0  # nothing new → let Claude stop
    lines = [f"[llm-bus] {len(hits)} new message(s) for {a.name}:"]
    for m in hits:
        lines.append(f"  [{m['where']}] {_fmt_msg(m)}")
    lines.append(
        f'Act on them, then reply: `llm-bus -c {args.agent_ref} -p PROJECT send --reply ID "..."`'
        f' or `llm-bus -c {args.agent_ref} dm NAME "..."`.'
        " Run `llm-bus guide` if unsure."
    )
    print(json.dumps({"decision": "block", "reason": "\n".join(lines)}))
    return 0


# Matches only the exact shape install-hook writes. A hand-written entry with extra flags or an
# absolute binary path won't be recognised and re-install appends a second entry — acceptable.
HOOK_CMD_RE = re.compile(r"^llm-bus\s+-c\s+(\S+)\s+hook$")


def _hook_targets_agent(command: str, a: Agent) -> bool:
    m = HOOK_CMD_RE.match(command.strip())
    if not m:
        return False
    try:
        return load_agent(m.group(1)).dir.resolve() == a.dir.resolve()
    except ValueError:
        return False


def cmd_install_hook(bus, args):
    a = _agent(args)
    path = Path(args.file)
    settings = {}
    if path.is_file():
        try:
            settings = json.loads(path.read_text() or "{}")
        except json.JSONDecodeError as e:
            raise BusError(f"{path}: invalid JSON: {e}") from None
        if not isinstance(settings, dict):
            raise BusError(f"{path}: expected a JSON object at top level")
    cmd = f"llm-bus -c {args.agent_ref} hook"
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict) or not isinstance(hooks.get("Stop", []), list):
        raise BusError(f"{path}: 'hooks' / 'hooks.Stop' have an unexpected shape")
    stop = hooks.setdefault("Stop", [])
    already = any(
        _hook_targets_agent(h.get("command", ""), a)
        for entry in stop
        if isinstance(entry, dict)
        for h in entry.get("hooks", [])
        if isinstance(h, dict)
    )
    if not already:
        stop.append({"hooks": [{"type": "command", "command": cmd}]})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2) + "\n")
    _emit(
        args,
        {"agent": a.name, "file": str(path), "command": cmd, "installed": not already},
        (
            f"installed Stop hook in {path}: {cmd}"
            if not already
            else f"already installed in {path}: {cmd}"
        )
        + "\nClaude Code will now be interrupted at end of turn whenever this agent has unread messages.",
    )


def cmd_search(bus, args):
    project, group, tail = _group(args, args.pos, 1, tail_name="search QUERY")
    msgs = bus.search(project, group, tail[0], limit=args.limit)
    _emit(args, msgs, _fmt_msgs(msgs, "(no matches)"))


def cmd_guide(bus, args):
    print(guide_text(), end="")


# --- parser ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="llm-bus",
        description="Message bus for agents. Local SQLite, no daemon.",
        epilog=(
            "Quick start:\n"
            "  llm-bus init alice --role 'backend dev'\n"
            f"  llm-bus project init demo --group dev          # writes ./{PROJECT_FILE}\n"
            f"  llm-bus -c alice -p {PROJECT_FILE} join\n"
            f"  llm-bus -c alice -p {PROJECT_FILE} send 'hello'\n"
            f"  llm-bus -c alice -p {PROJECT_FILE} wait\n"
            "  llm-bus -c alice dm bob 'hi'   ·   llm-bus -c alice ask 'who can help with X?'\n\n"
            "Run `llm-bus guide` for the full agent-oriented guide; `llm-bus -c NAME whoami` for your context."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "-c",
        "--agent",
        dest="agent_ref",
        metavar="NAME|DIR",
        help="who you are: agent name in ~/.llm_bus/agents, or path to an agent folder",
    )
    p.add_argument(
        "-p",
        "--project",
        dest="project_ref",
        metavar="FILE|NAME",
        help=f"which project: path to a {PROJECT_FILE} file (or its dir), or a project name",
    )
    p.add_argument(
        "--db", help="SQLite path (default: $LLM_BUS_DB or ~/.llm_bus/bus.db)"
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("guide", help="print the full agent-oriented usage guide")
    s.set_defaults(fn=cmd_guide)

    s = sub.add_parser("init", help="create an agent folder (~/.llm_bus/agents/NAME)")
    s.add_argument("name")
    s.add_argument("--role")
    s.add_argument("--context", help="initial CONTEXT.md text")
    s.add_argument("--dir", help="create the agent folder here instead")
    s.add_argument("--force", action="store_true")
    s.add_argument(
        "--cmd",
        help="shell command that runs this agent on demand ({name} {dir} {session} placeholders)",
    )
    s.add_argument("--session", help="screen session name (default: NAME)")
    s.add_argument("--cwd", help="working dir for --cmd (default: agent folder)")
    s.add_argument(
        "--idle-timeout", type=float, help="minutes idle before `llm-bus reap` kills it"
    )
    s.set_defaults(fn=cmd_init)
    s = sub.add_parser("agents", help="list agents in the global agents folder")
    s.set_defaults(fn=cmd_agents)

    s = sub.add_parser("ps", help="agents and whether their screen session is running")
    s.set_defaults(fn=cmd_ps)
    s = sub.add_parser("kill", help="quit an agent's screen session")
    s.add_argument("name")
    s.set_defaults(fn=cmd_kill)
    s = sub.add_parser(
        "reap", help="kill alive sessions idle longer than their idle_timeout"
    )
    s.add_argument(
        "--idle", type=float, help="minutes; override every agent's idle_timeout"
    )
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_reap)

    s = sub.add_parser(
        "hook",
        help="Claude Code Stop hook: blocks the stop with unread messages (-c required)",
    )
    s.set_defaults(fn=cmd_hook)
    s = sub.add_parser(
        "install-hook",
        help="add `llm-bus -c NAME hook` as a Stop hook to Claude Code settings",
    )
    s.add_argument(
        "--file",
        default=".claude/settings.local.json",
        help="settings file to edit (default: ./.claude/settings.local.json)",
    )
    s.set_defaults(fn=cmd_install_hook)

    s = sub.add_parser(
        "whoami", help="dump this agent's full context (-c required, -p optional)"
    )
    s.set_defaults(fn=cmd_whoami)
    s = sub.add_parser(
        "remember", help="append a note to this agent's NOTES.md (-c required)"
    )
    s.add_argument("text", nargs="?")
    s.add_argument("--stdin", action="store_true")
    s.set_defaults(fn=cmd_remember)

    s = sub.add_parser(
        "directory", help="list all agents with roles/context; optional QUERY filter"
    )
    s.add_argument("query", nargs="?")
    s.set_defaults(fn=cmd_directory)
    hb = sub.add_parser("hub", help="the central directory agent").add_subparsers(
        dest="sub", required=True
    )
    s = hb.add_parser("init", help=f"create the '{HUB_NAME}' agent")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_hub_init)
    s = sub.add_parser(
        "ask", help="DM the hub a question and wait for its reply (-c required)"
    )
    s.add_argument("question")
    s.add_argument("-t", "--timeout", type=float, default=120)
    s.add_argument("--interval", type=float, default=0.5)
    s.add_argument("--no-spawn", action="store_true", help="don't auto-start the hub")
    s.set_defaults(fn=cmd_ask)
    s = sub.add_parser("dm", help="direct messages (-c required, no -p needed)")
    s.add_argument("peer", nargs="?", help="agent name; omit to list conversations")
    s.add_argument("body", nargs="?", help="send this; omit to read unread from PEER")
    s.add_argument("--stdin", action="store_true", help="read body from stdin")
    s.add_argument(
        "--wait", action="store_true", help="block until PEER (or anyone) DMs you"
    )
    s.add_argument(
        "--all", action="store_true", help="show full history, not just unread"
    )
    s.add_argument("-n", "--limit", type=int, default=50)
    s.add_argument("-t", "--timeout", type=float, default=0)
    s.add_argument("--interval", type=float, default=0.5)
    s.add_argument(
        "--no-spawn", action="store_true", help="don't auto-start a dead PEER on send"
    )
    s.add_argument("--reply", type=int, metavar="ID", help="reply to message ID")
    s.set_defaults(fn=cmd_dm)

    pr = sub.add_parser("project", help="manage projects").add_subparsers(
        dest="sub", required=True
    )
    s = pr.add_parser("create", help="create a project in the DB")
    s.add_argument("name")
    s.set_defaults(fn=cmd_project_create)
    s = pr.add_parser(
        "init", help=f"create project (if needed) and write a {PROJECT_FILE} file"
    )
    s.add_argument("name")
    s.add_argument("--group", help="default group (created if missing)")
    s.add_argument(
        "--file",
        default=PROJECT_FILE,
        help=f"where to write (default ./{PROJECT_FILE})",
    )
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_project_init)
    s = pr.add_parser("list", help="list projects")
    s.set_defaults(fn=cmd_project_list)

    gr = sub.add_parser("group", help="manage groups (-p required)").add_subparsers(
        dest="sub", required=True
    )
    s = gr.add_parser("create", help="create a group")
    s.add_argument("name")
    s.set_defaults(fn=cmd_group_create)
    s = gr.add_parser("list", help="list groups")
    s.set_defaults(fn=cmd_group_list)

    s = sub.add_parser(
        "join", help="join the project, or a group in it (-c -p required)"
    )
    s.add_argument(
        "group",
        nargs="?",
        help="group (default: project file's group, else project only)",
    )
    s.set_defaults(fn=cmd_join)
    s = sub.add_parser(
        "members", help="list members of the project or a group (-p required)"
    )
    s.add_argument("group", nargs="?")
    s.set_defaults(fn=cmd_members)

    s = sub.add_parser("send", help="send a message (-c -p required)")
    s.add_argument("pos", nargs="*", metavar="[GROUP] BODY")
    s.add_argument("--stdin", action="store_true", help="read body from stdin")
    s.add_argument(
        "--reply", type=int, metavar="ID", help="reply to message ID (thread)"
    )
    s.add_argument(
        "--no-spawn",
        action="store_true",
        help="don't auto-start dead @mentioned agents",
    )
    s.set_defaults(fn=cmd_send)

    s = sub.add_parser("read", help="read latest messages (-p required)")
    s.add_argument("pos", nargs="*", metavar="[GROUP]")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.add_argument("--after", type=int, default=0, help="only messages with id > AFTER")
    s.add_argument(
        "--unread",
        action="store_true",
        help="only after -c agent's cursor; advances it",
    )
    s.add_argument(
        "--mark", action="store_true", help="advance -c agent's cursor to last shown"
    )
    s.add_argument(
        "--thread", type=int, metavar="ID", help="show the thread containing ID"
    )
    s.set_defaults(fn=cmd_read)

    s = sub.add_parser(
        "wait", help="block until unread messages from others arrive (-c -p required)"
    )
    s.add_argument("pos", nargs="*", metavar="[GROUP]")
    s.add_argument(
        "--after", type=int, help="wait for ids > AFTER (default: agent's cursor)"
    )
    s.add_argument(
        "-t", "--timeout", type=float, default=0, help="seconds, 0 = forever"
    )
    s.add_argument("--interval", type=float, default=0.5, help="poll interval seconds")
    s.add_argument(
        "--include-self", action="store_true", help="also return own messages"
    )
    s.add_argument(
        "--all",
        action="store_true",
        help="wait on every group I'm in + all DMs (no -p needed)",
    )
    s.set_defaults(fn=cmd_wait)

    flowmod.add_parsers(sub)

    s = sub.add_parser("search", help="search messages in a group (-p required)")
    s.add_argument("pos", nargs="*", metavar="[GROUP] QUERY")
    s.add_argument("-n", "--limit", type=int, default=50)
    s.set_defaults(fn=cmd_search)
    return p


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:  # bare `llm-bus`: teach the caller how to use it
        print(guide_text(), end="")
        return 0
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    # Let flags precede the free positionals (`send --reply 3 "body"`): argparse otherwise
    # refuses trailing positionals after an option for nargs="*" params.
    if extra:
        if hasattr(args, "pos") and not any(x.startswith("-") for x in extra):
            args.pos = list(args.pos) + extra
        else:
            parser.error(f"unrecognized arguments: {' '.join(extra)}")
    try:
        args.agent = load_agent(args.agent_ref) if args.agent_ref else None
        args.project = load_project(args.project_ref) if args.project_ref else None
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    bus = Bus(Path(args.db) if args.db else None)
    args.bus = bus
    try:
        if (
            args.agent is not None
        ):  # every command, incl. the Stop hook, is a liveness signal
            bus.touch(args.agent.name, args.cmd)
        return args.fn(bus, args) or 0
    except BusError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        bus.close()
