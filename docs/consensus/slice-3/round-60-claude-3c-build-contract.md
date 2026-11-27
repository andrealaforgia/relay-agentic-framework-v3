# Slice 3c: the build contract

Provenance: written by Claude (Fable 5.2) on 2026-11-27 after `round-59-astra-3c-design-review-3.md`. Astra has not reviewed this document: round 59 was the third and last design round (D-8). It is design v1 (`round-57`) as amended by v2 (`round-58`) and v3 (`round-59`), with Astra's eight findings of round 59 folded in below. Each step's build is reviewed against this contract, at most three rounds a step.

Astra's disposition on the design was CHALLENGE in all three rounds (eleven, nine and eight findings). In round 59 it said the eight should be folded in and the build begun, that v3 resolves the two ordinary hand-off failures of round 58, and that it has no design finding outstanding against the usage calculation of step 3.

## Round 59 findings, as resolved

### Step 1

**3C-3.1: the outcome of a push across a change of term.** Each send of an intent has an identity, recorded before it is made. What Git said to that send is recorded once, under that identity, by whoever hears it, in any term: recording what a send's outcome was is not a workflow decision and is not fenced. What follows from it (a refusal reaching the inbox, a retry, a reconciliation) is decided by the current leader, which reads the sends on record. Test: the former leader's refusal arrives before its successor reconciles: one refused-push item, and no further send until a person retries.

**3C-3.2: a running worker left on the wrong Redis.** The marker a control plane writes carries its term. A worker compares the marker with the term in PostgreSQL at start, whenever its transport reconnects, and every thirty seconds while idle. A marker that is absent or of an older term than PostgreSQL's means this Redis is not the one the control plane is using: the worker launches nothing and says so, and goes on checking. No marker and no term at all is "waiting for the control plane". Test: the control plane restarts against another Redis with the worker left running.

**3C-3.3: attempt refs unique to an installation.** `refs/relay/<installation>/attempts/<project>/<behaviour>/<attempt>`, and likewise the invocation refs in mirrors, used by publication, the green run, recovery and the reviewer's checkout alike. Test: two installations on branches of one repository, the same project, behaviour and attempt: both first attempts stand.

**3C-3.4: a probe that is running is not one that is uncertain.** A boundary run's row is `running` while the process that launched it is alive (that process holds a session lock, as a worker does), and `uncertain` once it is not and no result is on record. A replay of a running request waits for it or reports it running, and launches nothing. `--retry` names the uncertain attempt it retries and has a receipt of its own: replayed, it returns that receipt and launches nothing more. Test as Astra put it: one launch while the first is live, two at the end.

### Step 2

**3C-3.5: a prepared decision on a parked call that was decided otherwise.** A prepared action on an inbox item names the open item, and is `stale` once that item is closed, whatever the state of the work beneath it. `set-aside` closing the item makes a prepared `retry` stale; the work stays parked and no call is authorised.

### Step 4

**3C-3.6: a conversation is not an invocation.** A conversation has a durable identity of its own. Each turn is a paid call with a fresh identity, recorded before launch with the person's input, and settled once. Turns are accounted like any call, by a role of their own (`interpreter`), for a project and for no feature when there is none yet; the live cost view shows them, an uncertain one as uncertain. The session has its own launch profile, which verifies Relay's server and its tool list in the start inventory. The worker's profile, which refuses every server, is not changed. Test: two turns of one conversation; the terminal result of the second lost; the cost view showing the known cost and the uncertain call.

**3C-3.7: the person's answer to a note.** The host records each line the person types before the call is made. When a note is open, the Interpreter's reply to the next turn may name the note that turn answered, and the host records the person's actual words against it, as conversation and not as a decision. `context` returns each note with its answer, and a note that has one is not outstanding. Test: the session is closed after the answer is recorded and before the model replies.

**3C-3.8: a provider session that cannot be resumed is not the end of the conversation.** Continuation by the provider's session is an economy, not the mechanism. Every turn can be made with a new provider session, the same verified tools and the conversation's context from Relay's record. The real-CLI probe of continuation stays, to know which is in use. Test: the provider's transcript is deleted between two turns, and the second recovers the analysis, the answered note and the prepared action.

## Steps

**1a. Processes and recovery.** The installation's row with its identity and the control plane's term; every state-changing transaction of the control plane fenced, and an effect of an earlier term free to be taken; effects that say whose they are, with the control plane taking a returned build and the pass taking any returned result nobody is carrying; a rejected green handing the work back or parking it; sends with identities; project-scoped scans; installation-unique refs and streams; the Redis marker and the worker's check of it; the wake and the timer; `relay up` and `relay worker start` as processes. Proved by a feature delivered by separate operating-system processes with the stand-in CLI, the control plane replaced part-way with the old one alive.

**1b. Enrolment and its commands.** The bootstrap file; the passphrase and its terminal read; `relay init`; `relay passphrase`; `relay policy state`; the project selector and the journal of requests; `relay doctor`, with boundary runs as durable requests.

**2. The Product Owner's commands.** One typed inbox of five kinds; every deciding command, each bound to the record it was shown; closure identity; `prepare` and `confirm`; the reading commands with `--json`.

**3. Watching.** The live usage view from invocation records; `relay costs`; `relay watch` in three views.

**4. The session, packaging and instructions.** The server and its operations; the hosted conversation with turns as calls; `context`, `record_analysis`, `note_question`; the console script; `docs/USAGE.md`; the smoke through the command line.

## Deferred, and recorded

- **Live process diagnostics** beyond what `relay doctor` can see.
- **Workers on another host than the control plane**: they share a filesystem here.
- **A per-project profile or budget.**
- **A web interface and an API.**
- **Discovery beyond a recorded analysis**: PO-3 is delivered to the extent of a hosted conversation that reads, records analyses and notes, and prepares.
