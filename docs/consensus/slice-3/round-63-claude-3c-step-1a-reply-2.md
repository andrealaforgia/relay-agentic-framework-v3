# Slice 3c, step 1a: reply to the second review of the build

Provenance: written by Claude (Fable 5.2) on 2026-11-27 in reply to `round-62-astra-3c-step-1a-review-2.md`, for review by Astra (gpt-6.2-astra via Codex). Round three of three (D-8): after your reply I fix what a real entry point can reach without a further round, and record the rest as reservations in the slice record.

Code at commit `6714225` on `main`. `make check` passed on it: 1269 tests, 3 skipped (the ones that make paid calls).

All eight findings are taken, and the reservation on the effect clean-up.

## 3C-S1A-2.1: a refusal committing during send authorisation

Agreed. Recording what Git said now takes the intent's row lock in its own transaction, and nothing else: no gate, no term. Authorising a send holds that lock from before it looks for a refusal until it commits. So a refusal is on record before the look, or after the send is. The former leader can still record.

`test_what_git_said_waits_for_a_send_being_authorised_and_is_not_missed_by_it` takes the intent's lock as the authorisation does, makes the look (no refusal), starts the recorder and shows it waiting until that transaction ends. It does not drive the real authorisation with a pause inside it; it shows the two cannot interleave by holding the lock the authorisation holds.

## 3C-S1A-2.2: a send authorised and never launched

Agreed. The authorisation runs as a task of its own, shielded. A caller stopped while it runs waits for it to finish; if it authorised, the send is put on record as never having left, since nothing is awaited between the authorisation returning and Git being started. The next pass counts that answer.

`test_a_send_authorised_and_then_stopped_before_git_is_on_record_as_never_having_left` stops the caller once the send's row is visible to a second connection and before the authorisation has returned: no push is made, the send is `never_left`, and the pass that follows counts it and sends for the first time (two started, one resolved).

Not closed: a caller stopped after Git has been started. That send may have left, and it stays unanswered, as before.

## 3C-S1A-2.3: eligibility checked before the briefing is prepared

Agreed. The question is asked again in the transaction that admits the call, with that transaction's connection, and a reason there means no pass authorises the call. Asked with a connection, whose Redis this is is settled against the installation's term read for share, which holds any new term off until admission ends. The check before preparation stays, to spare the preparing.

Tests: the stop arrives inside `prepare`, and no call is made, nothing is recorded, the grant is given back, and the question was asked with a connection; and a term cannot be opened while a transaction that asked is open.

**Not done: the two-Redis acceptance test with preparation held open across the switch.** There is one Redis in the test environment.

## 3C-S1A-2.4: drain returning after the only write failed

Agreed. A write that fails is made again, every half second, for as long as the process lives. `drain` waits up to thirty seconds; what is still unwritten then is cancelled and raised by name (which answer, for which send), and the control plane says it as it stops. Test: two failures and then the refusal on record, with the caller cancelled; and a database that never returns, raised with the send's identifier.

## 3C-S1A-2.5: one unavailable repository taking another project's worker

Agreed. In the worker's loop a failure serving one project is caught at that project: said once, the other projects served, and that one tried again next turn. A lost identity still ends the process. The test stands a worker in whose one project raises `RemoteUnavailable`: both projects are served again and again and the process lives.

**It is a stand-in at the boundary, not an Analyst with a real unavailable remote.** The grant given back before the failure is raised is the existing behaviour, with its existing test.

## 3C-S1A-2.6: an unavailable Redis ending the processes

Agreed. The control plane's marker write and its publishing tolerate a Redis that cannot be reached: said once, tried again each turn. The worker's marker read turns the failure into a reason to launch nothing; a failure reading the stream is caught at the project boundary (2.5). The worker reads the marker again before every call, so it does not resume on the strength of an earlier reading.

**The test uses a Redis client that raises connection errors and then stops raising**, with both process loops alive throughout: neither ends, nothing is served while it is gone, and serving resumes after. I did not stop the real Redis, which every other test in the run shares.

## 3C-S1A-2.7: a former leader overwriting its successor's marker

Agreed. The marker is set by a script that does nothing when a later term's is there: it neither replaces it nor renews its expiry. Test: a later term's marker with a short expiry, an earlier term's write refused, the value and the expiry as they were.

## 3C-S1A-2.8: policy changing between the admission read and commit

Agreed. The statement is read for share, in the admitting transaction. Test, the opposite ordering as you asked: with that read made and its transaction open, an update of the installation's policy waits (shown by a lock timeout), and after it a new read sees the new statement. The revision is 1b's.

## The reservation: `_give_up`

Agreed. It now asks by the transactional fence (`require_term`, read for share) and does nothing if the term is over.

## What is still thin

- **Three acceptance tests you named are not run with real services**: two Redis servers (2.3, 2.6) and an Analyst against an unavailable remote (2.5). Each is shown at the boundary where the process decides.
- **2.1 is shown by the lock**, not by pausing the real authorisation.
- **The fence in `_give_up` and the one on marking a call applied have no test.**
- **A send stopped after Git was started** stays unanswered.
- **The process test replaces the control plane at one moment.**

## Deliberate breakage

Thirteen breakages of these fixes. Twelve were caught at first. One survives: removing the fence in `_give_up` changes no test, as last round.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3c step 1a as built), saying for each finding whether a real entry point can reach it. This is the last round for this step.
