"""Flows: file round-trip, materialization (agents, worktrees), bus-enforced routing, supervisor,
graph editing."""

import json
import subprocess
import tomllib

import pytest

from llm_bus.cli import main
from llm_bus.flow import dump_flow, load_flow

from .test_spawn import env  # noqa: F401 — fake screen fixture

FLOW = """\
name = "loop"
project = "p"
repo = "{repo}"
entry = "impl"
max_turns = 6

[runners.fake]
cmd = "echo {{model}} {{prompt}}"

[agents.impl]
runner = "fake"
model = "m1"
role = "implement"
worktree = true
next = ["review"]

[agents.review]
runner = "fake"
worktree = "impl"
next = ["impl", "resolver"]

[agents.resolver]
runner = "fake"
next = []
[agents.resolver.on]
done = ["ideas"]

[agents.ideas]
runner = "fake"
next = ["impl"]
[agents.ideas.on]
dry = ["scout"]

[agents.scout]
runner = "fake"
next = ["ideas"]

[supervisor]
runner = "fake"
every = 3
"""


def run(capsys, *argv):
    rc = main(["--json", *argv])
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out) if out else None


def sessions(env):  # noqa: F811
    p = env / "sessions"
    return p.read_text().split() if p.exists() else []


def clear_sessions(env):  # noqa: F811
    (env / "sessions").write_text("")


@pytest.fixture
def flow_file(env):  # noqa: F811
    repo = env / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
    )
    f = env / "flow.toml"
    f.write_text(FLOW.format(repo=repo))
    return f


def test_example_roundtrips(env, capsys):  # noqa: F811
    assert main(["flow", "example"]) == 0
    text = capsys.readouterr().out
    f = env / "ex.toml"
    f.write_text(text)
    fl = load_flow(str(f))
    assert fl.entry == "implementer"
    again = tomllib.loads(dump_flow(fl))
    assert again["agents"]["resolver"]["on"] == {
        "ok": ["ideas"],
        "fix": ["implementer"],
    }
    assert again["agents"]["reviewer"]["on"]["dispute"] == ["resolver"]
    assert again["supervisor"]["every"] == 5
    assert again["agents"]["reviewer"]["worktree"] == "implementer"


