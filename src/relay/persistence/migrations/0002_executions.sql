-- Unsandboxed native execution shares the supervisor's account, so its evidence
-- opens a gate only where the Product Owner has approved that for the project.
ALTER TABLE projects ADD COLUMN native_evidence_approved boolean NOT NULL DEFAULT false;

-- What the oracle observed. Written by trusted code in the transaction that judges it.
CREATE TABLE executions (
    execution_id    uuid PRIMARY KEY,
    project_id      text NOT NULL,
    behaviour_id    text NOT NULL,
    purpose         text NOT NULL CHECK (purpose IN ('red', 'green')),
    revision        text NOT NULL CHECK (revision ~ '^[0-9a-f]{40}$'),
    contract_digest text NOT NULL,
    isolation       text NOT NULL,
    exit_code       integer,
    stdout          bytea NOT NULL,
    stderr          bytea NOT NULL,
    ineligibility   text,
    recorded_at     timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, behaviour_id) REFERENCES behaviours
);
