"""llm-bus: a tiny message bus CLI for agents."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import (
    PROJECT_FILE,
    Agent,
    ProjectRef,
    create_agent,
    list_agents,
    load_agent,
    load_project,
    write_project_file,
)
from .db import Bus, BusError
from .guide import guide_text


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


def _group(args, pos: list[str], n_tail: int) -> tuple[str, str, list[str]]:
    """Resolve (project, group, tail) from -p plus positionals `[GROUP] *tail`."""
    pr = _project(args)
    extra = len(pos) - n_tail
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
    return f"#{m['id']} [{ts}] {_who(m['sender'], m.get('role'))}: {m['body']}"


def _fmt_members(ms: list[dict]) -> str:
    return ", ".join(_who(m["agent"], m["role"]) for m in ms) or "-"


# --- agent commands ---------------------------------------------------------
def cmd_init(bus, args):
    try:
        a = create_agent(
            args.name,
            args.role,
            args.context,
            Path(args.dir) if args.dir else None,
            args.force,
        )
    except ValueError as e:
        raise BusError(str(e)) from None
    _emit(
        args,
        {"name": a.name, "role": a.role, "dir": str(a.dir)},
        f"created agent {_who(a.name, a.role)} at {a.dir}",
    )


def cmd_agents(bus, args):
    agents = list_agents()
    lines = [
        f"{a['name']}  (invalid: {a['error']})"
        if "error" in a
        else f"{_who(a['name'], a['role'])}  {a['dir']}"
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


def cmd_whoami(bus, args):
    a = _agent(args)
    pr = args.project
    memberships = bus.memberships(a.name)
    unread = []
    for m in memberships:
        last = bus.last_id(m["project"], m["group"])
        cur = a.cursor(m["project"], m["group"])
        unread.append({**m, "unread": max(0, last - cur), "cursor": cur})
    data = {
        "name": a.name,
        "role": a.role,
        "dir": str(a.dir),
        "context": a.context(),
        "notes": a.notes(),
        "project": {
            "name": pr.name,
            "group": pr.group,
            "file": str(pr.path) if pr.path else None,
        }
        if pr
        else None,
        "memberships": unread,
    }
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
        return
    out = [f"YOU ARE: {_who(a.name, a.role)}", f"AGENT DIR: {a.dir}"]
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
    ctx, notes = a.context().strip(), a.notes().strip()
    out.append(f"\n--- CONTEXT ({a.context_path}) ---\n{ctx or '(empty)'}")
    out.append(f"\n--- NOTES ({a.notes_path}) ---\n{notes or '(empty)'}")
    out.append(
        "\nNext: `llm-bus guide` for commands. Read unread with `read --unread`, block with `wait`."
    )
    print("\n".join(out))


# --- project / group commands --------------------------------------------------
def cmd_project_create(bus, args):
    p = bus.create_project(args.name)
    _emit(args, p, f"created project '{p['name']}'")


def cmd_project_init(bus, args):
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
        try:
            bus.create_group(args.name, args.group)
        except BusError:
            pass
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
    g = bus.create_group(pr.name, args.name)
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
    m = bus.send(project, group, a.name, body, a.role)
    a.set_cursor(project, group, m["id"])  # own message counts as read
    _emit(args, m, f"sent #{m['id']} to {project}/{group}")


def cmd_read(bus, args):
    project, group, _ = _group(args, args.pos, 0)
    after = args.after
    if args.unread:
        after = max(after, _agent(args).cursor(project, group))
    msgs = bus.latest(project, group, limit=args.limit, after=after)
    if msgs and args.agent is not None and (args.unread or args.mark):
        args.agent.set_cursor(project, group, msgs[-1]["id"])
    _emit(args, msgs, "\n".join(_fmt_msg(m) for m in msgs) or "(no messages)")


def cmd_wait(bus, args):
    a = _agent(args)
    project, group, _ = _group(args, args.pos, 0)
    after = args.after if args.after is not None else a.cursor(project, group)
    deadline = None if args.timeout <= 0 else time.monotonic() + args.timeout
    while True:
        msgs = bus.latest(project, group, limit=1000, after=after)
        if msgs:
            a.set_cursor(project, group, msgs[-1]["id"])
        if not args.include_self:
            msgs = [m for m in msgs if m["sender"] != a.name]
        if msgs:
            _emit(args, msgs, "\n".join(_fmt_msg(m) for m in msgs))
            return 0
        if deadline is not None and time.monotonic() >= deadline:
            _emit(args, [], "(timeout: no new messages)")
            return 2
        time.sleep(args.interval)


def cmd_search(bus, args):
    project, group, tail = _group(args, args.pos, 1)
    msgs = bus.search(project, group, tail[0], limit=args.limit)
    _emit(args, msgs, "\n".join(_fmt_msg(m) for m in msgs) or "(no matches)")


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
            f"  llm-bus -c alice -p {PROJECT_FILE} wait\n\n"
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
    s.set_defaults(fn=cmd_init)
    s = sub.add_parser("agents", help="list agents in the global agents folder")
    s.set_defaults(fn=cmd_agents)
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
    s.set_defaults(fn=cmd_send)

    s = sub.add_parser("read", help="read latest messages (-p required)")
    s.add_argument("pos", nargs="*", metavar="[GROUP]")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.add_argument("--after", type=int, default=0, help="only messages with id > AFTER")
    s.add_argument(
        "--unread",
        action="store_true",
        help="only messages after -c agent's cursor; advances it",
    )
    s.add_argument(
        "--mark",
        action="store_true",
        help="advance -c agent's cursor to the last shown message",
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
    s.set_defaults(fn=cmd_wait)

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
    args = build_parser().parse_args(argv)
    try:
        args.agent = load_agent(args.agent_ref) if args.agent_ref else None
        args.project = load_project(args.project_ref) if args.project_ref else None
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    bus = Bus(Path(args.db) if args.db else None)
    try:
        return args.fn(bus, args) or 0
    except BusError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        bus.close()