def test_show_table(flow_file, capsys):
    assert main(["flow", "show", str(flow_file)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert (
        lines[1].startswith("  impl        next → review")
        and "(fake/m1; own worktree; implement)" in lines[1]
    )
    assert (
        lines[2].startswith("  review      next → impl, resolver")
        and "worktree of impl" in lines[2]
    )
    assert lines[3:5] == [
        "  resolver    next → –" + " " * 19 + "  (fake)",
        "              on done → ideas",
    ]
    assert lines[-1].startswith("  supervisor  every 3 handoffs")


def test_up_creates_agents_group_and_worktrees(flow_file, env, capsys):  # noqa: F811
    rc, info = run(capsys, "flow", "up", str(flow_file))
    assert rc == 0
    names = {a["agent"] for a in info["agents"]}
    assert names == {
        "loop.impl",
        "loop.review",
        "loop.resolver",
        "loop.ideas",
        "loop.scout",
        "loop.supervisor",
    }
    wt = env / "repo" / ".llm_bus_worktrees" / "loop-impl"
    assert wt.is_dir() and info["worktrees"]["impl"] == str(wt)
    branches = subprocess.run(
        ["git", "-C", str(env / "repo"), "branch", "--list", "llm-bus/loop/impl"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    assert "llm-bus/loop/impl" in branches
    # reviewer shares impl's worktree; scout has no worktree → repo root
    cwd = {a["node"]: a["cwd"] for a in info["agents"]}
    assert cwd["review"] == str(wt) and cwd["scout"] == str(env / "repo")
    # members of the group
    _, members = run(capsys, "-p", "p", "members", "loop")
    assert {m["agent"] for m in members} == names
    # spawn cmd wraps the runner with `flow done`
    cfg = tomllib.loads((env / "home/agents/loop.impl/config.toml").read_text())
    assert cfg["spawn"]["cmd"].startswith("echo m1 ")
    assert cfg["spawn"]["cmd"].endswith("; llm-bus -c loop.impl flow done --rc $?")
    assert cfg["flow"] == {"file": str(flow_file.resolve()), "node": "impl"}
    # idempotent
    rc, _ = run(capsys, "flow", "up", str(flow_file))
    assert rc == 0
    # context mentions the routes
    ctx = (env / "home/agents/loop.resolver/CONTEXT.md").read_text()
    assert "flow signal done" in ctx and "→ ideas" in ctx


def test_routing_cycle(flow_file, env, capsys):  # noqa: F811
    rc, r = run(capsys, "flow", "run", str(flow_file), "improve AUC")
    assert rc == 0 and r["to"] == "impl" and r["spawn"] == "spawned"
    assert sessions(env) == ["loop.impl"]
    _, msgs = run(capsys, "-p", "p", "read", "loop")
    assert msgs[-1]["sender"] == "user" and "improve AUC" in msgs[-1]["body"]

    clear_sessions(env)  # impl's runner "exited"
    _rc, d = run(capsys, "-c", "loop.impl", "flow", "done")
    assert rc == 0 and d["turn"] == 1 and d["routed"] == {"review": "spawned"}
    assert sessions(env) == ["loop.review"]

    clear_sessions(env)
    rc, d = run(capsys, "-c", "loop.review", "flow", "done", "--rc", "0")
    assert d["turn"] == 2 and set(d["routed"]) == {"impl", "resolver"}
    assert set(sessions(env)) == {"loop.impl", "loop.resolver"}

    # resolver exits without a signal → nobody; turn 3 wakes the supervisor (every=3)
    clear_sessions(env)
    rc, d = run(capsys, "-c", "loop.resolver", "flow", "done")
    assert d["turn"] == 3 and d["routed"] == {}
    assert sessions(env) == ["loop.supervisor"]
    _, msgs = run(capsys, "-p", "p", "read", "loop")
    assert "waking supervisor: periodic" in msgs[-1]["body"]
    assert "resolver → (nobody)" in msgs[-2]["body"]

    # supervisor finishing doesn't count a turn or route
    clear_sessions(env)
    rc, d = run(capsys, "-c", "loop.supervisor", "flow", "done")
    assert d["routed"] == [] and sessions(env) == []
    _, st = run(capsys, "flow", "status", str(flow_file))
    assert st["turns"] == 3 and st["status"] == "running"

    # signal routing: resolver says done → ideas; signal is consumed
    rc, s = run(capsys, "-c", "loop.resolver", "flow", "signal", "done")
    assert s["routes_to"] == ["ideas"]
    rc, d = run(capsys, "-c", "loop.resolver", "flow", "done")
    assert d["signal"] == "done" and d["routed"] == {"ideas": "spawned"}
    state = json.loads((env / "home/agents/loop.resolver/state.json").read_text())
    assert not state.get("flow_signal")
    clear_sessions(env)
    rc, d = run(capsys, "-c", "loop.resolver", "flow", "done")
    assert d["signal"] is None and d["routed"] == {}

    # unknown signal pauses; `blocked` goes to the supervisor; non-zero rc wakes the supervisor
    run(capsys, "-c", "loop.ideas", "flow", "signal", "nonsense")
    rc, d = run(capsys, "-c", "loop.ideas", "flow", "done")
    assert d["routed"] == {} and d["turn"] == 6
    _, msgs = run(capsys, "-p", "p", "read", "loop", "-n", "2")
    assert (
        "unknown signal" in msgs[0]["body"]
    )  # msgs[1] = periodic supervisor wake (turn 6)
    # turn 7 > max_turns=6 → stopped + supervisor woken
    clear_sessions(env)
    rc, d = run(capsys, "-c", "loop.scout", "flow", "done")
    assert d["stopped"] and sessions(env) == ["loop.supervisor"]
    _, st = run(capsys, "flow", "status", str(flow_file))
    assert st["status"] == "stopped"
    clear_sessions(env)
    rc, d = run(capsys, "-c", "loop.ideas", "flow", "done")
    assert d["stopped"] and sessions(env) == []


def test_blocked_and_rc_wake_supervisor(flow_file, env, capsys):  # noqa: F811
    run(capsys, "flow", "up", str(flow_file))
    run(capsys, "-c", "loop.impl", "flow", "signal", "blocked")
    _rc, d = run(capsys, "-c", "loop.impl", "flow", "done")
    assert d["routed"] == {"supervisor": "spawned"}
    clear_sessions(env)
    _rc, d = run(capsys, "-c", "loop.impl", "flow", "done", "--rc", "3")
    assert set(sessions(env)) == {"loop.review", "loop.supervisor"}
    _, msgs = run(capsys, "-p", "p", "read", "loop", "-n", "2")
    assert "rc=3" in msgs[0]["body"] and "exited with rc=3" in msgs[1]["body"]


def test_stop_resume_down(flow_file, env, capsys):  # noqa: F811
    run(capsys, "flow", "run", str(flow_file), "go")
    run(capsys, "flow", "stop", str(flow_file))
    _rc, d = run(capsys, "-c", "loop.impl", "flow", "done")
    assert d["stopped"] and sessions(env) == ["loop.impl"]  # nothing new spawned
    run(capsys, "flow", "resume", str(flow_file))
    _rc, d = run(capsys, "-c", "loop.impl", "flow", "done")
    assert d["routed"] == {"review": "spawned"}
    _rc, d = run(capsys, "flow", "down", str(flow_file), "--worktrees")
    assert set(d["killed"]) == {"loop.impl", "loop.review"} and sessions(env) == []
    assert not (env / "repo/.llm_bus_worktrees/loop-impl").exists()


def test_edit_graph(flow_file, env, capsys):  # noqa: F811
    run(capsys, "flow", "up", str(flow_file))
    rc, g = run(
        capsys,
        "flow",
        "add-agent",
        str(flow_file),
        "tester",
        "--runner",
        "fake",
        "--model",
        "m9",
        "--role",
        "run tests",
        "--next",
        "review",
        "--on",
        "fail=impl,supervisor",
        "--worktree",
        "impl",
    )
    assert rc == 0
    t = g["graph"]["agents"]["tester"]
    assert t == {
        "runner": "fake",
        "model": "m9",
        "role": "run tests",
        "worktree": "impl",
        "next": ["review"],
        "on": {"fail": ["impl", "supervisor"]},
    }
    assert (env / "home/agents/loop.tester/config.toml").exists()
    rc, g = run(capsys, "flow", "route", str(flow_file), "impl", "tester")
    assert g["graph"]["agents"]["impl"]["next"] == ["review", "tester"]
    rc, g = run(capsys, "flow", "unroute", str(flow_file), "impl", "review")
    assert g["graph"]["agents"]["impl"]["next"] == ["tester"]
    rc, g = run(
        capsys, "flow", "route", str(flow_file), "ideas", "tester", "--on", "dry"
    )
    assert g["graph"]["agents"]["ideas"]["on"]["dry"] == ["scout", "tester"]
    rc, g = run(capsys, "flow", "set", str(flow_file), "max_turns", "50")
    assert g["graph"]["max_turns"] == 50
    # regenerated context reflects the new routes, NOTES survive
    (env / "home/agents/loop.impl/NOTES.md").write_text("keep me\n")
    rc, g = run(capsys, "flow", "rm-agent", str(flow_file), "tester")
    assert "tester" not in g["graph"]["agents"]
    assert g["graph"]["agents"]["impl"]["next"] == []
    assert g["graph"]["agents"]["ideas"]["on"]["dry"] == ["scout"]
    assert (env / "home/agents/loop.impl/NOTES.md").read_text() == "keep me\n"
    rc, _ = run(capsys, "flow", "route", str(flow_file), "impl", "ghost")
    assert rc == 1
    # bad flow files
    (env / "bad.toml").write_text('name = "x"\n[agents.a]\nnext = ["zzz"]\n')
    assert main(["flow", "show", str(env / "bad.toml")]) == 1
    assert "unknown 'zzz'" in capsys.readouterr().err
