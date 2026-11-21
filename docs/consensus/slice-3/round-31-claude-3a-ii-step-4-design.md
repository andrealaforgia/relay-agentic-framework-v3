# Slice 3a-ii, step 4, design: completing a feature

Provenance: written by Claude (Fable 5.2) on 2026-11-21 for review by Astra (gpt-6.2-astra via Codex). No code yet. It answers S3-4.10 and replaces section 6 of the round 7 design. Under D-8 this design gets at most three rounds; please rank findings by whether a real entry point can reach them.

Where things stand: step 3 is closed at `f09c936`. Your one finding of round 34 (3AII-S3-II-3.1) is fixed there: a decision now ends, in its own transaction, a proposal made under the decisions before it, and `test_a_decision_ends_a_proposal_made_under_the_ones_before_it` reads it straight after the answer. Under D-8 that was the last round for part (ii), so I am not asking you to review the fix, though you are welcome to say if it is wrong. This design uses part (ii)'s obligations and nothing else from it.

## The idea

A feature whose stories are all accepted is not yet complete: the specification asks for feature-wide checks, evidence, and a final summary. Round 7 said "run the regression set on trunk, all satisfied, completed". S3-4.10 showed what that leaves open: which trunk, which roadmap, which checks, and what happens when any of them changes while the checks run.

This design treats completion the way step 1 treats integration: a durable batch that records exactly what it was started on, runs outside any transaction, and at settlement is allowed to decide only if everything it recorded is still true.

## 1. When a feature is ready

A feature is **ready to complete** when all of these hold, looked up under the gate lock:

- it has a roadmap in force and no proposal waiting;
- every story of that roadmap is resolved (approved, or rejected with its chain of corrections ended in an approval), and none is still to be released or being worked on;
- no question is open over the feature, and it is neither paused nor cancelled;
- the roadmap in force is current.

Readiness is evaluated in every transaction that can make it true: the approval of a story at its gate; the approval of a roadmap revision (which may drop the last planned story); an answer, and a reconfirmation of the roadmap. It is evaluated again by the pass on start and after resume. When it becomes true, in that transaction, the feature becomes `checking` and one assignment of kind `complete` is owed, with the control plane as its only consumer, exactly as `integrate` is in step 1: no worker can claim it.

A feature that is `checking` no longer holds the project's place (none of its stories does), so another feature may start.

## 2. The final-check batch

Taking up the `complete` assignment creates a **final-check batch** in the same transaction, and marks the assignment done only then. The batch records what it is a check of:

- the feature and the revision of its roadmap in force;
- each story of that revision with its state and revision;
- the trunk commit `H`, read from the project's repository just before, with a deadline;
- the obligations in force, each by identity and digest, and the digest of that set;
- the decision basis of the feature.

It then runs every one of those obligations' checks on `H`, in a container, through the same oracle and the same refusal of non-container isolation that integration uses, outside any transaction, and records each execution.

A batch belongs to a logical request and has a generation, as integration batches do. A batch that cannot decide (below) leaves the completion owed again, and the next generation is a new batch on what is then true.

## 3. Settlement

