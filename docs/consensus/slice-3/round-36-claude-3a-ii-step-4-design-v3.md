# Slice 3a-ii, step 4, design v3: completing a feature

Provenance: written by Claude (Fable 5.2) on 2026-11-21 for review by Astra (gpt-6.2-astra via Codex). No code yet. It replaces design v2 and is complete in itself. This is design round three, the last under D-8: what you find now I fold into the build contract without a further round, and say so in the record.

All six findings of round 35 are accepted. Sections 4, 5 and 6 are as in v2 except where marked.

## 0. One project per managed trunk (S3-16.1)

A project's gate lock protects the project; the guarantee completion needs is about a Git ref. Two projects on the same repository and branch could each push under the other's final read. They could already do worse: obligations are per project, so neither project's candidates are checked against what the other put on that trunk.

So `open_project` refuses a repository and trunk that another project already manages (a unique constraint on the canonical repository and the full ref). Two projects may share a repository on different branches, as now. With that, "no outstanding intent of this project, under this project's exclusive gate lock" is a statement about the ref, as far as Relay's own writers go. Writers outside Relay remain what they were in slice 2: detected at the next read, never prevented. This is a product rule, and I record it as a delegated decision in `docs/DECISIONS.md`.

Test: project B on the same repository and `main` is refused; on another branch it is accepted, and its push while A's completion settles changes nothing for A.

## 1. Prerequisites, admission, and release (S3-16.2)

The **prerequisites** of completion, defined once: a roadmap in force that is current, with no proposal waiting; every story of it resolved, none to be released or being worked on; no roadmap change owed (section 5); the feature not cancelled.

`evaluate_completion(connection, project, feature)` is called in every transaction that changes one of those (a decision at a story gate; a decision at a roadmap gate whatever its outcome, and the withdrawal or invalidation of a proposal; an answer, an assumption or a replacement of a decision in scope; a reconfirmation; a resume; the pass). It moves the feature both ways:

- **Admission**: the feature is `delivering`, the prerequisites hold, and it is neither paused nor under an open question. It becomes `checking`, a **completion request** is created for it, and one assignment of kind `complete` is owed for that request. No worker can claim it.
- **Release**: the feature is `checking` and the prerequisites no longer hold (in practice: a decision has made its roadmap stale). It returns to `delivering`, its completion request is ended as `overtaken`, and a wanted `complete` assignment is retired. The Analyst can then reconfirm the roadmap or replace it. A batch of the ended request can decide nothing (section 3.1).

A pause or an open question is neither: the feature stays `checking`, and nothing is taken up or decided until it clears (sections 2 and 3).

There is at most one live completion request per feature, by a unique index.

**Who consumes the assignment.** `Features.decide(action)` calls `Stories.decide` and then, after it commits, `Features.reconcile(project)`; the receipt is the one `Stories.decide` produced, and a replay returns it and runs the pass again. `Decisions.answer` and the reconfirmations already call the pass. **A settled integration wakes it too (S3-16.4)**: `Integration` is given a callback that `Features` supplies, called after the transaction that settles an intent has committed, whatever the outcome, which takes up the completions owed for that project. So a completion deferred behind a push completes when the push settles, with no restart, no manual pass and no human action.

In one pass a **completion request** is attempted at most once, however many assignments it has had: the pass remembers requests, not assignment ids.

## 2. Take-up and the final-check batch (S3-16.3)

Taking up a `complete` assignment:

1. Without a lock: if the feature is paused, under a question or cancelled, stop. Nothing is read from the repository.
2. Read trunk's head with a deadline. Unreadable: stop. Nothing was created; the assignment is still wanted.
3. In one transaction under the gate lock: the assignment must still be `wanted` and belong to the feature's live request; the feature must be `checking`, neither paused nor under a question, with the prerequisites holding. If the assignment is no longer wanted, the generation already bound to it is returned and nothing starts. If the feature is paused or questioned, nothing starts and the assignment stays wanted. Otherwise the next generation of the request is created, **bound to this assignment by a unique key**, and the assignment is marked done.

Each of those refusals leaves zero launches, and one wanted assignment wherever work is still owed.

The batch records: the request and generation; the roadmap revision in force; each story of it with its state and revision; the head `H`; the obligations in force, each by identity and digest; the feature's decision basis.

It runs each obligation's check on `H`, in a container, outside any transaction. **A receipt is one row per batch and obligation identity** (S3-16.6), recording the execution, the digest observed against and `H`, committed as each check finishes. Two obligations that ask for the same observation are two receipts with their own executions. Committed receipts survive a process that dies; an observation made and not yet committed does not, and the next generation runs every check again in any case.

## 3. Settlement, and ending a batch any other way

**One rule ends a generation, whoever calls it** (settlement after the runs; cleanup after a take-up cut short, an export that failed or an oracle that raised; the pass on start for batches left running) (S3-16.5). In one transaction, under the gate lock held exclusively from first read to commit:

