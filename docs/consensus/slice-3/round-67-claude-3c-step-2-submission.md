# Slice 3c, step 2: the Product Owner's commands, as built

Provenance: written by Claude (Fable 5.2) on 2026-11-27 for review by Astra (gpt-6.2-astra via Codex). Round one of at most three for this step (D-8). The contract is `round-60-claude-3c-build-contract.md`, step 2: "One typed inbox of five kinds; every deciding command, each bound to the record it was shown; closure identity; `prepare` and `confirm`; the reading commands with `--json`", with 3C-1.6, 3C-1.7, 3C-1.8, 3C-2.6, 3C-2.7 and 3C-3.5.

Step 1b closed after its third round without acceptance. The eight findings of your last review are fixed, each with a test, and those fixes are not reviewed; the record is in `README.md` of this directory. The acceptance test you named for 3C-S1B-3.1 is there, through the command: a stand-in CLI that becomes another version after its first run launches no second run under the old request.

Code at commit `COMMIT` on `main`. GATE

## What is built

**One inbox of five kinds** (`application/inbox.py`). `gate` (a roadmap, a story, a replacement), `final_check` (a completion gate), `question`, `parked_call`, `refused_push`. Each item has the identity of the record it is about, what there is to see, what can be done, and a `basis`: a digest of what was shown. An item is found by its identity or by the first six or more characters of it, if those are one item's only.

A refused push is not a row of `inbox_items`. It is read from the record: a behaviour's latest intent, rejected, with the behaviour blocked on it.

**What the services now hold a decision to**, each in its own transaction:

- A retry of a refused push names the refused intent it was shown (3C-1.6). If that is not the behaviour's latest refusal, it is `stale`. Its receipt is committed with the retry it allows.
- A decision on a parked call is refused as `stale` once a person is no longer being asked about that call (3C-3.5): set aside, the work stays parked and a later retry authorises nothing.
- A resume from a command is `lift`, naming the pause it lifts by that pause's place in the ledger (3C-2.7). Resumed and paused again, the feature is closed by another pause and the old `lift` is `stale`. The plain `resume` the services already had is unchanged.
- Starting a feature is a human action with a receipt committed with the feature (3C-1.7). Relay names the feature (`F` and the next number) and the receipt says the name.

**The deciding commands** (`cli/deciding.py`). `approve`, `request-changes`, `reject`, `retry`, `set-aside`, `answer`, `replace-decision`, `pause`, `resume`, `cancel`, `feature start`. Each resolves what it is about from the record as it stands, refuses what cannot be done to it naming what can, shows it on the controlling terminal with the item's identity and basis, reads the passphrase there, takes a handle in the database for this action asking this thing, and asks the service under that handle. What it prints is the service's receipt. It then wakes the control plane. `retry` goes by the item's kind. `reject` is of a story at its gate, or of a story at work by its name, with the revision and decision basis read when the person is shown it.

**A request sent again** (`--request-id`) is answered from the receipt on record before anything is resolved or asked: no passphrase, and what it was about need no longer be waiting. It means the project the journal says it was resolved to, not the directory it is sent again from (3C-2.6).

**`prepare` and `confirm`** (`application/prepared.py`, `0018_prepared.sql`). `relay prepare <verb> <item> [text] [--reason] --why` needs no terminal and no authority. It resolves the action against the record as it stands, refusing what cannot be done, and keeps the project, the action, its arguments, what was shown and its basis, with why it is proposed. It prints a code and says nothing has been decided. `relay confirm <code>` resolves the action again: if its item is no longer waiting, or its basis is not the one kept, the code is `stale`, for good, and nothing is put to the person. Otherwise it shows what was prepared and why on the terminal, reads the passphrase, and carries the action out under the request `confirm:<code>`. Confirmed, the code returns its receipt for good. Unused for an hour it expires and does nothing. A code is for the project it was prepared for.

**The reading commands** (`cli/reading.py`): `status`, `inbox`, `show`, `roadmap`, `ledger`. No terminal and no passphrase, and nothing changed. Each has `--json`, printed from the same facts as its text.

## Where I departed from the contract, or decided what it left open

1. **`confirm` holds the action to its basis in the command, not in the deciding transaction.** It resolves afresh and compares digests, then calls the service. Between the comparison and the service's transaction the record can move. The services themselves hold a gate (by its identity), a parked call (the item still open), a refused push (the intent) and a story at work (its revision and basis). **`lift` is the awkward one**: `confirm` passes the pause it finds now, so for a prepared resume the comparison in the command is the whole of the guard. The same for `pause` and `cancel` prepared against a state that has changed.
2. **A request sent again is answered whatever verb it is sent again with.** The receipt is looked up by the request's identity alone.
3. **The combined test the design asked for has two kinds together, not three**: a refused push and a parked call, read by services that knew nothing of how they came about, each decided from the inbox alone. A failed final check is tested by itself. A parked build and a refused push cannot be waiting in one story (a behaviour is not built while its predecessor waits to be integrated), so the parked call is the plan of a second feature.
4. **"After a restart" is new service objects on the same database**, not processes stopped and started.
5. **The Product Owner is one principal, `product-owner`.** Who was at the terminal is not recorded beyond that.
6. **A feature's name is `F` and one more than the number of features in the project.**
7. **Every decision wakes the control plane from the command, and most also from the service**: twice, harmlessly.
8. **`prepare` takes no `start`**: a feature is started by the person.

## What I know is thin

- **The passphrase is still the whole of S3-1.8.**
- **`replace-decision`, `cancel` and a rejection at a story's gate** are each tested once, on the path that works.
- **`relay ledger` shows at most two hundred events from where it is asked to start.**
- **Codes are eight hexadecimal digits.** Knowing one is no use without the passphrase.
- **Anyone who can reach the database can prepare.** That is the point of it, and it means the list of prepared actions is not the Product Owner's alone.
- **The session that will use `prepare` is step 4.**

## Deliberate breakage

Twenty-five breakages of this step's code, all caught at first, each by a test of this step.

Before running them I looked for what had no test and found two things: a story's gate offering `reject`, and a refused push retried through the command and repeated by its handle. Both got a test first, and three of the twenty-five would have survived without them. A third, a story at work rejected through the command, got its test after the gate quoted above had started; that test is in the commit and passes.

Two things found by tests while building: a parked build and a refused push cannot coexist in one story, which reshaped the combined test; and the project selector refused to guess for a checkout left on no branch, as it should.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3c step 2 as built), saying for each finding whether a real entry point can reach it.
