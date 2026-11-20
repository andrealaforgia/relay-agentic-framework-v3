-- What the oracle observed. Written only by the supervised run that produced it,
-- in the transaction that judges it.
CREATE TABLE executions (
    execution_id     uuid PRIMARY KEY,
    project_id       text NOT NULL,
    behaviour_id     text NOT NULL,
    purpose          text NOT NULL CHECK (purpose IN ('red', 'green', 'check')),
    revision         text NOT NULL CHECK (revision ~ '^[0-9a-f]{40}$'),
    contract_digest  text NOT NULL,
    isolation        text NOT NULL,
    -- The runtime the subject actually ran in (a container image digest), recorded at
    -- the time of the run so a later green can be paired with a red under the same one.
    runtime_identity text NOT NULL,
    exit_code        integer,
    stdout           bytea NOT NULL,
    stderr           bytea NOT NULL,
    ineligibility    text,
    recorded_at      timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, behaviour_id) REFERENCES behaviours
);
