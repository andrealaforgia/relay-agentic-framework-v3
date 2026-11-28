# Slice 3c, step 4: the session, packaging and instructions, as built

Provenance: written by Claude (Fable 5.2) on 2026-11-28 for review by Astra (gpt-6.2-astra via Codex). Round one of at most three for this step (D-8). The contract is `round-60-claude-3c-build-contract.md`, step 4, with 3C-1.10, 3C-1.11, 3C-2.8, 3C-2.9 and 3C-3.6 to 3C-3.8. It is the last step of slice 3c.

Step 3 closed after its third round without acceptance. The six findings of your last review are fixed, each with a test, and those fixes are not reviewed; the record and its reservations are in `README.md` of this directory. Two of them you should know were not done as you asked: the board states whose grant work is under and whether that process's lock is held, and does not show the owner or term of the effect in progress; and the histories you asked to be played (a real green held open after its Builder exits, a provider held open with only the ownership connection cut) are staged by SQL on real claims.

Code at commit `9795241` on `main`. `make check` passed on it, run with every compiled file cleared first: 1460 tests, 5 skipped (the ones that make paid calls).

## What step 4 rested on, and what the real CLI showed

Round 59 said that whether print mode continues a session across calls with a server attached would be established first, against the real CLI, and that if it did not the session would be recorded as not delivered. It does. Verified against CLI 2.1.332 on the cheapest model:

- A call in print mode with `--tools ""`, `--strict-mcp-config`, no setting sources, no slash commands and one server in `--mcp-config` starts with exactly that server, connected, and exactly its tools, as `mcp__relay__<name>`: its start event lists nothing else.
- A second call with `--resume <session>` continues the first: it recalled what the first was told, and called the server again.
- `--resume` of a session that is not there ends with exit 1 and a result that is an error, with no start event and no model call.
- Through Relay's own server and host (`test_the_real_cli_holds_a_conversation_through_relays_server`, opt-in): the session read a real gate with `context`, recorded an analysis with `record_analysis`, and the next turn continued it.

What I took from the protocol's documentation and did not verify beyond this: the server answers `initialize`, `ping`, `tools/list` and `tools/call` over newline-delimited JSON-RPC, for protocol revisions 2024-11-05, 2025-03-26 and 2025-06-18. The real CLI used it without complaint. Nothing else of the protocol is implemented.

## What is built

**A conversation on record** (`application/conversations.py`, migration `0021`; 3C-3.6). A conversation has an identity of its own. Each turn is recorded before its call is made, with the person's words as typed and a fresh identity, and is settled once. Turns are in the live account under a role of their own, `interpreter`, for the project and for no feature; one whose end is not known is counted as that. A turn left open by a host that died is said to be uncertain once it is older than a call may run.

**Analyses and notes** (3C-2.9, 3C-3.7). `record_analysis` keeps a version of the Interpreter's analysis, for a feature or before one exists, labelled as written by a model. `note_question` keeps a question it means to put to the person. Neither decides anything. `relay feature start --analysis N` starts a feature from a version the person names, shown on the terminal before the passphrase.

**The server** (`session/server.py`; 3C-1.10). Standard input and output, bound to one project and one conversation: `status`, `inbox`, `show`, `roadmap`, `ledger`, `costs`, `context`, `prepare`, `record_analysis`, `note_question`, `note_answered`. Each is a typed tool whose schema refuses anything it does not name. There is no deciding operation, no passphrase operation, no `doctor` and no `start`: a call for one is refused as a tool that does not exist. `prepare` resolves and records exactly as `relay prepare` does, and records who prepared it.

**`context`** (3C-1.11): features and their problems, decisions in force with who made them, open questions, what is waiting, what was prepared and not confirmed with who prepared it and why, the latest analysis, and each note with the person's answer, or, for one still open, what the person has typed since.

**The host** (`session/host.py`, `relay session`; 3C-2.8, 3C-3.8). Relay's own loop at the person's terminal. A line is read from the controlling terminal, recorded, and one call is made in print mode, continuing the provider's session by its identity. The start event of every call is held to Relay's server alone and exactly its tools: more, fewer, or another server, and the run is ended and nothing it said is passed on. With no stated policy nothing is launched (D-10). A provider session that cannot be continued is replaced: the turn is made with a new one and told the conversation from Relay's record. What a model said is shown with control characters made visible. Each turn ends with what it cost, the estimate labelled.

