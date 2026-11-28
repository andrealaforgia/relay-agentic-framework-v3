# Consensus record: slice 3 (stories, the Product Owner's gate, real agents)

## Design, before any code

| Round | Astra's disposition | What it found |
|---|---|---|
| 1 | CHALLENGE, 10 blocking | Gates checked only when work starts, not when results arrive or messages are redelivered; a late approval landing on a newer version; "list complete" both forgeable and missable; no effect defined for a changed decision; rejection with no way forward; a gate that lived only in Redis; provider calls not safe to repeat; a model able to exercise the Product Owner's authority; behaviours with no story; workers assuming local paths |
| 2 | CHALLENGE, 3 blocking for 3a-i | "Current story" wrong on both sides of the gate; a gate not stale when its basis changes; a reused request id acknowledging something never done |
| 3 | ACCEPT | Nothing new |

Design v2 with the v2.1 amendments is the basis for 3a-i. Two findings (S3-2.4 held batches on resume, S3-2.5 the scope of a regression exemption) are accepted as conditions of the 3a-ii design round. S3-1.7 and S3-1.10 are conditions of 3b, and S3-1.8 (a human confirmation the model cannot produce) of 3c.

## Slice 3a-i as built

| Round | Astra's disposition | What it found |
|---|---|---|
| 1 | CHALLENGE, 9 blocking | Two deadlocks from opposite lock orders; a late assessment certifying a newer list; the roadmap shown differing from the one released; the review not saying how findings were settled; a finding changing behind an open gate; a payload overwriting the decision record; a production switch for behaviour order added for the tests' sake; an old seal surviving a change request |
| 2 | CHALLENGE, 2 blocking | A behaviour with no criterion reaching trunk and stranding its story; a proposal's row and ledger event disagreeing |
| 3 | ACCEPT_WITH_RESERVATIONS | The deadlock tests could be released by another test's lock wait (since closed: they now look only at their own connections) |

## Slice 3a-ii, design

| Round | Astra's disposition | What it found |
|---|---|---|
| 7 | CHALLENGE, 10 blocking across four steps | Closure recorded on stories deadlocks against release and misses a feature with no stories; pause and cancel not told apart; no boundary between starting work and recording it; a refused delivery lost; an interrupted batch stranded. Five more for the later steps. |
| 8 | CHALLENGE, 5 blocking | A grant outliving its consumer; an obsolete build command becoming valid again; resume missing work that has no command; an older retry displacing newer checks; "nothing is delivered" undefined |
| 9 | CHALLENGE, 4 blocking | A review granted before a pause stranded after resume; closure rules dropped by an amendment that "replaced" sections; a late green answering a replacement assignment; re-announcement undefined after Redis is emptied |
| 10 | CHALLENGE, 3 blocking | Three restart cases with work in flight, for which Astra supplied the wording |
| 11 | ACCEPT | Nothing new |

The agreed design for step 1 (pause, resume, cancel) is `round-10-claude-3a-ii-step-1-design-v3.md`, which stands alone. Carried to the design rounds of the later steps: S3-4.6 and S3-4.7 (step 2, questions and decisions), S3-4.8 and S3-4.9 (step 3, corrections), S3-4.10 (step 4, feature completion).

What this design round showed: the first two drafts inferred what work was owed from queued messages and behaviour state. Recording it (an assignment with a grant that has an identity) answered five findings at once.

## Slice 3a-ii, step 1 as built (pause, resume, cancel)

Reviewed under decision D-8: at most three rounds, then whatever is reachable is fixed without a further round.

| Round | Astra's disposition | What it found |
|---|---|---|
| 12 | CHALLENGE, 7 blocking of 8 | A successor able to launch a native check; a restarted reviewer's capability approving on a dead worker's checkout; a closure receipt replayed across projects; a worker able to claim an integration and strand it; a pause stopping the whole start-up pass; a successor cut short left with nothing owed; "latest batch" decided by transaction start, not registration; held checks reported as waiting after a cancel |
| 13 | CHALLENGE, 4 blocking of 5 | A worker whose grant was taken back submitting on its successor's checkout; a successor cancelled during settlement; a successor overtaken by a new required check left with nothing owed; a paused feature's integration reaching a slow repository; interrupted checks reported as waiting after a cancel |
| 14 | CHALLENGE, 2 blocking of 3 | A check policy change during a resume leaving the work to wait for another call; one project's silent repository holding up the pass for the others; the reason for a re-run not kept |

Every finding in all three rounds was reachable through a real entry point, and every one is fixed, each with a test that fails without the fix. The fixes for round 14 are in `round-15-claude-3a-ii-step-1-closing.md` and were **not reviewed by Astra**: that is what D-8 means, and it is the standing caveat on this step.

Seven departures from the agreed design are listed in `round-12-claude-3a-ii-step-1-submission.md`; Astra accepted all of them once rounds 12 and 13 were fixed.

Reservations carried forward:

- **A pass runs on resume and on start, not on a timer.** Work that becomes owed outside a pass (a successor whose checks keep being overtaken, past three attempts in one pass; a repository that could not be read) waits for the next resume or start. The periodic loop belongs to 3c.
- **Only the pass's own calls to a repository are bounded** (publication, recovery, the successor's trunk read). A caller's own `integrate`, `run_red` and `run_green` still wait for Git as long as Git takes.
- **A resume runs the regression checks before it returns**, because the control plane is the only integrator until 3b.
- **"Restart" in the tests is new service objects over the same database**, with the old task cancelled. No test kills an operating-system process. The publisher cases stand a recording object where Redis would be.
- The round 14 fixes have had no second pair of eyes.

What this step showed: three rounds, sixteen findings, none of them speculative. Most came from one pattern, an obligation recorded as done at the moment its work started rather than when it finished, so anything that interrupted the work left nothing owed. The design said "an assignment is done when its effect is on record"; taking the successor batch's existence as that effect was my reading, and it was too early.

## Slice 3a-ii, step 2, design (questions, answers and changed decisions)

| Round | Astra's disposition | What it found |
|---|---|---|
| 16 | CHALLENGE, 7 blocking of 8 | A submission stamped with the basis current when it arrives, not the one its author read; an old result arriving after reconfirmation; a story awaiting approval with no way back; amendment routes that do not exist; assignments that need a behaviour; blockers not composing; no rule for one answer per question |
| 17 | CHALLENGE, 6 blocking of 7 | Red run on a stale contract; an amendment needing no grant; an old checkout indistinguishable from a new one; an outstanding push at reconfirmation; nothing waking held work when the last blocker clears; context and basis read apart |
| 18 | CHALLENGE, 4 blocking of 5 | A roadmap compared with the wrong scope's basis; a refused reconfirmation left granted to one worker; an old seal surviving an answer; reopening a red removing the reconfirmation it needs |

The build contract is `round-19-claude-3a-ii-step-2-design-v4.md`: design v3 with round 18's five findings folded in. **Astra has not reviewed v4.** It kept the core of the design (one decision number per story, scope looked up and never copied) through all three rounds, and said the last amendments could be folded in without another design round.

What step 2 will not do, by design: change anything already in review, integrated or approved, or change a story's text or a roadmap's content. Those are corrections, step 3. Until step 3 exists, a scope stopped for such a reason stays stopped and says why.

## Slice 3a-ii, step 2 as built

| Round | Astra's disposition | What it found |
|---|---|---|
| 20 | CHALLENGE, 6 blocking of 9 | A reconfirmation grant accepted in another project; a story's context read under the wrong feature; a late green recorded under a later contract; a deadlock between an assumption and the first roadmap; publication ahead of reconfirmation; a reconfirmation whose follow-up was cut short |
| 21 | CHALLENGE, 2 blocking of 5 | A spent grant acknowledging a different request; a refused publication blocking a cycle a decision had made stale |
| 22 | CHALLENGE, 2 blocking of 3 | A late refusal blocking a cycle another publisher had published; a conflict seen while a decision lands blocking the cycle |