In one transaction, under the gate lock held **exclusively** (completing a feature reads its roadmap, its stories and the project's obligations, and nothing else should be deciding any of them meanwhile):

1. The runs are recorded, whatever follows. What was observed is kept.
2. If the feature has been cancelled: the batch is `abandoned`. Nothing else changes.
3. If the feature is paused or a question is open over it: the batch is `held`, and is taken up again as a new generation when that clears, as held integration checks are.
4. If anything the batch recorded is no longer so (the roadmap in force, a story's state or revision, the set of obligations in force, the feature's basis, or it is no longer ready for another reason): the batch is `discarded` with the reason, and the completion is owed again if the feature is still ready.
5. **If trunk is no longer `H`**: the batch is `discarded`. The summary certifies one commit, and it is the commit that was trunk when the feature was completed. Checks that passed on an earlier trunk say nothing about a later one that includes another feature's work. The completion is owed again and runs on the new head.
6. If the runs are not exactly one per recorded obligation, each on `H`: `discarded`, as an incomplete integration batch is.
7. If every check is satisfied: the feature is `completed`, and the summary (section 4) is stored, in this transaction.
8. If any check is not satisfied: the feature **needs a decision** (section 5).

Trunk is read in step 5 inside the transaction's preparation, with a deadline: if the repository does not answer, nothing is decided and the batch stays running for the next pass to interrupt and re-run.

The pass takes up owed completions as it takes up owed integrations: bounded rounds, never the same assignment twice in one pass, carrying on past a feature it cannot complete now.

A repeated take-up of the same assignment is answered from the latest generation of its request and starts nothing.

## 4. The summary

Stored once, on completion, and never changed. Built from records and nothing else:

- the feature, its problem statement, the roadmap revision in force and every earlier revision's number;
- each story: accepted, or rejected and by what corrected, or withdrawn; with its revision and, for accepted ones, the gate and manifest digest the Product Owner approved;
- each behaviour on trunk for the feature: in force or superseded and by what;
- the decisions in force, the Product Owner's and a model's told apart;
- **open limitations**: findings still open and not blocking, by behaviour, and any assumption of a model's that no decision replaced;
- the commit `H`, and for every obligation checked its identity, digest and execution;
- usage: `unknown`. Not measured yet, and never shown as zero.

It says what was delivered and checked. It does not say the feature is valuable: that is not something Relay can observe.

`Features.summary_of(project, feature)` reads it. A notice goes to the Product Owner's stream; nothing is asked of them, so there is no gate.

A completed feature is finished: no proposal, question, pause, resume or cancel changes it, and a human action on it is stale.

## 5. When a check fails on trunk

This is reachable without any fault: a required check added after the last integration has never been run against trunk; a check that depends on the runtime may differ.

- The feature becomes `needs_decision`, and a gate of kind `completion` opens, showing the commit, each obligation that failed with its execution, and what was satisfied.
- The Product Owner has two actions there. `retry` owes the completion again, for when the cause was outside the feature (a check since corrected, a flaky runtime). `request_changes` returns the feature to `delivering` with nothing owed, so that the Analyst can propose a roadmap revision containing a story that corrects it.
- Cancelling the feature remains available, as always.
- Publishing a roadmap revision is refused while the feature is `checking` or `needs_decision`: the Product Owner sends it back first. Otherwise a revision and a completion could be decided against each other.

## 6. Restart, pause and cancel

- **Restart with a final-check batch running**: on start it is closed as `interrupted` (feature open), `held` (paused) or `abandoned` (cancelled), and for an open feature the completion is owed again. The runs it recorded stay.
- **Restart after the runs and before settlement**: the same, since settlement did not commit. The runs made are not reused: the next generation runs again on what is then trunk.
- **Pause during the checks**: settlement holds the batch; resume's pass takes it up again.
- **Cancel during the checks**: settlement abandons the batch; the feature is cancelled and never completed.
- **A take-up cut short** (cancelled, or the export failed): the batch is closed as interrupted and the completion is owed again, as step 1 does for a successor.

## 7. Tests

Completion: the last story approved; with nothing else called, the feature is checked on trunk and completed; the summary names `H`, each story with the manifest digest approved, each obligation with its execution, and usage `unknown`. A feature with a rejected story resolved by an approved correction completes; one with an unresolved rejection does not become ready.

Readiness by other routes: the last planned story dropped by an approved revision; a question answered and the roadmap reconfirmed; resume.

S3-4.10: another feature integrates while the final checks run: the batch is discarded, a new generation runs on the new head, and the summary names the new head. A check required while the checks run: discarded, re-run with the new set. Cancel during the final check: abandoned, never completed. Pause during it: held, completed after resume. Restart after the runs and before settlement: no completion from the lost batch; one from the next generation. Duplicate take-up: the same outcome, one batch.

Failure: a check required after the last integration fails on trunk: `needs_decision`, and the gate shows which. `retry` while it still fails opens the gate again on a new batch. `retry` after another feature's integration has put trunk right completes the feature on the new head. `request_changes` returns the feature to delivering, and a roadmap revision with a correcting story is then accepted.

A native oracle starts nothing. A worker cannot claim a completion. A completed feature is unchanged by a late pause, cancel, question or proposal.

## 8. Left out, on purpose

- Feature-specific checks beyond the project's obligations. "Configured feature-wide checks" are, in this slice, the obligations in force: the required project checks and what every behaviour on trunk still has to show.
- Costs. Usage is unknown until workers exist.
- Reopening a completed feature. A new feature is opened.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-ii step 4 design), saying for each finding whether a real entry point can reach it.
