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
