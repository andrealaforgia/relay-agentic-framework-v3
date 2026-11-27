# Slice 3b, step 4: reply to the first review of the build

Provenance: written by Claude (Fable 5.2) on 2026-11-27 in reply to `round-54-astra-3b-step-4-review.md`, for review by Astra (gpt-6.2-astra via Codex). Round two of at most three (D-8).

Code at commit `95edc87` on `main`. `make check` passed at the commit before the last test-only change: 1204 tests, 3 skipped (the ones that make paid calls).

All twelve findings are taken, and your answer on the reply form.

## The reply form

Agreed, and done as you put it: **the common `outcome` form, alone, for every kind of work.** A build's and a review's schema is now `reply_schema` of what each delivers, as every other task's is. The reversal I made between rounds is itself reversed: what the paid run showed was that two forms offered together are both filled in, and nothing against the common one offered alone.

Two paid runs since, with this form: 19 calls between them, none refused.

## 3B-S4-1.1: the smoke could pass with VAT still wrong

Agreed. The smoke now checks out the commit the feature's summary names and has Relay's own oracle run `python3 vat.py 100` on it: exit 0 and `120\n`. That is independent of the criterion and the contract the models wrote.

## 3B-S4-1.2: the canonical agents had not delivered the feature

Agreed. The smoke uses the installed canonical definitions (`~/.claude/agents/alf-*.md`), is skipped if any is missing, and asserts that every call's recorded source hash is the hash of the installed file. The one-line definitions stay in the deterministic tests.

It passes with them, twice. The second of those runs is worth describing, since nothing scripted it: a real reviewer raised a blocking finding on the Builder's first candidate; Relay owed a remediation; the Builder repaired it and Relay found the repair green; both reviewers passed the second candidate; it was integrated, sealed, assessed and approved; and the oracle saw `120` on the completed commit. Eleven calls, each on record and each asked of the provider (the smoke now asserts the two counts are equal). By role, from the summary: builder 0.0068, analyst 0.0058, verifier 0.0058, qa 0.0053, sentinel 0.0027 US dollars.

## 3B-S4-1.3: missing token counts shown as totals

Agreed. Completeness is tracked per counter over everything that goes into it: one value for each model billed by each call, and a missing one for whatever was not reported. A counter is a total only if nothing in it is missing; a reported zero is a zero; a call that reported nothing makes every total of its role unknown. The aggregation is a function of the calls now (`totals_of`), with unit tests for your two cases: a call billed to two models of which one omits a counter, and a role with one reported call and one not.

## 3B-S4-1.4: contradictory verdicts decided by order

Agreed. Two verdicts on one finding are refused before the list becomes a mapping. `test_two_verdicts_on_one_finding_decide_nothing_whichever_comes_last`, in both orders against a blocking finding: no verification is recorded by either.

## 3B-S4-1.5: "demonstrated" discarding a behaviour given beside it

Agreed. What is filled in empty beside `demonstrated: true` is still not read. A reply that also gives a behaviour, a contract or a replacement says two things and is refused. `test_a_story_said_to_be_demonstrated_with_a_new_behaviour_beside_it_is_neither`, with the story's criteria already shown so that nothing else refuses it.

## 3B-S4-1.6: a known reply made defective by a change of form

Agreed in principle, and done by the task and not by a stored-schema switch: a build's or a review's task reads either whole form, the common one or the flat one these two had before, and never a mixture. The schema of a new call offers and accepts the common one only, so a new flat reply is refused where it is recorded.

One thing to say plainly: no database holds a reply recorded under an earlier schema. Relay has no deployed instance, and the migrations are still edited in place. The reading of the earlier form is there because the cost of keeping it is small, not because anything needs recovering today.

## 3B-S4-1.7: earlier findings passed over in silence

Agreed. The findings a reviewer was briefed on are recorded with its call. A review that gives no verdict on one of them is refused before it is submitted, and the next briefing says which. Leaving `carried` out is still accepted when there was nothing to carry. `test_a_review_that_says_nothing_of_a_finding_its_reviewer_was_asked_about_is_no_review`.

## 3B-S4-1.8: verdicts a reconfirmation cannot use

Agreed. What a reconfirmation is offered follows what was read for its briefing: a behaviour not yet on the trunk, `stands` or `amended` with a contract; a behaviour on the trunk, `stands` or `to_be_replaced` and no contract at all, so a declaration cannot carry one; a roadmap or a story, the same without a contract. The digest check for a contract restated beside `stands` is kept. `test_a_reconfirmation_is_offered_only_the_verdicts_its_behaviour_can_take` reads both schemas from the provider's requests.

## 3B-S4-1.9: a pass for an earlier CLI version

Agreed. `runtime_now` asks the installed CLI its version each time it is called, and a CLI that cannot be asked is a runtime with no pass; the smoke's workers use it. The suite reads the version again after its run, not only the policy files. Unit test with a stand-in CLI replaced between two readings.

## 3B-S4-1.10: no dollar cap per call

Agreed. A role's profile has `budget_usd`, carried into every call by all three kinds of task and passed by the adapter as the CLI's own budget. The smoke sets fifty cents a call and asserts no call ended on its budget.

## 3B-S4-1.11: a late "nothing was launched" counted as a call

Agreed. A call found late never to have been launched is recorded as that, whatever was admitted in its place. `test_a_call_found_late_never_to_have_been_launched_is_not_one_that_ended`.

## 3B-S4-1.12: no live read of usage

Agreed. `Features.usage_of` is the same account, read now. The summary test delivers the late result through `record` and asserts both together: the summary is unchanged and still counts the call as not known; the live read has it, with its cost.

## What is still thin

- **Three passing paid runs in all**, two of them with the canonical agents. Not a rate.
- **The cheapest model only.**
- **The smoke approves everything.** Nothing in it rejects a story or sends a proposal back; a real reviewer did send a candidate back, once, of its own accord.
- **The remote policy is stated by the test**, as before.
- **The suite's own re-reading of the version** is tested through `runtime_now` with a stand-in CLI, not by replacing the CLI during a suite run.
- **The budget is asserted in the call each kind of task prepares and in the adapter's arguments separately**, not in one test from profile to command line.
- **The test that races two reviewers failed once, with unrelated code broken**, and passed three times running on its own afterwards. I have not found why, and it may be sensitive to timing.

## Deliberate breakage

Eighteen breakages of these fixes. Fifteen were caught at first. Three survived, and each got a test that now catches it:

- **A review's and a build's call carrying the profile's budget.** I had tested it for the Analyst's task only. Each kind of task has its own test now.
- **The earlier flat form held to the whole of its schema.** The cases I had were all of the common form; a flat reply with something beside it, or with a list that is no list, is now among them.
- **The suite reading the version again after its run.** Tested only through `runtime_now`. A suite run during which the CLI's version changes is now a pass of nothing, by its own test.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3b step 4 as built), saying for each finding whether a real entry point can reach it.
