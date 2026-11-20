-- A reviewed candidate lost its footing (trunk moved, or the runtime changed) and its
-- contract is being observed again. Decides what an already-passing red then means.
ALTER TABLE behaviours ADD COLUMN reopened_from_review boolean NOT NULL DEFAULT false;

-- Checks every candidate must pass before it reaches trunk. Kept here, not in the
-- repository, so a candidate cannot edit what it is checked against.
ALTER TABLE projects ADD COLUMN check_policy_version integer NOT NULL DEFAULT 0;

CREATE TABLE required_checks (
    project_id      text NOT NULL REFERENCES projects,
    contract_id     text NOT NULL,
    contract        jsonb NOT NULL,
    contract_digest text NOT NULL,
    registered_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, contract_id)
);

-- A failed check sends work back just as a blocking finding does.
ALTER TABLE remediation_requests ADD COLUMN failed_checks jsonb NOT NULL DEFAULT '[]';

-- One run of the whole regression set on one cycle's candidate, for one request.
CREATE TABLE check_batches (
    batch_id             uuid PRIMARY KEY,
    project_id           text NOT NULL,
    behaviour_id         text NOT NULL,
    request_id           text NOT NULL,
    cycle_id             uuid NOT NULL REFERENCES review_cycles,
    candidate_revision   text NOT NULL,
    expected_trunk       text NOT NULL,
    check_policy_version integer NOT NULL,
    regression_digest    text NOT NULL,
    members              jsonb NOT NULL,
    state                text NOT NULL DEFAULT 'running' CHECK (state IN
        ('running', 'authorised', 'failed', 'superseded', 'discarded', 'abandoned', 'base_moved',
         'runtime_changed', 'held', 'interrupted')),
    reason               text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, behaviour_id) REFERENCES behaviours,
    -- A request repeated gets the batch it already has, never a second one.
    UNIQUE (project_id, behaviour_id, request_id)
);

CREATE TABLE check_runs (
    batch_id        uuid NOT NULL REFERENCES check_batches,
    contract_digest text NOT NULL,
    contract_id     text NOT NULL,
    execution_id    uuid NOT NULL REFERENCES executions,
    satisfied       boolean NOT NULL,
    PRIMARY KEY (batch_id, contract_digest)
);

-- The authority to move trunk to one commit. From the moment it is recorded it owns the
-- update, and nothing else may move this project's trunk until it is settled.
CREATE TABLE integration_intents (
    intent_id          uuid PRIMARY KEY,
    project_id         text NOT NULL REFERENCES projects,
    behaviour_id       text NOT NULL,
    cycle_id           uuid NOT NULL REFERENCES review_cycles,
    batch_id           uuid NOT NULL UNIQUE REFERENCES check_batches,
    -- Where the update goes, fixed when it is authorised: a later change to the project
    -- must not redirect a push already owed.
    repository         text NOT NULL,
    trunk              text NOT NULL,
    candidate_revision text NOT NULL CHECK (candidate_revision ~ '^[0-9a-f]{40}$'),
    expected_trunk     text NOT NULL CHECK (expected_trunk ~ '^[0-9a-f]{40}$'),
    state              text NOT NULL DEFAULT 'outstanding'
        CHECK (state IN ('outstanding', 'integrated', 'rejected', 'conflicted')),
    -- Every push is counted before it is sent. One that was started and never answered
    -- may still land, however long ago it was sent.
    sends_started      integer NOT NULL DEFAULT 0,
    sends_resolved     integer NOT NULL DEFAULT 0,
    -- A refusal that could not settle the intent because another send was unanswered.
    refusal            text,
    reason             text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, behaviour_id) REFERENCES behaviours
);

CREATE UNIQUE INDEX one_outstanding_intent_per_project
    ON integration_intents (project_id) WHERE state = 'outstanding';

-- A conflict on the candidate ref seen while an authorised integration owned the cycle.
-- It could not stop that integration; it must stop the cycle being used for another.
ALTER TABLE review_cycles ADD COLUMN conflict_observed text;
