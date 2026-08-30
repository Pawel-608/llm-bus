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
config.toml   name = "alice"  role = "backend dev"   [+ optional [spawn] table, see below]
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

### On demand (screen sessions)

Or let the bus start agents when they're needed. An agent declares how to run itself; liveness is
simply "does a screen session with that name exist", and whoever DMs a dead agent starts it:

```toml
# ~/.llm_bus/agents/bob/config.toml
name = "bob"
role = "qa"

[spawn]
cmd = "claude --dangerously-skip-permissions 'You are {name}. Run `llm-bus -c {name} whoami` and act on unread.'"
session = "bob"        # screen session name (default: agent name)
cwd = "/path/to/repo"  # default: the agent folder
```

```sh
llm-bus init bob --role qa --cmd "claude ... {name} ..." --cwd ~/repo   # same thing at creation
llm-bus ps                               # who is running (* = session alive)
llm-bus kill bob                         # quit the session (starting = just message it)
screen -r bob                            # watch or take over
llm-bus -c alice dm bob "hi"             # auto-starts bob if dead (--no-spawn to skip)
llm-bus -c alice ask "who does X?"       # auto-starts the hub the same way
```

Any number of agents run concurrently, one screen session each. Spawned agents should exit when
idle (`wait -t N` → exit 2 → wrap up) so they come back fresh next time. For several copies of one
role, create separate agents (`bob-1`, `bob-2`): cursors and notes are per agent folder.

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

llm-bus -c alice dm bob "can you review #12?" | dm bob | dm --wait -t 60 | dm    # DMs, no -p needed
llm-bus hub init | directory ["query"] | -c alice ask "who can help with X?"   # the hub agent
llm-bus ps | kill NAME | reap                                                   # on-demand agents
```

Add `--json` before the subcommand for machine-readable output. Exit codes: 0 ok, 1 error, 2 wait timeout.

## Push delivery (Claude Code Stop hook)

```sh
llm-bus -c alice install-hook        # → .claude/settings.local.json: Stop hook `llm-bus -c alice hook`
```

At the end of every Claude Code turn the hook checks alice's groups and DMs. If anything is unread it
prints `{"decision":"block","reason":"<the messages>"}`, so Claude keeps going and answers instead of
stopping; otherwise it's silent. With `[spawn]` this makes the whole system event-driven: dead agents are
started by whoever @mentions/DMs them, live agents are interrupted at end of turn.

## Threads, mentions, waiting on everything

```sh
llm-bus $A send "@bob: can you check the schema?"   # @mention starts bob if he has a spawn cmd
llm-bus -c bob -p demo send --reply 12 "looks fine"  # threaded reply
llm-bus read --thread 12                             # the whole exchange
llm-bus -c alice wait --all -t 300                   # block on every group + DM at once (no -p)
```

## Presence & reaping

`llm-bus ps` shows each agent's last bus command and how long ago (an alive session that hasn't
touched the bus is probably stuck). Give spawnable agents `idle_timeout = N` (minutes) in `[spawn]`
(or `init --idle-timeout N`) and run `llm-bus reap` (e.g. from cron) to kill idle sessions;
`--dry-run` to preview, `--idle MIN` to override.

## Flows: multi-agent loops with bus-enforced routing

Describe a loop once in a TOML file; the bus materializes it into ordinary agents and routes
between them deterministically. Each agent can run on a different CLI/model (`claude`, `codex`,
`kimi`, or your own template) and in its own git worktree.

```sh
llm-bus flow example > flow.toml     # full annotated example (the ML loop below)
llm-bus flow up flow.toml            # project/group, agents `<flow>.<node>`, worktrees — idempotent
llm-bus flow run flow.toml "improve AUC on dataset X"   # task → entry agent, loop self-propagates
llm-bus flow status flow.toml | stop | resume | down [--worktrees]
```

```toml
name = "ml_loop"; project = "ml"; repo = "~/projects/ml"; entry = "implementer"; max_turns = 200

[agents.implementer]
runner = "claude"; model = "claude-opus-5"; worktree = true
role = "implement the idea; answer review comments: fix, or argue why it is already good"
next = ["reviewer"]                      # default route when the process exits

[agents.reviewer]
runner = "codex"; model = "gpt-5"; worktree = "implementer"    # shares the worktree
next = ["implementer"]                   # comments → implementer fixes or pushes back; none → approve
[agents.reviewer.on]
approve = ["ideas"]                      # `llm-bus -c ml_loop.reviewer flow signal approve`
dispute = ["resolver"]                   # implementer disagrees, reviewer still objects

[agents.resolver]
runner = "claude"; model = "claude-sonnet-5"; next = []        # arbiter, only on disputes
[agents.resolver.on]
ok = ["ideas"]; fix = ["implementer"]

[agents.ideas]
runner = "kimi"; next = ["implementer"]
[agents.ideas.on]
dry = ["scout"]                          # out of ideas → web research → back to ideas

[agents.scout]
runner = "claude"; next = ["ideas"]

[supervisor]
runner = "claude"; model = "claude-sonnet-5"; every = 5
```

**How routing works.** Every node's spawn command is `<runner>; llm-bus -c <flow>.<node> flow done --rc $?`.
When the LLM process exits, the bus (not the LLM) picks the next agents: `next` by default, or
`on.<signal>` if the agent ran `llm-bus -c NAME flow signal <signal>` before exiting. Targets are
started as screen sessions; a handoff line (`[flow:ml_loop] turn 12/200: reviewer → implementer, resolver`)
is posted to the flow's group so the next agent knows why it was woken. Agents never start each
other. Unknown signals pause; `max_turns` stops the flow.

**Supervisor.** An optional agent outside the graph, woken every `every` handoffs, on
`flow signal blocked`, on non-zero runner exit codes and at `max_turns`. It reads the traffic and
may rewrite the graph — `flow up` is idempotent so edits apply immediately:

```sh
llm-bus flow show flow.toml
llm-bus flow add-agent flow.toml tester --runner codex --role "run the test-suite" --next reviewer --on fail=implementer --worktree implementer
llm-bus flow rm-agent flow.toml scout
llm-bus flow route flow.toml implementer tester [--on SIGNAL]  |  unroute ...
llm-bus flow set flow.toml max_turns 400        # also: entry, every
llm-bus flow run flow.toml "please retry with a smaller LR" --to implementer
```

**Runners.** Built-in templates for `claude`, `codex`, `kimi`; override or add under `[runners.NAME]`
with `cmd = "... {model} ... {prompt}"` (`{prompt}` is already shell-quoted) and an optional default
`model`. **Worktrees:** `worktree = true` → `<repo>/.llm_bus_worktrees/<flow>-<node>` on branch
`llm-bus/<flow>/<node>`; `worktree = "other"` shares another node's; otherwise cwd is `repo`.
Agent folders (`NOTES.md` included) survive `flow up`; only `config.toml`/`CONTEXT.md` are regenerated.

## Dev

```sh
uv run pytest
uv run ruff check . && uv run ruff format .
```
