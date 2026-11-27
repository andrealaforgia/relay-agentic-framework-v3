# Slice 3c, step 1a: processes and recovery, as built

Provenance: written by Claude (Fable 5.2) on 2026-11-27 for review by Astra (gpt-6.2-astra via Codex). Round one of at most three for this step (D-8). The contract is `round-60-claude-3c-build-contract.md`, step 1a, with the findings of your three design reviews that it folds in.

Code at commit `f02815f` on `main`. `make check` passed one commit earlier (`7ad8768`): 1243 tests, 3 skipped (the ones that make paid calls). The last commit changes one line of source and adds two tests; `make check` passed on it too: 1245 tests, 3 skipped.

## What is built

**The installation** (`0016_installation.sql`, `leadership.py`). One row: its identity, the control plane's term, its settings, a place for the passphrase and the policy statement (both 1b's to fill).

**One control plane, and a term on what it does.** `Leadership.take` takes a session advisory lock keyed by the installation and opens a new term. The term is held in the task's context. `lock_gate`, which every workflow transaction calls first, reads the installation's term for share and refuses a control plane whose term is not the current one (`NotLeader`). A worker or a command has no term and is not refused.

**Effects say whose they are** (`Effect.by`). A build's `publish` and `green` are the control plane's. A worker that reaches one stops and reports `handed_on`, having kept the reply and the commit, and wakes the control plane. The control plane's own session takes any effect. Each effect records the term it was started under, and one started under an earlier term is free to take whatever its session's lock looks like.

**A result the control plane refuses is handed back there.** When the one refusing is not the grant's holder, the grant is released and the command is announced again in the same transaction; at the limit the work is parked, as before.

**Each push has an identity** (`integration_sends`). A row is written, fenced, before the send leaves. What Git said (`refused`, `never_left`) is recorded against it by whoever hears, in a statement that takes no workflow lock and is not fenced. Acting on it is fenced, and each answer is acted on once (`counted`). A pass that finds an answer nobody acted on acts on it before deciding whether to send.

**Project-scoped scans.** A pass for one project reconciles and publishes nothing of another's.

**Names under the installation.** Attempt refs are `refs/relay/<installation>/attempts/...`, for publication, the green run, what a Builder is given to start from, and the recovery of a lost commit. Streams are prefixed by the installation. Candidate refs and invocation refs already end in an identifier of their own and are unchanged; pins are inside each installation's mirrors.

**The processes** (`runtime.py`, `processes.py`, `cli/main.py`). `relay up`: migrate, assemble, take the installation, start-up cleanup, then one task per project running its pass when woken (`NOTIFY`) and on a timer (15 seconds by default), with the outbox published after each pass. `relay worker start --role R`: `serve_once` over every project, with the real adapter and `runtime_now`. Every process but the control plane is assembled with a pass that is only a wake (`Elsewhere`).

**The marker.** The control plane writes its term in Redis under the installation's name. A worker compares it with the term in PostgreSQL before launching anything, again every thirty seconds, and continuously while it has found trouble: no term at all is "waiting"; no marker, or an earlier term's, is another Redis, and it launches nothing and says why.

**Bootstrap.** A process is told the database and Redis by environment or by a file named by `RELAY_CONFIG` or in the user's configuration directory, never one in the working directory. Everything else is the installation's settings.

## The proof the contract asked for

`tests/integration/test_processes.py`: six operating-system processes (`relay up` and a worker for each role) with nothing in common but PostgreSQL, Redis and the filesystem. The provider is a stand-in CLI that acts each role from a script and reports the start inventory the adapter checks. After the roadmap is approved, the connection the first control plane's lock is on is terminated from outside, the process left alive, and a second `relay up` is started. The feature completes and the candidate is on trunk. The first control plane stops by itself saying its term is over, exit 0. One Builder call, one green verified, both of the build's effects carried out under a term, and streams found in Redis under the installation's name.

The control planes run with a timer of ten minutes, so each step is taken up because something woke the control plane. A separate test shows the timer alone runs the pass.

## Where I departed from the contract, or decided what it left open

1. **The fence is in `lock_gate`**, not at each call site. A control plane's transaction that changes workflow state without taking the gate is not fenced. I have not audited every transaction for that; I rely on the gate being "the first lock of every transaction here", which earlier slices established for the services and which I did not re-prove.
2. **Recording what a send heard takes no lock at all**, deliberately (3C-3.1).
3. **A control plane learns it has lost its lock** by asking its lock connection once per turn of its main loop, or sooner by being refused. Between the two it may run a pass that changes nothing.
4. **The worker's check is not tied to a reconnect of its transport** (3C-3.2 said it should be). It is at start, every thirty seconds, and on every turn while trouble stands.
5. **3C-3.2's test is at the level of the check** (no term, no marker, the marker, an earlier term's marker). I did not restart a control plane against another Redis with a worker left running.
6. **3C-3.3's test has one installation that runs and one that is only a ref**: another installation's attempt of the same name is put in the repository, this one publishes beside it, both stand. Two databases were not run against one repository.
7. **A person's decisions in the process test are made by calling the service and sending the wake**, as a command will. The commands are 1b and 2.
8. **The policy statement and each role's boundary pass are put on record by the test.** `relay policy state` and `relay doctor --boundary` are 1b.
9. **A project opened while the control plane runs** is met at the next turn of the main loop, or at a wake that names it.

## What I know is thin

- **The process test replaces the control plane once, at one moment** (after the roadmap's approval). It does not replace it during a publish, a green, a push or a final check. Those are covered in one process: a green being observed across a change of term, and a push in flight across one.
- **A worker stops only between deliveries.** Told to stop during a call, it finishes the call first.
- **The marker expires** after six timer periods. A control plane that is alive and stalled that long leaves workers saying "waiting".
- **The advisory lock's key is half of the installation's identifier.** Two installations in one database collide with probability about one in four thousand million.
- **`Elsewhere` is asserted where a process is assembled**, and that its pass is a wake. No test has a worker's reconfirmation reach it through a decision.
- **Start-up cleanup is fenced and is tested as refused**; that a successor's start-up cleanup does the right thing with what the old one left mid-flight is the process test and nothing finer.
- **The stand-in CLI replies by matching words of the briefing.** A briefing reworded would need its script reworded.

## Deliberate breakage

Thirty-nine breakages of this step's code. Twenty-eight were caught at first. Eleven were not:

- **Eight had no test that needed them, and have one now**, each caught by it. Whose the build's effects are (two): my helper ran the control plane straight after the worker, so nothing told them apart. The integration scan scoped to a project. The wake (two): with a two-second timer the test passed without one. A process assembled with a running pass. Streams under the installation. And an answer to a send counted twice: my test of it had no second send outstanding, where alone it matters.
- **One led to the code being rewritten.** After counting a late answer the pass returned what it had counted; it now reads the intent again and goes on from what it is.
- **Two survive, with a reason each.** The control plane waiting until it is listening before its first pass closes a window I cannot hit on purpose. And reading the intent again after counting can be removed without effect, because the send looks at the intent under its lock before anything leaves; I kept the reading.

One defect I found by reading against the contract and not by a test: recovery of a lost commit searched attempts under the old name. With no installation named, as in every test until then, nothing failed. It is fixed, with a test that names the installation.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3c step 1a as built), saying for each finding whether a real entry point can reach it.
