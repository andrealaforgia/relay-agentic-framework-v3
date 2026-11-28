-- The Product Owner's conversation with Relay (PO-3), hosted by Relay itself.
--
-- A conversation is not an invocation (3C-3.6). It has a durable identity of its own; each
-- turn of it is a paid call with a fresh identity, recorded with the person's own words
-- before the call is made, and settled once.
CREATE TABLE conversations (
    conversation_id  uuid PRIMARY KEY,
    project_id       text NOT NULL REFERENCES projects,
    -- The provider's session this conversation last continued. An economy, not the
    -- mechanism (3C-3.8): a turn can always be made with a new one, from Relay's record.
    provider_session uuid,
    opened_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    closed_at        timestamptz
);

CREATE TABLE conversation_turns (
    turn_id            uuid PRIMARY KEY,
    conversation_id    uuid NOT NULL REFERENCES conversations,
    project_id         text NOT NULL REFERENCES projects,
    number             integer NOT NULL,
    -- What the person typed, as typed: on record before anything is launched.
    said               text NOT NULL,
    role               text NOT NULL DEFAULT 'interpreter' CHECK (role = 'interpreter'),
    model              text NOT NULL,
    state              text NOT NULL DEFAULT 'intended' CHECK (state IN
        ('intended', 'applied', 'errored', 'uncertain', 'not_started')),
    -- Whether the provider's session was continued, or a new one given Relay's record.
    continued          boolean,
    provider_session   uuid,
    reply              text,
    reason             text,
    usage              jsonb,
    provider_cost_usd  numeric,
    estimated_cost_usd numeric,
    price_table        text,
    envelope           jsonb,
    started_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    ended_at           timestamptz,
    UNIQUE (conversation_id, number),
    CHECK ((state = 'intended') = (ended_at IS NULL))
);

-- The Interpreter's analysis of a problem, for a feature or before one exists (3C-2.9).
-- Written by a model and labelled so wherever it is shown. It decides nothing: it becomes
-- a feature only when the Product Owner starts one.
CREATE TABLE analyses (
    project_id      text NOT NULL REFERENCES projects,
    version         integer NOT NULL,
    feature_id      text,
    text            text NOT NULL,
    written_by      text NOT NULL DEFAULT 'model' CHECK (written_by = 'model'),
    conversation_id uuid NOT NULL REFERENCES conversations,
    recorded_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, version),
    FOREIGN KEY (project_id, feature_id) REFERENCES features
);

-- A question the Interpreter means to put to the person in conversation (3C-2.9), and
-- the person's own words in answer to it (3C-3.7). Conversation, and not a decision: a
-- workflow question is put by a worker's reply and answered with the passphrase.
CREATE TABLE notes (
    note_id         uuid PRIMARY KEY,
    project_id      text NOT NULL REFERENCES projects,
    feature_id      text,
    text            text NOT NULL,
    conversation_id uuid NOT NULL REFERENCES conversations,
    noted_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    answered_in     uuid REFERENCES conversation_turns,
    answer          text,
    answered_at     timestamptz,
    FOREIGN KEY (project_id, feature_id) REFERENCES features,
    CHECK ((answered_in IS NULL) = (answer IS NULL)),
    CHECK ((answered_in IS NULL) = (answered_at IS NULL))
);

-- Who prepared an action: the session's Interpreter, or someone at the command line.
ALTER TABLE prepared_actions ADD COLUMN prepared_by text NOT NULL DEFAULT 'the command line';