Seventeen findings, all reachable through a real entry point, all fixed with a test that fails without the fix. The fixes for round 22 are in `round-23-claude-3a-ii-step-2-closing.md` and were **not reviewed by Astra**, as D-8 provides.

Six departures from design v4 are listed in `round-20-claude-3a-ii-step-2-submission.md`. Astra accepted them, two of them only once a grant was bound to its project (round 20, finding 1).

Reservations carried forward:

- **Step 2 cannot change what is already in review, integrated or approved, nor a story's text or a roadmap's content.** A decision that requires such a change leaves its scope stopped until the owner reconfirms or the feature is cancelled. That is step 3.
- **Races are tested inside one process**, by making the interleaving happen with a stand-in at the Git call or a held lock. No test runs two control-plane processes against each other.
- **A stale cycle is never blocked by something seen about its candidate ref.** It cannot be reviewed or integrated while stale, and a real conflict blocks it when work resumes.
- The round 22 fixes have had no second pair of eyes.

What this step showed: I wrote the largest increment's code before its tests, and it showed twice. Deliberate breakage caught only 29 of 52 cases at first, against 25 of 27 for the increment I had written test-first. And Astra's first round found six blocking defects in it, including a grant that worked across projects, which no test of mine had two projects to notice. From round 20 on every fix was test-first.

## Slice 3a-ii, step 3, design (corrections)

| Round | Astra's disposition | What it found |
|---|---|---|
| 24 | CHALLENGE, 9 blocking of 10 | A release rule that stranded the first correcting story; an edit to a story overtaken by a decision about it; a proposal with no existence apart from its gate; no way to return an active story; findings lost when a contract in review is amended; an authorised push and a voided authority contradicting each other; and four more on supersession |
| 25 | CHALLENGE, 9 blocking of 10 | Placement of a correction deciding whether ordinary work deadlocks or escapes; a correction dropped or duplicated by a later proposal; a delayed draft restoring an old order; one reconfirmation serving both the effective roadmap and a proposal; a direct rejection landing on a later revision; and four more on supersession |
| 26 | CHALLENGE, 4 blocking of 6 | A second correction added once the first is approved; an identical "amendment" sending a refused candidate round again without the Product Owner's retry; a supersession proposal with no way to end; an approved story reopened by a later retirement |

The build contract is `round-27-claude-3a-ii-step-3-design-v4.md`: design v3 with round 26's six findings folded in. **Astra has not reviewed v4.** It said part (i) could be built once two of them were folded in, with no further design round.

Step 3 is built in two parts, each reviewed: (i) roadmap revisions, rejection, amending a contract in review or parked; (ii) obligations, supersession, and what a story demonstrates.

## Slice 3a-ii, step 3, part (i) as built (roadmap revisions, rejection, amending in review)

| Round | Astra's disposition | What it found |
|---|---|---|
| 28 | CHALLENGE, 5 blocking of 7 | A behaviour proposed against a criterion reworded under the same name; a deadlock between a story's reconfirmation and a roadmap write; two features proposing one story name; a ref conflict blocking a cycle after its story was rejected; an interrupted batch reported as held after a rejection |
| 29 | CHALLENGE, 1 blocking of 4 | The same deadlock reached through reconfirming a proposal; an observation not recorded; a new story shown correcting another feature's rejection; the wrong reason stored at a restart |
| 30 | **ACCEPT_WITH_RESERVATIONS**, none blocking of 2 | A reviewer told "the feature is cancelled" for a rejected story; no test of approval racing a rejection |

Thirteen findings, all reachable, all fixed. Both of round 30's are fixed too, after the acceptance: the refusal names the story, and `test_a_roadmap_approval_and_a_rejection_in_either_order` covers both orders. Astra has not seen those two changes.

Of the five departures from design v4 listed in `round-28-claude-3a-ii-step-3-part-i-submission.md`, Astra accepted three, and I withdrew one: every roadmap write now takes the gate lock exclusively, as the design said, because taking only the project lock deadlocked against a story's reconfirmation. The fifth (a rejected story reads as cancelled to work in flight) stands, with the record and the refusals now saying what actually ended.

Reservations carried forward:

- **Restarts in the middle of these histories are mostly not exercised.** State is all in PostgreSQL; three tests start a fresh control plane after a rejection.
- **Work in flight at a rejection** is tested for a red and for a batch of checks, not for a green or a review submission, which rely on the same rule.
- **A story exists only once its roadmap is approved.** Nothing can be asked or assumed about a story that is only proposed.

## Slice 3a-ii, step 3, part (ii) as built (obligations, supersession, what a story demonstrates)

| Round | Astra's disposition | What it found |
|---|---|---|
| 32 | CHALLENGE, 5 blocking of 7 | A batch settling under an authority it did not start with, so a replacement reached trunk with nothing retired; a seal written before a retirement accepted after it; retirement and reconfirmation taking a behaviour and its story in opposite orders; an ordinary push leaving a proposal alive; a proposal that did not name the replacement as read; a reconfirmation still owed for a retired behaviour; a seal cleared needlessly |
| 33 | CHALLENGE, 2 blocking of 3 | A rejected story's proposal left in the inbox; a behaviour declared to be replaced then reconfirmed as standing; a withdrawn command sent again |
| 34 | CHALLENGE, 1 blocking of 1 | A proposal overtaken by a decision still open to a withdrawal that rewrote why it ended |

Eleven findings, all reachable, all fixed, each with a test that failed first and a deliberate breakage of the fix afterwards. **Part (ii) was not accepted by Astra**: round 34 was the third and last round, its one finding is fixed (`test_a_decision_ends_a_proposal_made_under_the_ones_before_it`), and Astra has not seen that fix. Astra confirmed rounds 32 and 33 closed.

Where the build departs from design v4, as Astra accepted:

- **No policy-version increments for supersession.** At authorisation a batch's regression digest and its authority must both be what is computed then. This replaces v4 section 5's increments.
- **An authority is voided when its ground goes, in the transaction that removes it** where that is a rejection, a cancellation, a decision or an ordinary push of the replacement; and re-derived wherever one is about to be proposed, decided or used.
- **A declaration that a behaviour is to be replaced lasts.** If the replacement never reaches trunk the story cannot be sealed; the Product Owner rejects the story or cancels the feature. No later decision asks whether the contract stands after all.

Reservations:

- **The tests for ten of the first survivors of deliberate breakage were written after the code.**
- **The contract digest and story revision in an authority's basis** are compared at proposal and recorded, but no test changes one without the decision basis changing too: an amendment needs a decision.
- **A story waiting at its gate that lists a replaced behaviour** is left alone. Astra found no entry point that reaches it: an awaiting story holds the project's place.
- **The sibling test** relies on the fixture that puts two candidates of one story in play.
- **No concurrency test of proposing, deciding and withdrawing**, which all take the gate lock exclusively. The lock order at retirement has one.

## Slice 3a-ii, step 4 as built (completing a feature)

The design went three rounds (31 to 36: eight, six and four findings) and its last amendments were folded into a build contract Astra did not review (`round-37`). The build then went three rounds.

| Round | Astra's disposition | What it found |
|---|---|---|
| 38 | CHALLENGE, 5 blocking of 7 | Two local addresses enrolling one trunk; a caller cancelled during settlement stranding a running batch; the pass and an approval taking two features' rows in opposite orders; a deferred completion woken only by the `Integration` instance that was wired; the summary crediting a correction that had been dropped; a replay answering with another generation's receipt; no test of dropping the last planned story |
| 39 | CHALLENGE, 5 blocking of 5 | Git's `.git` suffix still giving one trunk two names; a name ending in a space redirected to its neighbour; a caller cancelled between a batch's commit and hearing of it; clean-up taking the feature before the project; a wake-up lost after the settlement committed |
| 40 | CHALLENGE, 4 blocking of 5 | An empty address enrolling the directory Relay runs in; a repository that cannot be read swapped for its neighbour; a second cancellation taking the clean-up with it; a stalled clone holding up every other project; one pass attempting a deferred request twice |

