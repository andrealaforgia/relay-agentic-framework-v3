# Slice 3b: the build contract

Provenance: written by Claude (Fable 5.2) on 2026-11-21 after `round-43-astra-3b-design-review-3.md`. Astra has not reviewed this document: round 43 was the third and last design round (D-8). It is design v2 (`round-42`) as amended by v3 (`round-43`), with Astra's eight findings of round 43 folded in below. Each step's build is reviewed against this contract, at most three rounds a step.

Astra's disposition on the design was CHALLENGE in all three rounds (ten, nine and eight findings). Astra accepted, in round 43: the assignment as one occasion with successors; retaining the candidate before `returned`; the recorded remediation base; and the control plane owning the first integration. It stated that none of the remaining findings needs another design round.

## Round 43 findings, as resolved

### Step 1

**S3-20.1: who carries out an authorised retry.** The retry limit is counted per assignment (one occasion), across grants. Recovery of a grant whose owner is dead, by the state of the assignment's latest invocation:

| Latest invocation | What recovery does |
|---|---|
| none | the grant is taken back; the assignment is wanted. No inbox item. |
| `intended` | `uncertain`; the assignment is parked with an inbox item. The grant is taken back. |
| `errored`, `rejected`, `not_started` | the grant is taken back; the assignment is wanted, and admission decides whether another call is allowed. |
| `returned` | applied by the pass under the original grant. |

Every invocation keeps the grant it ran under as evidence. A person's `retry` clears the park and announces the command again; a live worker claims it under a new grant, and admission admits it because the retry is authorised and unconsumed. A successor to a review prepares its own checkout. Tested with two distinct worker identities.

**S3-20.2: which oracle try may settle.** A verdict is written in a transaction that checks the effect is `started` and that the try settling is the effect's current try. A try that was marked interrupted contributes its evidence and decides nothing. Test: try 1 passes late after try 2 has started and failed; no review cycle opens.

**S3-20.3: a known result during a temporary stop.** The paid call is the grant's first effect: the grant has `first_effect_at` set when the call is admitted, so a pause does not take it back. Before any effect of an apply:

- the scope is ended for good (cancelled, rejected, retired, or the recorded basis, base or revisions are no longer current): the invocation is `overtaken`. Whether fresh work is owed is decided by the rule for owed work, not here.
- the scope is stopped for now (paused, or a question is open over it): the invocation stays `returned`, nothing is done, and the pass continues it when the stop is cleared, after checking the first point again.

Test: a pause after retention; no publication while paused; one publication after resume and no second call.

**S3-20.4: lock order at admission.** Gate (shared), then the behaviour where the kind has one, then the assignment; the grant is read under those locks. `reopen_red` takes the behaviour and then retires the assignment, in the same order. Test: the two raced; a reopen that wins leaves no call for the retired build.

### Step 2

**S3-20.5: what each confinement probe observes.** Three kinds of probe, each with its own pass condition:

- a tool the role has: the tool call is in the stream and its recorded outcome is what is expected (a refusal for a forbidden target, a success for a permitted one), and the host agrees;
- a tool the role does not have: it is absent from the tools the run's start event lists. The adapter checks this on every run, not only in the suite: a start event listing a tool or a server Relay did not ask for ends the run before the briefing is acted on;
- something loaded automatically (a candidate-local `CLAUDE.md`, settings, agents): verified from what the run reports as loaded where it reports it, and by a canary instruction that must have no effect.

The pass is bound to the effective configuration, including managed policy as the CLI itself reports it, not only a local settings file. If the installed CLI gives no way to read the effective managed policy, the pass records that it could not be attested and the operator is told so when a worker starts.

### Step 3

**S3-20.6: a successor is owed whenever the obligation survives**, whether or not it can start. A pause does not prevent the successor being recorded; it prevents it being claimed. A review's successor is bound to the existing review task so `prepare_review` accepts its grant.

**S3-20.7: `specify` proposes, enters test design and registers the contract in one transaction.** There is no state in which a behaviour of a delivered `specify` exists without its contract. The red is a separate request (below).

**S3-20.8: one red request per behaviour and red cycle.** Registering or amending a contract owes it in that transaction; the pass alone carries it out. A red that is definitively invalid (the observation already passes, or the check cannot be evaluated) is recorded as such and is not run again by the pass: the Verifier is owed the next step (`specify` again, briefed with why the red was invalid).

## Steps

1. **Records and the adapter.** Workers and their liveness; the grant's owner; invocations, admission, record, apply (one transaction, and several effects with identities and tries), recovery by the pass, a person's retry or set-aside, a late result; the Claude adapter against a stub; usage as reported; agent definitions. No task is driven yet: step 1's apply is given by the caller.
2. **The boundary and the two existing tasks.** Confinement and its suite; principals on every worker-callable operation; `review`, `build` and `remediate` driven end to end.
3. **The owed work that is missing.** Successors; `plan`, `specify`, `seal`, `assess`, `reconfirm`, `correct`, `revise`; the rule for what a story needs; the control plane's red requests and first integration.
4. **The real provider.** The opt-in smoke of a small feature; usage in the feature summary.

Already built ahead of the review of step 1, because no finding touched them: the stream reader, the price table and agent definition discovery (`43d6076`).
