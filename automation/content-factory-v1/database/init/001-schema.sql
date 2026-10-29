CREATE TABLE IF NOT EXISTS ai_jobs (
    id text PRIMARY KEY,
    type text NOT NULL CHECK (type IN (
        'GENERATE_POST', 'REVISE_POST', 'GENERATE_CONTENT_PLAN', 'REVISE_CONTENT_PLAN'
    )),
    status text NOT NULL CHECK (status IN (
        'PENDING', 'CLAIMED', 'PROCESSING', 'COMPLETED',
        'WAITING_RETRY', 'FAILED', 'CANCELLED'
    )),
    priority integer NOT NULL DEFAULT 100,
    payload_json jsonb NOT NULL,
    result_json jsonb,
    attempt integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
    idempotency_key text NOT NULL UNIQUE,
    worker_id text,
    lease_token text,
    lease_expires_at timestamptz,
    available_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    claimed_at timestamptz,
    started_at timestamptz,
    finished_at timestamptz,
    last_error_code text,
    last_error_message text
);

CREATE INDEX IF NOT EXISTS ix_ai_jobs_claim
    ON ai_jobs (status, available_at, priority, created_at);

CREATE TABLE IF NOT EXISTS post_packages (
    post_id text PRIMARY KEY,
    current_revision integer NOT NULL DEFAULT 1,
    status text NOT NULL CHECK (status IN (
        'DRAFT', 'GENERATING', 'REVIEW', 'REVISION_REQUESTED',
        'APPROVED', 'SCHEDULED', 'PUBLISHING', 'PUBLISHED',
        'DEFERRED', 'CANCELLED', 'FAILED'
    )),
    manual_lock boolean NOT NULL DEFAULT false,
    package_json jsonb NOT NULL,
    approved_at timestamptz,
    approved_by text,
    scheduled_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS post_revisions (
    post_id text NOT NULL REFERENCES post_packages(post_id) ON DELETE CASCADE,
    revision integer NOT NULL,
    origin text NOT NULL CHECK (origin IN ('CODEX', 'MANUAL', 'TECHNICAL')),
    package_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL,
    change_note text NOT NULL DEFAULT '',
    PRIMARY KEY (post_id, revision)
);

CREATE TABLE IF NOT EXISTS publications (
    id bigserial PRIMARY KEY,
    post_id text NOT NULL REFERENCES post_packages(post_id),
    revision integer NOT NULL,
    platform text NOT NULL,
    status text NOT NULL CHECK (status IN (
        'SCHEDULED', 'PUBLISHING', 'PUBLISHED', 'FAILED', 'CANCELLED'
    )),
    scheduled_at timestamptz NOT NULL,
    attempt integer NOT NULL DEFAULT 0,
    idempotency_key text NOT NULL UNIQUE,
    platform_post_id text,
    published_url text,
    published_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker_id text PRIMARY KEY,
    status text NOT NULL,
    codex_status text NOT NULL,
    capabilities jsonb NOT NULL DEFAULT '[]'::jsonb,
    platform text NOT NULL DEFAULT '',
    last_seen_at timestamptz NOT NULL DEFAULT now()
);

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


