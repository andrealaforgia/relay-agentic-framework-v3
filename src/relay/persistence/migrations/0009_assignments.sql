-- One row per unit of work Relay wants done. A command announces a wanted assignment and
-- may arrive any number of times; only a grant lets a worker begin, and the worker's
-- first effect has to name the grant it was given.
CREATE TABLE assignments (
    assignment_id   uuid PRIMARY KEY,
    project_id      text NOT NULL,
    -- What the work is about: a behaviour, a story, or a feature's roadmap. Its feature
    -- is always recorded, and its story unless the subject is the roadmap.
    subject         text NOT NULL CHECK (subject IN ('behaviour', 'story', 'roadmap')),
    feature_id      text NOT NULL,
    story_id        text,
    behaviour_id    text,
    kind            text NOT NULL
                    CHECK (kind IN ('build', 'remediate', 'review', 'integrate', 'reconfirm',
                                    'correct', 'complete', 'revise')),
    role            text NOT NULL,
    issued_on       jsonb NOT NULL,
    state           text NOT NULL DEFAULT 'wanted'
                    CHECK (state IN ('wanted', 'granted', 'done', 'retired')),
    grant_id        uuid UNIQUE,
    granted_to      text,
    first_effect_at timestamptz,
    command_message_id uuid,
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (project_id, behaviour_id) REFERENCES behaviours,
    FOREIGN KEY (project_id, feature_id) REFERENCES features,
    CHECK ((subject = 'behaviour') = (behaviour_id IS NOT NULL)),
    CHECK ((subject = 'roadmap') = (story_id IS NULL)),
    CHECK (state <> 'wanted' OR grant_id IS NULL),
    CHECK (state <> 'granted' OR grant_id IS NOT NULL),
    CHECK ((grant_id IS NULL) = (granted_to IS NULL))
);

-- A behaviour is being built for one reason at a time.
CREATE UNIQUE INDEX one_build_owed_per_behaviour ON assignments (project_id, behaviour_id)
    WHERE kind IN ('build', 'remediate') AND state IN ('wanted', 'granted');

-- Committed before the oracle is launched: the first effect of a build or a remediation.
-- A pause that finds this record leaves the grant alone and lets the run be judged.
CREATE TABLE green_runs (
    run_id        uuid PRIMARY KEY,
    assignment_id uuid NOT NULL REFERENCES assignments,
    grant_id      uuid NOT NULL,
    attempt       integer NOT NULL,
    state         text NOT NULL DEFAULT 'started'
                  CHECK (state IN ('started', 'finished', 'interrupted')),
    started_at    timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Set when the candidate is published, which is when the review becomes owed.
ALTER TABLE review_tasks ADD COLUMN assignment_id uuid REFERENCES assignments;

CREATE INDEX assignments_by_command ON assignments (command_message_id);

-- The grant under which the tree was handed over. A first submission needs a checkout
-- made under the review's current grant: a tree given to a worker that is gone is
-- history, not a licence for whoever holds the capability next.
ALTER TABLE review_checkouts ADD COLUMN grant_id uuid;
