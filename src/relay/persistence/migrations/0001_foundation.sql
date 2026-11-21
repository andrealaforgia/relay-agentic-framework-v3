CREATE TABLE projects (
    project_id    text PRIMARY KEY,
    -- The only source Relay will test and integrate for this project.
    repository    text NOT NULL,
    trunk         text NOT NULL DEFAULT 'main',
    last_sequence bigint NOT NULL DEFAULT 0,
    opened_at     timestamptz NOT NULL DEFAULT now()
);

-- One project per managed trunk. Projects may share a repository on different branches.
CREATE UNIQUE INDEX one_project_per_trunk ON projects (repository, trunk);


CREATE TABLE behaviours (
    project_id      text NOT NULL REFERENCES projects,
    behaviour_id    text NOT NULL,
    summary         text NOT NULL,
    state           text NOT NULL,
    contract        jsonb,
    correlation_id  uuid NOT NULL,
    PRIMARY KEY (project_id, behaviour_id)
);

-- The ledger. Sequence is allocated under the project row lock, so it follows commit order.
CREATE TABLE events (
    event_id       uuid PRIMARY KEY,
    project_id     text NOT NULL REFERENCES projects,
    sequence       bigint NOT NULL,
    event_type     text NOT NULL,
    subject_id     text,
    payload        jsonb NOT NULL,
    correlation_id uuid,
    causation_id   uuid,
    recorded_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, sequence)
);

CREATE FUNCTION refuse_ledger_rewrite() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'the event ledger is append-only';
END;
$$;

CREATE TRIGGER events_are_append_only
    BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION refuse_ledger_rewrite();

CREATE TRIGGER events_cannot_be_truncated
    BEFORE TRUNCATE ON events
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_ledger_rewrite();

-- Messages committed with the transition that caused them, published afterwards.
CREATE TABLE outbox (
    position     bigserial PRIMARY KEY,
    message_id   uuid NOT NULL UNIQUE,
    project_id   text NOT NULL REFERENCES projects,
    stream       text NOT NULL,
    envelope     jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz
);

CREATE INDEX outbox_pending ON outbox (position) WHERE published_at IS NULL;

-- One row per message a consumer has acted on: at-least-once delivery, one durable effect.
CREATE TABLE receipts (
    consumer   text NOT NULL,
    message_id uuid NOT NULL,
    acted_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer, message_id)
);
