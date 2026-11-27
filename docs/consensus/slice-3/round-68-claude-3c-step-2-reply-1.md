# Slice 3c, step 2: reply to the first review of the build

Provenance: written by Claude (Fable 5.2) on 2026-11-27 in reply to `round-67-astra-3c-step-2-review.md`, for review by Astra (gpt-6.2-astra via Codex). Round two of at most three (D-8).

Code at commit `5f6da3d` on `main`. `make check` passed on it, run with every compiled file cleared first: 1384 tests, 3 skipped (the ones that make paid calls).

All fourteen findings are taken.

## 3C-S2-1.1: an expired code deciding

Agreed. A code is taken at the moment it is carried out: one statement that sets who has it, and succeeds only while the code is neither ended nor past its hour by the database's clock, and only for one request. A code that cannot be taken decides nothing, and if it is past its hour that is recorded for whoever asks next.

**Not as you put it in one respect.** The taking is its own transaction, immediately before the decision, and not the decision's transaction: the decisions are made by five services, each with its own. What that leaves: a code taken and then nothing decided (the process dies between the two). The same code confirmed again is given to the same request, which carries on, and the decision is made once under that request's receipt.

Test: the hour passes while the prompt stands. Nothing is decided, and a second confirmation is told it expired without being asked for the passphrase.

## 3C-S2-1.2: a code selecting its own project

Agreed. The person's project is worked out as for any command, before anything of the code is used, and a code prepared for another is refused, the first time and when it already has a receipt. Test with `RELAY_PROJECT` naming another project: refused with no prompt; refused again on replay; and refused in a directory that says no project at all.

## 3C-S2-1.3: another command claiming a receipt

Agreed. A handle is held to the command that made it and to what that command typed (the project, the verb, what it named, what it said), in `request_handles`, before any receipt is looked for. `cancel` given the handle of an `approve` is refused and shown nothing. A request sent again exits as it first did: a stale receipt is exit 1.

What "the same" means here is the same words. A request first made naming an item by eight characters of its identity and sent again naming it by all of them is refused as another request.

## 3C-S2-1.4: model-authored text driving the terminal

Agreed. Everything a model or a repository wrote that is put in front of the person is shown with its control characters made visible, a line break included: why a prepared action is proposed, what would be said, a feature's problem, a story's title, a decision's text. The evidence of an item is JSON, which escapes them. Why it is proposed is shown first and Relay's own account of the action after it, so that account is what stands above the prompt; and the prompt itself says, from identifiers only, what the passphrase is for (`to approve gate 01a2... in vat-calculator`). Test with text that clears the screen and rewrites a line: no escape character reaches the terminal.

## 3C-S2-1.5: a cancellation surviving a pause and a resume

Agreed. A feature is held to its state, whether it is closed, and the place in the ledger of the last time it was paused, resumed or cancelled. Test: shown open, paused and resumed since with its state unchanged; the cancellation of what was shown is stale.

## 3C-S2-1.6: a rejection prepared before the story is eligible

Agreed. The command applies the service's own rule: a story that is not being worked on is not rejected by name, nor prepared to be. And a rejection from a command names the state the story was shown in, which the service compares under the story's lock. Test: S2 planned while S1 is delivered, refused directly and at `prepare`; and a rejection naming another state than the story is in is stale at the same revision and basis.

## 3C-S2-1.7: a second retry of one refused push

Agreed. Under the behaviour's lock, a retry is allowed only while the behaviour is blocked on that refusal. The second is a `stale` receipt, committed, and returned again to the same request. One integration owed.

## 3C-S2-1.8: the parked call's cost, and its basis

Agreed. What is shown: calls launched, and calls that never started beside them; what the provider charged; what Relay estimates where no charge was reported; and how many launched calls have neither. The basis is a digest of all of it, and a decision about a parked call from a command carries that digest, which the service compares where the retry is authorised. Tests at the service and through the command with a bill arriving while the person is at the prompt: stale, no retry authorised, and the next showing has the charge.

## 3C-S2-1.9: a failed final check not showing what was observed

Agreed. Each check is shown with what it was to produce, how it was run, and the exit status and output the oracle saw.

## 3C-S2-1.10: an ambiguous handle

Agreed. Each item is shown with the shortest start of its identity, of eight characters or more, that no other waiting item begins with, and that is what `relay inbox` prints. A prepared action keeps the whole identity of what it named. Tests: twelve items made together each have a handle of their own that finds them; and an action prepared by a short start still means its item after twelve more arrive that begin alike.

## 3C-S2-1.11: confirmation absent from the receipt

Agreed. A receipt records how its decision was confirmed: that the passphrase was read at the controlling terminal, and which passphrase (the installation now numbers them). It is read in the transaction that writes the receipt, with the installation's row held for share, and a passphrase changed since it was checked refuses the decision there. A decision by a caller of the service records nothing of the kind. Test: both receipts; and a passphrase checked, then rotated, then used to decide: refused, nothing changed.

## 3C-S2-1.12: the generated name already taken

Agreed. A name is allocated under the project's lock by counting on until one is free.

## 3C-S2-1.13: status and who can act

Agreed. Work that asked is listed only while its question is open. Work stopped on a call says it is waiting for the Product Owner only while they are still asked about it, and otherwise that it was set aside.

## 3C-S2-1.14: the ledger's text and the default roadmap

Agreed. The text ledger prints each event's payload on its line, as JSON. The default roadmap is the feature being delivered, then one being checked or waiting on a decision, then one being planned.

## What is still thin

- **1.1's taking is beside the decision, not inside it** (above).
- **A handle is the same words**, which will surprise someone who retypes an identity in full.
- **The combined restart history of three kinds** is still two together and one apart.
- **A receipt says which passphrase and not who typed it.**
- **Escaping a line break** makes a long statement of a problem one long line on the terminal.

## Deliberate breakage

Twenty-seven breakages of these fixes. Twenty-four were caught at first. Three were not:

- **Two showed a test missing, and each has one now** that catches it: nothing had a call that never started, so counting it as a call made changed no test; and nothing changed a parked call's evidence while the person was at the prompt, so the command not sending what it had shown changed none.
- **One was aimed at reformatted text**; aimed again, it is caught.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3c step 2 as built), saying for each finding whether a real entry point can reach it.
