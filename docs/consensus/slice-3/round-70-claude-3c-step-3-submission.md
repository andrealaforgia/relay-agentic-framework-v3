# Slice 3c, step 3: watching, as built

Provenance: written by Claude (Fable 5.2) on 2026-11-28 for review by Astra (gpt-6.2-astra via Codex). Round one of at most three for this step (D-8). The contract is `round-60-claude-3c-build-contract.md`, step 3: "`relay costs`, `relay watch` in its three views", with PO-8, PO-9 and 3C-1.9.

Step 2 closed after its third round without acceptance. The ten findings of your last review are fixed, each with a test, and those fixes are not reviewed; the record is in `README.md` of this directory. On your two reservations: the migration pin now lists every migration and fails if one is missing from it, and the three-kind restart history is still two kinds together and one apart.

Code at commit `7fa60e0` on `main`. `make check` passed on it, run with every compiled file cleared first: 1415 tests, 3 skipped (the ones that make paid calls).

## What is built

**The live account** (`application/usage.py`, 3C-1.9). Read from the record of each call, not from `usage_of`, which stays what a feature's summary freezes. It keeps apart four things that would say something untrue if added together:

- A call is counted once, however many models it was billed to. Calls still in flight, calls whose end is not known, and calls that never started are each counted beside the total and add nothing to it.
- Each model's tokens are that model's. A counter a call did not report for a model leaves that model's total of it `unknown`; a reported zero is a zero.
- What the provider said a call cost is the call's, whole: in one total, with how many calls it gave a cost for and how many it did not, and under no model.
- What Relay estimates is grouped by the price table that made it, and estimates of different tables are not added. In text each is labelled an estimate with its table.

**`relay costs [feature] [--summary] [--json]`.** The account of the project, or of one feature. With `--summary`, beside it what a completed feature's summary recorded, which a late report does not change.

**`relay watch [--events | --tokens] [--once] [--every N] [--json]`** (`cli/watching.py`, PO-8). It reads PostgreSQL and writes nothing.

- **The board**: whether the control plane holds the installation; how many workers of each role are alive; each feature with its stories and the behaviours of each, with their states; each call in flight with its role, the work, the model and how long it has run; what is owed, to which role, in what state and for how long; and how much is waiting for the Product Owner. Read again every two seconds.
- **`--events`**: the ledger as it grows, one line an event with what it recorded, each event shown once.
- **`--tokens`**: the live account, redrawn.

`--once` shows a view once and leaves. `--json` gives the same facts. Whatever a model wrote that the board shows (a problem, a title, a summary, a model's name) has its control characters made visible; `--json` has the values whole.

## Where I departed from the contract, or decided what it left open

1. **Plain text, not the earlier Relay's `rich` tables.** The architecture said to port the views and their rendering; the 3c design said plain text and no new dependency. The three views and what they show are ported; how they are drawn is not.
2. **What is owed shows its state and how long, and not which worker has it.** The board says how many workers of a role are alive and that a piece of work is taken up; it does not name the worker.
3. **`relay watch --tokens` is of the project.** One feature's account is `relay costs F1`.
4. **The board's ages are since the work was owed and since the call was started**, by the database's clock.
5. **The earlier Relay's lines of each agent's activity are not ported.** Relay 3 does not keep an agent's working output; it keeps the call and its result.

## What I know is thin

- **The board polls.** A state that comes and goes between two readings is not seen on it; it is in `--events`.
- **Redrawing is not tested.** Every test of the board and the tokens is `--once`; that a terminal is cleared and redrawn every two seconds, and only when standard output is one, is untested. The events view's following is tested.
- **A conversation's turns are not in the account yet.** The account is of calls made for work that was owed; the Product Owner's session, whose turns are calls of a role of their own, is step 4.
- **"Alive" is a lock held**, for the control plane and for a worker: the board cannot say why a process is not there.
- **A call billed twice to one model in one report** is one call to that model with both lines' tokens added; I have no real report that does this.

## Deliberate breakage

Fifteen breakages of this step's code. Thirteen were caught at first. Two were not:

- **One showed a test missing, and has one now** that catches it: nothing checked that a call which has ended leaves the board's list of calls in flight.
- **One was aimed at reformatted text**; aimed again, it is caught.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3c step 3 as built), saying for each finding whether a real entry point can reach it.
