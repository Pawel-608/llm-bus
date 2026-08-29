# llm-bus

Tiny CLI message bus so agents (or people) can talk to each other. SQLite-backed, no daemon, stdlib only.

## Install

```sh
./install.sh          # uv tool install --editable . → ~/.local/bin/llm-bus
```

Global state lives in `~/.llm_bus/`: `bus.db` (shared messages) and `agents/<name>/` (one folder per agent).
`LLM_BUS_DB` / `LLM_BUS_HOME` exist only to redirect storage (tests, sandboxes).

## Two things you always pass

Nothing is auto-discovered and no env vars are consulted, so any number of agents can run in the same
folder without interfering:

| flag | meaning | resolves from |
|---|---|---|
| `-c NAME\|DIR` | **who you are** | `~/.llm_bus/agents/<name>/` or a path to an agent folder |
| `-p FILE\|NAME` | **which project** | a `.llm_bus_project` TOML file (or its directory), or a bare project name |

```sh
llm-bus -c alice -p ./.llm_bus_project send "hello"
```

### Agent folder  `~/.llm_bus/agents/<name>/`

```
config.toml   name = "alice"  role = "backend dev"
CONTEXT.md    free-form instructions / knowledge for this agent (you or the agent edit it)
NOTES.md      the agent's memory; append with `llm-bus -c alice remember "..."`
state.json    CLI-managed read cursors per project/group
```

```sh
llm-bus init alice --role "backend dev" --context "You own the API. Bob does QA."
llm-bus agents
llm-bus -c alice whoami          # everything the agent needs to know about itself
```

### Project file  `.llm_bus_project` (lives in the repo)

```toml
project = "demo"
group = "dev"      # optional default group → [GROUP] can be omitted in commands
```

```sh
llm-bus project init demo --group dev      # creates project+group in DB, writes ./.llm_bus_project
```

## Spawning an agent

Point a fresh LLM session at its folder and project file, e.g.:

> You are agent `alice`. Run `llm-bus -c alice -p ./.llm_bus_project whoami` to load your context,
> then `llm-bus guide` for how to communicate.

`whoami` prints identity, role, CONTEXT.md, NOTES.md, memberships and unread counts.
Bare `llm-bus` / `llm-bus guide` prints a full agent-oriented reference (JSON shapes, exit codes, loop).

## Commands

```sh
A="-c alice -p .llm_bus_project"

llm-bus project init demo --group dev | project create NAME | project list
llm-bus -p demo group create review | group list
llm-bus $A join                 # join default group (implies project); `join review` for another
llm-bus -p demo members [GROUP]

llm-bus $A send "hey bob"                 # default group
llm-bus $A send review "please look"      # explicit group
echo "multi-line" | llm-bus $A send --stdin

llm-bus -p demo read [GROUP] -n 20 [--after ID]     # history, no identity needed
llm-bus $A read --unread                            # only what alice hasn't seen; advances her cursor
llm-bus $A wait [-t 300]                            # block until others post; exit 2 on timeout
llm-bus -p demo search [GROUP] "ticket"
llm-bus -c alice remember "bob owns the DB layer"
```

Add `--json` before the subcommand for machine-readable output. Exit codes: 0 ok, 1 error, 2 wait timeout.

## Dev

```sh
uv run pytest
uv run ruff check . && uv run ruff format .
```
