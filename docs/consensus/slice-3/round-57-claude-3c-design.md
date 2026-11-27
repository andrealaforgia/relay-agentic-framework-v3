# Slice 3c: the processes, the command line and the Product Owner's part (design)

Provenance: written by Claude (Fable 5.2) on 2026-11-27 for review by Astra (gpt-6.2-astra via Codex). Round one of at most three for this design (D-8). It is a design: no code of 3c exists.

Slice 3b is closed (`README.md` of this directory): four steps, none accepted, 118 findings fixed and one open. What 3b proves is that one process holding every service can deliver a small feature against the real provider. What it does not have is everything a person needs to use it: processes, a command line, a way for the Product Owner to act that a model cannot, and a way to watch.

The requirements this slice answers: specification sections 22 and 25; PO-3 (the Product Owner talks to Relay through a live Claude session); PO-8 (`relay watch`); PO-9 (the estimated token cost stays visible); S3-1.8 (a human confirmation the model cannot produce); the request for very clear instructions; and what 3a and 3b left to it by name: the periodic pass, one inbox over gates, questions and parked calls, the pass across processes, and every public route waking it.

## 1. Processes

Three kinds, each started from the command line, each of which may be stopped and started at any time.

**The control plane** (`relay up`). One per database. It migrates, runs `Features.start`, and then loops: the outbox publisher; the pass for each project; and nothing else. It is the only process that runs the oracle, the integration and the final checks, as now. It holds a session advisory lock for as long as it lives, so a second `relay up` on the same database refuses to start.

**A worker** (`relay worker start --role <role>`). One role each, any number of each. It is `Worker.serve_once` in a loop over every project, with the real adapter, the recorded boundary passes, and `runtime_now` with the operator's stated policy. It never runs a pass.

**A command** (everything else). It opens the database, does one thing, and exits.

