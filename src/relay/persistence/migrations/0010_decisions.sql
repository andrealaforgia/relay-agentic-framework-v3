-- A question for the Product Owner. Its scope is a whole feature (scope_story = '') or
-- one story of it. While it is open, nothing starts in that scope.
CREATE TABLE questions (
    question_id uuid PRIMARY KEY,
    project_id  text NOT NULL,
    feature_id  text NOT NULL,
    scope_story text NOT NULL DEFAULT '',
    asked_by    text NOT NULL,
    request_id  text NOT NULL,
    digest      text NOT NULL,
    text        text NOT NULL,
    why         text NOT NULL,
    options     jsonb NOT NULL,
    state       text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'answered', 'withdrawn')),
    asked_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (project_id, feature_id) REFERENCES features,
    -- A retry after a lost reply is the same question, not a second one.
    UNIQUE (asked_by, request_id)
);

-- What was decided, by whom. Numbered per feature in the order decisions commit. One
-- answer per question; one successor per decision, in the same scope as the one it
-- replaces, which the composite key makes impossible to get wrong.
CREATE TABLE decisions (
    decision_id uuid PRIMARY KEY,
    project_id  text NOT NULL,
    feature_id  text NOT NULL,
    scope_story text NOT NULL DEFAULT '',
    number      integer NOT NULL,
    source      text NOT NULL CHECK (source IN ('product_owner', 'model')),
    text        text NOT NULL,
    question_id uuid UNIQUE REFERENCES questions,
    replaces    uuid UNIQUE,
    decided_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (project_id, feature_id) REFERENCES features,
    UNIQUE (project_id, feature_id, number),
    UNIQUE (decision_id, project_id, feature_id, scope_story),
    FOREIGN KEY (replaces, project_id, feature_id, scope_story)
        REFERENCES decisions (decision_id, project_id, feature_id, scope_story)
);

ALTER TABLE inbox_items ADD COLUMN question_id uuid REFERENCES questions;

-- What stops work in a scope, if anything: the one place that says so, for SQL and for
-- code alike. Null: nothing. 'cancelled' exactly, for a cancelled feature. Otherwise
-- every reason that applies, the pause first.
CREATE FUNCTION standing(project text, feature text, story text) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT CASE
        WHEN f.closed_by = 'cancelled' THEN 'cancelled'
        ELSE nullif(concat_ws(', and ', f.closed_by, (
            SELECT 'waiting on question ' || string_agg(q.question_id::text, ', ' ORDER BY q.asked_at)
            FROM questions q
            WHERE q.project_id = f.project_id AND q.feature_id = f.feature_id
              AND q.state = 'open' AND (q.scope_story = '' OR q.scope_story = story)
        )), '')
    END
    FROM features f WHERE f.project_id = project AND f.feature_id = feature
$$;
