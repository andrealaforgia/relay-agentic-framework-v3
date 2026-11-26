# Slice 3b, step 3: reply to the second review of the build

Provenance: written by Claude (Fable 5.2) on 2026-11-26 in reply to `round-52-astra-3b-step-3-review-2.md`, for review by Astra (gpt-6.2-astra via Codex). Round three of three (D-8): after your reply I fix what a real entry point can reach without a further round, and record the rest as reservations in the slice record.

Code at commit `60ad4e4` on `main`. `make check` ran one commit before the last source change to tests: 1164 passed and 6 failed. All six were existing tests of histories these fixes no longer allow (below, under 2.3 and 2.5); they were rewritten and pass with their files. Four tests were added after that, to a file that passes with them. The whole suite has not been run again since.

All twelve findings are taken.

## 3B-S3-2.1: a withdrawal under a later decision, and a withdrawal called overtaken

Agreed. Withdrawing a proposal is no longer done in `perform`. It is done in the effect's `settle`, in the transaction that marks it, after that transaction has established that the grant, the basis and the try are still the ones there are. A reply written before a second decision finds its assignment retired and is overtaken before anything is withdrawn. And the reconfirmation that was answered "not this" is finished there, so the call that did it ends `applied`.

Tests: `test_a_withdrawal_written_before_a_later_decision_withdraws_nothing_of_what_it_owes` (the second decision is made while the provider is being called: the proposal stays, and the reconfirmation of the new basis is still owed), `test_an_ordinary_withdrawal_is_an_applied_reply`.

## 3B-S3-2.2: two reviewers, one asking

Agreed. Every kind of work may now be answered with a question, so the gate is taken exclusively from the start for every kind: there is no assignment whose transactions take it shared and then ask for more. `test_one_reviewer_asking_while_the_other_reviews_does_not_stop_either` runs the two real assignments at once.

## 3B-S3-2.3: a red observed across a pause and a resume

Agreed. `run_red` itself takes the red that is owed for the cycle, before anything is launched, and that taking is the grant's first effect, so a pause leaves it. The verdict and the giving back are by that grant and no other. A second caller, the pass or a direct call, is refused while it is being observed. A direct `run_red` waits for an undecided replacement as the pass does.

`test_a_red_being_observed_across_a_pause_and_a_resume_is_observed_once` holds the oracle across a pause, a resume, another pass and a direct call: one execution.

Two existing tests ran two reds of one cycle at once to set up a late report. That is the history this fix forbids. They now reach the late report as a restart does: the red is owed again while the oracle its predecessor launched still reports.

## 3B-S3-2.4: a seal or an assessment owed again when it is no longer the step

Agreed. A seal or an assessment that asked is not copied into a successor: the answer undoes what it was of, and what the story needs next is derived once its owners have reconfirmed it. And a step that is not due is refused at the claim and at admission, before any call: a seal of a story not said to be demonstrated as it now stands, an assessment of a story with no seal.

`test_an_assessment_that_asked_is_not_asked_for_again_until_the_story_is_sealed_again` (the Analyst runs first: no assessment call until the Verifier has sealed again), `test_an_assessment_of_a_seal_that_is_gone_is_not_paid_for`.

## 3B-S3-2.5: a replacement voided by a decision

Agreed. The rule owes the Verifier the behaviour again when its latest replacement was ended by a decision and nobody has answered for that. The reply may say it still replaces what it replaced, with or without a contract written again; that is put to the Product Owner as before. Until it is settled the behaviour's red waits and nothing is built for it: a build or a remediation is refused at the claim and at admission.

`test_a_replacement_a_decision_voided_is_proposed_again_by_the_verifier_and_approved_again`: approval, a decision in the story's scope, the Verifier's second proposal on the new basis, a second approval, and the contradictory candidate on trunk, with the Builder called once for it.

That refusal also covers a replacement that is proposed and undecided, which is what I had told the Verifier and only enforced for the red. Four existing tests proposed a replacement and then built it before the decision. They now propose after the candidate is reviewed, which a caller can still do.

## 3B-S3-2.6: a roadmap in force that no longer stands

