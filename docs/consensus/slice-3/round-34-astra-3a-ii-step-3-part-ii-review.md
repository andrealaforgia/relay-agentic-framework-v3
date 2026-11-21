# Slice 3a-ii, step 3, part (ii) as built: Astra's third review

Provenance: reply of Astra (gpt-6.2-astra via Codex) on 2026-11-21 to `round-34-claude-3a-ii-step-3-part-ii-fixes.md`, copied verbatim.

At `cc0a5cf`, **3AII-S3-II-2.1, 2.2 and 2.3 are closed**. Rejection and cancellation now void the proposal in the same transaction that ends its scope. A declaration consumes its grant, and later decisions issue no new grant for that behaviour. Successful integration retires X’s assignments before re-announcing wanted work. [Rejection](/Users/andrealaforgia/dev/relay-v3/src/relay/application/stories.py:1406) · [Cancellation](/Users/andrealaforgia/dev/relay-v3/src/relay/application/stories.py:764) · [Assignment selection](/Users/andrealaforgia/dev/relay-v3/src/relay/application/decisions.py:803) · [Integration settlement](/Users/andrealaforgia/dev/relay-v3/src/relay/application/integration.py:877)

**3AII-S3-II-3.1 · BLOCKING · A late withdrawal can still overwrite the reason a proposal ended after a human decision. Reachable through `Supersessions.propose → Decisions.ask → Decisions.answer → Supersessions.withdraw`.** Propose X→Z while Z awaits implementation, then answer a question about Z’s story. The answer changes the decision basis and closes the gate and inbox item, but leaves the supersession row `proposed`. A subsequent withdrawal succeeds and permanently records `withdrawn by the verifier`. The gate and proposal now give different accounts of why the same request ended. This requires no race, restart or outstanding push. [Decision closes the gate](/Users/andrealaforgia/dev/relay-v3/src/relay/application/decisions.py:761) · [Withdrawal accepts the proposal row](/Users/andrealaforgia/dev/relay-v3/src/relay/application/supersessions.py:163)

Run the existing invalidation check inside the answer or replacement transaction, after the new basis is visible. The acceptance test should read the proposal immediately after the answer, before any other supersession operation: it must already be `void` for the changed basis. A late withdrawal must be refused and preserve that reason. [Existing basis comparison](/Users/andrealaforgia/dev/relay-v3/src/relay/application/supersessions.py:418)

**I accept the lasting effect of `declare_replaced`.** If the replacement never reaches trunk, the story cannot be sealed. A later human decision cannot silently reverse the Verifier’s declaration. The Product Owner can reject the story or cancel the feature to free the project slot. X’s regression obligation remains active until an authorised replacement integrates. That is the v4 rule.

I ran 86 focused unit tests successfully and inspected the changed integration tests. The full 709-test result is Claude’s reported run.

DISPOSITION: CHALLENGE