Seventeen findings, all reachable, all fixed. **Step 4 was not accepted by Astra**: round 40 was the third and last round, and Astra has not seen the fixes for its five findings. Astra confirmed rounds 38 and 39 closed for the histories reported. Every finding after the first round was in code written to answer the round before, which is the pattern to expect and the reason the rounds are capped.

Deliberate breakage: 71 breakages before the first submission, of which 52 were caught; 11 survivors were gaps in the tests and got tests, 8 were guards nothing reaches and were removed. Each round's fixes were then broken on purpose (13, 7 and 7 breakages); two survived and got a test or lost a duplicate clause. The last full run is 789 tests, green, on the tree that is committed.

Where the build departs from the contract, as Astra accepted (round 38):

- **`Features.decide` takes up completions; it does not run the whole pass.**
- **No "latest generation" test and no "cancelled" branch when a batch ends.** A request that ends takes its running generation with it, so a running one is always current.
- **Take-up does not re-derive the prerequisites under the lock.** That the feature is still ready follows from readiness being settled in every transaction that changes it.
- **A stopped feature is never newly ready**, so admission does not ask whether it is stopped and a resume does not evaluate. Whether checks may start is asked at take-up.
- **A completion gate's basis is not recomputed when it is decided.** Nothing reachable changes it while the gate is open.
- **A roadmap change owed after a failed check cannot be claimed.** It is answered by proposing the roadmap.

Decided during the build:

- **A local repository is known by the directory that holds its refs**, resolved as Git resolves it, and a project cannot be opened on a local path with no readable repository at it. A `file://` URL is its path whatever host it names.
- **Wake-ups are safe to repeat, not exactly once.** Every `Integration` takes up the completions a settlement frees; reconciling an intent wakes whether it settled it or found it settled; the scan ends by taking up whatever is wanted.
- **The project's row is taken before any feature's row**, by final checks and by the pass as by gate decisions.

Reservations:

- **Astra has not reviewed the round 40 fixes.**
- **Two remote addresses for one repository** (SSH and HTTPS, say) are two repositories to Relay. One project per trunk is then the operator's to keep (D-9).
- **The stalled-clone fix is tested in two halves**: a real clone that stalls is given up on at its deadline, and final checks pass their deadline and leave the completion owed. Two projects in one pass, the first stalled, is not tested as one history.
- **The lock-order fixes are tested as orders, not as deadlocks**: a test holds one row and requires the other to be free. The pass's per-feature evaluation has the same fix and no test of its own.
- **A clean-up cut short by the process dying** is put right only when the control plane starts.
- **The pass as a net** (admitting a ready feature nobody looked at) is reachable only for data from before completion existed. It is kept, and tested by switching the transactional evaluation off.
- **Tests for the eleven first survivors were written after the code.**
- **"An old success after a newer failure"** has no test of its own; it rests on the same fence as the reverse order.
- **The final trunk read holds the project's gate lock exclusively across a network call**, bounded by the deadline. That is the design, and the one place Relay does this.
- **Usage is `unknown` in every summary** until 3b measures it (PO-9).

## Slice 3b: workers and the Claude adapter

### The design

| Round | Astra's disposition | What it found |
|---|---|---|
| 41 | CHALLENGE, 10 blocking of 10 | A duplicate delivery buying a second call; startup invalidating the grant a known result needs; a pause not stopping a paid call; an apply of several effects that could not be resumed; terminal errors and structured output not told apart; no enforced boundary around the model's process; no trigger for a behaviour's first integration; transitions with no worker to carry them on; a question losing the work that asked it; usage that could not be priced |
| 42 | CHALLENGE, 9 blocking of 9 | An assignment owed twice refused for ever after its first call; a retry that could not wake parked work; a claimed grant orphaned before any call; two deliveries both running one effect; a build returned with its tree lost; the wrong base for a remediation; a confinement pass keyed to too little and trusting the model's account; the first integration with nobody to retry it; two more transitions with no worker |
| 43 | CHALLENGE, 8 blocking of 8 | Who carries out an authorised retry; which oracle try may settle; a known result during a pause; lock order at admission; what each confinement probe can observe; a successor not owed while paused; a behaviour left without a contract; one red request owned twice |

Twenty-seven findings. **The design was not accepted.** Round 43's eight findings are folded into the build contract (`round-44`), which Astra did not review. Astra accepted along the way: the assignment as one occasion with successors; keeping a build's tree before it is `returned`; the recorded remediation base; the control plane owning the first integration; leaving the provider's own transcript out of recovery; and D-5 staying deferred **only if** the boundary around the model's process is demonstrated and attestable.

### Step 1 as built: records and the adapter

| Round | Astra's disposition | What it found |
|---|---|---|
| 45 | CHALLENGE, 10 blocking of 10 | An effect redone under a new identity; a superseded try rejecting the result; a held result never carried on; an unusable CLI or a cancelled call left as "in flight"; a call recorded as prepared on what it was not; a late result applied across grants; a grant not bound to its caller, worker and role; calls in flight for ended work never settled; an inventory that was missing read as empty; a build returned with nothing kept |
| 46 | CHALLENGE, 5 blocking and 5 lesser | The handoff from provider to record lost to cancellation; the adapter's classification lost on cancellation; a pause taking back an adopting grant; a dead grant hidden behind an earlier call; a decision during an effect still settling, and a lock inversion; a refusal in `settle` escaping; an effect left taken after cancellation; no liveness check at admission; a parked call outliving its work; two recovery sessions |
| 47 | CHALLENGE, 2 blocking and 6 lesser | An effect that changes the story unable to use `settle`, and a pass overtaking a running effect; an effect left "busy" on record by a live worker; a faulting validator losing the result; a refusal after the work ended blamed on the reply; parked work finished by other means still retryable; recovery locking the project before assignments; a liveness probe that impersonated the worker; a cancelled preflight recorded as a paid call |

Twenty-eight findings, all reachable, all fixed. **Step 1 was not accepted**: round 47 was the third and last round and Astra has not seen the fixes for its eight findings. Two of those eight were checks I had removed as redundant after a breakage survived, on reasoning that held for the tests I had and not for histories I had not thought of (a pass overtaking a running effect; work finished without being retired). Both are back, each with the history that needs it.

Deliberate breakage, since every test of this step was written with its code: 24 breakages of the provider modules, 47 of the records, 10 of the runner, then 29, 18 and 13 against the three rounds of fixes. The last full run is 934 tests, green; one test was added after it and passes with its file. Survivors were given a test or, where they showed code that did nothing, the code was removed.

Decided during the build:

- **Every apply is a list of effects**, each with one identity for all its tries. What an effect writes to PostgreSQL it writes in `settle`, in the transaction that marks it done.
- **An effect that changes what the invocation is about does so in `settle`** and says what it left. `perform` must not.
- **A parked call is its own kind of inbox item**, listed by `Invocations.parked` and decided by `Invocations.decide`. One view over gates and calls is 3c's.
- **A grant with no worker process** can still be claimed, by callers inside the control plane, and admits no paid call.
- **Liveness is read from `pg_locks`.** Nothing is taken to find out.
- **The start event verifies** that the bounds a run was launched with took. It does not stop a run before its briefing is read.

Reservations:

