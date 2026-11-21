-- Completing a feature. A request is one wish to check a feature whose stories are all
-- resolved; it has generations, each a batch of checks on one commit, and only the
-- latest generation of a live request may decide anything.
CREATE TABLE completion_requests (
    request_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    feature_id text NOT NULL,
    -- live: still to be decided. completed, failed: a batch decided it. overtaken: the
    -- feature stopped being ready. abandoned: the feature was cancelled.
    state      text NOT NULL DEFAULT 'live'
        CHECK (state IN ('live', 'completed', 'failed', 'overtaken', 'abandoned')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (project_id, feature_id) REFERENCES features
);

CREATE UNIQUE INDEX one_live_completion_per_feature
    ON completion_requests (project_id, feature_id) WHERE state = 'live';

CREATE TABLE completion_batches (
    batch_id      uuid PRIMARY KEY,
    request_id    uuid NOT NULL REFERENCES completion_requests,
    project_id    text NOT NULL,
    feature_id    text NOT NULL,
    generation    integer NOT NULL,
    -- The assignment this generation was taken up for: one generation per assignment,
    -- however often the assignment is delivered.
    assignment_id uuid NOT NULL UNIQUE REFERENCES assignments,
    -- The commit that was trunk when the batch started, and everything else it is a
    -- check of: the roadmap revision, each story's state and revision, each obligation
    -- in force by identity and digest, and the feature's decision basis.
    trunk         text NOT NULL CHECK (trunk ~ '^[0-9a-f]{40}$'),
    snapshot      jsonb NOT NULL,
    state         text NOT NULL DEFAULT 'running' CHECK (state IN
        ('running', 'completed', 'failed', 'abandoned', 'held', 'interrupted', 'discarded',
         'deferred', 'overtaken')),
    reason        text,
    created_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (request_id, generation)
);

CREATE UNIQUE INDEX one_running_generation ON completion_batches (request_id)
    WHERE state = 'running';

-- One receipt per batch and obligation, by the obligation's identity: two obligations
-- that ask for the same observation are each observed. Committed as each check finishes.
CREATE TABLE completion_receipts (
    batch_id      uuid NOT NULL REFERENCES completion_batches,
    obligation_id uuid NOT NULL REFERENCES obligations,
    digest        text NOT NULL,
    revision      text NOT NULL,
    execution_id  uuid NOT NULL REFERENCES executions,
    satisfied     boolean NOT NULL,
    PRIMARY KEY (batch_id, obligation_id)
);

-- Written once, in the transaction that completes the feature, and never changed.
CREATE TABLE feature_summaries (
    project_id text NOT NULL,
    feature_id text NOT NULL,
    batch_id   uuid NOT NULL REFERENCES completion_batches,
    summary    jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, feature_id),
    FOREIGN KEY (project_id, feature_id) REFERENCES features
);
