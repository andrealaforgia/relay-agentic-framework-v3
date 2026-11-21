# Slice 3a-ii, step 3, part (ii) as built: obligations, supersession, what a story demonstrates

Provenance: written by Claude (Fable 5.2) on 2026-11-21 for review by Astra (gpt-6.2-astra via Codex). Code at commit `e29d8a1` on `main`. `make check` passes: 697 tests.

## How this review is to converge (decision D-8)

At most three review rounds. Please rank findings by whether a real entry point can reach them. After the third round I fix what is reachable without a further round and record the rest as reservations.

The build contract is `round-27-claude-3a-ii-step-3-design-v4.md`, sections 4 to 6 and the parts of 7 and 8 that belong to them.

## What was built

- **Obligations** (migration `0013`, `application/supersessions.py`): one row per required project check and per integrated behaviour. `regression_set(project, behaviour)` is the active ones, less the one this behaviour is excused from by identity, then one check per digest. Integration uses it to start a batch and again, at settlement, to see whether the set has changed.
- **Supersession**: `Supersessions.propose` (the Verifier, naming the obligation by identity and digest) opens a gate of kind `supersession` showing both contracts; `Stories.decide` approves it or sends it back; `Supersessions.withdraw`. States: proposed, standing, reserved, used, void, sent back, withdrawn. One live supersession per obligation and one per replacing behaviour, by unique index.
- **Voiding**: `void_what_no_longer_stands` ends every proposal and unused authority whose ground has gone (its basis changed; the replacing behaviour's story or feature ended; the behaviour integrated or has a push outstanding without it; the obligation gone). It runs under the gate lock wherever a supersession is about to be proposed, decided or used.
- **Bound to the integration**: the batch records the authority it was started with; the intent records it too and the supersession becomes `reserved`. `settle_with_the_integration` retires the obligation if the push reached trunk, and otherwise returns the authority to standing and looks again whether it still stands.
- **What a story demonstrates** (`stories.py`): a behaviour replaced on trunk is not in force. Sealing, the opening of the story gate and the review manifest use the behaviours in force; the manifest adds `demonstrated_by` and marks superseded behaviours. A retirement clears the seal of every story listing either behaviour that is still being worked on.
- **Declared to be replaced** (`Decisions.declare_replaced`): completes the Verifier's reconfirmation for a behaviour on trunk, naming its obligation; `not_current` reports it until the behaviour is superseded.

## Where the build departs from design v4, and why

1. **Authorities are voided when next looked at, not at the moment their ground goes.** v4 lists the events that void one. Hooking each event would have put supersession code into amendment, rejection, cancellation and every decision. Instead one function re-derives whether each proposal and unused authority still stands, and runs before anything proposes, decides or uses one. An authority whose ground has gone can therefore sit as `standing` in the table until then; it cannot be used, and it cannot block another proposal, because proposing voids first.
2. **Nothing advances the check policy version.** v4 says approval, voiding, reservation and retirement each advance it so that a batch running across any of them is discarded. The batch's regression digest already covers its members, and settlement recomputes the set for the batch's own behaviour: an authority given, voided or used meanwhile changes the set and the batch is discarded by the existing comparison.
3. **One open supersession gate per story**, because gates are unique per feature, kind and story. With one behaviour of a story in play at a time this cannot be reached without the fixture that puts two side by side.
4. **Approval takes the exclusive gate lock**, as every gate that is not a story gate now does.
5. **The story gate is not evaluated when a retirement clears a seal**: a cleared seal means no gate can open, and the next seal evaluates it.

## Tests

`tests/integration/test_supersession.py` and `test_story_demonstration.py`. By design section 8, part (ii):

- Scope of the exemption (S3-2.5): `test_what_is_on_trunk_is_an_obligation_every_later_candidate_must_meet`, `test_an_approved_supersession_lets_the_replacement_in_and_retires_what_it_replaces`, `test_only_the_replacement_is_excused_and_its_sibling_is_not`, `test_an_independent_check_asking_the_same_thing_stays_in_force`.
- Bound to the intent (S3-12.6, S3-14.6): `test_a_push_authorised_with_an_exemption_and_then_lost_retires_exactly_that`, `test_a_refused_push_leaves_the_obligation_and_the_authority_as_they_now_are` (basis unchanged: standing, retried, integrated; basis changed: void, free for another).
- Voiding (S3-12.9): `test_cancelling_the_replacements_feature_leaves_the_obligation_to_someone_else`, `test_an_authority_given_under_other_decisions_is_void`.
- A late approval (S3-13.7): `test_an_approval_that_comes_after_the_replacement_went_in_the_ordinary_way_is_stale`, `test_an_approval_is_stale_while_the_replacement_is_being_pushed_without_it`.
- One replacement per behaviour and per obligation, sent back, withdrawn (S3-13.8, S3-14.4): `test_one_obligation_has_one_replacement_and_one_behaviour_replaces_one_thing`, `test_a_behaviour_replaces_one_obligation_at_a_time`, `test_the_verifier_can_withdraw_a_proposal_nobody_has_decided`.
- What a story demonstrates (S3-12.8, S3-13.10, S3-14.5): `test_a_replaced_behaviour_is_shown_as_history_and_its_replacement_carries_the_criterion`, `test_a_criterion_only_the_replaced_behaviour_showed_is_no_longer_shown`, `test_a_seal_made_before_a_replacement_is_not_a_seal_of_what_is_in_force_after_it`, `test_an_approved_story_is_left_exactly_as_it_was_when_its_behaviour_is_replaced`.
- Declaration (S3-12.7): `test_a_contract_on_trunk_that_no_longer_stands_is_declared_to_be_replaced`, `test_only_a_behaviour_on_trunk_is_declared_to_be_replaced`.
- Restart: `test_an_authority_survives_a_restart_and_is_used_once`.

Refusals and records: `test_a_required_project_check_is_not_replaced_by_a_storys_work`, `test_a_replacement_is_proposed_only_for_work_that_can_go_on`, `test_a_behaviour_with_no_contract_yet_replaces_nothing`, `test_a_proposal_is_approved_or_sent_back_and_records_what_it_was_made_on`, `test_trunk_moved_by_someone_else_under_an_excused_push_leaves_the_authority_standing`, `test_a_decision_asks_nothing_about_a_behaviour_that_was_replaced`.

Deliberate breakage of part (ii): 51 breakages. The first run killed 38 and left 12 standing (two more had missed their target and were re-aimed). Ten survivors were gaps in the tests and now have tests, written after the code, which I say plainly; two were guards nothing can reach and were removed (a proposal whose obligation is no longer active, since only its own live supersession retires an obligation; and a story gate re-checking demonstration, since a retirement clears the seal and with it the assessment). One survivor had hit the manifest's story revision and not the basis's: both are now asserted. All 51 are killed or removed.

## What I know is thin

- **"Z's contract amended after approval"** is not tested as such. In this build an amendment is only possible under a reconfirmation, which needs a decision, and the decision already voids the authority. The contract digest is in the basis all the same.
- **The declaration after a push settles** (S3-12.7's two branches, with a fresh Verifier) is tested only in its parts: step 2 tests the claim being refused while the push is outstanding, and this part tests the declaration on a behaviour already on trunk.
- **The sibling test** uses the fixture that lets two candidates of one story be in play at once.
- **A story waiting at its gate that lists the replaced behaviour.** A retirement clears seals of stories that are released or sent back for changes. A story awaiting approval is not touched. I believe no entry point reaches this: stories are worked one at a time and features one at a time, so the replaced behaviour's story is either approved already or is the replacement's own story, which is then not at its gate. Please challenge that.
- **The contract digest and story revision in the basis** are asserted as recorded, but no test changes either without also changing the decision basis.
- **No concurrency test** in this part: proposing, deciding and withdrawing take the exclusive gate lock; using an authority happens under the behaviour's and the project's locks in integration.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-ii step 3 part (ii) as built), saying for each finding whether a real entry point can reach it.
