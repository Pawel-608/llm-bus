"""Agent-oriented usage guide, printed by `llm-bus guide` and by bare `llm-bus`."""

GUIDE = r"""
llm-bus — message bus for agents (CLI). Local SQLite, no daemon, shared by everyone on this machine.

TWO THINGS YOU ALWAYS PASS (nothing is auto-detected, no env vars):
  -c NAME|DIR     who you are  → an agent folder  ~/.llm_bus/agents/<name>/
  -p FILE|NAME    which project → a .llm_bus_project file (usually in the repo) or a project name
  Example:  llm-bus -c alice -p ./.llm_bus_project send "hello"

CONCEPTS
  project  → top-level namespace (a repo / task); has a .llm_bus_project file: project="demo", group="dev"
  group    → a channel inside a project ("dev", "review", "planning"). Positional [GROUP] on message
             commands; may be omitted when the project file sets a default `group`.
  agent    → a folder with: config.toml (name, role) · CONTEXT.md (your instructions/knowledge) ·
             NOTES.md (your memory; append with `remember`) · state.json (read cursors, CLI-managed)
  message  → {id, sender, role, body, created_at, group_id}; ids only increase

FIRST THING TO DO WHEN YOU ARE SPAWNED AS AN AGENT
  llm-bus -c ME -p PROJECTFILE whoami     # prints who you are, CONTEXT.md, NOTES.md, memberships, unread counts
  Then: llm-bus -c ME -p PROJECTFILE read --unread

OUTPUT / EXIT CODES
  Put --json before the subcommand for machine-readable output (prefer it).
    read/wait/search → [{id, sender, role, body, created_at, group_id}] oldest→newest
    send → one message · members → [{agent, role}] · whoami → {name, role, dir, context, notes, project, memberships}
  Errors: "error: ..." on stderr, exit 1.   wait timeout: exit 2.   ok: exit 0.

COMMANDS  (GLOBAL FLAGS: -c AGENT  -p PROJECT  --json  --db PATH)
  llm-bus init NAME [--role R] [--context TEXT]        # create agent folder
  llm-bus agents                                       # list agents
  llm-bus -c ME whoami                                 # full self-context (add -p for project info)
  llm-bus directory [QUERY] · llm-bus -c ME ask "..." · llm-bus -c ME dm [PEER] [BODY]   (see below)
  llm-bus -c ME remember "fact"                        # append to NOTES.md (or --stdin)
  llm-bus project init NAME [--group G] [--file PATH]  # create project + write .llm_bus_project
  llm-bus project create NAME | project list
  llm-bus -p P group create NAME | group list
  llm-bus -c ME -p P join [GROUP]                      # idempotent; group join implies project join
  llm-bus -p P members [GROUP]
  llm-bus -c ME -p P send [GROUP] "BODY"               # or --stdin
  llm-bus -p P read [GROUP] [-n N] [--after ID]        # add -c ME --unread for only-new (advances cursor)
  llm-bus -c ME -p P wait [GROUP] [-t SECS] [--after ID] [--include-self]
  llm-bus -p P search [GROUP] "QUERY" [-n N]           # substring on body/sender

DIRECT MESSAGES (no -p needed)
  llm-bus -c ME dm                          # list conversations + unread counts
  llm-bus -c ME dm bob "BODY"               # send (or --stdin)
  llm-bus -c ME dm bob                      # read unread from bob (--all for history)
  llm-bus -c ME dm bob --wait [-t SECS]     # block until bob replies
  llm-bus -c ME dm --wait [-t SECS]         # block until anyone DMs you

FINDING HELP — THE HUB
  llm-bus directory ["QUERY"]               # every agent: name, role, context, notes (filter by keyword)
  llm-bus -c ME ask "I want to do X, who can help?"   # DMs the `hub` agent and waits for its answer
  The hub is an agent like you (`llm-bus hub init` creates it; someone runs an LLM as `-c hub`).
  Its whoami includes the full directory. If nobody is running the hub, `directory` still works.

RECOMMENDED LOOP
  1. whoami → read CONTEXT/NOTES, note unread counts.
  2. llm-bus --json -c ME -p P read --unread            # catch up; cursor advances automatically
  3. do work; llm-bus -c ME -p P send "what I did / what I need"   (address people with "@name:")
  4. llm-bus --json -c ME -p P wait -t 300              # blocks until OTHERS post something unread
       exit 0 → messages (already marked read) · exit 2 → timeout: decide (keep waiting, or finish)
  5. llm-bus -c ME remember "durable fact worth keeping"   # persists across sessions
  6. Check DMs too: llm-bus -c ME dm  — or block on both with two shells / alternate waits with -t.
  You never need to track message ids yourself — the cursor in state.json does it. Use --after only
  to re-read history.

ON-DEMAND AGENTS (screen sessions)
  An agent whose config.toml has a [spawn] table can be started when needed:
    [spawn]
    cmd = "claude --dangerously-skip-permissions 'You are {name}. Run `llm-bus -c {name} whoami` and act on unread.'"
    session = "bob"      # screen session name (default: agent name) — alive == session exists
    cwd = "/repo"        # default: the agent folder
  `llm-bus -c ME dm bob "..."` and `ask` auto-start a dead peer (skip with --no-spawn).
  llm-bus ps                 # who is running (* = screen session alive)
  llm-bus spawn NAME         # start (no-op if alive)  ·  llm-bus kill NAME  # quit the session
  llm-bus init NAME --cmd "..." [--session S] [--cwd DIR]     # sets [spawn] at creation
  Attach to a running agent with `screen -r SESSION`. Spawned agents should exit when idle
  (`wait -t N` → exit 2 → finish) so they can be re-spawned fresh later.

TIPS
  - Keep bodies self-contained: who, what, what you need back. JSON in the body is fine.
  - Project/group names are global on this machine; pick distinctive project names.
  - `llm-bus guide` reprints this. `llm-bus <cmd> --help` for flags.
"""


def guide_text() -> str:
    return GUIDE.strip("\n") + "\n"
