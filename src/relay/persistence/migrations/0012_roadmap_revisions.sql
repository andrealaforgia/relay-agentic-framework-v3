-- What one roadmap revision says about each story, exactly as it was proposed. A story of
-- the roadmap being revised is kept, reworded or dropped; a new story is added, and may
-- correct a rejected one. The text of a reworded or new story is stored here and
-- reaches the stories table only if a person approves the revision.
CREATE TABLE roadmap_entries (
    project_id     text NOT NULL,
    feature_id     text NOT NULL,
    revision       integer NOT NULL,
    story_id       text NOT NULL,
    -- Where the story comes in this revision's order; null for a dropped one.
    position       integer,
    kind           text NOT NULL CHECK (kind IN ('kept', 'reworded', 'new', 'dropped')),
    title          text,
    outcome        text,
    criteria       jsonb,
    corrects       text,
    -- For a reworded or dropped story: its revision and decision basis as the Analyst
    -- read them. If either has moved by approval, the edit is no longer about that story.
    story_revision integer,
    story_basis    integer,
    PRIMARY KEY (project_id, feature_id, revision, story_id),
    FOREIGN KEY (project_id, feature_id, revision) REFERENCES roadmaps,
    CHECK ((kind = 'dropped') = (position IS NULL)),
    CHECK ((kind IN ('reworded', 'new')) = (title IS NOT NULL)),
    CHECK ((kind IN ('reworded', 'dropped')) = (story_revision IS NOT NULL))
);

-- A feature has at most one proposal waiting, whether or not a gate is open on it.
CREATE UNIQUE INDEX one_proposal_per_feature ON roadmaps (project_id, feature_id)
    WHERE state = 'proposed';

-- And one roadmap in force.
CREATE UNIQUE INDEX one_roadmap_in_force ON roadmaps (project_id, feature_id)
    WHERE state = 'approved';
