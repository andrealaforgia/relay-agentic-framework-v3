# Slice 3 design v2.1: three amendments for 3a-i, all accepted

v2 stands, with these changes. S3-2.4 and S3-2.5 are accepted as conditions of the 3a-ii design round and are not designed here.

## S3-2.1: eligibility is asked of a transition, not of a subject

There is no single "is this subject eligible". Each transition has its own precondition, and all share "the feature is neither paused nor cancelled and no blocker applies".

- **Releasing a story.** The story is `planned`, in the feature's current approved roadmap revision; every earlier story of that roadmap is `approved`; and the project's story slot is free. One function, `release_next(project)`, makes the choice under the project lock: it looks at every feature of the project in the order the features were opened and releases the first story that qualifies. It runs in the transaction of each event that can free the slot or create a candidate: a roadmap approval, a story approval, and later resume, cancel and feature completion. So a feature that lost the slot is reconsidered when it comes free, with no restart.
- **Behaviour work** (propose or append, begin test design, register a contract, run red, run green, integrate, seal). The story is in a working state: `released` or `changes_requested`. `awaiting_approval` occupies the slot and permits none of these. A behaviour may enter test design only when every earlier behaviour of the story is `integrated`.
- **A human action on a gate.** The gate named is open and its basis is current (below).

Tests: an append refused while awaiting approval; behaviour 2 refused test design before behaviour 1 is integrated; two approved features of one project, the second released only when the first feature's story is approved, in that approval's transaction.

## S3-2.2: a gate is stale the moment its basis changes

- In 3a-i nothing may change the basis of an open story gate: while a story is `awaiting_approval`, appending, sealing and assessing are refused (by the rule above). Any later path that must change it (3a-ii) supersedes the gate and its inbox item in the same transaction.
- A gate stores its **manifest**, not only a digest: the roadmap revision, the story revision, the ordered sealed list with each behaviour's contract digest, the integrated commit and the red, green and check execution ids of each, the findings and how each was settled, the decisions with their source, the Analyst's assessment text and its id, and usage as `unknown`. The manifest is immutable and is what the review shows.
- The first human action on a gate recomputes the basis under the lock (story revision, sealed list revision, assessment id) and compares it with the gate's. A difference makes the action stale and supersedes the gate.

Tests: restart with Redis flushed and retrieve the identical manifest; an identical retry after the decision returns its receipt; a basis changed behind the gate (written directly, since no 3a-i path can) makes a first approval stale and releases nothing.

## S3-2.3: a receipt acknowledges one exact request

A human action is keyed by principal and request id, and bound to its target, its action and a digest of its canonical payload. An exact replay returns the original receipt. The same key with any different content is a conflict: nothing changes and the conflict is what the caller is told. The receipt, the gate's outcome, the inbox change and any release it causes commit together. 3a-i implements `approve` and the disposition of `request_changes` (the story leaves the gate for `changes_requested`); what follows a change request is 3a-ii.

Tests: a lost reply; the request id of `approve(G1)` reused for `request_changes(G2)`; approve racing request-changes on one gate.

## 3a-i exit test, as you set it

An approved two-story roadmap; story 1 integrated; its displayed gate prevents further implementation; restart with Redis empty; the same review retrieved; approved exactly once; story 2 released.

Building 3a-i now on this basis.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3 design v2.1, as the basis for 3a-i).
