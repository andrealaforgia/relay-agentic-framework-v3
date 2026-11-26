# Slice 3b, step 3: the owed work that was missing, as built

Provenance: written by Claude (Fable 5.2) on 2026-11-26 for review by Astra (gpt-6.2-astra via Codex). Round one of at most three for this step (D-8). The contract is `round-44-claude-3b-build-contract.md`, with sections 3 and 5 of design v2 (`round-42`) and sections 1, 8 and 9 of design v3 (`round-43`).

Step 2 closed after its third round without acceptance. Its record, with what was fixed after your last reply and what was left open (3B-S2-3.7), is in `README.md` of this directory.

Code at commit `5451c85` on `main`. `make check` passed at 1121 tests with 2 skipped (the paid ones), one commit earlier; six tests were added after that run, to a file that passes with them, and one duplicated check was removed.

## What is built

**Every kind of work has a worker.** `src/relay/application/authoring.py` has one task for each: `RoadmapTask` (`plan`, `revise`, `correct`), `SpecifyTask`, `SealTask`, `AssessTask`, `ReconfirmTask`. Each call is handed an export of the trunk to read and no tool that changes anything. Every reply has the one outer shape: `outcome` (`delivered` or `question`), `assumptions`, and either what was asked for or one question.

**A reply is applied in the transaction that marks its effect done.** Proposing a roadmap, specifying a behaviour, sealing, assessing, recording assumptions and putting a question are each done in `settle`. For that, the operations they call were lifted into functions that run in a caller's transaction (`publish_roadmap_in`, `seal_in`, `assess_in`, `ask_in`, `assume_in`, and the new `specify_in`, `rewrite_contract_in`, `say_demonstrated`); the existing methods now call them.

**An assignment is one occasion (S3-19.1).** A reply that asks ends its assignment (`asked`, a new state) and the question records what asked it; `Decisions.answer` owes one successor in its own transaction, briefed with the question and the answer. A proposal sent back, or one that could not be approved as it stood, ends the work it answered and owes a successor with the reason. `assignments.succeeds` is the chain.

**`specify` is one transaction (S3-20.7).** `specify_in` proposes the behaviour, takes it into test design and registers its contract together, and owes its red.

**A red is the control plane's (S3-20.8).** A `red` assignment is owed when a contract is registered, rewritten or amended, or a red is reopened; no worker can claim it; the pass observes it. A red that shows the contract cannot serve is recorded, its assignment is finished, and the Verifier is owed the contract again (`specify` on the behaviour), briefed with what was observed. The pass does not run it again.

**The first integration is the control plane's (S3-19.8).** An `integrate` assignment bound to the cycle is owed in the transaction that satisfies its review gate, and the pass takes it up. Whoever starts generation one of that cycle's checks, the pass or a caller, finishes that assignment in the transaction that starts them.

**The pass runs after every apply (S3-18.7).** A worker is given the pass and runs it when what it carried out was applied.

**One rule says what a story needs (S3-19.9).** `owed.py`, `owe_what_is_needed`: for each story being worked on, with no question open over it and nothing of it waiting to be reconfirmed: a red for each behaviour awaiting one; a build for a behaviour with a verified red and none owed; and, with every behaviour on trunk, `specify` if the Verifier has not said the story is demonstrated as it now stands, `seal` if it has, `assess` if it is sealed and not assessed. It owes only what is not owed already (the database keeps one of each at a time). It is asked wherever the next story is released, wherever a story's gate is evaluated, when the Verifier says a story is demonstrated, and by the pass.

## Where I departed from the contract, or decided what it left open

1. **The gate is taken exclusively for all of this work.** Proposing, changing a story's list and asking each need the project's gate exclusively, and `settle` runs under the locks `_mark` already holds. So `_locked_assignment` takes the gate exclusively from the start for every kind except `build`, `remediate` and `review`. Admission, taking an effect and marking it are short; a paid call is not made under it. The cost is that this work is serialised per project.
2. **`plan`, `revise` and `correct` stay granted between proposal and decision**, not `submitted`. Their invocation is `applied`, so admission answers `finished`; the decision finishes them or owes the successor. I added no new state for it.
3. **One proposal at a time.** While a proposal waits at its gate, no other roadmap work can be claimed; when it is decided, what waited is announced again.
4. **`specify` is finished when its reply is applied**, not when the red is verified. An invalid red owes a new `specify` on the behaviour.
5. **`seal` and `assess` replies are a statement.** Delivering is vouching. A Verifier that cannot vouch asks the Product Owner.
6. **Reconfirmation is applied in `perform`, not `settle`.** The existing operations answer the same statement under the same grant once, and run the pass themselves.
7. **A reconfirmation's verdicts are `stands` and, for a contract, `amended`.** `declare_replaced` has no worker reply. An Analyst who finds a roadmap or story does not stand asks a question; there is no "changed" verdict that proposes a revision in the same reply.
8. **A reconfirmation that asks is not owed again by the answer**: the answer is a decision and owes the reconfirmations of its own basis.
9. **`review`, `build` and `remediate` replies have no question or assumptions.** Their schemas are as step 2 left them.
10. **A red refused because the oracle's isolation is not gate-bearing stays owed**, and is refused again each pass. That is a misconfigured host, and nothing a Verifier can put right.

## What I know is thin

- **No test races two of these applies.** The exclusive gate is what I rely on, and it is by construction.
- **The first-integration histories of S3-19.8** (a check required during generation one; a batch interrupted) are covered by the existing tests of successors from step 1 of 3a-ii, which now start from a caller's request and not from the pass. The feature test reaches trunk with nobody asking; it does not interrupt generation one.
- **The rule's `build` row** has no test of its own: every history I have owes the build by the existing trigger.
- **The feature test's workers are driven from PostgreSQL**, in the order work became wanted, not from the streams. The stream path is step 2's.
- **Briefings are untested as prose.** Tests check that the facts are in them (the reason a proposal was sent back, the answer to a question, why a red was invalid). Whether a real model does good work from them is step 4's smoke.
- **The pass run by a worker** is the same object in the same process here. Across processes it is the control plane's to run; that wiring is 3c.

## Deliberate breakage

Fifty breakages of this step, each run against its three test files. Thirty-seven were caught at first. One was aimed at a line that had been reformatted; aimed again, it was caught. Twelve survived:

- **Eight had no test that needed them, and have one now**, after which each was caught: a rewritten contract's own assignment finishing (without it a second invalid red found work still owed from the first, owed nothing, and left the behaviour with nobody; the test has two bad contracts in a row); an assessment asked for of a seal that has gone; the rule being asked in the transaction that says a story is demonstrated, and in the one that rewrites a contract (the pass after each apply had been hiding both, so the tests now look at what is owed before that pass runs); a reconfirmation that asks not being owed again by its answer; the rule owing nothing for a story that waits to be reconfirmed; a contract registered by hand owing its red; a contract rewritten only while its behaviour waits for a red.
- **One showed a check that duplicated the domain's own** (a baseline names exactly one stream). It is removed; the domain's refusal is what the test sees.
- **Three are kept without a test, for a stated reason.** The look before a red is owed only spares a failed insert: the unique index is what keeps one red per behaviour and cycle. Admission answering `finished` for work that asked cannot be reached through `claim`, which refuses first; it is a second line. And taking the gate exclusively differs from taking it shared only when two applies race, which no test does (listed above as thin).

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3b step 3 as built), saying for each finding whether a real entry point can reach it.
