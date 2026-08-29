"""On-demand agents: screen-session liveness, spawn, kill, auto-spawn on dm/ask."""

import json
import os
import shutil
import stat
import subprocess
import time

import pytest

from llm_bus.cli import main

# A fake `screen` that records invocations and keeps a session list in a file, so tests are
# deterministic and don't touch the real screen server.
FAKE_SCREEN = r"""#!/bin/sh
state="$FAKE_SCREEN_STATE"
log="$FAKE_SCREEN_LOG"
echo "$@" >> "$log"
case "$1" in
  -ls)
    echo "There are screens on:"
    [ -f "$state" ] && while read -r name; do printf '\t%s.%s\t(Detached)\n' 4242 "$name"; done < "$state"
    echo "1 Socket in /tmp/screens."
    exit 1 ;;   # real screen returns non-zero here too
  -dmS)
    echo "$2" >> "$state"; exit 0 ;;
  -S)  # -S pid.name -X quit
    n="${2#*.}"; grep -v "^$n$" "$state" > "$state.tmp" 2>/dev/null; mv "$state.tmp" "$state"; exit 0 ;;
esac
exit 1
"""


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


def screen_log(env):
    p = env / "screen.log"
    return p.read_text().splitlines() if p.exists() else []


def spawn_via_dm(capsys, peer, *flags):
    """The only way to start an agent is to message it: DM from a throwaway 'alice'."""
    if (
        not (os.environ["LLM_BUS_HOME"] + "/agents/alice/config.toml")
        or main(["-c", "alice", "whoami"]) != 0
    ):
        main(["--json", "init", "alice"])
    capsys.readouterr()
    rc, m = run(capsys, "-c", "alice", "dm", peer, "wake up", *flags)
    return rc, (m or {}).get("spawn")


def test_init_writes_spawn_table(env, capsys):
    rc, info = run(
        capsys,
        "init",
        "bob",
        "--cmd",
        "echo hi {name} {dir}",
        "--session",
        "bob-s",
        "--cwd",
        str(env),
    )
    assert rc == 0 and info["spawn"] == "echo hi {name} {dir}"
    cfg = (env / "home/agents/bob/config.toml").read_text()
    assert "[spawn]" in cfg and 'session = "bob-s"' in cfg and f'cwd = "{env}"' in cfg
    _, rows = run(capsys, "ps")
    assert len(rows) == 1
    assert {
        k: rows[0][k] for k in ("name", "role", "session", "alive", "spawnable")
    } == {
        "name": "bob",
        "role": None,
        "session": "bob-s",
        "alive": False,
        "spawnable": True,
    }


def test_spawn_ps_kill(env, capsys):
    run(capsys, "init", "bob", "--cmd", "sleep 1 # {name}")
    run(capsys, "init", "carol")  # no cmd

    rc, r = spawn_via_dm(capsys, "bob")
    assert rc == 0 and r["status"] == "spawned" and r["cmd"] == "sleep 1 # bob"
    assert r["cwd"] == str(env / "home/agents/bob")
    assert "-dmS bob sh -c sleep 1 # bob" in screen_log(env)

    _, r = spawn_via_dm(capsys, "bob")  # idempotent
    assert r["status"] == "alive"
    assert screen_log(env).count("-dmS bob sh -c sleep 1 # bob") == 1

    _, rows = run(capsys, "ps")
    assert {x["name"]: x["alive"] for x in rows if x["name"] != "alice"} == {
        "bob": True,
        "carol": False,
    }
    _, r = spawn_via_dm(capsys, "carol")
    assert r is None  # no cmd → nothing to start

    _, r = run(capsys, "kill", "bob")
    assert r["killed"] is True
    _, r = run(capsys, "kill", "bob")
    assert r["killed"] is False
    assert not (env / "home/agents/bob/.spawn.lock").exists()


def test_dm_auto_spawns_dead_peer(env, capsys):
    run(capsys, "init", "alice")
    run(capsys, "init", "bob", "--cmd", "true")
    rc, m = run(capsys, "-c", "alice", "dm", "bob", "hey")
    assert rc == 0 and m["spawn"]["status"] == "spawned"
    _, m = run(capsys, "-c", "alice", "dm", "bob", "again")
    assert m["spawn"]["status"] == "alive"
    # peers without a cmd, or --no-spawn, don't spawn
    _, m = run(capsys, "-c", "bob", "dm", "alice", "yo")
    assert m["spawn"] is None
    run(capsys, "kill", "bob")
    _, m = run(capsys, "-c", "alice", "dm", "bob", "quiet", "--no-spawn")
    assert m["spawn"] is None and "-dmS bob" not in screen_log(env)[-1]


def test_ask_spawns_hub(env, capsys):
    run(capsys, "init", "alice")
    run(capsys, "hub", "init")
    cfg = env / "home/agents/hub/config.toml"
    cfg.write_text(cfg.read_text() + '\n[spawn]\ncmd = "true"\n')
    rc, msgs = run(
        capsys, "-c", "alice", "ask", "who?", "-t", "0.2", "--interval", "0.05"
    )
    assert rc == 2 and msgs == []  # nobody answered, but the hub got started
    assert any(line.startswith("-dmS hub ") for line in screen_log(env))


def test_spawn_lock_prevents_double_start(env, capsys):
    run(capsys, "init", "bob", "--cmd", "true")
    (env / "home/agents/bob/.spawn.lock").touch()  # someone else is mid-spawn
    _, r = spawn_via_dm(capsys, "bob")
    assert r["status"] == "alive" and "-dmS" not in "".join(screen_log(env))


def test_missing_screen_is_an_error(env, capsys, monkeypatch):
    monkeypatch.setenv("LLM_BUS_SCREEN", str(env / "nope"))
    run(capsys, "init", "bob", "--cmd", "true")
    assert main(["ps"]) == 1
    # but sending a DM still succeeds; the spawn failure is only a warning
    run(capsys, "init", "alice")
    rc, m = run(capsys, "-c", "alice", "dm", "bob", "hi")
    assert rc == 0 and m["spawn"]["status"] == "error"


@pytest.mark.skipif(shutil.which("screen") is None, reason="GNU screen not installed")
def test_real_screen_roundtrip(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LLM_BUS_DB", str(tmp_path / "bus.db"))
    monkeypatch.setenv("LLM_BUS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LLM_BUS_SCREEN", raising=False)
    session = f"llmbus-test-{os.getpid()}"
    marker = tmp_path / "ran"
    run(
        capsys,
        "init",
        "bob",
        "--session",
        session,
        "--cmd",
        f"echo {{name}} > {marker}; sleep 30",
    )
    try:
        _, r = spawn_via_dm(capsys, "bob")
        assert r["status"] == "spawned"
        for _ in range(50):
            if marker.exists():
                break
            time.sleep(0.1)
        assert marker.read_text().strip() == "bob"
        _, rows = run(capsys, "ps")
        assert rows[0]["alive"] is True
    finally:
        _, r = run(capsys, "kill", "bob")
    assert r["killed"] is True
    assert (
        session
        not in subprocess.run(
            ["screen", "-ls"], capture_output=True, text=True, check=False
        ).stdout
    )