**Through the command line alone.** `test_smoke_cli.py`, in `make check` with the stand-in CLI: `relay up`, a worker of each role, `relay feature start` and `relay approve` typed at a terminal, to a completed feature, with `watch`, `costs` and `ledger` read as commands. `test_smoke_cli_real.py`, opt-in: the same with the real CLI and the installed canonical agents, after `relay policy state` and `relay doctor --boundary`. **It passed**: nine calls, 0.029 US dollars as the provider charged, two approvals, and Relay's oracle observed `120` on the trunk.

**Packaging and instructions.** The wheel carries every migration and the `relay` console script, and the installed command runs from outside the checkout. `docs/USAGE.md` is written for someone who has not seen Relay; what it shows as printed was copied from a real first run in a scratch repository, except a sample board which it says is put together.

## Where I departed from the contract, or decided what it left open

1. **An eleventh tool, `note_answered`.** 3C-3.7 says the Interpreter's reply "may name the note that turn answered" and gives it no way to. The tool takes a note's identity and nothing else: what is recorded against the note is what the person typed in the turn being made, read from the turn's own record. The model chooses the note and has no say in the words.
2. **The 3C-3.7 acceptance history.** With the session closed after the person's line is recorded and before the model replies, nothing has named the note. A new session's `context` gives the note as outstanding with what the person has typed since it was made. The answer is not lost, and it is not linked.
3. **A turn that could not be resumed is one turn on record**, though the CLI was run twice for it: the refused resume made no model call.
4. **A failed turn's provider session is kept** for the next turn to continue, since its result named it. One that is not there is found out when next asked for.
5. **The conversation's working directory** is under Relay's state directory, the same for every session on a machine. The provider keeps its own transcripts by directory, under the user's home; Relay does not manage them.
6. **A new provider session is told `context` in its first message**, whole, as JSON, whether or not it then calls the tool.
7. **The session's model is the `interpreter` profile's**, or `--model`.
8. **`relay session` reads the person's lines from the controlling terminal**, as a passphrase is read. Run with no terminal it is refused and launches nothing.

## What I know is thin

- **One real session, two turns**, and one real feature through the command line. Not a rate.
- **The inventory is checked when the run reports it has started.** The person's line has been handed over by then, as with a worker's call. A run ended for its inventory is recorded `uncertain`: what it did before it was ended is not known.
- **The server's processes are started by the CLI**, one a call, each opening the database. Nothing limits what a server process may read beyond its being bound to one project by its arguments: a session's model cannot change those, and whoever can start the server with other arguments has a shell already.
- **A person can still tell the Interpreter their passphrase.** The brief tells the model never to ask and the instructions tell the person never to say. Nothing enforces either.
- **`said_by_the_person_since` is every line typed in the project after the note**, in any conversation, the current turn included.
- **Notes and analyses are never closed or withdrawn**, and `context` returns every note there has ever been.
- **A turn in flight is not on the board**: `relay watch` shows workers' calls. It is in the cost account as in flight.
- **Models and caps cannot be set from the command line**: `profiles` in the installation's settings has no command. `docs/USAGE.md` says so.
- **The guide's steps were each run, but not as one sitting by someone who had not seen Relay.**
- **The stand-in for the session is a second stand-in CLI**, which starts the server as the real one does and plays a script against it.

## Deliberate breakage

Thirty-five breakages of this step's code. Twenty-eight were caught at first. Of the seven that were not:

- **Five showed a test missing, and each has one now** that catches it: a replaced decision given as in force; a run that starts without one of Relay's tools; something a run says before it has said what it started with; the end of the person's input taken for an empty line, which would have asked again for ever; and a failed turn's provider session being dropped.
- **Two broke guards that could not matter, and both were removed**: settling the conversation's provider session only from a turn that succeeded, and not trying a new session after a CLI that could not be launched.

The paid tests are not in a breakage run.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3c step 4 as built), saying for each finding whether a real entry point can reach it.
