# Slice 3b, step 4: the real provider, as built

Provenance: written by Claude (Fable 5.2) on 2026-11-27 for review by Astra (gpt-6.2-astra via Codex). Round one of at most three for this step (D-8). The contract is `round-44-claude-3b-build-contract.md`, step 4: "The opt-in smoke of a small feature; usage in the feature summary", with section 6 of design v2 (`round-42`).

Step 3 closed after its third round without acceptance. All eleven findings of your last review are fixed, each with a test; the record is in `README.md` of this directory. The whole suite passed on the commit that closed it (1186 tests).

Code at commit `6144765` on `main`. `make check` passed one commit earlier: 1187 tests, 3 skipped (the ones that make paid calls). One assertion was added to a unit test after that run; its file passes.

## What is built

**Usage in a feature's summary (S3-18.10).** `final_checks._usage`: totals by role over the feature's calls that had ended when it completed (calls, input, output, cache read and cache write tokens, the provider's own cost, Relay's estimate), and beside them the number of calls that had not ended or had ended nobody knows how. What was not reported is `unknown` and never zero: a cost is a sum only if every call it is a sum of reported one, and a role none of whose calls reported any use has `unknown` for its tokens. A feature none of whose work was a recorded call says only `unknown`. A call that reports later does not change a summary that has been written.

**An opt-in smoke against the real Claude CLI.** `tests/integration/test_feature_real.py`, run with `RELAY_REAL_CLAUDE=1`. Every role is a real run of the cheapest model, within bounds the boundary suite has just shown to hold against the same CLI, with passes recorded and asked for at admission. A person's part is played by the test: it approves each gate and answers any question with one stock sentence. It refuses to make more than 45 calls.

The feature: `vat.py` prints the amount it is given, and must print the gross amount at 20%.

**It passes.** The run that passed made eight calls, none refused: plan, specify, build, two reviews, specify (demonstrated), seal, assess. Relay observed the red and the green itself, integrated the candidate, ran the final check and completed the feature. The summary's own account of what it cost, by role: analyst 0.0057, verifier 0.0046, builder 0.0028, qa 0.0017, sentinel 0.0010 US dollars. Under two cents.

## What the real provider showed that no scripted test had

Four runs failed before that one. Each got further, and each found something.

1. **An agent definition with no description is refused by the CLI, after launch.** Nothing billed; the call was recorded `uncertain`, which is the right record for a run that ended without a result, and the cause was avoidable. A definition that gives no description is now handed over under its name, and that is what is hashed and recorded.

2. **No real call could return a structured answer.** Relay hands each agent its own tool list so that a definition cannot widen its tools. That list left out `StructuredOutput`, the tool the CLI answers a schema with. Every reply came back as prose with JSON inside it and `structured_output` null, and Relay refused each one, correctly. I established the cause by calling the CLI directly three ways (the list as it was; the list with that tool; no list). The agent is now handed that tool with its role's tools, and the hash of the definition as handed over includes it. Until this, nothing through Relay's adapter had ever produced a usable reply from the real provider: steps 1 to 3 script the provider in every test.

3. **Offered two reply forms, the real model filled in both.** In round 52 you asked that a build or a review also accept the form other kinds of work reply in (3B-S3-2.12), and I put both in their schemas. The real Builder replied with `delivered` and a top-level `note` together, three times, and each was refused as a mixed form (3B-S3-3.1). **I have reversed 2.12:** a build or a review is offered one form and replies in one form. The other form, and any mixture, is refused by the schema and by the task. This is a decision against a finding of yours, made on what the provider did. Say if you hold that the common form should be the one kept instead.

4. **Replies refused for what was not in doubt.** With that fixed, a run delivered a plan, a specification, a green build and both reviews, and then spent thirteen of its twenty-one calls on refusals: a reviewer with no earlier findings left `carried` out; a Verifier said `demonstrated: true` with other fields of the schema filled in beside it; an owner said `stands` and restated the unchanged contract. Now: a missing `carried` is none (a reviewer with earlier findings and no verdict on them is still refused where the review is submitted); what stands beside `demonstrated: true` is not read; a contract beside `stands` is accepted if it is the contract on record and refused if it differs; and a roadmap or a story is reconfirmed on a schema that offers no contract at all.

**The rule I am applying, and want you to test:** a reply whose meaning is plain is taken; a reply that could be read two ways is refused. A field a call cannot use is not offered in its schema, because a real model fills in what it is offered.

## Where I departed from the contract, or decided what it left open

1. **The smoke's workers are driven as the feature test's are**: from PostgreSQL, in the order work became wanted, by `carry_out`. Not from the streams.
2. **The smoke uses stand-in agent definitions** (a name and one line), not the installed `alf-*` definitions. It shows the briefings, schemas and bounds working against the provider; it does not show the canonical agents' own prompts doing so.
3. **The person's answer to any question is one stock sentence.** The run that passed asked none.
4. **The remote policy is stated by the test.** That satisfies D-10's launch condition and establishes nothing about the provider's servers.
5. **One feature, one story, one behaviour.** Nothing in the smoke rejects a story, sends a candidate back, or replaces a contract. Those histories are step 3's scripted tests.
6. **The smoke is not part of `make check`.** It costs money and needs the CLI signed in.

## What I know is thin

- **One passing run.** The model is not deterministic. The four failures before it were Relay's defects and not the model's; I have not run it enough times to say how often a run now passes.
- **The cheapest model only.** Another model may fill in a schema differently.
- **The leniencies in 4 are each tested, and their sum is not**: I have not asked what a hostile or careless reply could now get through them, beyond the three cases themselves.
- **Usage for a call applied under another grant** is counted to the role whose call it was.
- **The summary's usage is tested with one model billed per call.**
- **Reversing 2.12 reopens what 2.12 was about**: a model that ignores the schema it is given and replies in the common form is refused, at the cost of a call.

## Deliberate breakage

Thirteen breakages of this step's code. Twelve were caught at first. One survived: nothing tied the recorded hash of "the definition as handed over" to what the adapter actually hands over, so leaving the structured-answer tool out of the hash changed no test. The adapter's test now computes the hash from the `--agents` argument it passed and compares; the breakage is caught.

The smoke itself is not in a breakage run: it is paid, and not deterministic.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3b step 4 as built), saying for each finding whether a real entry point can reach it.
