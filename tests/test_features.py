"""Stop hook, install-hook, @mention spawn, wait --all, threads, atomic state, presence, reap."""

import io
import json
import os
import stat
import threading
import time

import pytest

from llm_bus.cli import main
from llm_bus.config import load_agent
from llm_bus.db import Bus
from tests.test_spawn import FAKE_SCREEN


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BUS_DB", str(tmp_path / "bus.db"))
    monkeypatch.setenv("LLM_BUS_HOME", str(tmp_path / "home"))
    shim = tmp_path / "screen"
    shim.write_text(FAKE_SCREEN)
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("LLM_BUS_SCREEN", str(shim))
    monkeypatch.setenv("FAKE_SCREEN_STATE", str(tmp_path / "sessions"))
    monkeypatch.setenv("FAKE_SCREEN_LOG", str(tmp_path / "screen.log"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run(capsys, *argv):
    rc = main(["--json", *argv])
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out) if out else None


def setup(capsys):
    run(capsys, "project", "init", "p", "--group", "dev")
    for a in ("alice", "bob"):
        run(capsys, "init", a, "--role", f"{a}-role")
        run(capsys, "-c", a, "-p", ".llm_bus_project", "join")


P = ["-p", ".llm_bus_project"]


# --- 1. Stop hook -------------------------------------------------------------
def test_hook_blocks_only_when_unread(env, capsys, monkeypatch):
    setup(capsys)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = main(["-c", "alice", "hook"])
    assert (
        rc == 0 and capsys.readouterr().out == ""
    )  # nothing unread → silent, let it stop

    run(capsys, "-c", "bob", *P, "send", "@alice: please review #12")
    run(capsys, "-c", "bob", "dm", "alice", "also a dm")
    rc = main(["-c", "alice", "hook"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["decision"] == "block"
    assert "[p/dev]" in out["reason"] and "[dm:bob]" in out["reason"]
    assert "please review #12" in out["reason"] and "also a dm" in out["reason"]
    assert "llm-bus -c alice" in out["reason"]

    # delivered → cursors advanced → next stop is not blocked
    rc = main(["-c", "alice", "hook"])
    assert rc == 0 and capsys.readouterr().out == ""
    # own messages never trigger the hook
    run(capsys, "-c", "alice", *P, "send", "done")
    main(["-c", "alice", "hook"])
    assert capsys.readouterr().out == ""


def test_install_hook(env, capsys):
    setup(capsys)
    rc, info = run(capsys, "-c", "alice", "install-hook")
    assert rc == 0 and info["installed"]
    cfg = json.loads((env / ".claude" / "settings.local.json").read_text())
    assert cfg["hooks"]["Stop"] == [
        {"hooks": [{"type": "command", "command": "llm-bus -c alice hook"}]}
    ]
    # idempotent, and merges into existing settings
    (env / ".claude" / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(ls)"]}, **cfg})
    )
    rc, info = run(capsys, "-c", "alice", "install-hook")
    assert not info["installed"]
    cfg2 = json.loads((env / ".claude" / "settings.local.json").read_text())
    assert (
        cfg2["permissions"] == {"allow": ["Bash(ls)"]}
        and len(cfg2["hooks"]["Stop"]) == 1
    )
    # custom file + different agent appends a second hook entry
    rc, info = run(capsys, "-c", "bob", "install-hook", "--file", "s.json")
    assert (
        json.loads((env / "s.json").read_text())["hooks"]["Stop"][0]["hooks"][0][
            "command"
        ]
        == "llm-bus -c bob hook"
    )


# --- 2. @mention spawn ---------------------------------------------------------
def test_send_spawns_mentioned_agents(env, capsys):
    setup(capsys)
    run(capsys, "init", "carol", "--cmd", "echo carol {name}")
    run(capsys, "init", "bob", "--force", "--cmd", "echo bob {name}")
    rc, m = run(
        capsys, "-c", "alice", *P, "send", "@bob: and @carol, see this. @alice @nobody"
    )
    assert rc == 0 and m["spawned"] == {"bob": "spawned", "carol": "spawned"}
    log = (env / "screen.log").read_text()
    assert "-dmS bob " in log and "-dmS carol " in log
    # already alive → not respawned; --no-spawn → untouched; email-ish text isn't a mention
    rc, m = run(capsys, "-c", "alice", *P, "send", "@bob again")
    assert m["spawned"] == {"bob": "alive"}
    rc, m = run(capsys, "-c", "alice", *P, "send", "--no-spawn", "@carol x")
    assert m["spawned"] == {}
    rc, m = run(capsys, "-c", "alice", *P, "send", "mail me@bob.com")
    assert m["spawned"] == {}


# --- 3. wait --all -------------------------------------------------------------
def test_wait_all(env, capsys):
    setup(capsys)
    rc, msgs = run(
        capsys, "-c", "alice", "wait", "--all", "-t", "0.2", "--interval", "0.05"
    )
    assert rc == 2 and msgs == []  # no -p needed

    def later():
        time.sleep(0.15)
        b = Bus(env / "bus.db")
        b.ensure_group("_dm", "alice~bob")
        b.join_group("_dm", "alice~bob", "alice")
        b.join_group("_dm", "alice~bob", "bob")
        b.send("_dm", "alice~bob", "bob", "dm hit")
        b.send("p", "dev", "bob", "group hit")

    threading.Thread(target=later).start()
    rc, msgs = run(
        capsys, "-c", "alice", "wait", "--all", "-t", "3", "--interval", "0.05"
    )
    assert rc == 0 and sorted(m["body"] for m in msgs) == ["dm hit", "group hit"]
    _, who = run(capsys, "-c", "alice", "whoami")
    assert who["memberships"][0]["unread"] == 0 and who["dms"] == [
        {"peer": "bob", "unread": 0}
    ]


# --- 4. threads ------------------------------------------------------------------
def test_reply_and_thread(env, capsys):
    setup(capsys)
    _, root = run(capsys, "-c", "alice", *P, "send", "question?")
    _, r1 = run(capsys, "-c", "bob", *P, "send", "--reply", str(root["id"]), "answer")
    _, r2 = run(capsys, "-c", "alice", *P, "send", "--reply", str(r1["id"]), "thanks")
    run(capsys, "-c", "bob", *P, "send", "unrelated")
    assert r1["reply_to"] == root["id"] and r2["reply_to"] == r1["id"]

    for start in (
        root["id"],
        r1["id"],
        r2["id"],
    ):  # any message in the thread finds the root
        _, t = run(capsys, "read", "--thread", str(start))
        assert [m["body"] for m in t] == ["question?", "answer", "thanks"]

    assert (
        main(["-c", "bob", "-p", ".llm_bus_project", "send", "--reply", "999", "x"])
        == 1
    )
    run(capsys, *P, "group", "create", "other")
    run(capsys, "-c", "bob", *P, "join", "other")
    assert (
        main(["-c", "bob", "-p", "p", "send", "other", "--reply", str(root["id"]), "x"])
        == 1
    )

    _, d = run(capsys, "-c", "bob", "dm", "alice", "dm q")
    _, d2 = run(capsys, "-c", "alice", "dm", "bob", "--reply", str(d["id"]), "dm a")
    assert d2["reply_to"] == d["id"]
    main(["read", "--thread", str(d["id"])])
    assert "↳#" in capsys.readouterr().out


# --- 5. robustness -------------------------------------------------------------------
def test_state_write_is_atomic_and_merged(env, capsys):
    setup(capsys)
    a1 = load_agent("alice")
    a2 = load_agent("alice")  # a second process as the same agent
    a1.set_cursor("p", "dev", 5)
    a2.set_cursor(
        "p", "review", 3
    )  # stale in-memory view of p/dev, must not clobber it
    on_disk = json.loads((env / "home/agents/alice/state.json").read_text())
    assert on_disk["cursors"] == {"p/dev": 5, "p/review": 3}
    a2.set_cursor("p", "dev", 2)  # lower than disk → no regression
    on_disk = json.loads((env / "home/agents/alice/state.json").read_text())
    assert on_disk["cursors"]["p/dev"] == 5
    assert not [f for f in os.listdir(env / "home/agents/alice") if f.endswith(".tmp")]


def test_presence_in_ps(env, capsys):
    setup(capsys)
    run(capsys, "init", "zed")  # init doesn't run as -c zed → never seen
    _, rows = run(capsys, "ps")
    by = {r["name"]: r for r in rows}
    assert by["alice"]["last_cmd"] == "join" and by["alice"]["idle_s"] < 5
    assert by["zed"]["last_seen"] is None
    # the hook itself doesn't count as activity
    main(["-c", "alice", "hook"])
    capsys.readouterr()
    _, rows = run(capsys, "ps")
    assert {r["name"]: r for r in rows}["alice"]["last_cmd"] == "join"


def test_reap(env, capsys):
    setup(capsys)
    run(capsys, "init", "w1", "--cmd", "echo {name}", "--idle-timeout", "10")
    run(capsys, "init", "w2", "--cmd", "echo {name}", "--idle-timeout", "10")
    run(capsys, "init", "w3", "--cmd", "echo {name}")  # no idle_timeout → never reaped
    for w in ("w1", "w2", "w3"):
        run(capsys, "-c", "alice", "dm", w, "wake up")
    cfg = (env / "home/agents/w1/config.toml").read_text()
    assert "idle_timeout = 10" in cfg
    b = Bus(env / "bus.db")
    b.touch("w1", "wait", now=time.time() - 11 * 60)  # idle 11 min
    b.touch("w2", "wait")  # active now
    b.touch("w3", "wait", now=time.time() - 999 * 60)

    _, dry = run(capsys, "reap", "--dry-run")
    assert [e["agent"] for e in dry] == ["w1"] and not dry[0]["killed"]
    _, rows = run(capsys, "ps")
    assert all(r["alive"] for r in rows if r["name"].startswith("w"))
    _, reaped = run(capsys, "reap")
    assert [e["agent"] for e in reaped] == ["w1"] and reaped[0]["killed"]
    _, rows = run(capsys, "ps")
    alive = {r["name"]: r["alive"] for r in rows}
    assert not alive["w1"] and alive["w2"] and alive["w3"]
    # --idle overrides per-agent config (w3 has none but is idle 999m)
    _, reaped = run(capsys, "reap", "--idle", "100")
    assert [e["agent"] for e in reaped] == ["w3"]
