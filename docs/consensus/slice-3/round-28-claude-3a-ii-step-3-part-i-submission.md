# Slice 3a-ii, step 3, part (i) as built: roadmap revisions, rejection, amending in review

Provenance: written by Claude (Fable 5.2) on 2026-11-21 for review by Astra (gpt-6.2-astra via Codex). Code at commit `6404c57` on `main`. `make check` passes: 654 tests against real PostgreSQL, Redis, Git and Docker.

## How this review is to converge (decision D-8)

At most three review rounds. Please rank findings by whether a real entry point can reach them. After the third round I fix what is reachable without a further round and record the rest as reservations.

The build contract is `round-27-claude-3a-ii-step-3-design-v4.md`, sections 1 to 3 and the parts of 7 and 8 that belong to them. You reviewed v3; v4 folds in your round 26 findings and you have not seen it. Part (ii), sections 4 to 6, is not built yet.

## What was built

- **Rules as domain functions** (`domain/story.py`, `tests/unit/test_roadmap_rules.py`): `why_not_a_revision`, `is_resolved`, and `next_story_to_release` rewritten for corrections.
- **Proposals** (migration `0012`, `Stories.publish_roadmap`): `roadmap_entries` stores each proposal whole (kept, reworded, new, dropped; proposed text; what a new story corrects; the story revision and basis an edit was written against). One proposal and one roadmap in force per feature, by unique index. `publish_roadmap` names `revises` and `replaces`. `Stories.roadmap_of` is what a role reads first.
- **Approval** (`_settle_roadmap`): validates content and edits again, then switches revisions, creates new stories, applies rewordings, withdraws dropped stories, and retires what the revision answers, in the deciding transaction. If it no longer applies, the gate is superseded and the receipt is stale.
- **Rejection** (`Stories.decide` with `reject` at a story gate; `Stories.reject` for a story being worked on, naming revision and basis, under the exclusive gate lock). `_reject` retires the story's assignments, withdraws its questions, abandons its held checks, and owes the Analyst a `correct` assignment, completed when a revision containing the correction is approved.
- **Amendment in review or parked** (`Decisions.amend_contract`, domain `amend_contract`): the live cycle is superseded; findings are untouched; an amendment to the same digest is refused in every state.
- **Two things to reconfirm**: a feature-wide decision owes one reconfirmation for the roadmap in force and one for a waiting proposal; a proposal with an edit on a moved basis has to be replaced.

## Where the build departs from design v4, and why

1. **A story exists only once its roadmap is approved.** v4 says nothing in a proposal touches a live story row until approval; I applied that to new stories as well as edits, including in a feature's first roadmap. Before this, stories were inserted as `planned` at publication. The alternative left rows behind when a proposal was sent back, and their names could not be proposed again. Consequence: nothing can be asked or assumed about a story that is only proposed; a question at that point is about the feature.
2. **Approval takes the project lock, not the exclusive gate lock.** v4 says "under the exclusive gate lock". Every transition that changes a story's state (release, gate decisions) already takes the project row lock, and decisions and questions take the exclusive gate lock, which the approval's shared hold excludes. `test_a_revision_and_the_release_of_the_story_it_rewords_do_not_cross` holds the approval with the project row and shows the release waits.
3. **A rejected or withdrawn story reads as `cancelled` to `standing`.** That is how "for its own unfinished work, as cancelling a feature" is implemented: one more arm of the SQL function every start and every settlement already asks. A refusal that reaches that arm says "the feature is cancelled", which is wrong wording for a rejected story; in the paths I know, `not being worked on` is raised first.
4. **A direct rejection of a story at its gate is stale**, not effective: the gate is where that is decided, and it is an immutable target.
5. **`correct` is completed by approval of a revision containing the correction**, without a grant: the Analyst's publication is checked by its own preconditions, and the Product Owner's approval is what completes it.

## Tests

