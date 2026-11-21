# Slice 3a-ii, step 4, design v2: completing a feature

Provenance: written by Claude (Fable 5.2) on 2026-11-21 for review by Astra (gpt-6.2-astra via Codex). No code yet. It replaces `round-31-claude-3a-ii-step-4-design.md` and is complete in itself. This is design round two of at most three (D-8).

All eight findings of round 31 are accepted. Where each is answered is marked.

## 1. When a feature is ready

A feature is **ready to complete** when all of these hold, looked up under the gate lock:

- it is `delivering`, and neither paused nor cancelled;
- it has a roadmap in force, that roadmap is current, and no proposal is waiting;
- every story of that roadmap is resolved, and none is still to be released or being worked on;
- no question is open over it;
- **no roadmap change is owed for it** (section 5; S3-15.2).

One function, `evaluate_completion(connection, project, feature)`, decides this and is called in every transaction that changes one of its inputs (S3-15.4): a decision at a story gate; a decision at a roadmap gate, **whatever its outcome**, and the withdrawal or invalidation of a proposal; an answer; a reconfirmation of the roadmap or of a story; a resume; and the pass. When it finds the feature ready it sets it to `checking` and owes one assignment of kind `complete`, which no worker can claim.

**Who consumes it (S3-15.4).** The Product Owner's actions at gates get the entry point that pause and resume already have: `Features.decide(action)` calls `Stories.decide` and then, after that transaction has committed, `Features.reconcile(project)`. The receipt returned is the one `Stories.decide` produced. A replay of the same request returns the same receipt and runs the pass again, which finds nothing new to do. If the process dies between the commit and the pass, the assignment is owed and the pass on start takes it. `Decisions.answer` and the reconfirmations already call the pass after their transaction.

## 2. The final-check batch

There is at most **one live completion request per feature** (a unique index over requests that have not ended), and each request has generations, of which only the latest can be running (S3-15.3).

Taking up the `complete` assignment creates generation one, or the next generation of the live request, in the transaction that marks the assignment done. The batch records: the feature and the revision of its roadmap in force; each story of that revision with its state and revision; the trunk commit `H`, read with a deadline just before; the obligations in force, each by identity and digest, and the digest of the set; the feature's decision basis.

It runs each obligation's check on `H`, in a container, through the oracle that integration uses, outside any transaction. **Each execution is committed as it finishes, in a transaction of its own, against the batch** (S3-15.5): a process that dies before settlement loses no observation. Executions of a batch that does not decide are kept as evidence and are never reused by a later generation.

## 3. Settlement

One transaction, holding the gate lock **exclusively** from its first read to its commit.

1. **Only the current generation decides (S3-15.3).** If the batch is not `running`, or is not the latest generation of its request, nothing happens: the caller is told what that generation's outcome already is.
2. Cancelled feature: the batch is `abandoned`.
3. Paused, or a question open: the batch is `held`; the completion is taken up again as a new generation when that clears.
4. Anything recorded no longer so (roadmap in force, a story's state or revision, the obligations in force, the feature's basis, readiness otherwise): `discarded`, and the completion is owed again if the feature is still ready.
5. Not exactly one execution per recorded obligation, each on `H`: `discarded`, owed again.
6. **Trunk (S3-15.1).** If the project has an outstanding integration intent, the batch is `deferred`: a push may be on its way, or may have landed with its settlement not yet committed, and either way the database and the repository cannot both be read as one moment. Otherwise trunk is read with a deadline. Unreadable: `deferred`. Not `H`: `discarded`. In each case the completion is owed again. Because the lock is exclusive and no intent is outstanding, no push can be authorised between this read and the commit: every authorisation takes the gate lock shared.
7. Every check satisfied: the feature is `completed`; the summary is stored and the notice to the Product Owner's stream is written, in this transaction, each at most once per feature by a unique constraint.
8. A check not satisfied: section 5.

**One recovery owner (S3-15.5).** A batch never stays `running` across a settlement attempt: settlement always ends it. `deferred` is a durable, ended state with one completion owed after it, which the ordinary pass takes up without a restart and without touching any other batch. Only a start interrupts batches left `running`, because only then is it known that no oracle of this process is still running them. The pass takes at most a bounded number of rounds and never the same assignment twice, so an unreachable repository costs one deferred batch per pass, not a loop.

## 4. The summary

Stored once, with its notice, and never changed. From records only: the feature, its problem statement, the roadmap revision in force and the numbers of earlier ones; each story (accepted with the gate and manifest digest approved, or rejected and by what corrected, or withdrawn); each behaviour of the feature on trunk, in force or superseded and by what; the decisions in force, the Product Owner's and a model's told apart; the commit `H` with every obligation checked, by identity, digest and execution; usage `unknown`.

**Open limitations (S3-15.8)** are derived, not filtered by status. For each behaviour in force, from the review cycle that was integrated: a finding is a limitation if, under that cycle's frozen policy, it was not withdrawn, was not verified fixed in that cycle, and did not block. This is the classification the story review already shows the Product Owner (`settled`), reused. An assumption of a model's that no human decision replaced is listed too.