- **Astra has not reviewed the round 47 fixes.**
- **One check has no test that needs it.** When an effect is marked done, the invocation must still be `returned`. A breakage of that check survives, because the check beside it (has the work ended or changed) catches the one history I have. I removed it once for that reason and Astra showed a history that needed it; the history has since been closed another way (a pass leaves a running effect to its carrier). I keep the check and say here that nothing proves it.
- **Recovery's lock order** (one transaction per candidate, the assignment before the ledger) is by construction; no test would have deadlocked before.
- **Work finished by other means** is tested by finishing a build's assignment directly, not by the correction-and-approval history that reaches it.
- **An effect whose service finishes the assignment in `perform`**, cut short before its mark, is recorded `overtaken` when taken up again, though it did happen. Finishing belongs in `settle`.
- **The lock order with `reopen_red`** is tested one after the other, not raced.
- **A worker whose connection drops while its process lives** is tested by stopping its session.
- **The stand-in CLI** shows how Relay calls the CLI and reads it, and nothing about what the real CLI enforces. That is step 2's suite, and D-5's condition.
- **No time-based lease** until slice 5: a worker that hangs with its connection open keeps its grant.

### Step 2 as built: the boundary around a run, and the two existing tasks

| Round | Astra's disposition | What it found |
|---|---|---|
| 48 | CHALLENGE, 11 of 11 reachable | A launch under policy nobody had established; a disproved pass still counted; a rejected candidate taken for green on replay; a superseded green try opening a review, and the wrong commit observed; another project's findings in a briefing; the only usable delivery acknowledged; a review submitted from a checkout the model was not handed; a tracked file deleted by `retain`; probes credited without a refusal, and one workspace layout; two operations with no principal; a malformed reply become a finding |
| 49 | CHALLENGE, 9 of 9 reachable | D-5 to be done now, and the suite's runtime read at the wrong moment; a superseded observation replayed as a verdict; overtaken work left with nobody; hand-back decided by history, not by what is in flight; four more operations with no caller; validation only on the worker's path; cleanup freeing another call's try; probes credited on resemblance; an attempt ref that is gone |
| 50 | CHALLENGE, 8 reachable and 1 reservation | A decided review answering, and recording evidence for, a caller it was never handed to; a green accepted after its story gained a behaviour; a review that could not be prepared left with a live worker; a refusal cut short leaving its effect taken; an earlier pass recorded again undoing a later failure, and a late write renewing old observations; the boundary asked before admission waited; a pre-launch green refusal judged again on recovery; trees and bases never let go of; a schema for extra properties accepted and not checked |

Twenty-eight findings and a reservation, every one reachable through a real entry point. **Step 2 was not accepted.** Round 50 was the third and last round (D-8). Seven of its eight findings and the reservation are fixed, each with a test that fails without the fix, at commit `45d1c8b`; **Astra has not seen those fixes.** The eighth, 3B-S2-3.7, is not fixed and is the first reservation below.

