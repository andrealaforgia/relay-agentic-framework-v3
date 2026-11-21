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

Conditions carried to 3b: a principal on every worker-callable operation; durable task and invocation identity before any provider is called; a worker receives the story and list revisions with what it reviews and hands them back unchanged.

All replies are from `gpt-6.2-astra`. Agreement between two models is not evidence of correctness.
