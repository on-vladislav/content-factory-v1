ALTER TABLE ai_jobs DROP CONSTRAINT IF EXISTS ai_jobs_type_check;
ALTER TABLE ai_jobs ADD CONSTRAINT ai_jobs_type_check CHECK (
    type IN ('GENERATE_POST', 'REVISE_POST', 'GENERATE_CONTENT_PLAN', 'REVISE_CONTENT_PLAN')
);

UPDATE ai_jobs
SET payload_json = payload_json || '{"correction_comment":"","current_post_json":""}'::jsonb
WHERE type = 'GENERATE_POST'
  AND (NOT payload_json ? 'correction_comment' OR NOT payload_json ? 'current_post_json');

CREATE TABLE IF NOT EXISTS content_plans (
    plan_id text PRIMARY KEY,
    current_revision integer NOT NULL DEFAULT 1,
    status text NOT NULL CHECK (status IN (
        'GENERATING', 'REVIEW', 'REVISION_REQUESTED', 'APPROVED', 'CANCELLED', 'FAILED'
    )),
    manual_lock boolean NOT NULL DEFAULT false,
    plan_json jsonb NOT NULL,
    approved_at timestamptz,
    approved_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS content_plan_revisions (
    plan_id text NOT NULL REFERENCES content_plans(plan_id) ON DELETE CASCADE,
    revision integer NOT NULL,
    origin text NOT NULL CHECK (origin IN ('CODEX', 'MANUAL', 'TECHNICAL')),
    plan_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL,
    change_note text NOT NULL DEFAULT '',
    PRIMARY KEY (plan_id, revision)
);