## 5. A check that fails on trunk

The feature becomes `needs_decision` and a gate of kind `completion` opens.

**Its basis (S3-15.6)** is the failed batch, the digest of its evidence manifest (the commit, each obligation with its execution and whether satisfied), the roadmap revision, and the feature's decision basis. A decision that changes the feature's basis supersedes this gate as it does a roadmap gate (the invalidation that today names `roadmap` for feature-level gates names `completion` too), returns the feature to `delivering`, and owes the roadmap's reconfirmation as usual; when that is made, `evaluate_completion` finds the feature ready and fresh checks run. A decision sent to the superseded gate is stale; an exact replay of one that was effective returns its receipt. Trunk is not part of the basis: after another feature has moved trunk, `retry` still means what the person meant, fresh checks on the head as it now is.

The Product Owner's actions there:

- `retry`: the feature is `checking` and the completion is owed again.
- `request_changes` **(S3-15.2)**: the feature is `delivering`, and **a roadmap change is owed**: an assignment of kind `revise` to the Analyst, recording the failed batch and the roadmap revision in force. While it is owed the feature is not ready, on any evaluation, including a restart. It is done when a later roadmap revision is approved, and not before: a proposal sent back leaves it owed. The revision may add an ordinary story after the approved ones; it need not name a rejected story to correct, because there is none. (If the roadmap rules as built refuse that, the rule is changed in this step, with a test.)

Publishing a roadmap revision is refused while the feature is `checking` or `needs_decision`.

## 6. A completed feature is finished (S3-15.7)

Every entry point that can change a feature's context refuses a completed one, in the same place and the same way as a cancelled one, saying it is completed: proposals, questions, answers, **assumptions**, **replacement of a decision in its scope**, reconfirmations, pause, resume, cancel, rejection of a story, a supersession proposal for one of its behaviours. A human request replayed exactly returns its original receipt, since replays are answered from the record before anything is evaluated. A decision that spans the project is not in a completed feature's scope and does not touch it. Its behaviours on trunk remain obligations of the project and can be replaced by another feature's work, which the summary, being a record of that moment, does not follow.

## 7. Restart, pause and cancel

- Restart with a batch `running`: closed as `interrupted` (open), `held` (paused) or `abandoned` (cancelled); for an open feature the completion is owed again. Its executions stay.
- Restart after the runs and before settlement: the same.
- Pause during the checks: settlement holds the batch; resume's pass takes it up.
- Cancel during the checks: settlement abandons it.
- A take-up cut short: the batch is closed as interrupted and the completion owed again.

## 8. Tests

Completion through the public action: `Features.decide` approves the last story and, with nothing else called, the feature is completed on trunk; the summary names `H`, each story with its approved manifest digest, each obligation with its execution, usage `unknown`; one notice. The reply lost and the request replayed: the same receipt, one summary, one notice. A rejected story resolved by an approved correction completes; an unresolved rejection is not ready.

Readiness by other routes (S3-15.4): the last story approved while a proposal waits, then the proposal sent back through `Features.decide`; the last planned story dropped by an approved revision; an answer and the roadmap's reconfirmation; resume.

Trunk (S3-15.1): another feature's push authorised and waiting at the remote when completion settles: deferred, nothing completed; after it lands and settles, a new generation completes on the new head, and where that push retired an obligation the summary lists the obligations as they then are. The reverse: the remote already at the new head with the integration's settlement not committed: deferred likewise.

Generations (S3-15.3): an old generation's failure arriving after a newer success changes nothing and opens no gate; an old success after a newer failure completes nothing.

Recovery (S3-15.5): the first check finished and the process stopped before settlement: its execution is on record, and the next generation runs every check again. The head unreadable at settlement: deferred, and the ordinary pass, with no restart, completes once it answers; another batch running meanwhile is untouched.

S3-4.10: a check required while the checks run; cancel during the checks; pause during them; duplicate take-up.

Failure (S3-15.2, S3-15.6): a check required after the last integration fails on trunk: `needs_decision`, the gate shows which. `retry` while it still fails opens a gate on a new batch; `retry` after another feature put trunk right completes on the new head. `request_changes`, then a restart: not checking; a proposed repair sent back, then a restart: still not checking; an ordinary added story approved, delivered and accepted: completed. A feature-wide answer while `needs_decision`: the gate is superseded, a decision sent to it is stale, and after the roadmap's reconfirmation the checks run again.

Terminal (S3-15.7): after completion, an assumption, a replacement of a feature-wide decision, a question, a proposal, pause, resume and cancel each change nothing: context, assignments and summary are as they were; a replayed earlier request returns its receipt.

Limitations (S3-15.8): a low finding left open beside one verified fixed on the delivered candidate: only the first is listed.

A native oracle starts nothing. A worker cannot claim a completion.

## 9. Left out, on purpose

Feature-specific checks beyond the project's obligations; costs (usage is `unknown` until workers exist); reopening a completed feature.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-ii step 4 design v2), saying for each finding whether a real entry point can reach it.
