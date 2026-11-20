-- The decision basis: the number of the latest decision of the Product Owner's in force
-- over a scope. A feature's counts feature-wide decisions; a story's counts those and
-- its own. Zero if there are none.
CREATE FUNCTION feature_basis(project text, feature text) RETURNS integer
LANGUAGE sql STABLE AS $$
    SELECT coalesce(max(number), 0)::integer FROM decisions
    WHERE project_id = project AND feature_id = feature
      AND source = 'product_owner' AND scope_story = ''
$$;

CREATE FUNCTION story_basis(project text, story text) RETURNS integer
LANGUAGE sql STABLE AS $$
    SELECT coalesce(max(d.number), 0)::integer
    FROM stories s LEFT JOIN decisions d
      ON d.project_id = s.project_id AND d.feature_id = s.feature_id
     AND d.source = 'product_owner' AND d.scope_story IN ('', s.story_id)
    WHERE s.project_id = project AND s.story_id = story
$$;

-- The basis a behaviour's contract, and all the evidence gathered for it, is current
-- on. Null until a contract is registered. The Verifier vouches for the contract and
-- its evidence together or not at all, so one number serves for all of it.
ALTER TABLE behaviours ADD COLUMN current_on integer;

-- Why work on a story, or on one of its behaviours, may not start: something it relies
-- on was made under decisions that have since changed, and its owner has not
-- reconfirmed it. Null if everything is current.
CREATE FUNCTION not_current(project text, story text, behaviour text) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT CASE
        WHEN s.current_on <> story_basis(s.project_id, s.story_id) THEN
            s.story_id || ' is not current: it was confirmed on decision basis '
            || s.current_on || ' and the basis is now ' || story_basis(s.project_id, s.story_id)
        WHEN b.contract IS NOT NULL AND b.current_on <> story_basis(s.project_id, s.story_id) THEN
            'the contract of ' || b.behaviour_id || ' is not current: it was confirmed on '
            || 'decision basis ' || b.current_on || ' and the basis is now '
            || story_basis(s.project_id, s.story_id)
    END
    FROM stories s LEFT JOIN behaviours b
      ON b.project_id = s.project_id AND b.story_id = s.story_id AND b.behaviour_id = behaviour
    WHERE s.project_id = project AND s.story_id = story
$$;

-- The basis each of these began on. What it produces counts only if the basis has not
-- moved by the time it finishes.
ALTER TABLE green_runs ADD COLUMN basis integer NOT NULL;
ALTER TABLE review_checkouts ADD COLUMN basis integer NOT NULL;
ALTER TABLE check_batches ADD COLUMN basis integer NOT NULL;
ALTER TABLE executions ADD COLUMN basis integer;

-- An owner's statement that something made under earlier decisions still stands under
-- the present ones. It is a record of its own: it never rewrites the basis anything
-- was made on.
CREATE TABLE reconfirmations (
    reconfirmation_id uuid PRIMARY KEY,
    project_id        text NOT NULL REFERENCES projects,
    subject           text NOT NULL CHECK (subject IN ('behaviour', 'story', 'roadmap')),
    subject_id        text NOT NULL,
    basis             integer NOT NULL,
    by                text NOT NULL,
    grant_id          uuid NOT NULL,
    -- What was vouched for: a revision, or a contract digest with the evidence kept.
    statement         jsonb NOT NULL,
    recorded_at       timestamptz NOT NULL DEFAULT clock_timestamp()
);