1. The batch is not `running`, or is not the latest generation of the feature's live request: nothing changes. The caller is told what that generation's outcome already was.
2. The feature is cancelled: `abandoned`. Nothing is owed.
3. The feature is paused or under an open question: `held`, and the completion is owed again for the same request; take-up refuses it until that clears (section 2).
4. The caller is cleanup, or receipts are not exactly one per recorded obligation, each for the recorded digest and `H`: `interrupted`, owed again.
5. Anything recorded is no longer so (roadmap in force, a story's state or revision, the obligations in force, the feature's basis): `discarded`, owed again. (That the feature is `checking` and its prerequisites hold follows from step 1: release ends the request.)
6. The project has an outstanding integration intent, or trunk cannot be read within the deadline: `deferred`, owed again. Trunk is not `H`: `discarded`, owed again. Under section 0 and the exclusive lock, no push by Relay can be authorised between this read and the commit.
7. Every check satisfied: the feature is `completed`, the request is ended, and the summary and its notice are written, each at most once per feature by a unique constraint.
8. A check not satisfied: section 5; the request is ended.

"Owed again" always means one new `complete` assignment for the same live request, and only under this rule: nothing re-owes a completion unconditionally. A batch never stays `running` across an attempt to end it. Only a start ends batches left `running` by a process that is gone.

## 4. The summary

Stored once, with its notice, and never changed. From records only: the feature, its problem statement, the roadmap revision in force and the numbers of earlier ones; each story (accepted with the gate and manifest digest approved, or rejected and by what corrected, or withdrawn); each behaviour of the feature on trunk, in force or superseded and by what; the decisions in force, the Product Owner's and a model's told apart; the commit `H` with every obligation checked, by identity, digest and execution; usage `unknown`.

**Open limitations (S3-15.8)** are derived, not filtered by status. For each behaviour in force, from the review cycle that was integrated: a finding is a limitation if, under that cycle's frozen policy, it was not withdrawn, was not verified fixed in that cycle, and did not block. This is the classification the story review already shows the Product Owner (`settled`), reused. An assumption of a model's that no human decision replaced is listed too.

## 5. A check that fails on trunk

The feature becomes `needs_decision` and a gate of kind `completion` opens.

**Its basis (S3-15.6)** is the failed batch, the digest of its evidence manifest (the commit, each obligation with its execution and whether satisfied), the roadmap revision, and the feature's decision basis. A decision that changes the feature's basis supersedes this gate as it does a roadmap gate (the invalidation that today names `roadmap` for feature-level gates names `completion` too), returns the feature to `delivering`, and owes the roadmap's reconfirmation as usual; when the roadmap is reconfirmed or replaced, `evaluate_completion` admits the feature again and fresh checks run. A decision sent to the superseded gate is stale; an exact replay of one that was effective returns its receipt. Trunk is not part of the basis: after another feature has moved trunk, `retry` still means what the person meant, fresh checks on the head as it now is.

The Product Owner's actions there:

- `retry`: the feature is `checking`, with a new completion request and its assignment.
- `request_changes` **(S3-15.2)**: the feature is `delivering`, and **a roadmap change is owed**: an assignment of kind `revise` to the Analyst, recording the failed batch and the roadmap revision in force. While it is owed the feature is not ready, on any evaluation, including a restart. It is done when a later roadmap revision is approved, and not before: a proposal sent back leaves it owed. The revision may add an ordinary story after the approved ones; it need not name a rejected story to correct, because there is none. (If the roadmap rules as built refuse that, the rule is changed in this step, with a test.)

Publishing a roadmap revision is refused while the feature is `checking` or `needs_decision`.

## 6. A completed feature is finished (S3-15.7)

Every entry point that can change a feature's context refuses a completed one, in the same place and the same way as a cancelled one, saying it is completed: proposals, questions, answers, **assumptions**, **replacement of a decision in its scope**, reconfirmations, pause, resume, cancel, rejection of a story, a supersession proposal for one of its behaviours. A human request replayed exactly returns its original receipt, since replays are answered from the record before anything is evaluated. A decision that spans the project is not in a completed feature's scope and does not touch it. Its behaviours on trunk remain obligations of the project and can be replaced by another feature's work, which the summary, being a record of that moment, does not follow.

## 7. Tests

As in v2 section 8, with these added or changed.

Admission and release (S3-16.2): the ordinary last-story approval completes the feature on its first undisturbed batch. A feature-wide answer during the checks: the feature is `delivering`, the request is overtaken, the old batch's late success completes nothing and opens nothing; the Analyst replaces the roadmap, and the feature is admitted and completed under the new one.

Take-up (S3-16.3): a pause committed after the assignment and before take-up; a pause committed after the head was read and before the take-up transaction; an unreadable first head. Each: zero launches, one wanted assignment, completion after resume or once the repository answers. Two take-ups of one assignment at once: one generation, one set of launches.

Wake-up (S3-16.4): another feature's push held at the remote until the completion pass has returned, deferred; then the push is released and nothing else is called: the feature completes on the new head. In the pass that deferred, the request was attempted once.

Cleanup (S3-16.5): cancel commits while the export is in flight and the export then fails: the batch is abandoned, no completion is wanted, and a restart finds none.

Receipts (S3-16.6): a required check with the same digest as an integrated behaviour's contract: two receipts, two executions, both identities in the summary.

One project per trunk (S3-16.1): as in section 0.

## 8. Left out, on purpose

Feature-specific checks beyond the project's obligations; costs (usage is `unknown` until workers exist); reopening a completed feature; projects sharing a trunk.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-ii step 4 design v3), saying for each finding whether a real entry point can reach it.
