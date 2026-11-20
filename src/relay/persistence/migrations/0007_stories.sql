-- A feature is one problem the Product Owner brought to a project.
CREATE TABLE features (
    project_id text NOT NULL REFERENCES projects,
    feature_id text NOT NULL,
    problem    text NOT NULL,
    state      text NOT NULL DEFAULT 'planning'
        CHECK (state IN ('discovery', 'planning', 'delivering', 'completed', 'cancelled')),
    paused     boolean NOT NULL DEFAULT false,
    -- Features of a project take their turn in the order they were opened.
    opened     bigserial,
    PRIMARY KEY (project_id, feature_id)
);

-- The ordered stories of a feature, as proposed at one moment. Only an approved
-- revision releases anything.
CREATE TABLE roadmaps (
    project_id text NOT NULL,
    feature_id text NOT NULL,
    revision   integer NOT NULL,
    state      text NOT NULL DEFAULT 'proposed'
        CHECK (state IN ('proposed', 'approved', 'superseded')),
    PRIMARY KEY (project_id, feature_id, revision),
    FOREIGN KEY (project_id, feature_id) REFERENCES features
);

CREATE TABLE stories (
    project_id       text NOT NULL,
    story_id         text NOT NULL,
    feature_id       text NOT NULL,
    roadmap_revision integer NOT NULL,
    position         integer NOT NULL,
    title            text NOT NULL,
    outcome          text NOT NULL,
    -- Acceptance criteria by stable id: {"AC1": "text", ...}
    criteria         jsonb NOT NULL,
    state            text NOT NULL DEFAULT 'planned' CHECK (state IN
        ('planned', 'released', 'changes_requested', 'awaiting_approval', 'approved', 'rejected')),
    revision         integer NOT NULL DEFAULT 1,
    -- Every change to the list of behaviours makes a new list revision; the Verifier's
    -- seal names the one it vouches for.
    list_revision    integer NOT NULL DEFAULT 0,
    sealed_revision  integer,
    PRIMARY KEY (project_id, story_id),
    FOREIGN KEY (project_id, feature_id, roadmap_revision) REFERENCES roadmaps
);

-- One story at a time per project. A story at its gate still holds the place.
CREATE UNIQUE INDEX one_story_at_a_time ON stories (project_id)
    WHERE state IN ('released', 'changes_requested', 'awaiting_approval');

-- There is no behaviour without a story, and so none outside a feature's gates.
ALTER TABLE behaviours ADD COLUMN story_id text NOT NULL;
ALTER TABLE behaviours ADD COLUMN position integer NOT NULL;
ALTER TABLE behaviours ADD COLUMN demonstrates text[] NOT NULL;
ALTER TABLE behaviours ADD FOREIGN KEY (project_id, story_id) REFERENCES stories;

-- The Analyst's view of whether a sealed list shows the story's outcome. Code does not
-- judge it. Code requires it, and shows it to the Product Owner as it was written.
CREATE TABLE story_assessments (
    assessment_id  uuid PRIMARY KEY,
    project_id     text NOT NULL,
    story_id       text NOT NULL,
    story_revision integer NOT NULL,
    list_revision  integer NOT NULL,
    statement      text NOT NULL,
    recorded_at    timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, story_id) REFERENCES stories,
    UNIQUE (project_id, story_id, story_revision, list_revision)
);

-- A point where only a person may decide. The manifest is exactly what that person is
-- shown, kept whole so the same review can be shown again after any restart.
CREATE TABLE gates (
    gate_id         uuid PRIMARY KEY,
    project_id      text NOT NULL,
    feature_id      text NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('roadmap', 'story')),
    story_id        text,
    -- What the gate was opened on. A gate whose basis is no longer current is stale.
    basis           jsonb NOT NULL,
    manifest        jsonb NOT NULL,
    manifest_digest text NOT NULL,
    state           text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'decided', 'superseded')),
    outcome         text,
    opened_at       timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, feature_id) REFERENCES features
);

CREATE UNIQUE INDEX one_open_gate_per_subject
    ON gates (project_id, feature_id, kind, coalesce(story_id, '')) WHERE state = 'open';

-- What is waiting for the Product Owner. This table, not a message, is the inbox.
CREATE TABLE inbox_items (
    item_id    uuid PRIMARY KEY,
    project_id text NOT NULL REFERENCES projects,
    kind       text NOT NULL CHECK (kind IN ('gate')),
    gate_id    uuid REFERENCES gates,
    state      text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed')),
    opened_at  timestamptz NOT NULL DEFAULT now()
);

-- One row per thing a person asked for. The receipt acknowledges exactly that request.
CREATE TABLE human_actions (
    principal   text NOT NULL,
    request_id  text NOT NULL,
    target      text NOT NULL,
    action      text NOT NULL,
    payload     jsonb NOT NULL,
    digest      text NOT NULL,
    disposition text NOT NULL CHECK (disposition IN ('effective', 'stale')),
    outcome     text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal, request_id)
);