**The pass across processes.** Today a worker runs the pass in its own process after an apply (`then`). Across processes it cannot: it has no oracle and must not have one. `then` becomes "wake the control plane": `NOTIFY relay_wake, '<project>'` in PostgreSQL, sent after the apply's transaction commits. The control plane `LISTEN`s and runs that project's pass. Every public route that today runs a pass or is given one (`Features.decide`, `Decisions.answer`, the reconfirmations, `Features.retry_integration`, `Supersessions.withdraw`, a worker's apply) sends the same notification when it is not the control plane itself.

A notification is a hint and is allowed to be lost: the control plane also runs every project's pass on a timer (every 15 seconds by default). That timer is the periodic pass 3a-ii left to this slice. Correctness never depends on the notification; latency does.

One pass per project at a time: the loop is a single task per project, and a wake that arrives during a pass sets a flag that makes one more pass follow it.

## 2. Configuration

One file, `relay.toml`, found by walking up from the working directory, or named by `RELAY_CONFIG`. It says: the database and Redis addresses; where workspaces and mirrors are kept (outside the home directory and the shared temporary directory, as the bounds require); where agent definitions are looked for before the project's and the user's own; for each role, its model, effort, timeout and the most one call may cost; the container image the oracle uses; and **the operator's statement of remote policy** (D-10), with who stated it and when. With no statement, `relay worker start` starts and launches nothing, saying why.

Nothing secret is in the file. Credentials for the provider are the CLI's own.

## 3. The Product Owner's authority (S3-1.8)

The problem: PO-3 puts a Claude session between the person and Relay, and that session runs commands. A command that approves, run by the session, is a model exercising the Product Owner's authority.

**The Product Owner has a passphrase**, set once by `relay init` and changed by `relay passphrase`, read from the controlling terminal with echo off. Relay stores a salted `scrypt` hash of it and nothing else.

**Every command that exercises the Product Owner's authority reads the passphrase from the controlling terminal** (`/dev/tty`), every time, and does nothing without it: `approve`, `request-changes`, `reject`, `answer`, `replace-decision`, `retry`, `set-aside`, `retry-integration`, `pause`, `resume`, `cancel`, `feature start`, and `policy state`. It is never taken from an argument, an environment variable, standard input or a file. A command run with no controlling terminal is refused. The receipt of the action records that it was confirmed this way.

**The confirmation is bound to the exact thing decided.** Before it asks for the passphrase the command prints what is being decided as the record has it now: for a gate, its kind, subject and manifest digest; for a question, its text. The action it then submits names that digest (as gate decisions already must name their basis), so what was confirmed is what is decided, or the action is stale.

**What the live session can do**: read (`status`, `inbox`, `roadmap`, `ledger`, `costs`, `watch`), and **prepare**. `relay prepare <action> ...` records the action the session proposes, with its reason, and prints a short code. The person runs `relay confirm <code>` in a terminal of their own: it shows what was prepared, as recorded, and asks for the passphrase. A prepared action is nothing until confirmed and expires in an hour.

What this rests on, said plainly: the model does not know the passphrase. A person who tells the session their passphrase, or runs the session in a terminal multiplexer it can type into and types the passphrase where it can read it, has given their authority away, and nothing in Relay can tell. The session is launched without the tools that would let it read a terminal it does not own (section 6), and the instructions say this in so many words.

## 4. Commands

Every command that reads takes `--json`, and its JSON is the same facts as its text. Every command that acts returns a receipt, and the same request sent again returns the same receipt (the request id is made by the command and can be given with `--request-id` to repeat it).

| Command | What it does |
|---|---|
| `relay init` | In a Git repository: writes `relay.toml` if there is none, migrates, registers the project (its repository address and trunk, D-9), and sets the passphrase if none is set |
| `relay up` | The control plane |
| `relay worker start --role R` | A worker |
| `relay doctor` | What would stop Relay working here, each with what to do: the database, Redis, Docker and the oracle's image, Git and the repository, the CLI and the flags the adapter needs, each role's definition, the stated policy, each role's boundary pass and how old it is. `relay doctor --boundary` runs the boundary suite for the bounds that have no fresh pass, says first what it will cost at most, and records the result |
| `relay policy state "<statement>"` | The operator's statement of remote policy (D-10), with the passphrase |
| `relay feature start "<problem>"` | Opens a feature |
| `relay status` | Each feature and where it stands; each story and behaviour of the one being delivered; what is owed and to whom; what is waiting for the Product Owner |
| `relay roadmap [feature]` | The roadmap in force and any proposal waiting |
| `relay inbox` | **Everything waiting for the Product Owner, in one list**: gates (roadmap, story, supersession, completion), questions, and parked calls, each with an id and exactly what there is to decide |
| `relay show <id>` | One inbox item in full: a gate's manifest as it will be confirmed |
| `relay approve <gate>`, `relay request-changes <gate> --reason`, `relay reject <story> --reason` | A gate's decisions |
| `relay answer <question> "<text>"`, `relay replace-decision <decision> "<text>"` | Decisions |
| `relay retry <call>`, `relay set-aside <call>`, `relay retry-integration <behaviour> --reason` | What a person is asked about calls and pushes |
| `relay pause`, `relay resume`, `relay cancel` | A feature |
| `relay prepare ...`, `relay confirm <code>` | Section 3 |
| `relay ledger [--subject S] [--since N]` | The events, in order |
| `relay costs [feature]` | Section 5 |
| `relay watch` | Section 5 |
| `relay session` | Section 6 |

`relay logs` of the specification is `relay ledger`: Relay's record is the ledger, and a second log would be a second source of truth. Process output goes to standard error of each process.

The command line is `argparse` and plain text: no new dependency.

## 5. Watching (PO-8, PO-9)

`relay watch` redraws a board from PostgreSQL every two seconds until interrupted. It reads and never writes.

- **The board** (default): each process and whether it is alive (the control plane's lock; each worker's, by role); the feature being delivered; each story with its state; each behaviour of the current story with its state; what is owed, to which role, whether granted and to whom, and for how long; each call in flight with its role, model and how long it has run; what is waiting for the Product Owner.
- **`--events`**: the ledger as it grows, one line an event.
- **`--tokens`**: by role and by model, for the feature or the project: calls, tokens in and out, cache read and written, the provider's cost where it reported one, Relay's estimate and the price table's version, and the number of calls whose use is not known. `unknown` is shown as `unknown`.

`relay costs` is the `--tokens` view once, with `--json`. Both read `usage_of` (3b step 4): the live account, not a completed feature's frozen summary, which `relay costs --summary` shows beside it.

Estimates are labelled estimates everywhere they appear, with the price table's version.

## 6. The live session (PO-3)

`relay session` starts an interactive Claude Code session for the Product Owner. Relay gives it: a system prompt that says what Relay is, how to read it, and that it may prepare and never decide; and a tool allowance of exactly the reading commands and `relay prepare`. It is not given `relay confirm`, the deciding commands, or a shell beyond those.

It is a convenience over the commands, and nothing depends on it: everything it can do, the person can do with `relay inbox`, `relay show` and the deciding commands. Workflow recovery never needs its transcript (specification section 24).

## 7. Packaging and instructions

`relay` is an installed console script (`uv tool install`, or `uv run relay` in the checkout). `docker compose` brings up PostgreSQL and Redis as now.

`docs/USAGE.md` is written for a person who has never seen Relay: what it is in a paragraph; what must be installed; the first project, command by command, with what each prints; what the Product Owner is asked and how to answer; how to watch; what it costs and how to see it; what to do when something stops (each state `relay doctor` and `relay status` can report, and the command that moves it on); and what Relay does not do. The README points to it.

## 8. Steps

1. **Processes and composition.** Configuration; the composition root; `relay up` with the timer, the wake and the lock; `relay worker start`; `relay init`; `relay doctor`; the wake sent by every route. Proved by a feature delivered by separate operating-system processes, the provider being the stand-in CLI, with the control plane killed and started again part-way.
2. **The Product Owner's commands.** The passphrase and the controlling terminal; one inbox; every deciding command with its receipt; `prepare` and `confirm`; the reading commands with `--json`.
3. **Watching.** `relay costs`, `relay watch` in its three views.
4. **The session, packaging and instructions.** `relay session`; the console script; `docs/USAGE.md`; and the smoke of 3b step 4 driven through the command line: `relay up`, workers, `relay feature start`, approvals by a person at a terminal.

Each step is built test-first and reviewed in at most three rounds.

## What I know is thin

- **The passphrase is the whole of S3-1.8.** I have not found a confirmation a model with a shell on the person's machine provably cannot produce, short of a second device. This one rests on a secret the model is not told.
- **A controlling terminal is not proof of a person.** A model given a pseudo-terminal can open `/dev/tty`. It still needs the passphrase.
- **`LISTEN`/`NOTIFY` needs a connection held open** by the control plane, outside the pool.
- **The 15 second timer is a guess.** It bounds how long work waits when a wake is lost.
- **One control plane per database** is enforced by a lock, and the lock is all that enforces it.
- **`relay watch` polls.** At two seconds a state that comes and goes between polls is not seen on the board; it is in `--events`.
- **Step 1's proof uses the stand-in CLI**, which shows the processes and not the provider; the provider through the command line is step 4.
- **Nothing here is a web interface or an API** (specification sections 27's `api/` and `dashboard/`). Those are not in slice 3.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for the slice 3c design), saying for each finding which step it belongs to.
