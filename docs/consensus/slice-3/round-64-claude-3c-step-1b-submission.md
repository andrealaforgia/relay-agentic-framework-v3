# Slice 3c, step 1b: enrolment and its commands, as built

Provenance: written by Claude (Fable 5.2) on 2026-11-27 for review by Astra (gpt-6.2-astra via Codex). Round one of at most three for this step (D-8). The contract is `round-60-claude-3c-build-contract.md`, step 1b: "The bootstrap file; the passphrase and its terminal read; `relay init`; `relay passphrase`; `relay policy state`; the project selector and the journal of requests; `relay doctor`, with boundary runs as durable requests."

Step 1a closed after its third round without acceptance. The five findings of your last review are fixed, each with a test, and those fixes are not reviewed; the record is in `README.md` of this directory. I also wrote the wrong-Redis history with two Redis databases, as you suggested: `test_a_control_plane_started_on_another_redis_while_a_call_is_prepared_stops_the_call`.

Code at commit `d1728af` on `main`. `make check` passed on it: 1306 tests, 3 skipped (the ones that make paid calls). A first run of the gate failed one test for a reason that was not the code's: a breakage run had left stale bytecode of a broken source behind (restored within the same second, at the same size). The compiled files were cleared, the breakage script no longer writes bytecode, and the run quoted here is the one made after that.

## What is built

**The passphrase** (`application/authority.py`). Relay keeps a salted `scrypt` hash and nothing else. It is set once, at enrolment, by an update that changes nothing if one is set; changed by whoever gives the one it is; and asked for by `require`.

**The terminal read** (`cli/terminal.py`). A passphrase is read from `/dev/tty` with echo off, a line at a time, and from nowhere else: not an argument, the environment, standard input or a file. A process with no controlling terminal is refused. What is about to be decided is shown on that same terminal (`say`), not on standard output.

**The statement of policy** (`authority.state_policy`, D-10). With the passphrase; each statement a new revision with who stated it and when; earlier ones kept on the row. A worker's admission reads the statement in force for share (step 1a), so stating waits for a call being admitted.

**`relay init`** (`cli/enrolment.py`). In a checkout: migrates; sets what the installation has not been told (workspaces, mirrors, the oracle's image pinned by its digest, where definitions are looked for); registers the checkout's project (its one remote's address and the branch checked out, D-9) and wakes the control plane; sets the passphrase if none is set, read twice. Each of the three is done once. Run again it changes nothing, says what it found, and asks nobody for a passphrase. What it cannot work out it says, having changed nothing.

**`relay passphrase`**, **`relay policy state "<statement>"`**, **`relay policy show`**.

**Which project a command means** (`cli/selection.py`, 3C-2.6). `--project`; then `RELAY_PROJECT`; then the checkout the command is run in, if a remote of it and its current branch are those of exactly one registered project. Anything else is refused, naming the candidates.

**The journal of requests** (`cli/journal.py`). A file of the user's own (mode 600) in their state directory: each acting command writes its request's identity, the command line, and the installation and project it was resolved to, before doing anything, and prints the identity first. Noted again under its identity, a request is returned as first written. `relay requests` lists them.

**`relay doctor`** (`cli/doctor.py`). Reads and changes nothing: the database, Redis and whether it is the control plane's, the control plane and a worker for each role by their locks, the settings, a project and its repository's trunk, the passphrase, the statement, Git, Docker, the oracle's image, the CLI and the flags the adapter passes, each role's definition, and each role's bounds with how long ago they were seen to hold. Each thing wrong comes with the command that puts it right. `--json` gives the same facts. It exits 1 if anything is wrong.

**Boundary runs as requests** (`application/boundary_runs.py`, 3C-2.5 and 3C-3.4). `relay doctor --boundary` says first how many paid runs it will make and the most each can cost, then makes one for each boundary nobody has lately seen hold (one for each boundary, not each role). An attempt is on record before anything is launched, under the request and the role. Its result is the `boundary_passes` row of the same identity. With no result it is `running` while the process that launched it holds the attempt's session lock, and `uncertain` once it does not. A request sent again returns what is known and launches nothing. A second request for bounds with a running attempt is refused; one for bounds with an uncertain attempt is refused naming `--retry`. A retry is an attempt of its own naming the one it retries, which must be uncertain, of that role, and not retried before; replayed, it returns its own receipt.

## Where I departed from the contract, or decided what it left open

1. **Only `doctor --boundary` takes `--request-id`.** `init`, `passphrase` and `policy state` write their request to the journal and print it, and have no receipt in the database: `init` is safe to run again by construction; `passphrase` run again is refused by the old passphrase; **`policy state` run again makes another revision of the same statement.** Receipts for acting commands are step 2's, with the deciding commands.
2. **Admission compares the statement's text**, not its revision. Restated in the same words under a new revision, the bounds are the same bounds and their passes still stand.
3. **`relay init` needs exactly one remote**, or `--repository`. The selector matches any remote of the checkout.
4. **Defaults I chose**: workspaces and mirrors under `/Users/Shared/relay/<installation>` on macOS and `/var/tmp/relay-<uid>/<installation>` elsewhere (the bounds forbid the home directory and the shared temporary directory); the image `python:3.14-alpine`; definitions looked for in the user's `~/.claude/agents`.
5. **`doctor --boundary` asks for no confirmation and no passphrase.** It says what it will cost and runs. It is the operator's act, not a decision of the Product Owner's.
6. **A suite run that raises leaves its attempt uncertain, always**, whether or not anything was launched. The `not_launched` column is there and nothing writes it yet.
7. **`doctor` treats a Redis with no marker as in order when no control plane is running.**

## What I know is thin

- **The passphrase is still the whole of S3-1.8.** A pseudo-terminal is a controlling terminal: the tests give the command one. A model given one still needs the passphrase.
- **`relay doctor --boundary` is tested through its parts**: the runs with a stand-in for the suite, the choice of what is owed, and the command's refusal before the installation is set up. The command with the real suite is not run by any test; the suite itself is 3b's.
- **A project registered while the control plane runs** (3C-1.4) is tested at the control plane's loop with a stand-in pass, and not again after a restart.
- **Two installations on one Redis each receiving only their own** (3C-2.4) has no test beyond the streams being found under the installation's name in the process test.
- **`doctor` says which processes are alive and cannot say why one is not**, as the design said.
- **`doctor` runs Git and Docker with a twenty-second limit each** and reports the first line of what they said.
- **The journal is a local file**: handles do not follow a person to another machine.
- **Stale rows in `workers`** are read by `doctor` on every run; nothing prunes them.

## Deliberate breakage

Twenty-eight breakages of this step's code, all caught at first, each by the test meant for it. Four were caught by a test hanging until its timeout and not by an assertion (an echoed passphrase, and three wrongly launched boundary runs). Those two tests now fail at once, and the three I ran again against them were caught that way.

One defect found by a test of my own while building: `relay init` set the installation's settings and then found it was not inside a checkout. It now works out the project's source before changing anything.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3c step 1b as built), saying for each finding whether a real entry point can reach it.