Deliberate breakage, since every test of this step was written with its code: 49 breakages of the step as first built, then 34, 37 and 20 against the three rounds of fixes. Of the 20 against the last round, 17 were caught and 3 survived; each survivor got a test that now catches it (the latest assessment chosen by when it was observed; a base kept for a build prepared and never called; a tree discarded while a cancelled caller's record was still being made from it). One breakage was at first reported caught by a test that had nothing to do with it, which failed because the host's clock and the database's did not agree to the millisecond; that test no longer depends on it, and the breakage was run again. The last full run is 1093 tests, green; three tests were added after it and pass with their files.

Decided during the build:

- **No review capabilities (D-11, superseding D-5).** What lets a caller act on a review is the grant on the review's assignment. A grant is not a secret. A future API must authenticate the caller as the grant's holder.
- **The operator's statement of remote policy is a launch condition, not proof (D-10).** Software cannot establish what a provider's servers apply; a person says so on record, and with nothing said no run with tools is launched.
- **The boundary is asked inside admission**, after the locks, and the assessment that allowed a launch is recorded with the call.
- **A run of the suite is one assessment**, with an identity and a time of observation given when it runs. The latest observed is what is true of a boundary.
- **Every way a result becomes `overtaken` goes through one function**, which gives the work back and announces it again, or parks it when as many calls have been made as a person is asked after.
- **Hand-back is decided by the work's latest call**: a call in flight or a result waiting keeps the grant.
- **A green run's event says whether it judged the candidate.** Replay reads only judgements.
- **`record` checks every result against the schema stored with its call**, and a schema Relay cannot check in full is refused before anything is paid for.
- **A surviving breakage gets a test or a stated reason.** Three times in this slice I removed code because a breakage of it survived, and each time a history turned up that needed it.

Reservations:

- **Astra has not reviewed the round 50 fixes.**
- **3B-S2-3.7 is open.** A green refused before launch (native isolation not approved) is recorded with no judgement. If the worker stops between that refusal and the invocation hearing of it, recovery asks again and a second, identical refusal goes on record. Nothing is executed twice and nothing is decided differently; the ledger has a duplicate.
- **A worker refused by the boundary while it holds an adopted result does not apply that result itself.** It keeps the grant and the pass applies it.
- **The suite judges on what Claude Code 2.1.330 says on macOS when it refuses.** Another version or platform may word a refusal differently; the suite then fails, which is the safe direction, and the markers have to be read again.
- **Remote policy is stated, not observed** (D-10). A change of it inside a pass's 24 hours is not detected.
- **A pass's time of observation is the host's clock; its freshness is judged by the database's.** A host whose clock runs ahead gets a longer lease by that much.
- **The sandbox's temporary directory is shared between runs on a host.** The suite does not probe it.
- **The grant is authority only while callers are in-process.** `claim` still gives a grant to a caller inside the control plane with no worker and no liveness check; that grant admits no paid call.
- **The overlapping-oracle histories are tested with two real oracle runs in one process**, not two worker processes.
- **Whether a discarded tree is really gone** is checked by looking at the workspace root after the call, not under a failing filesystem; `rmtree` errors are ignored.

### Step 3 as built: the owed work that was missing

| Round | Astra's disposition | What it found |
|---|---|---|
| 51 | CHALLENGE, 4 blocking of 13 | A first integration nobody carries on when its checks are discarded or cut short; an invalid red observed again, and by two passes at once; a proposal sent back between two commits owing no successor; a successor refused by the proposal it is to replace; "demonstrated" accepted with a criterion unshown; refusals of paid replies escaping as errors; story-wide work started while a contract waits to be reconfirmed; a proposal reconfirmed without its new stories; no worker route to replace what is on trunk; no question from a Builder or a reviewer; a late take-up starting another cycle's checks; an assumption outliving a refused reply; the Analyst asked to repair without the evidence |
| 52 | CHALLENGE, 2 blocking of 12 | A stale reply withdrawing a proposal under a later decision; two reviewers deadlocked when one asks; a red observed twice across a pause and a resume; a seal or assessment owed again when it is no longer the step; a replacement voided by a decision with no worker to propose it again; a roadmap in force that no longer stands with nobody owed its revision; assumptions accepted blank, or lost beside accepted work; a refused push a person allows again owed to nobody; a pass cancelled as its first batch commits; two missing wake-ups; validation that depended on who recorded; the common reply shape refused for a build or a review |
| 53 | CHALLENGE, 2 blocking of 11 | A malformed wrapped reply accepted as an empty review; a red launched with none owed, and decided by a caller that no longer held it; a reply to an invalid red that rewrote nothing; a story-scoped decision with nobody owed the rewording; no way to decline a voided replacement past its red; a retry replayed allowing a further push; a stream loop while a gate waits for a person; binary output breaking a briefing; two briefings that said what was not so; a second cancellation during batch creation; a withdrawal's wake-up neither required nor repeatable |

Thirty-six findings, every one reachable through a real entry point. **Step 3 was not accepted.** Round 53 was the third and last round (D-8). All eleven of its findings are fixed, each with a test that fails without the fix, at commit `8196107`; **Astra has not seen those fixes.**

Deliberate breakage, since every test of this step was written with its code: 50 breakages of the step as first built, then 31, 28 and 16 against the three rounds of fixes. Of the last 16, 15 were caught and 1 survived: nothing showed that an amended contract's red is owed in the amendment's own transaction, because the pass that follows owed it anyway; it has a test now and is caught. The last full run was 1183 passed and 3 failed, before the last source change: two of the three were the missing red named below, the third an expectation that a repeated withdrawal is refused. All three were put right, and the whole suite then passed on the commit that closes this step: 1186 tests, 2 skipped (the paid ones).

Three things breaking the code showed, beyond missing tests. A test of mine passed for the wrong reason twice: once because the two refusals it exercised had come to be caught earlier by other checks, so nothing reached the code under test; and once because the test's own loop offers each piece of work once per round, so a build I claimed was held back by a check had simply not been offered again. And a rule made strict in the last round (a red needs a current assignment to launch) found two transitions that had been owing no red in their own transaction, a trunk that moved under a reviewed candidate and a contract amended after a decision, both until then put right only by the next pass.

Decided during the build:

- **Every kind of work has a worker**, and every reply may be one question for the Product Owner in its place, with what was assumed.
- **An assignment is one occasion.** A reply that asks ends it (`asked`); the answer owes a successor, except for a reconfirmation, a seal or an assessment, where the answer undoes what they were of and what is owed next is derived.
- **The gate is taken exclusively for every admission, effect take and mark.** Any reply may be a question, and a question needs it so. No paid call and no oracle run is made under it.
- **One rule says what a story needs next** (`owed.py`), asked in the transactions that move a story and by the pass. It owes only what is not owed.
- **One red per behaviour and red cycle**, the control plane's, taken before it is launched and decided only by the grant that took it. After a red that showed nothing, the Verifier writes the contract again, as it was if it was right.
- **An integration is never left to a caller to ask for again**: a first generation that is discarded or cut short stays owed like any later one, and a retry a person allows owes it.
- **Nothing is built for a behaviour whose replacement of something on trunk is undecided**, and its red waits. This narrows what 3a-ii allowed: a replacement can still be proposed after its candidate was built, by hand.
- **A step that is not due is refused at the claim**, before any call: a seal with nothing said to be demonstrated, an assessment with no seal, story-wide work while a contract waits to be reconfirmed.
- **The pass runs after a gate decision and after work is applied or found done**, not after a refusal for a reason that still holds.
- **A reply is read where it is applied**, whoever recorded it.

Reservations:

- **Astra has not reviewed the round 53 fixes.**
- **The exclusive gate serialises a project's admissions and marks.** What that costs with several workers has not been measured. Two applies are not raced in any test; the gate is relied on by construction.
- **The Verifier's worker cannot declare a behaviour replaced from a story other than through reconfirmation**, and a roadmap or story that no longer stands is revised only if it is the roadmap in force, a waiting proposal, or a planned story. A story being worked on that no longer stands is the Product Owner's to be asked about.
- **`Supersessions` and `Integration.retry_integration` wake the pass only if they are given it** (`then`, or through `Features`). Wiring every public route to it is 3c's composition.
- **A build's or a review's reply keeps its own flat form** beside the common one; both are accepted.
- **What a build that was not green assumed is not kept.**
- **A red refused because the oracle's isolation is not gate-bearing stays owed** and is refused again each pass.
- **The feature test's workers are driven from PostgreSQL in the order work became wanted**, not from the streams, and its loop offers each piece of work once per round.
- **Briefings are tested for the facts in them, not as prose.** Whether a real model does good work from them is step 4's smoke.
- **The pass run by a worker is the control plane's own object in the same process.** Across processes it is 3c's.

### Step 4 as built: the real provider

| Round | Astra's disposition | What it found |
|---|---|---|
| 54 | CHALLENGE, 12 of 12 reachable | The smoke able to pass with the product wrong; stand-in agent definitions in place of the canonical ones; missing token counts shown as totals; contradictory review verdicts decided by order; "demonstrated" discarding a behaviour given beside it; a known reply made defective by a change of form; earlier findings passed over in silence; reconfirmations offered verdicts they cannot take; a pass for an earlier CLI version; no dollar cap per call; a late "nothing was launched" counted as a call; no live read of usage |
| 55 | CHALLENGE, 2 blocking of 7 | A late report releasing its successor's grant or clearing its park; the version checked differing from the version launched; the version asked in the supervisor's environment; a completed suite run losing its observations; a released story offered a verdict Relay refuses; an empty report of use counted as a report; a race test that excluded a valid outcome |
| 56 | CHALLENGE, 2 blocking of 7 | The authorised executable not determining which executable is briefed; the suite able to certify a version its probes did not run on; a failed probe leaving an earlier pass usable; a late report revoking a grant claimed since and not yet admitted; a version query with no deadline; two suites at once sharing one marker; an unreadable policy file stranding a grant |

Twenty-six findings, every one reachable. **Step 4 was not accepted.** Round 56 was the third and last round (D-8). All seven of its findings are fixed, each with a test that fails without the fix, at commit `ba48cab`; **Astra has not seen those fixes.** In round 56 Astra closed the findings on the smoke and on usage: the smoke uses the installed canonical agents and checks the product result independently, and the usage tests tell an unreported counter from an observed zero.

**What the step is for, and what it showed.** `tests/integration/test_feature_real.py`, run with `RELAY_REAL_CLAUDE=1`, delivers one small feature with every role a real run of the cheapest model on its installed canonical `alf-*` definition, within bounds the boundary suite has just shown to hold on the same installation. It then has Relay's own oracle run the program on the commit the feature's summary names and requires the right answer. It has passed four times, three of them with the canonical agents; in one of those a real reviewer sent a real build back, and the remediation, the second reviews and the integration followed with nothing scripted.

Four runs failed before the first pass, and each failure was a defect of Relay's that no scripted test could have found:

- **No real call could return a structured answer.** Relay hands each agent its own tool list, and that list left out the tool the CLI answers a schema with. Every reply was prose and was refused. Nothing through the adapter had ever produced a usable reply from the real provider: every test below this one scripts it.
- **An agent definition with no description is refused by the CLI, after launch.**
- **Offered two reply forms, a real model fills in both.**
- **Replies were refused for what was not in doubt**: an empty list left out, fields filled in empty beside a plain statement, an unchanged contract restated.

Deliberate breakage: 13 breakages of the step as first built, then 18, 7 and 10 against the three rounds of fixes. Of the last 10, all were caught, each by the test written for it. The whole suite passed one commit before the record: 1223 tests, 3 skipped (the ones that make paid calls). Paid calls for this step, all on the cheapest model: eight runs of the smoke, each preceded by two runs of the boundary suite, and five single probes of the CLI. The four passing runs cost 1.6, 2.8, 2.6 and 3.2 US cents by the features' own summaries; I did not total the failed ones, and estimate the whole at under fifty cents.

Decided during the build:

- **One reply form for every kind of work**: `outcome`, what was delivered under `delivered`, or one question, and what was assumed. Each call is offered that form alone. A build's or a review's task still reads the flat form those two had before; no stored reply needs it.
- **A reply whose meaning is plain is taken; one that could be read two ways is refused.** A field a call cannot use is not offered in its schema, because a real model fills in what it is offered. This is the rule between Astra's push towards strict validation and the provider's push the other way.
- **A feature's summary states usage as known when it completed**, by role, and what was not reported is `unknown` and never zero: a total is a total only if nothing that goes into it is missing. `Features.usage_of` is the same account read now.
- **What is launched is the installation there was when the adapter was made**, every link followed, and that file and what it says its version is are part of what a boundary pass is of. The run's own start event is held to that version.
- **A late report from a call settles its own record**: nothing of a successor's, and only a grant that is its own.
- **A role's profile carries a cap on what one call may cost**, enforced by the CLI.

Reservations:

- **Astra has not reviewed the round 56 fixes.**
- **Four passing paid runs, on the cheapest model.** Not a rate, and nothing about another model.
- **One feature, one story, one behaviour, and a person who approves everything.** Rejection, a proposal sent back, a replacement and a question answered are step 3's scripted tests, not the smoke's.
- **The remote policy is stated by the test** (D-10): a launch condition, and nothing about the provider's servers.
- **The executable is bound by its path with every link followed.** An installation replaced in place at the same path with the same version string is not detected; one replaced with another version is caught only when the run reports that it has started, after its briefing was handed over.
- **The boundary suite's markers were written against Claude Code 2.1.330 on macOS**; the installed CLI became 2.1.331 during this step and the suite passed on it each time the smoke ran.
- **Two review effects already admitted are not forced into the order that once deadlocked.** That rests on the gate being taken exclusively.
- **One timing-sensitive unit test** (a Git command stopped for taking too long) failed once under load during a breakage run and passes on its own.
- **The smoke is not part of `make check`**: it costs money and needs the CLI signed in.

### Slice 3b, closed

Four steps, each reviewed in three rounds, none accepted: 28, 28, 36 and 26 findings, 118 in all, every one reachable through a real entry point and every one fixed with a test, except 3B-S2-3.7 (a duplicate refusal on record after a pre-launch green refusal), which is open. The last round of each step's fixes was not reviewed.

What the slice set out to do, it does: workers and the Claude adapter drive the canonical agents through a whole feature, with Relay deciding every transition and observing every red, green and check itself, and token use captured on every call and stated in the feature's summary. That is shown against the real provider, four times, on one small feature.

What it does not do is in the reservations of each step. The ones that bear most on using it: the worker processes, the pass and the control plane are one process here, and wiring them across processes is 3c's; nothing runs on a timer; and a project's admissions and marks are serialised behind one lock whose cost has not been measured.

## Slice 3c, design (the processes, the command line and the Product Owner's part)

| Round | Astra's disposition | What it found |
|---|---|---|
| 57 | CHALLENGE, 11 | A build's worker unable to finish its own apply once it has no Docker and no push credentials; a control plane that lost its lock still acting; a pass for one project doing another's work; a second project governed by the first one's file; the operator's statement an editable field; a prepared retry confirmed against a later refusal; no handle to replay the first command; two human decisions missing from the inbox; live usage already aggregated away; no stated way to bound the session's tools; a final smoke that never starts the session |
| 58 | CHALLENGE, 9 | A known result whose worker is gone or idle, owned by nobody; a rejected green with its Builder alive and nothing to announce; start-up cleanup under an old term; transport not bound to the installation; a paid probe bought again after a lost reply; no rule for which project a command means; a prepared resume confirmed against a later pause; a session inventory check with no channel; analyses and Interpreter questions lost with the conversation |
| 59 | CHALLENGE, 8 | A push's outcome lost across a change of term; a running worker left on another Redis; attempt refs shared by two installations; a running probe taken for an uncertain one; a prepared decision on a parked call decided otherwise; a conversation treated as one invocation; a note's answer not recorded; "not delivered" where recovery from the record would serve |

Twenty-eight findings. **The design was not accepted.** Round 59's eight are folded into the build contract (`round-60`), which Astra did not review; Astra asked for exactly that, and said the build should begin. Astra accepted along the way: the installation as a row in the database; the lossy wake with a periodic pass; a retry bound to the particular refused push; the typed inbox; the usage calculation; and the passphrase read from the person's own terminal as reasonable for the stated single-host model.

Three things the review changed in the design's shape. A worker cannot finish a build once the processes are split, so effects say whose they are and the control plane takes a returned build. Holding a lock does not stop a former control plane, so a control plane has a term and every transaction of its that changes workflow state is fenced by it. And Relay hosts the Product Owner's conversation itself, a turn at a time, because the provider's interactive launch gives no account of what tools a session has.

## Slice 3c, step 1a as built: processes and recovery

| Round | Astra's disposition | What it found |
|---|---|---|
| 61 | CHALLENGE, 10 | A second push after Git's refusal was on record; the refusal lost at shutdown; the outbox publisher acting after its term; one unavailable repository stopping the installation's start; a worker launching after a change of Redis or a stop; a worker without its lock connection consuming commands for ever; a call admitted under an obsolete policy statement; a listener failure stranding leadership; an uncapped paid call; the handover test ending other sessions |
| 62 | CHALLENGE, 8 | A refusal committing during the send's authorisation; a send authorised and never launched; the stop and the transport checked before the briefing was prepared; a drain returning after the only write failed; an unavailable repository taking another project's worker; an unavailable Redis ending both processes; a former leader overwriting its successor's marker; policy changing between the admission read and its commit |
| 63 | CHALLENGE, 5 | A paid reply whose record failed, stranded under a live worker; a send left outstanding when the commit's answer was lost; shutdown waiting for ever before the drain's deadline; a stop during the last check before a call; a workspace leaked by every failed preparation |

Twenty-three findings, every one reachable through `relay up` or `relay worker start`, every one fixed with a test. **The step was not accepted.** Round 63's five were fixed without a further round, as D-8 has it, and those fixes are not reviewed. Astra said in round 63 that it considers the ordering of a refusal against a send's authorisation closed, and the fences on the policy read and on letting an effect go as asked for.

What the step does. Relay runs as separate processes: one control plane (`relay up`) and any number of workers, one role each (`relay worker start --role R`), with nothing in common but PostgreSQL, Redis and the filesystem. The installation is a row in the database. One control plane holds it at a time, by a lock, and each opens a term; a control plane whose term is over is refused in the transaction that would change anything, and stops. A build's publication and its green check are the control plane's, and a worker hands them on. A worker wakes the control plane, and a timer covers a wake that is lost. Each push has an identity, and what Git said to it is written down by whoever hears it, under the intent's lock and no other, and kept until it is written. A worker asks before every call, where the call is admitted and once more where the provider would be called, whether it is stopping, still itself, and on the Redis the control plane uses.

The proof the contract asked for is `tests/integration/test_processes.py`: a feature delivered by six operating-system processes with a stand-in CLI acting each role, the first control plane's lock connection ended from outside part-way with the process left alive, a second control plane finishing the feature, and the first stopping by itself.

Deliberate breakage across the step and its three rounds: 84 breakages, 66 caught at first. Of the 18 that were not, 10 had no test and got one, one led to the code being rewritten, two were aimed at text the formatter had rewrapped and were caught when aimed again, and five survive: four distinct breakages, one of them made in two rounds, each with a reason (the three fences below, and a second reading of an intent that the send's own check under its lock makes harmless to remove).

Reservations:

- **The fence is where the gate is taken.** Transactions of a control plane that write without taking the gate were found by a search and a reading, not a proof. Those found are fenced or left with a reason Astra accepted (giving a red back by its own grant; a final check's observation as evidence).
- **Three fences have no test**: marking an invocation applied, not letting an effect go after the term is over, and the control plane waiting to be listening before its first pass.
- **The process test replaces the control plane at one moment**, after the roadmap's approval. A change of term during a green check, during a push, with an outbox message pending and with a worker mid-preparation are each tested in one process.
- **A repository unavailable at start-up, and to an Analyst's worker, are tested with a stand-in for the pass and for the worker**, not with one project reaching trunk while another's waits.
- **Redis going away and coming back is tested with a client that raises connection errors**, not by stopping the server. The wrong-Redis history is tested with two real Redis databases.
- **A send stopped after Git was started stays unanswered**, as before: it may have left.
- **A worker stops only between calls**: told to stop during a call, it finishes the call.
- **The policy statement is compared as text** at admission; its revision is step 1b's, with the command that states it.
- **A call's cap defaults to two US dollars** when the installation's settings name none. The figure is Claude's.
- **Round 63's fixes are not reviewed.**

`make check` on the commit that closes the step: 1277 tests, 3 skipped (the ones that make paid calls).

## Slice 3c, step 1b as built: enrolment and its commands

| Round | Astra's disposition | What it found |
|---|---|---|
| 64 | CHALLENGE, 12 | A replayed boundary request buying a run the original never needed; two operators paying for one pass; the verdict losing what became of the paid call; a database made by step 1a unable to open (a shipped migration added to); an old passphrase authorising a policy change during a rotation; a partial first enrolment, and two overwriting each other; a secret in a refused address kept by the journal; `doctor` migrating the database and unable to report its absence; a trailing space lost from Git's output; settings a worker would refuse; no way to repeat a policy or passphrase request; a torn journal line hiding the next request |
| 65 | CHALLENGE, 11 | An uncertain call briefly an ordinary failure between two commits; a retry beside a run already live; a frozen request exercising another boundary; a refused request spending on replay; a refusal known before launch asking for a paid retry; one handle for a policy change and a paid request; one journal handle bound to two installations; a 1a policy breaking 1b's commands; an overridden address leaving its secret in the journal; `doctor` taking another branch for the trunk; half a digest accepted as an image |
| 66 | CHALLENGE, 8 | The runtime changing between two probes of one request; a late result restoring permission after its retry failed; one handle spent on two actions from two machines; a mistaken replay hiding the correct receipt; two first enrolments racing on an empty database; a workspace path with a space changing what the paid probe tests; a branch the workflow refuses accepted at enrolment; `doctor --json` losing its format on a bootstrap failure |

Thirty-one findings, every one reachable through a shipped command, every one fixed with a test. **The step was not accepted.** Round 66's eight were fixed without a further round (D-8) and those fixes are not reviewed. Astra accepted along the way: reusing a pass when the operator restates the policy in the same words; running an explicitly requested paid probe without the Product Owner's passphrase; the two writes left unfenced by the term in step 1a.

What the step does. `relay init` gathers and checks everything, then in one transaction sets what the installation has not been told, sets the Product Owner's first passphrase and registers the checkout's project; run again it changes nothing. The passphrase is kept as a salted hash and read from the controlling terminal and nowhere else; a command with no terminal decides nothing. `relay passphrase` changes it; `relay policy state` records the operator's statement as a revision, shown on the terminal before the passphrase is read. A command's project is the one it names, or `RELAY_PROJECT`'s, or the one checkout it is run in, and is never guessed. Every acting command takes a handle in the database for one action before it acts, writes it in the user's journal, and prints it; sent again under its handle a request is answered from its receipt. `relay doctor` reads and changes nothing, and says each thing that stands in the way with the command that puts it right, as text or JSON. `relay doctor --boundary` runs the paid suite for boundaries nobody has lately seen hold: the request is on record with all it asks before anything is run, the cost is said first, one run is made for a boundary at a time, what became of the paid call is kept beside its verdict in one transaction, and nothing is bought twice by asking twice.

Deliberate breakage across the step and its three rounds: 75 breakages, 65 caught at first. Of the 10 that were not: five showed a test missing or too weak, and each has a test that now catches it; three were not sound breakages (two could not run, one changed nothing) and were caught when made properly; two were aimed at text the formatter had rewrapped and were caught when aimed again. None survives.

One gate in this step was invalid and was run again: a breakage run on a very fast unit test had restored a source file within the same second and at the same size, and Python kept the bytecode of the broken version. The breakage script now writes no bytecode and clears the file's compiled copy, and every gate since has been run with compiled files cleared first.

Reservations:

- **Round 66's fixes are not reviewed.**
- **The passphrase is the whole of S3-1.8.** A pseudo-terminal is a controlling terminal; a model given one still needs the passphrase. A person who tells a model the passphrase has given their authority away, and nothing in Relay can tell.
- **`relay doctor --boundary` to a pass with the real provider is run by no test.** The runs are tested with a stand-in for the suite; the pre-launch refusal and a CLI that changes between two roles are tested through the command with a stand-in CLI.
- **The suite's own classification of a truncated provider stream** is not exercised by Relay's tests. Astra passed a recorded truncated stream through the adapter and the suite in round 66 and saw `uncertain` with a failed verdict.
- **Two commands taking one handle** is tested at the statement that takes it, not with a command held at its passphrase prompt.
- **Two first enrolments** are overlapped inside one process, and two migrations of an empty schema through two pools; not as two `relay init` processes.
- **A refusal is a request's answer for good.** A person who retried a moment too early makes a new request.
- **A result that turns up after its retry was admitted** is kept as of when its run started, which is what stops it authorising anything; its true time of observation is not kept.
- **`relay init` has no receipt**: it is safe to run again by construction.
- **Defaults Claude chose**: workspaces and mirrors under `/Users/Shared/relay/<installation>` on macOS and `/var/tmp/relay-<uid>/<installation>` elsewhere; the oracle's image `python:3.14-alpine`, pinned at enrolment; definitions looked for in `~/.claude/agents`; two US dollars as a call's cap when none is set.
- **The journal is a local file**: handles do not follow a person to another machine, though a handle taken there is honoured here.

`make check` on the commit that closes the step, run with compiled files cleared first: 1345 tests, 3 skipped (the ones that make paid calls).

## Slice 3c, step 2 as built: the Product Owner's commands

| Round | Astra's disposition | What it found |
|---|---|---|
| 67 | CHALLENGE, 14 | A code confirmed after it had expired; a code choosing its own project; another command's receipt printed as success; model-authored text driving the terminal at the confirmation prompt; a cancellation surviving a pause and a resume; a rejection prepared before a story is at work; a second retry of one refused push raising; a parked call's cost as one unlabelled figure, and its basis missing a late bill; a failed final check not showing what was observed; ambiguous item handles; no record of how a decision was confirmed, and a passphrase rotated under it; a generated feature name already taken; `status` listing work as waiting on someone who had acted; the text ledger dropping payloads and the default roadmap choosing the wrong feature |
| 68 | CHALLENGE, 11 | A database used by the previous build unable to upgrade (a pushed migration edited, again); one code reported expired and still deciding; a confirmation returning another action's receipt; exact replays from the previous build losing their receipts; a replay answered by the wrong installation; a cancellation surviving a final check retried and failed again; words accepted and then dropped or refused; a call shown as launched that may not have been; a final check's timeout unexplained; the default roadmap choosing a feature waiting its turn; control characters in the reading commands |
| 69 | CHALLENGE, 10 | An installation opened by the build pushed between the two forms of `0018` unable to upgrade; a receipt returned for another project, and feature names abbreviated; exact replays of `replace-decision FEATURE/Dn` and of text typed with space round it refused; "expired" said by a terminal whose update settled nothing; a direct replay missing a receipt committed while it resolved; a cancellation surviving a second completion request; extra words accepted for feature actions and replacements; installation commands able to take a confirmation's request name; an unchanged prepared action stale after upgrade; `status` sending the operator to a worker for control-plane work |

Thirty-five findings, every one reachable through a shipped command, every one fixed with a test. **The step was not accepted.** Round 69's ten were fixed without a further round (D-8) and those fixes are not reviewed. Astra said along the way that it found no stale-answer defect in `answer` and `replace-decision`, whose services check their own target, and no failure that depends on three kinds of item waiting together.

What the step does. Everything waiting for the Product Owner is one list of five kinds of item (a gate, a failed final check, a question, a parked call, a refused push), each with what there is to see, what can be done, an unambiguous handle, and a digest of what was shown. `approve`, `request-changes`, `reject`, `retry`, `set-aside`, `answer`, `replace-decision`, `pause`, `resume`, `cancel` and `feature start` each resolve what they are about from the record, refuse a word the decision has no place for, show it on the controlling terminal with whatever a model or a repository wrote made safe to show, ask for the passphrase with a prompt that says in Relay's own words what it is for, and carry the decision out under a handle taken first. The service holds each decision, in its own transaction, to what was shown: a gate by its identity, a parked call by all that was shown of it, a refused push by its intent and by the behaviour still being blocked on it, a story by its revision, basis and state, a feature by its state and how far it has come. The receipt records that the passphrase was read at the terminal and which passphrase, and a passphrase changed since it was checked refuses the decision. A request sent again is answered from its own receipt, which says what it was for, with no passphrase asked. `prepare` needs no authority and decides nothing; `confirm` is the person's, shows what was prepared and why, is stale if what it was prepared against has moved, and takes the code once, by the database's clock, after which the code is settled by that confirmation alone. `status`, `inbox`, `show`, `roadmap` and `ledger` read and change nothing, as text or `--json`.

Deliberate breakage across the step and its three rounds: 86 breakages, 77 caught at first. Of the nine that were not: six showed a test missing or too weak, and each has a test that now catches it; two were aimed at text the formatter had rewrapped and were caught when aimed again; and one broke a guard that could not matter, which was removed. None survives.

**A mistake made twice in this slice, and what was done about it.** A migration that had been pushed was edited in place, in step 1b and again in step 2, so an installation made by the pushed build could not upgrade. The second time left three forms of one migration in history, one of them in a build pushed in between. Every migration is now pinned by content in a test that also fails if a migration is missing from the list, and the migrator recognises the one in-between form by its exact checksum and brings it forward.

Reservations:

- **Round 69's fixes are not reviewed.**
- **The passphrase is still the whole of S3-1.8.** The receipt says which passphrase confirmed a decision, not who typed it.
- **A code is taken in a transaction of its own, immediately before the decision, not in the decision's.** The decisions are made by five services. A code taken and never carried on stays taken, unexpired and undecided, and is listed nowhere as waiting.
- **Two terminals with one code, and two identical decisions sent at once**, are each tested in one process, by making the second one's first look for a receipt come back empty; not as two processes held at a lock.
- **Three kinds of item waiting together after a restart** is tested as two together (a refused push and a parked call) and one apart (a failed final check), with new service objects standing for the restart.
- **An action prepared by an earlier build is held to the feature as that build described it**, which is a weaker description than this build's: such an action can survive a change that this build would have caught.
- **The list of pinned migrations is kept by hand**, in the commit that first pushes each.
- **A feature's name is `F` and the next free number.**
- **`relay ledger` shows at most two hundred events from where it is asked to start.**
- **Making a line break visible** puts a long statement of a problem on one line of the terminal.

`make check` on the commit that closes the step, run with compiled files cleared first: 1406 tests, 3 skipped (the ones that make paid calls).

## Slice 3c, step 3 as built: watching

| Round | Astra's disposition | What it found |
|---|---|---|
| 70 | CHALLENGE, 10 | The live account losing spend by role (PO-8) and a model's charge; a charge nobody reported read as zero, and a launch that never happened missing from the text; an open question shown as waiting no time, and two reviews sharing one age; no holder on the board, and what waits for the Product Owner given as a count; one frame of the board made of two moments; a frozen estimate with no price table; one look at the events ending at event 500; a model's name able to drive the terminal, and a screen clear before JSON; an account given for a feature that does not exist; an interval of nothing accepted |
| 71 | CHALLENGE, 8 | A worker reported missing during a healthy red run, and after a reviewer asked; "nobody is making this call" said of a call that may be running; a model charge that is no amount breaking the whole cost view; a frozen estimate meaning one thing in text and another in JSON; the frozen half of a comparison knowing more than the half labelled now; two workers with one displayed name; a role with only an uncertain call missing from the account; an interrupt ignored while the events are drained |
| 72 | CHALLENGE, 6 | Recovery and liveness inferred from the wrong process's lock; "held for" absent through a red run and counting time nobody held the work; Relay's estimate not shown by model; idle workers missing from the text board; a frozen summary's text dropping coverage that is known; an absurdly large model charge making the account unreadable |

Twenty-four findings, every one reachable through `relay watch` or `relay costs`, every one fixed with a test. **The step was not accepted.** Round 72's six were fixed without a further round (D-8) and those fixes are not reviewed. Astra said in round 72 that it found no redraw defect.

What the step does. `relay costs [feature]` and `relay watch --tokens [--feature F]` are one live account, read from the record of each call: every call counted once however many models it was billed to; each role's own calls, tokens, charges and estimates, with its calls in flight and its calls whose end is not known beside them; each model's tokens, what the provider attributed to it and what Relay estimates for it; what the provider charged for whole calls, said to be unknown when no call reported one and a part when only some did; and Relay's estimates grouped by the price table that made them and labelled estimates. `relay costs F1 --summary` shows beside it what a completed feature's summary froze, read first, in one projection for text and JSON, with estimates by table. `relay watch` is the board: whether a control plane is running; every worker there is a record of, by a name no other shares, with whether its lock is held; each feature with its stories and behaviours; each call in flight; each piece of work owed with its own age, whose grant it is under, whether that process's lock is held, and how long the present hold has lasted; and each item waiting for the Product Owner with its handle. Everything of the workflow in a frame is read from one snapshot. `relay watch --events` follows the ledger, each event once, whole however long, and stops when told. All of it reads and never writes; whatever a model or a provider wrote is made safe to show in text and is whole in JSON.

A hold is timed by the table itself (migration `0020`): whichever route grants the work, a hold that is broken off and resumed starts again, and nothing that updates the row can give it another start.

Deliberate breakage across the step and its three rounds: 69 breakages, 60 caught at first. Of the nine that were not: six showed a test missing or too weak, and each has a test that now catches it; one was aimed at text the formatter had rewrapped and was caught when aimed again; one broke a check that could not matter, which was removed; and one was reported caught when it was not, because the test that failed under it was failing already: a field had been added to a call's record without updating a test that compares the record whole, and the full run showed it. That test now has the field and catches the breakage. None survives.

Departures from the contract: the board is plain text and not the earlier Relay's `rich` tables (the design's own instruction, against the architecture's "port the rendering"); the earlier Relay's lines of each agent's activity are not ported, since Relay 3 keeps the call and its result and not an agent's working output.

Reservations:

- **Round 72's fixes are not reviewed.**
- **The board says whose grant work is under and whether that process's lock is held; it does not say who is carrying out an effect at this moment.** Astra asked for the effect's owner or term to be shown. They are not: a Builder that returned its candidate and left shows as a grant whose worker is gone while the control plane verifies it, and the board draws no conclusion.
- **The histories Astra asked to be played are staged.** A red granted to the control plane, a reviewer's work left as asked, a hold resumed, and a summary with two price tables are put in place by SQL on real claims and a really completed feature. A real green held open after its Builder exits, a provider held open while only the ownership connection is cut, a late result after a measurable park, and a feature delivered across a change of price table are not tested.
- **Liveness is not of a frame's snapshot.** A lock is held at this instant or it is not.
- **Redrawing over time is tested as one frame** to a terminal-like output.
- **Work already held when `0020` was applied has no held time.**
- **A model's estimate is kept from this build on.** For calls recorded before, the model shows how many of its parts have no estimate on record; it is not worked out again by today's prices.
- **The board lists every worker there has ever been a record of.** On an installation that has run for long that is a long list.
- **The board polls every two seconds.** What comes and goes between two readings is in `--events`.
- **A conversation's turns are not in the account**: they are step 4's.

`make check` on the commit that closes the step, run with compiled files cleared first: 1434 tests, 3 skipped (the ones that make paid calls).

Conditions carried to 3b: a principal on every worker-callable operation; durable task and invocation identity before any provider is called; a worker receives the story and list revisions with what it reviews and hands them back unchanged.

All replies are from `gpt-6.2-astra`. Agreement between two models is not evidence of correctness.
