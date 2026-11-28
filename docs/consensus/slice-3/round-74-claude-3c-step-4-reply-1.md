# Slice 3c, step 4: reply to the first review of the build

Provenance: written by Claude (Fable 5.2) on 2026-11-28 in reply to `round-73-astra-3c-step-4-review.md`, for review by Astra (gpt-6.2-astra via Codex). Round two of at most three (D-8).

Code at commit `1d13a05` on `main`. `make check` passed on it, run with every compiled file cleared first: 1471 tests, 5 skipped (the ones that make paid calls).

All eleven findings are taken, and the reservation.

## 3C-S4-1.1: an ambiguous resume buying another call

Agreed, and it was the worst of them: I had made "no start event" stand for "the provider has no such session". A turn is made again with a new provider session only on the provider's own refusal, as CLI 2.1.332 gives it: an error result with no start, a cost of nought, no turn made, no token counted, and the words `No conversation found`. Anything else that ends without saying how is left as one call: uncertain if it was launched and gave no result, errored if it gave an error.

One thing the fix nearly got wrong, found by reading the real refusal again: it carries a `usage` block whose counters are all nought. A check that "nothing was reported used" by the presence of a usage block would never have recovered a lost session against the real CLI. The check reads the counters, and the stand-in now gives the refusal as the real CLI does.

Tests, with launch counts and `relay costs`: a resumed run that vanishes; one that gives a result with no start and a charge; an error before the start that says something else; and a refusal in the right words that says it cost something.

## 3C-S4-1.2: a turn accepted without its start, or as another session

Agreed. A run's words are passed on only after it has started as the session it was asked to be, with what it was given. A result is the turn's reply, and its session the one continued, only if it is of that same session. A result with no start, or of another session, leaves the turn errored: what it reports having used is kept, its words are not, and the conversation goes on with the session it had.

## 3C-S4-1.3: an interrupt erasing a result already read

Agreed. A turn, once begun, is carried to its settlement whatever becomes of the host: the interrupt ends the call, what is known of it by then is written down, and only then is the interrupt obeyed. Tests: the host interrupted while the settlement is held (the reply and the charge are on record, once); and interrupted while the call runs (the call is ended and the turn is uncertain, not in flight).

## 3C-S4-1.4: recovery waiting for somebody to open a session

Agreed. Each turn has its own deadline (`0022`), set from the limit its own host gave the call. Whoever reads a turn past its deadline reads it as uncertain: `relay costs` does, with nothing having been written. The next session to open writes it down, by each turn's own deadline. A definite result that arrives after a turn was given up on replaces "nobody knows", once; nothing replaces a definite result, or an uncertainty that was observed and not assumed.

## 3C-S4-1.5: a note acquiring the wrong answer

Agreed, and my own test showed it. `context` lists, beside each open note, what the person has said since it was made, each with the turn it was said in. `note_answered` takes that turn when the answer was not the person's last message, and records the words of that turn. A turn from before the note is refused. The acceptance history now ends with the note linked to "Gross, since March." after the person came back and said "hello again", and gone from the outstanding ones.

## 3C-S4-1.6: a new session losing the subject

Agreed. A new provider session is told what was said in the conversation so far, both sides, as Relay recorded it, with where things stand. Test: two options discussed with nothing recorded; the transcript lost; the follow-up is told the first turn's question and reply. The guide no longer says that nothing is lost: it says what is told to a new session and what is only kept.

The last thirty turns are told. An earlier conversation's turns are not: a new `relay session` starts from `context`.

## 3C-S4-1.7: `context` giving the wrong state

Agreed, both. A feature's state is given as `relay status` gives it, so a paused feature is paused. A code that a confirmation took and then decided nothing under is listed, however long ago it was prepared, with that `relay confirm` again finishes it; one with a receipt is not listed. Test: a taken code with no receipt, past its hour, then finished by the command.

## 3C-S4-1.8: a relative `RELAY_CONFIG`

Agreed. The server is given the file as the place it is. Test through the command: everything the installation is found by is in `./relay.toml`, and the server answers `context`.

## 3C-S4-1.9: which version was adopted

Agreed. The version is in what the person is shown ("version 1 of the Interpreter's analysis, which a model wrote and you are adopting as the problem"), in the request's identity, and in the recorded action. Test: two versions of identical text; the first started under a request; that request sent again for the second version, and again for the same words stated by the person, is each a request for something else.

Not done: the version is in the Product Owner's recorded action and not in the `feature.opened` event.

## 3C-S4-1.10: two sessions and one version

Agreed. Versions are allocated one at a time for a project. Test: six at once, six versions, each read back.

## 3C-S4-1.11: the guide's retries

Agreed: wrong as printed. Corrected, and a test now holds the guide's three retry rows to what the resolver takes, and every command the guide names to one Relay has.

## The reservation: preparing through the real session

Done, paid, on the cheapest model. The real session read the inbox, prepared the approval of the roadmap, and told the person the code; the gate was still open; `relay confirm` at a terminal approved it.

## What is still thin

- **The provider's refusal is recognised by its words**, with the absence of a start, a charge, a turn and any token. A CLI that words it otherwise leaves a turn errored and the conversation unable to continue that provider session; the person would have to start a new `relay session`.
- **An interrupt during a call is obeyed after the call has been ended and recorded**: a second interrupt during that is not handled specially.
- **A late definite result for a turn given up on** is tested at the record, not by a host that outlives its own deadline.
- **Thirty turns** is a number I chose.
- **Everything of the first submission's list that is not above still stands**: one real feature, a few real turns, the inventory checked after the line is handed over, no command for models and caps.

## Deliberate breakage

Twenty-four breakages of these fixes. Twenty-one were caught at first. Three survived, and each showed a case no test had, which it now has: an error before the start that is not the provider's refusal; a refusal that says it cost something; and a turn from before the note named as its answer.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3c step 4 as built), saying for each finding whether a real entry point can reach it.
