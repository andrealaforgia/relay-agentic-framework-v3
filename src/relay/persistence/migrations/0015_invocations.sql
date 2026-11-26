-- A worker process. It holds a session-level advisory lock on lock_key for as long as it
-- lives, on a connection of its own: PostgreSQL releases the lock when the process or its
-- connection is gone, so whether a worker is alive is never inferred from a clock.
CREATE TABLE workers (
    worker_id  uuid PRIMARY KEY,
    role       text NOT NULL,
    host       text NOT NULL,
    pid        integer NOT NULL,
    lock_key   bigint NOT NULL UNIQUE,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- The worker process a grant was given to. Null for a grant claimed by a caller in the
-- control plane's own process, which ends with it.
ALTER TABLE assignments ADD COLUMN granted_worker uuid REFERENCES workers;
-- Why nothing more is called for this work until a person says so; null if it is not parked.
ALTER TABLE assignments ADD COLUMN parked text;
-- An assignment is one occasion on which work is owed. Work owed again is a new one.
ALTER TABLE assignments ADD COLUMN succeeds uuid REFERENCES assignments;
-- The work whose reply was this question: answering it owes that work again.
ALTER TABLE questions ADD COLUMN asked_for uuid REFERENCES assignments;

-- What the Verifier last said was demonstrated: the story's revision, its list revision
-- and the decision basis it said so on. A statement about an earlier one of any of the
-- three is not a statement about the story as it now stands.
ALTER TABLE stories ADD COLUMN demonstrated jsonb;

-- Each step of a story is owed once at a time, and each red of a behaviour.
CREATE UNIQUE INDEX one_step_owed_per_story ON assignments (project_id, story_id, kind)
    WHERE kind IN ('specify', 'seal', 'assess') AND behaviour_id IS NULL
      AND state IN ('wanted', 'granted');
CREATE UNIQUE INDEX one_red_or_contract_owed_per_behaviour
    ON assignments (project_id, behaviour_id, kind)
    WHERE kind IN ('red', 'specify') AND behaviour_id IS NOT NULL
      AND state IN ('wanted', 'granted');

-- One row per call to a provider, written before the call is made.
CREATE TABLE invocations (
    invocation_id   uuid PRIMARY KEY,
    assignment_id   uuid NOT NULL REFERENCES assignments,
    project_id      text NOT NULL REFERENCES projects,
    -- The grant the call ran under, kept as evidence whatever becomes of the grant.
    grant_id        uuid NOT NULL,
    attempt         integer NOT NULL,
    worker_id       uuid NOT NULL REFERENCES workers,
    role            text NOT NULL,
    provider        text NOT NULL,
    model           text NOT NULL,
    effort          text,
    agent_name      text NOT NULL,
    agent_path      text NOT NULL,
    agent_sha256    text NOT NULL,
    agent_revision  text,
    agent_dirty     boolean NOT NULL,
    -- The definition as it was handed over: the file's, with the role's tools.
    effective_agent_sha256 text NOT NULL,
    briefing        text NOT NULL,
    briefing_sha256 text NOT NULL,
    schema_sha256   text NOT NULL,
    -- The schema itself: what came back is checked against exactly this.
    schema          jsonb NOT NULL,
    -- What preparation handed the model besides the briefing, by which what it replies
    -- is later tied to what it saw: for a review, the receipt of its checkout.
    evidence        jsonb NOT NULL DEFAULT '{}',
    -- What the call was admitted under: the grant, the attempt, and the revisions and
    -- decision basis of what it is about.
    admitted_on     jsonb NOT NULL,
    -- The revisions and decision basis the next effect of its apply expects to find. It
    -- starts as what the call was admitted on, and moves only by what an effect of its
    -- own says it changed.
    expects         jsonb NOT NULL,
    -- For a result that arrived after the work had been granted again: the grant it is
    -- applied under. Its own grant stays on record as what it ran under.
    applied_under   uuid,
    state           text NOT NULL DEFAULT 'intended'
                    CHECK (state IN ('intended', 'returned', 'errored', 'applied', 'rejected',
                                     'overtaken', 'not_started', 'uncertain')),
    reason          text,
    -- The provider's terminal event, whole, as it arrived.
    envelope        jsonb,
    reply           jsonb,
    -- For a build: the commit the supervisor made of the workspace, held in the mirror.
    retained        text,
    -- As reported, by model billed. Null where nothing was reported.
    usage           jsonb,
    provider_cost_usd  numeric,
    estimated_cost_usd numeric,
    price_table     text,
    -- The request by which a person allowed one more call after this one.
    retry_authorised_by text,
    started_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    ended_at        timestamptz,
    UNIQUE (assignment_id, attempt)
);

-- One call in flight, or one known result not yet applied, per assignment.
CREATE UNIQUE INDEX one_live_invocation_per_assignment ON invocations (assignment_id)
    WHERE state IN ('intended', 'returned');

-- The effects of applying a result. Each is carried out once: taken, performed, marked.
CREATE TABLE invocation_effects (
    invocation_id uuid NOT NULL REFERENCES invocations,
    position      integer NOT NULL,
    name          text NOT NULL,
    state         text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'started', 'done')),
    -- Which try is the current one. Only the current try may settle the effect.
    try           integer NOT NULL DEFAULT 0,
    worker_id     uuid REFERENCES workers,
    detail        jsonb,
    PRIMARY KEY (invocation_id, name)
);

ALTER TABLE inbox_items ADD COLUMN invocation_id uuid REFERENCES invocations;

-- A boundary around a model's process that was shown to hold: a role's tools and
-- settings, on one runtime, tried against the real CLI. A run with tools is made only
-- under a boundary with a recent pass.
CREATE TABLE boundary_passes (
    pass_id      uuid PRIMARY KEY,
    digest       text NOT NULL,
    role         text NOT NULL,
    runtime      jsonb NOT NULL,
    observations jsonb NOT NULL,
    -- Every run of the suite is kept, whichever way it went. The latest one of a
    -- boundary is what is true of it.
    passed       boolean NOT NULL,
    -- When the run was made, as the run says: not when it came to be recorded.
    observed_at  timestamptz NOT NULL
);
CREATE INDEX boundary_passes_by_digest ON boundary_passes (digest, observed_at DESC);

-- The attempt number a call's candidate is published under. One sequence per behaviour
-- across builds and remediations, allocated once per call: asked again, a call is given
-- the number it has.
CREATE TABLE build_attempts (
    project_id    text NOT NULL,
    behaviour_id  text NOT NULL,
    attempt       integer NOT NULL,
    invocation_id uuid NOT NULL UNIQUE REFERENCES invocations,
    PRIMARY KEY (project_id, behaviour_id, attempt),
    FOREIGN KEY (project_id, behaviour_id) REFERENCES behaviours
);