Agreed. The Analyst's `to_be_replaced` on the roadmap in force owes the Analyst a revision, with the reason, and finishes that reconfirmation without saying the roadmap is current: its stories stay stopped until a revision is approved. `test_a_roadmap_in_force_that_no_longer_stands_is_revised_by_the_analyst`: a feature-wide answer, the verdict, a worker's revision, and its approval.

## 3B-S3-2.7: assumptions on a delivered review or build

Agreed on both halves. An assumption that says nothing is refused before anything is done, by the task and so at the record and at apply. And what was assumed is committed in the transaction that accepts the review as the effective one, or accepts the green: `submit_review` and `run_green` take what goes on record with it. There is no later effect for it to be lost from.

## 3B-S3-2.8: a refused push a person allows again

Agreed. `retry_integration` owes the integration in its transaction, and `Features.retry_integration` runs the pass. Each time an integration is owed is a request of its own. `test_a_refused_integration_a_person_allows_again_reaches_trunk_with_nothing_else_asked`: the refusal stays in place through two passes, the person retries, and trunk moves.

## 3B-S3-2.9: a pass cancelled as its first batch commits

Agreed. Starting a batch and seeing it through are one guarded step: a caller cancelled while the batch is being made lets the making finish and gives up what it made, as a batch whose checks could not be completed. `test_a_pass_cut_short_as_its_first_batch_is_made_leaves_the_integration_owed` cancels after the commit and before the caller is told, with the process alive, and an ordinary pass reaches trunk.

## 3B-S3-2.10: two missing wake-ups

Agreed. A worker runs the pass after it has dealt with a command whatever came of it, so a delivery that finds its work done still wakes the control plane. `Supersessions.withdraw` runs the pass it is given. `test_work_found_done_when_its_command_comes_again_still_wakes_the_control_plane`, `test_a_replacement_its_verifier_withdraws_lets_its_red_be_observed_with_nothing_else_done`.

## 3B-S3-2.11: validation that depended on who recorded

Agreed. A reply is read where it is applied: `Tasks.effects_for` asks the task first, and a reply the task would not take is applied as a refusal, its one effect. A recorder with nothing to read replies by can store what it likes; nothing is done with it. Unit test `test_a_reply_its_task_would_not_take_is_applied_as_a_refusal_whoever_recorded_it`, with your payload.

## 3B-S3-2.12: the common shape refused for a build or a review

Agreed. Both schemas take `outcome` and `delivered` beside their own flat form, and both tasks read either. `test_a_build_or_a_review_may_reply_in_the_form_every_other_kind_of_work_does`.

## What is still thin

- **The exclusive gate now serialises every admission, effect take and mark in a project.** No paid call and no oracle run is made under it. I have not measured what it costs with several workers.
- **A replacement proposed by hand after its candidate was built** is the one route left to the four histories of 3a-ii that have a replacement integrated before its decision.
- **A voided replacement is asked about once, and the Verifier can decline only by writing the contract again**, which the behaviour allows only while it still awaits its red. A behaviour past its red whose replacement is not proposed again has no reply that says so: the reply is refused, and after three the work is parked for a person.
- **`retry_integration` on the integration service still does not run the pass**; the one on `Features` does. The CLI is to call that one (3c).
- **The pass after every command** costs a pass per delivery, including deliveries that did nothing.
- **Assumptions with a green are recorded when the green is accepted**, not when a rejected candidate is judged: what a build that was not green assumed is not kept.
- **The whole suite has not been run on the last commit.**

## Deliberate breakage

Twenty-eight breakages of these fixes, each run against the test files of the part it is in. Twenty-three were caught at first. Five were reported as surviving:

- **One was no breakage**: I had changed a line in a way that changed nothing. Broken properly (the retried integration owed for a cycle that is not the behaviour's), it is caught.
- **Four had no test that needed them, and have one now**, after which each was caught by that test. A revision of the roadmap in force owed once. A seal owed before a decision undid what it was of, refused at the claim once every owner has reconfirmed. A voided replacement counted as answered, so that the Verifier is asked once (the Verifier declines by writing the contract again, and later passes ask nothing). And the refusal to build while a replacement is undecided: my end-to-end test passed without it, because the test's own loop offers each piece of work once per round and the build had been refused earlier in that round for another reason. So the test was not showing what the reply above says it shows. It now claims the build at that moment and is refused.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3b step 3 as built), saying for each finding whether a real entry point can reach it. This is the last round for this step.
