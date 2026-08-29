import json
import subprocess
import sys
import threading
import time

import pytest

from llm_bus.cli import main
from llm_bus.db import Bus, BusError


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BUS_DB", str(tmp_path / "bus.db"))
    monkeypatch.setenv("LLM_BUS_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run(capsys, *argv):
    rc = main(["--json", *argv])
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out) if out else None


def setup(capsys, agents=("alice", "bob"), group="dev"):
    """Create project 'p' with .llm_bus_project (default group), and agents joined to it."""
    run(capsys, "project", "init", "p", "--group", group)
    for a in agents:
        run(capsys, "init", a, "--role", f"{a}-role")
        run(capsys, "-c", a, "-p", ".llm_bus_project", "join")


def test_project_init_and_lists(env, capsys):
    rc, info = run(capsys, "project", "init", "p1", "--group", "g1")
    assert rc == 0 and info["created"] and (env / ".llm_bus_project").exists()
    _, projects = run(capsys, "project", "list")
    assert [p["name"] for p in projects] == ["p1"]
    _, groups = run(capsys, "-p", ".llm_bus_project", "group", "list")
    assert [g["name"] for g in groups] == ["g1"]
    run(capsys, "-p", "p1", "group", "create", "g2")  # -p by name
    _, groups = run(capsys, "-p", "p1", "group", "list")
    assert [g["name"] for g in groups] == ["g1", "g2"]
    assert main(["project", "create", "p1"]) == 1  # duplicate
    assert main(["project", "init", "p1"]) == 1  # file exists, no --force
    assert main(["group", "list"]) == 1  # -p required


def test_agent_folder(env, capsys):
    rc, info = run(
        capsys, "init", "alice", "--role", "dev", "--context", "You review PRs."
    )
    d = env / "home" / "agents" / "alice"
    assert rc == 0 and info["dir"] == str(d)
    assert (d / "config.toml").is_file() and (d / "NOTES.md").is_file()
    assert "You review PRs." in (d / "CONTEXT.md").read_text()
    assert main(["init", "alice"]) == 1  # exists

    run(capsys, "-c", "alice", "remember", "bob owns the DB layer")
    _, who = run(capsys, "-c", "alice", "whoami")
    assert who["name"] == "alice" and who["role"] == "dev"
    assert "bob owns" in who["notes"] and "review PRs" in who["context"]
    assert who["memberships"] == []

    _, agents = run(capsys, "agents")
    assert [a["name"] for a in agents] == ["alice"]
    # -c by folder path works too
    _, who = run(capsys, "-c", str(d), "whoami")
    assert who["name"] == "alice"
    assert main(["-c", "nobody", "whoami"]) == 1
    assert main(["whoami"]) == 1


def test_join_send_read_search(env, capsys):
    setup(capsys)
    P = ["-p", ".llm_bus_project"]
    _, members = run(capsys, *P, "members")
    assert members == [
        {"agent": "alice", "role": "alice-role"},
        {"agent": "bob", "role": "bob-role"},
    ]

    for i in range(3):
        assert run(capsys, "-c", "alice", *P, "send", f"hello {i}")[0] == 0
    run(capsys, "-c", "bob", *P, "send", "dev", "needle here")  # explicit group
    run(capsys, *P, "group", "create", "other")
    run(capsys, "-c", "bob", *P, "send", "other", "elsewhere")

    _, msgs = run(capsys, *P, "read", "-n", "2")
    assert [m["body"] for m in msgs] == ["hello 2", "needle here"]
    assert msgs[-1]["role"] == "bob-role"
    _, msgs = run(capsys, *P, "read", "other")
    assert [m["body"] for m in msgs] == ["elsewhere"]
    _, msgs = run(capsys, *P, "search", "needle")
    assert len(msgs) == 1 and msgs[0]["sender"] == "bob"

    assert main(["-p", ".llm_bus_project", "send", "x"]) == 1  # -c required
    assert main(["-c", "alice", "send", "x"]) == 1  # -p required
    assert (
        main(["-c", "alice", "-p", "p", "send", "x"]) == 1
    )  # -p by name → no default group


def test_cursor_unread(env, capsys):
    setup(capsys)
    P = ["-p", ".llm_bus_project"]
    run(capsys, "-c", "bob", *P, "send", "m1")
    run(capsys, "-c", "bob", *P, "send", "m2")
    _, who = run(capsys, "-c", "alice", *P, "whoami")
    assert who["memberships"] == [
        {"project": "p", "group": "dev", "unread": 2, "cursor": 0}
    ]
    assert who["project"]["group"] == "dev"

    _, msgs = run(capsys, "-c", "alice", *P, "read", "--unread")
    assert [m["body"] for m in msgs] == ["m1", "m2"]
    _, msgs = run(capsys, "-c", "alice", *P, "read", "--unread")
    assert msgs == []
    _, msgs = run(capsys, *P, "read")  # plain read still shows history
    assert len(msgs) == 2

    run(capsys, "-c", "alice", *P, "send", "mine")  # own sends advance own cursor
    _, msgs = run(capsys, "-c", "alice", *P, "read", "--unread")
    assert msgs == []
    _, who = run(capsys, "-c", "bob", *P, "whoami")
    assert who["memberships"][0]["unread"] == 1  # bob hasn't seen "mine"


def test_wait(env, capsys):
    setup(capsys)
    P = ["-p", ".llm_bus_project"]
    rc, msgs = run(capsys, "-c", "alice", *P, "wait", "-t", "0.3", "--interval", "0.05")
    assert rc == 2 and msgs == []

    def later():
        time.sleep(0.2)
        Bus(env / "bus.db").send("p", "dev", "bob", "ping")

    threading.Thread(target=later).start()
    rc, msgs = run(capsys, "-c", "alice", *P, "wait", "-t", "3", "--interval", "0.05")
    assert rc == 0 and [m["body"] for m in msgs] == ["ping"]
    # already consumed → unread is empty, wait would block again
    _, msgs = run(capsys, "-c", "alice", *P, "read", "--unread")
    assert msgs == []


def test_bus_error_unknown_group(env):
    with pytest.raises(BusError):
        Bus(env / "bus.db").get_group("nope", "x")


def test_guide_help_entrypoint(env, capsys):
    assert main([]) == 0
    assert "RECOMMENDED LOOP" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0 and "llm-bus guide" in capsys.readouterr().out
    out = subprocess.run(
        [sys.executable, "-m", "llm_bus", "project", "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0 and "no projects" in out.stdout
