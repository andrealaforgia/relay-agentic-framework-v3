-- Which reviewers a project requires and from which severity a finding blocks.
-- Copied onto each review cycle when it is created.
ALTER TABLE projects ADD COLUMN required_reviewers text[] NOT NULL DEFAULT '{qa,sentinel}';
ALTER TABLE projects ADD COLUMN blocking_from integer NOT NULL DEFAULT 3;

-- One review of one verified green. A commit alone does not identify a cycle: the same
-- commit can be verified again after a red is reopened, under a different policy.
CREATE TABLE review_cycles (
    cycle_id           uuid PRIMARY KEY,
    project_id         text NOT NULL,
    behaviour_id       text NOT NULL,
    green_execution_id uuid NOT NULL REFERENCES executions,
    red_execution_id   uuid NOT NULL REFERENCES executions,
    candidate_revision text NOT NULL CHECK (candidate_revision ~ '^[0-9a-f]{40}$'),
    candidate_ref      text NOT NULL,
    required_roles     text[] NOT NULL,
    blocking_from      integer NOT NULL,
    publication        text NOT NULL DEFAULT 'pending'
        CHECK (publication IN ('pending', 'published')),
    state              text NOT NULL DEFAULT 'open'
        CHECK (state IN ('open', 'satisfied', 'changes_requested', 'parked', 'blocked', 'superseded')),
    blocked_reason     text,
    -- Why publication has not happened yet, when the cause may pass (the remote was
    -- unreachable, say). A cycle with this set is still pending and will be retried.
    deferred_reason    text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, behaviour_id) REFERENCES behaviours
);

-- A behaviour has at most one cycle that can still be acted on.
CREATE UNIQUE INDEX one_live_cycle_per_behaviour
    ON review_cycles (project_id, behaviour_id) WHERE state IN ('open', 'satisfied');

-- The capability is what lets a reviewer act on a task; the task id alone grants nothing.
CREATE TABLE review_tasks (
    task_id    uuid PRIMARY KEY,
    cycle_id   uuid NOT NULL REFERENCES review_cycles,
    role       text NOT NULL,
    capability text UNIQUE,
    -- The one command that announces this task. Announcing again re-sends this same
    -- message, so a consumer that has already acted on it does nothing new.
    command_message_id uuid,
    UNIQUE (cycle_id, role)
);

-- Proof that a reviewer was given exactly the cycle's commit to look at.
CREATE TABLE review_checkouts (
    receipt_id  uuid PRIMARY KEY,
    task_id     uuid NOT NULL REFERENCES review_tasks,
    revision    text NOT NULL,
    prepared_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE review_submissions (
    submission_id  uuid PRIMARY KEY,
    task_id        uuid NOT NULL REFERENCES review_tasks,
    content_digest text NOT NULL,
    content        jsonb NOT NULL,
    disposition    text NOT NULL CHECK (disposition IN ('effective', 'conflicting', 'stale')),
    finding_ids    text[] NOT NULL DEFAULT '{}',
    received_at    timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX one_effective_submission_per_task
    ON review_submissions (task_id) WHERE disposition = 'effective';

-- Findings belong to the behaviour and outlive the cycle that raised them. A finding is
-- never deleted and never "resolved": see finding_verifications.
CREATE TABLE findings (
    finding_id        text PRIMARY KEY,
    project_id        text NOT NULL,
    behaviour_id      text NOT NULL,
    role              text NOT NULL,
    severity          integer NOT NULL,
    claim             text NOT NULL,
    evidence          text NOT NULL,
    affected_files    text[] NOT NULL,
    remediation       text NOT NULL,
    raised_in_cycle   uuid NOT NULL REFERENCES review_cycles,
    status            text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'disputed', 'withdrawn')),
    dispute           text,
    withdrawal_reason text,
    FOREIGN KEY (project_id, behaviour_id) REFERENCES behaviours
);

-- The raising role confirmed the fix on this cycle's candidate. It says nothing about
-- any other candidate.
CREATE TABLE finding_verifications (
    finding_id text NOT NULL REFERENCES findings,
    cycle_id   uuid NOT NULL REFERENCES review_cycles,
    PRIMARY KEY (finding_id, cycle_id)
);

CREATE TABLE remediation_requests (
    cycle_id    uuid PRIMARY KEY REFERENCES review_cycles,
    finding_ids text[] NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