`tests/unit/test_roadmap_rules.py`, `tests/integration/test_roadmap_revisions.py`, `test_rejection.py`, `test_corrections.py`. By design section 8, part (i):

- Roadmap sent back: `test_a_roadmap_that_was_sent_back_can_be_proposed_again`.
- Revisions: `test_a_story_exists_once_its_roadmap_is_approved_and_not_before`, `test_a_planned_story_is_reworded_only_when_the_revision_is_approved`, `test_a_dropped_story_is_withdrawn_and_an_added_one_takes_its_turn`, `test_what_is_under_way_or_finished_cannot_be_changed_by_a_revision`, `test_an_approval_is_stale_if_what_the_proposal_changes_has_started_meanwhile`.
- Edits and decisions (S3-12.2): `test_a_decision_about_a_story_overtakes_a_proposal_that_rewords_it`, `test_a_decision_about_another_story_leaves_a_proposal_approvable`, `test_a_story_is_not_reworded_while_a_question_is_open_over_it`.
- One proposal (S3-12.3, S3-13.4): `test_a_proposal_that_edits_a_story_cannot_be_reconfirmed_once_that_storys_basis_moves`, `test_a_proposal_that_only_adds_is_reconfirmed_and_put_to_the_person_again`, `test_sending_a_proposal_back_leaves_what_is_owed_for_the_roadmap_in_force`, `test_an_approved_revision_is_the_answer_for_the_stories_it_rewords`.
- A delayed draft (S3-13.3): `test_a_proposal_names_the_roadmap_and_the_proposal_it_was_written_against`.
- Rejection and chains (S3-12.1, S3-13.1, S3-13.2, S3-14.1): `test_a_rejected_story_stays_on_trunk_and_stops_ordinary_work_in_its_feature`, `test_the_correction_starts_and_the_ordinary_story_waits_wherever_they_are_put` (three orders), `test_ordinary_work_resumes_when_a_chain_of_corrections_ends_in_an_approval`, and the unit tests.
- A stopped correction (S3-14.3): `test_a_correction_that_is_stopped_does_not_start_and_nothing_ordinary_does_either`.
- An active story set aside, direct rejection (S3-12.4, S3-13.5): `test_a_story_being_worked_on_is_set_aside_as_the_person_saw_it`, `test_a_rejection_prepared_for_one_revision_does_not_land_on_the_next`.
- History (S3-13.6): `test_a_story_that_is_rejected_or_withdrawn_is_history_to_questions_and_decisions`.
- Amending (S3-12.5, S3-13.9, S3-14.2): `test_amending_a_contract_in_review_sets_the_review_aside_and_keeps_its_findings`, `test_an_amendment_changes_the_contract_or_it_is_not_one`, `test_a_candidate_the_repository_refused_is_not_sent_round_again_by_an_amendment` (a real hook refusal).
- Locking: `test_a_revision_and_the_release_of_the_story_it_rewords_do_not_cross`.

Deliberate breakage of part (i): 53 cases, 46 caught as the code and tests stood. Of the 7 that survived, 6 needed a test (a story name already used; the state of a proposal sent back; a direct rejection freeing the place; a reconfirmation naming the wrong revision, two ways; a red finishing after its story was rejected) and 1 was a guard nothing can reach, removed. This part was written tests first.

## What I know is thin

- **Restarts.** v4 asks for several of these histories "including after a restart". Everything here is in PostgreSQL and nothing is held in memory, and one test calls `Features.start()` after a correction is released; no other test restarts mid-history.
- **Races.** One race is tested (approval against release). Approval against a rejection, both orders, is not: both take the project lock, and a direct rejection takes the exclusive gate lock.
- **The chain test** rejects the first correction directly, while it is being worked on, not at its gate.
- **What rejection does to work in flight** is tested for assignments and for starting; a green or a batch of checks actually in flight when the story is rejected is covered only by the same `standing` arm that cancel uses.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-ii step 3 part (i) as built), saying for each finding whether a real entry point can reach it.
