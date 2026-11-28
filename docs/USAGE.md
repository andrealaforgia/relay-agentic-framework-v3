# Using Relay

Relay delivers software for you with AI agents, and keeps control of the work itself. You state a problem. An Analyst plans it as user stories, a Verifier turns each behaviour into an acceptance test, a Builder makes the test pass, QA and a Sentinel review the result, and Relay runs every test and integrates every change itself. No agent's word that something works is ever taken: Relay launches the code in a container and looks.

You are the Product Owner. Relay asks you to decide a small number of things, and nothing moves past them without you.

This guide takes you from nothing to a first feature on your trunk. What is shown as printed by Relay was copied from real runs and shortened, except the board under [Watch](#8-watch), which is put together to show every part of it at once.

## What Relay costs

Every piece of work an agent does is a paid call to the model provider, made through the Claude CLI under the account that CLI is signed in to. A feature of one story and one behaviour took between eight and eleven calls on the cheapest model in our runs, for about three cents. A larger model costs more, and a feature with more stories makes more calls.

Each call has a cap on what it may spend: two US dollars unless the installation's settings say otherwise. Relay shows what has been spent at any time (see [What it has cost](#what-it-has-cost)).

Two things in this guide make paid calls before any feature is started: `relay doctor --boundary`, at most 25 cents for each run it makes, and `relay session`.

## What you need

- **Python 3.14 and `uv`.**
- **Docker**, running. Relay runs your project's tests in containers, and the PostgreSQL and Redis it needs can come from Docker too.
- **Git**, and a repository with a remote Relay can push to. Relay works on its own copies and pushes to your trunk.
- **The Claude CLI**, installed and signed in: `claude --version` should answer.
- **The agent definitions.** Each role runs on an agent definition file, looked for in `~/.claude/agents` unless you say otherwise:

  | Role | Definition |
  |---|---|
  | Analyst | `alf-user-story-writer.md` |
  | Verifier and Builder | `alf-atdd-developer.md` |
  | QA | `alf-test-design-reviewer.md` |
  | Sentinel | `alf-security-assessor.md` |

## Install

From a checkout of this repository:

```sh
uv tool install .
relay --help
```

Or, without installing, run every command in this guide as `uv run relay ...` from the checkout.

Relay needs PostgreSQL and Redis. The checkout has a Compose file that starts both, bound to this machine only:

```sh
docker compose up -d --wait
```

Tell Relay where they are. Either set two environment variables in every shell you use Relay from:

```sh
export RELAY_DATABASE_URL=postgresql://relay:relay-dev-only@127.0.0.1:55432/relay
export RELAY_REDIS_URL=redis://127.0.0.1:56379/0
```

or write them once in `~/.config/relay/relay.toml`:

```toml
[relay]
database = "postgresql://relay:relay-dev-only@127.0.0.1:55432/relay"
redis = "redis://127.0.0.1:56379/0"
```

The password above is the Compose file's and is for a database that listens on this machine only. Use your own for anything else. Nothing secret of the model provider's is kept by Relay: the credentials are the Claude CLI's.

## Your first project

### 1. Set it up

In your project's checkout, on its trunk branch:

```sh
cd ~/code/vat-calculator
relay init
```

Relay asks you to set a passphrase, twice. This is the Product Owner's passphrase: every decision you make is confirmed with it. Then it prints what it set up:

```
workspaces: /Users/Shared/relay/01544b3b-.../workspaces
mirrors: /Users/Shared/relay/01544b3b-.../mirrors
image: python@sha256:5862d971...
definitions: /Users/you/.claude/agents
the passphrase: set
project vat-calculator: registered, branch main of git@example.com:you/vat-calculator.git
```

The image is the container your project's tests run in. The default is `python:3.14-alpine`; choose another with `relay init --image`. `relay init --help` lists the rest.

### 2. See what is missing

```sh
relay doctor
```

`doctor` checks everything Relay needs here and says what to do about each thing that is not right. It only reads. Straight after `init` it will say that no control plane and no workers are running, and:

```
NOT  the operator's statement of remote policy: not made
       workers launch nothing without it: `relay policy state "<what applies to this host>"`
```

### 3. State the remote policy

Relay confines each agent: a Builder can write only in its own working copy, a reviewer can write nowhere. Part of what an agent may do is set by policy your organisation may push to this machine from elsewhere, which Relay cannot see. So you state it, once, as the operator:

```sh
relay policy state "This host has no managed remote policy."
```

Say what is true of your machine. Relay records the statement with who made it and when, and launches no call until one is made.

### 4. Show the bounds hold

```sh
relay doctor --boundary
```

This runs each role's confinement against the real CLI on this machine and records whether it held: an agent is asked to do things it must not be able to do, and Relay checks that it could not. **It makes paid calls**, at most 25 cents each, and says so before it starts. A worker is allowed a call only while its role has a pass on record for the CLI version installed. After the CLI is upgraded, run it again.

### 5. Start the processes

Relay is one control plane and a worker for each role. Start each in a terminal of its own, or under whatever keeps processes running for you:

```sh
relay up
relay worker start --role analyst
relay worker start --role verifier
relay worker start --role builder
relay worker start --role qa
relay worker start --role sentinel
```

Any of them can be stopped and started again at any time. Work that was in hand is picked up from the record. Only one control plane runs at a time: a second `relay up` refuses to start while the first holds the installation. You can run more than one worker of a role.

`relay doctor` should now have nothing to put right.

### 6. Start a feature

```sh
relay feature start "vat.py prints the amount it is given. It must print the gross amount at 20% VAT."
```

Relay shows you the problem as it will be recorded, asks for your passphrase, and answers:

```
request 01a2228f-ca69-72de-bc08-f7424a860581
effective: started F1
```

State the problem as an outcome someone needs, not as a solution. The Analyst plans from these words.

### 7. Decide what you are asked

Work now happens by itself until Relay needs you. Everything waiting for you is in one list:

```sh
relay inbox
```

Each item has a short handle. Look at one in full, exactly as you will be asked to decide it:

```sh
relay show 01a2228e
```

For a small feature you are asked twice:

- **The roadmap**: the stories the Analyst proposes. `relay approve <handle>`, or send it back with `relay request-changes <handle> --reason "..."`.
- **Each story, once it is built**: Relay shows what was demonstrated, with the tests it observed passing. `relay approve <handle>`, or `relay reject <handle> --reason "..."`.

Every deciding command shows what you are deciding on your terminal and asks for your passphrase. When the last story is approved, Relay runs a final check of the whole feature on the trunk and completes it.

A role may also ask you a question when your problem leaves something open:

```sh
relay answer <handle> "Exports are zero-rated."
```

Your answer becomes a decision in force for that feature. Replace one later with `relay replace-decision`.

### 8. Watch

```sh
relay watch
```

The board is redrawn every two seconds and never changes anything. It looks like this:

```
relay watch: vat-calculator
control plane: running
workers:       analyst 1  verifier 1  builder 1  qa 1  sentinel 1
  builder   01a21fb1  lock held
  ...

F1  delivering  vat.py prints the amount it is given...
  S1  released           Standard rate
    B1  awaiting_builder   100 net is shown as 120 gross

in flight:
  builder   build of B1  on claude-sonnet-5-5  for 42s  by worker 01a21fb1
owed:
  build of B1, to the builder: taken up, owed for 1m 03s; granted to worker 01a21fb1 (that worker's lock is held) for 42s

waiting for the Product Owner: nothing
```

`relay watch --events` follows Relay's record of everything that happened, one line an event. `relay watch --tokens` follows what has been spent. Add `--once` to print a view once, and `--json` for the same facts as data.

`relay status` is a shorter answer to "where are we?", and `relay roadmap` shows a feature's stories.

## What it has cost

```sh
relay costs            # the project
relay costs F1         # one feature
relay costs F1 --summary   # and what was recorded when F1 completed
```

Relay keeps four things apart, because adding them together would say something untrue:

- **Calls**, counted once each, by role. Calls still running and calls whose end is not known are counted beside them and add nothing to the spend.
- **Tokens**, by role and by model. If a call did not report a counter, the total says `unknown`. It is never shown as zero.
- **What the provider charged**, as the provider reported it. If no call reported a charge it says `unknown`; if only some did, it says the figure is for those.
- **What Relay estimates**, from a dated table of list prices. Every such figure is labelled an estimate with the table that made it. It is not your bill.

## Talking to Relay

```sh
relay session
```

This starts a conversation with an Interpreter that can read everything Relay has on record for the project: what is waiting for you, where each feature stands, what was decided, what things cost. Type a line and it answers. End with Ctrl-D or `/quit`. **Each line you type is a paid call.**

The Interpreter decides nothing, and cannot. When it thinks you should approve or answer something it prepares the action and gives you a code:

```sh
relay confirm 3f9a1c07
```

You run that yourself. Relay shows what was prepared and why, and asks for your passphrase.

It can also help you shape a problem before any feature exists. It keeps its analysis in Relay, in versions, and you can start a feature from one:

```sh
relay feature start --analysis 2
```

What matters of a conversation outlasts it. Its analysis, the questions it meant to ask you, your own words in answer and what it prepared are in Relay's record, and the next session starts from them. The rest of what was said is kept too, but a new session is not told an earlier conversation's every word.

### Your passphrase

The passphrase is what makes a decision yours. It is read from your terminal and from nowhere else: never from an argument, a file or standard input.

**Never tell it to the Interpreter, or to any AI agent, and do not type it where one can read your terminal.** An agent that knows your passphrase can decide for you, and Relay cannot tell the difference. Change it with `relay passphrase`.

## When something stops

Start with:

```sh
relay doctor
relay status
relay inbox
```

Between them they say what is not running, what work is owed and to whom, and what is waiting for you.

| What you see | What it means | What to do |
|---|---|---|
| `waiting for the control plane (relay up)` | Work only the control plane does: running a test, integrating, a final check | Start `relay up` |
| `waiting for a worker` | No worker of that role is running, or its role has no pass on record | Start the worker; run `relay doctor` |
| A worker starts and launches nothing | No policy stated, or no boundary pass for the installed CLI | `relay doctor` says which |
| A **parked call** in the inbox | A call ended and Relay cannot tell how, or several calls gave nothing usable. Relay will not pay for another without you | `relay retry <handle>` to allow one more, or `relay set-aside <handle>` to leave the work stopped |
| A **refused push** in the inbox | Your Git remote refused Relay's push to the trunk | Put the remote right, then `relay retry <handle> --reason "..."` |
| A **failed final check** in the inbox | The feature's tests did not all pass on the trunk at the end | `relay show <handle>` has what was observed; `relay retry <handle>` runs the check again |
| The board says a worker's lock is not held | That worker process has gone. Work it held is picked up again by the control plane | Start a worker of that role |
| You want a feature to wait | | `relay pause F1 --reason "..."`, later `relay resume F1` |
| You want a feature gone | It cannot be undone | `relay cancel F1 --reason "..."` |

Every command that changes something prints `request <id>`. If a command was interrupted and you do not know whether it took effect, send it again with `--request-id <id>`: you get the same answer, and nothing is done twice. `relay requests` lists the requests made from this machine.

`relay ledger` is Relay's whole record, in order. There is no other log: what is not in the ledger did not happen as far as Relay is concerned.

## What Relay does not do yet

- **It has only been run end to end on a very small feature**, on the cheapest model: one story, one behaviour, a few lines of Python. Expect rough edges on anything larger.
- **One provider.** Every role runs on the Claude CLI.
- **One feature is delivered at a time** in a project. Others wait their turn.
- **Python projects only have been tried.** The container image and the test commands are whatever the agents and you agree, but nothing else has been exercised.
- **Models and spending caps cannot be changed from the command line.** Each role uses `claude-sonnet-5-5` with a cap of two US dollars a call unless the installation's settings say otherwise, and there is no command yet to change those settings. `relay session --model` does choose the session's model.
- **No web interface and no API.** The command line is all there is.
- **No way to import an existing backlog or codebase analysis.**
- **Not reviewed to completion.** The design and each step were reviewed by a second model in up to three rounds. No step was accepted outright, and the fixes made after each last round have not been reviewed. `docs/consensus/` has the whole exchange and every reservation.
