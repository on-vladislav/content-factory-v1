from __future__ import annotations

import json
import os
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from pydantic import BaseModel, Field


DATABASE_URL = os.environ["DATABASE_URL"]
WORKER_TOKEN = os.environ["CONTENT_FACTORY_WORKER_TOKEN"]
API_TOKEN = os.environ.get("CONTENT_FACTORY_API_TOKEN", WORKER_TOKEN)
LEASE_SECONDS = 20 * 60

app = FastAPI(title="Content Factory v1 API", version="0.2.0")


def is_worker_endpoint(path: str) -> bool:
    if path in {
        "/api/content-factory/v1/workers/heartbeat",
        "/api/content-factory/v1/jobs/claim",
    }:
        return True
    return path.startswith("/api/content-factory/v1/jobs/") and path.endswith(
        ("/start", "/complete", "/fail")
    )


@app.middleware("http")
async def protect_api(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or is_worker_endpoint(path):
        return await call_next(request)

    expected = f"Bearer {API_TOKEN}"
    authorization = request.headers.get("authorization", "")
    if not secrets.compare_digest(authorization, expected):
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "invalid api token"},
        )
    return await call_next(request)


def connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def require_worker(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {WORKER_TOKEN}"
    if not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid worker token")


class CreateJobRequest(BaseModel):
    post_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{5,79}$")
    source: str = Field(min_length=1, max_length=8000)
    supporting_sources: list[str] = Field(default_factory=list, max_length=20)
    audience: str = Field(min_length=1)
    problem: str = Field(min_length=1)
    main_idea: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    cta: str = ""
    facts: list[str] = []
    limitations: list[str] = []
    additional_instruction: str = Field(default="", max_length=4000)
    max_attempts: int = Field(default=3, ge=1, le=10)


class WorkerRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=100)
    capabilities: list[str] = []


class LeaseRequest(BaseModel):
    worker_id: str
    lease_token: str


class CompleteRequest(LeaseRequest):
    result: dict[str, Any]


class FailBody(BaseModel):
    code: str
    message: str
    retryable: bool


class FailRequest(LeaseRequest):
    error: FailBody


class HeartbeatRequest(BaseModel):
    worker_id: str
    status: str
    codex: str
    platform: str = ""
    timestamp: str = ""
    capabilities: list[str] = []


class ScheduleRequest(BaseModel):
    scheduled_at: datetime
    platform: str = "telegram"


class PublicationClaimRequest(BaseModel):
    platform: str = "telegram"


class PublicationCompleteRequest(BaseModel):
    platform_post_id: str = Field(min_length=1, max_length=200)
    published_url: str = Field(min_length=1, max_length=1000)


class PublicationFailRequest(BaseModel):
    error: str = Field(min_length=1, max_length=2000)


class ManualEditRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=1024)
    change_note: str = Field(default="Ручная правка Влада", max_length=500)


class CreateContentPlanJobRequest(BaseModel):
    plan_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{5,79}$")
    revision: int = Field(default=1, ge=1, le=1000)
    source: str = Field(min_length=1)
    supporting_sources: list[str] = Field(default_factory=list, max_length=20)
    period_start: date
    period_end: date
    goal: str = Field(min_length=1, max_length=1000)
    audience: str = Field(default="Собственники малого бизнеса и руководители компаний сферы услуг", min_length=1, max_length=500)
    item_count: int = Field(default=8, ge=1, le=31)
    additional_instruction: str = Field(default="", max_length=4000)
    max_attempts: int = Field(default=3, ge=1, le=10)


class ContentPlanItem(BaseModel):
    item_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{5,79}$")
    planned_at: datetime
    topic: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=800)
    audience: str = Field(min_length=1, max_length=500)
    problem: str = Field(min_length=1, max_length=500)
    main_idea: str = Field(min_length=1, max_length=500)
    source_reference: str = Field(min_length=1, max_length=500)
    format: str = Field(min_length=1, max_length=100)
    cta: str = Field(default="", max_length=500)
    channel: str = "telegram"
    status: str = "DRAFT"


class ManualEditContentPlanRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=1000)
    items: list[ContentPlanItem] = Field(min_length=1, max_length=31)
    change_note: str = Field(default="Ручная правка контент-плана Владом", max_length=500)


class RequestContentPlanCorrection(BaseModel):
    comment: str = Field(min_length=1, max_length=4000)
    max_attempts: int = Field(default=3, ge=1, le=10)


class RequestPostCorrection(BaseModel):
    comment: str = Field(min_length=1, max_length=4000)
    max_attempts: int = Field(default=3, ge=1, le=10)


@app.get("/health")
def health() -> dict[str, str]:
    with connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/api/content-factory/v1/dev/jobs", status_code=201)
def create_job(body: CreateJobRequest) -> dict[str, Any]:
    job_id = f"JOB_{uuid.uuid4().hex[:12].upper()}"
    payload = {
        "post_id": body.post_id,
        "revision": 1,
        "sources": {"primary": body.source, "supporting": body.supporting_sources},
        "editorial_brief": {
            "audience": body.audience,
            "problem": body.problem,
            "main_idea": body.main_idea,
            "goal": body.goal,
            "cta": body.cta,
            "facts": body.facts,
            "limitations": body.limitations,
        },
        "platforms": ["telegram"],
        "additional_instruction": body.additional_instruction,
        "correction_comment": "",
        "current_post_json": "",
    }
    with connection() as conn:
        row = conn.execute(
            """
            INSERT INTO ai_jobs (id, type, status, payload_json, max_attempts, idempotency_key)
            VALUES (%s, 'GENERATE_POST', 'PENDING', %s::jsonb, %s, %s)
            ON CONFLICT (idempotency_key) DO UPDATE SET
                status = CASE
                    WHEN ai_jobs.status IN ('FAILED', 'WAITING_RETRY') THEN 'PENDING'
                    ELSE ai_jobs.status
                END,
                available_at = CASE
                    WHEN ai_jobs.status IN ('FAILED', 'WAITING_RETRY') THEN now()
                    ELSE ai_jobs.available_at
                END,
                attempt = CASE
                    WHEN ai_jobs.status IN ('FAILED', 'WAITING_RETRY') THEN 0
                    ELSE ai_jobs.attempt
                END,
                worker_id = CASE
                    WHEN ai_jobs.status IN ('FAILED', 'WAITING_RETRY') THEN NULL
                    ELSE ai_jobs.worker_id
                END,
                lease_token = CASE
                    WHEN ai_jobs.status IN ('FAILED', 'WAITING_RETRY') THEN NULL
                    ELSE ai_jobs.lease_token
                END,
                lease_expires_at = CASE
                    WHEN ai_jobs.status IN ('FAILED', 'WAITING_RETRY') THEN NULL
                    ELSE ai_jobs.lease_expires_at
                END,
                last_error_code = CASE
                    WHEN ai_jobs.status IN ('FAILED', 'WAITING_RETRY') THEN NULL
                    ELSE ai_jobs.last_error_code
                END,
                last_error_message = CASE
                    WHEN ai_jobs.status IN ('FAILED', 'WAITING_RETRY') THEN NULL
                    ELSE ai_jobs.last_error_message
                END
            RETURNING id, status, created_at
            """,
            (job_id, json.dumps(payload, ensure_ascii=False), body.max_attempts, f"GENERATE_POST:{body.post_id}:1"),
        ).fetchone()
    return {**dict(row), "post_id": body.post_id}


@app.post("/api/content-factory/v1/dev/content-plans/jobs", status_code=201)
def create_content_plan_job(body: CreateContentPlanJobRequest) -> dict[str, Any]:
    if body.period_end < body.period_start:
        raise HTTPException(status_code=422, detail="period_end must not be earlier than period_start")
    job_id = f"JOB_{uuid.uuid4().hex[:12].upper()}"
    payload = {
        "plan_id": body.plan_id,
        "revision": body.revision,
        "sources": {"primary": body.source, "supporting": body.supporting_sources},
        "period": {"start": body.period_start.isoformat(), "end": body.period_end.isoformat()},
        "goal": body.goal,
        "audience": body.audience,
        "primary_channel": "telegram",
        "timezone": "Europe/Moscow",
        "item_count": body.item_count,
        "additional_instruction": body.additional_instruction,
        "correction_comment": "",
        "current_plan_json": "",
    }
    with connection() as conn:
        row = conn.execute(
            """
            INSERT INTO ai_jobs (id, type, status, payload_json, max_attempts, idempotency_key)
            VALUES (%s, 'GENERATE_CONTENT_PLAN', 'PENDING', %s::jsonb, %s, %s)
            ON CONFLICT (idempotency_key) DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key
            RETURNING id, status, created_at
            """,
            (job_id, json.dumps(payload, ensure_ascii=False), body.max_attempts, f"GENERATE_CONTENT_PLAN:{body.plan_id}:{body.revision}"),
        ).fetchone()
    return dict(row)


@app.post("/api/content-factory/v1/workers/heartbeat", dependencies=[Depends(require_worker)])
def heartbeat(body: HeartbeatRequest) -> dict[str, bool]:
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO worker_heartbeats (worker_id, status, codex_status, capabilities, platform, last_seen_at)
            VALUES (%s, %s, %s, %s::jsonb, %s, now())
            ON CONFLICT (worker_id) DO UPDATE SET
                status = EXCLUDED.status,
                codex_status = EXCLUDED.codex_status,
                capabilities = EXCLUDED.capabilities,
                platform = EXCLUDED.platform,
                last_seen_at = now()
            """,
            (body.worker_id, body.status, body.codex, json.dumps(body.capabilities), body.platform),
        )
    return {"ok": True}


@app.post("/api/content-factory/v1/jobs/claim", dependencies=[Depends(require_worker)])
def claim_job(body: WorkerRequest) -> dict[str, Any]:
    if not body.capabilities:
        return {"job": None}
    lease_token = secrets.token_urlsafe(32)
    lease_expires = datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)
    with connection() as conn:
        with conn.transaction():
            row = conn.execute(
                """
                SELECT id FROM ai_jobs
                WHERE type = ANY(%s) AND ((
                    status IN ('PENDING', 'WAITING_RETRY') AND available_at <= now()
                ) OR (
                    status = 'CLAIMED' AND lease_expires_at < now()
                ))
                ORDER BY priority ASC, created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (body.capabilities,),
            ).fetchone()
            if not row:
                return {"job": None}
            job = conn.execute(
                """
                UPDATE ai_jobs SET
                    status = 'CLAIMED', worker_id = %s, lease_token = %s,
                    lease_expires_at = %s, claimed_at = now(), attempt = attempt + 1
                WHERE id = %s
                RETURNING id, type, status, attempt, max_attempts, lease_token,
                          lease_expires_at, payload_json
                """,
                (body.worker_id, lease_token, lease_expires, row["id"]),
            ).fetchone()
    return {
        "job": {
            "schema_version": "content-factory/ai-job-v1",
            "job_id": job["id"],
            "type": job["type"],
            "status": job["status"],
            "attempt": job["attempt"],
            "max_attempts": job["max_attempts"],
            "lease_token": job["lease_token"],
            "lease_expires_at": job["lease_expires_at"].isoformat(),
            "payload": job["payload_json"],
        }
    }


def assert_lease(conn, job_id: str, worker_id: str, lease_token: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT * FROM ai_jobs
        WHERE id = %s AND worker_id = %s AND lease_token = %s AND lease_expires_at > now()
        FOR UPDATE
        """,
        (job_id, worker_id, lease_token),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=409, detail="lease is invalid or expired")
    return row


@app.post("/api/content-factory/v1/jobs/{job_id}/start", dependencies=[Depends(require_worker)])
def start_job(job_id: str, body: LeaseRequest) -> dict[str, bool]:
    with connection() as conn:
        with conn.transaction():
            assert_lease(conn, job_id, body.worker_id, body.lease_token)
            conn.execute(
                "UPDATE ai_jobs SET status = 'PROCESSING', started_at = now() WHERE id = %s",
                (job_id,),
            )
    return {"ok": True}


@app.post("/api/content-factory/v1/jobs/{job_id}/complete", dependencies=[Depends(require_worker)])
def complete_job(job_id: str, body: CompleteRequest) -> dict[str, bool]:
    result = body.result
    if result.get("job_id") != job_id or result.get("status") != "REVIEW":
        raise HTTPException(status_code=422, detail="result is not bound to this job or not in REVIEW")
    with connection() as conn:
        with conn.transaction():
            job = assert_lease(conn, job_id, body.worker_id, body.lease_token)
            if job["type"] in ("GENERATE_CONTENT_PLAN", "REVISE_CONTENT_PLAN"):
                plan_id = result.get("plan_id")
                if plan_id != job["payload_json"].get("plan_id"):
                    raise HTTPException(status_code=422, detail="plan_id mismatch")
                revision = int(result.get("revision", 1))
                existing = conn.execute(
                    "SELECT manual_lock FROM content_plans WHERE plan_id = %s FOR UPDATE",
                    (plan_id,),
                ).fetchone()
                if existing and existing["manual_lock"]:
                    raise HTTPException(status_code=409, detail="MANUAL_LOCK prevents Codex overwrite")
                conn.execute(
                    """
                    INSERT INTO content_plans (plan_id, current_revision, status, manual_lock, plan_json)
                    VALUES (%s, %s, 'REVIEW', false, %s::jsonb)
                    ON CONFLICT (plan_id) DO UPDATE SET
                        current_revision = EXCLUDED.current_revision,
                        status = 'REVIEW', manual_lock = false,
                        plan_json = EXCLUDED.plan_json, approved_at = null,
                        approved_by = null, updated_at = now()
                    """,
                    (plan_id, revision, json.dumps(result, ensure_ascii=False)),
                )
                conn.execute(
                    """
                    INSERT INTO content_plan_revisions (plan_id, revision, origin, plan_json, created_by)
                    VALUES (%s, %s, 'CODEX', %s::jsonb, %s)
                    ON CONFLICT (plan_id, revision) DO NOTHING
                    """,
                    (plan_id, revision, json.dumps(result, ensure_ascii=False), body.worker_id),
                )
                conn.execute(
                    """
                    UPDATE ai_jobs SET status = 'COMPLETED', result_json = %s::jsonb,
                        finished_at = now(), lease_token = null, lease_expires_at = null,
                        last_error_code = null, last_error_message = null
                    WHERE id = %s
                    """,
                    (json.dumps(result, ensure_ascii=False), job_id),
                )
                return {"ok": True}

            post_id = result.get("post_id")
            if post_id != job["payload_json"].get("post_id"):
                raise HTTPException(status_code=422, detail="post_id mismatch")
            revision = int(result.get("revision", 1))
            existing = conn.execute(
                "SELECT manual_lock FROM post_packages WHERE post_id = %s FOR UPDATE",
                (post_id,),
            ).fetchone()
            if existing and existing["manual_lock"]:
                raise HTTPException(status_code=409, detail="MANUAL_LOCK prevents Codex overwrite")
            conn.execute(
                """
                INSERT INTO post_packages (post_id, current_revision, status, manual_lock, package_json)
                VALUES (%s, %s, 'REVIEW', false, %s::jsonb)
                ON CONFLICT (post_id) DO UPDATE SET
                    current_revision = EXCLUDED.current_revision,
                    status = 'REVIEW',
                    manual_lock = false,
                    package_json = EXCLUDED.package_json,
                    updated_at = now()
                """,
                (post_id, revision, json.dumps(result, ensure_ascii=False)),
            )
            conn.execute(
                """
                INSERT INTO post_revisions (post_id, revision, origin, package_json, created_by)
                VALUES (%s, %s, 'CODEX', %s::jsonb, %s)
                ON CONFLICT (post_id, revision) DO NOTHING
                """,
                (post_id, revision, json.dumps(result, ensure_ascii=False), body.worker_id),
            )
            conn.execute(
                """
                UPDATE ai_jobs SET status = 'COMPLETED', result_json = %s::jsonb,
                    finished_at = now(), lease_token = null, lease_expires_at = null,
                    last_error_code = null, last_error_message = null
                WHERE id = %s
                """,
                (json.dumps(result, ensure_ascii=False), job_id),
            )
    return {"ok": True}


@app.post("/api/content-factory/v1/jobs/{job_id}/fail", dependencies=[Depends(require_worker)])
def fail_job(job_id: str, body: FailRequest) -> dict[str, Any]:
    with connection() as conn:
        with conn.transaction():
            job = assert_lease(conn, job_id, body.worker_id, body.lease_token)
            retry = body.error.retryable and job["attempt"] < job["max_attempts"]
            next_status = "WAITING_RETRY" if retry else "FAILED"
            delay_seconds = min(3600, 30 * (2 ** max(0, job["attempt"] - 1)))
            conn.execute(
                """
                UPDATE ai_jobs SET status = %s, available_at = now() + (%s * interval '1 second'),
                    last_error_code = %s, last_error_message = %s,
                    lease_token = null, lease_expires_at = null,
                    finished_at = CASE WHEN %s = 'FAILED' THEN now() ELSE finished_at END
                WHERE id = %s
                """,
                (next_status, delay_seconds, body.error.code, body.error.message[:2000], next_status, job_id),
            )
    return {"ok": True, "status": next_status}


@app.get("/api/content-factory/v1/dev/jobs")
def list_jobs() -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT id, type, status, attempt, max_attempts, worker_id, created_at, finished_at, last_error_code FROM ai_jobs ORDER BY created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/content-factory/v1/dev/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    with connection() as conn:
        row = conn.execute(
            """
            SELECT id, type, status, attempt, max_attempts, worker_id,
                payload_json, result_json, created_at, started_at, finished_at,
                last_error_code, last_error_message
            FROM ai_jobs WHERE id = %s
            """,
            (job_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="job not found")
    return dict(row)


@app.get("/api/content-factory/v1/dev/posts/{post_id}")
def get_post(post_id: str) -> dict[str, Any]:
    with connection() as conn:
        row = conn.execute(
            """
            SELECT pp.*,
                publication.id AS publication_id,
                publication.status AS publication_status,
                publication.platform_post_id,
                publication.published_url,
                publication.published_at
            FROM post_packages AS pp
            LEFT JOIN LATERAL (
                SELECT id, status, platform_post_id, published_url, published_at
                FROM publications
                WHERE post_id = pp.post_id AND platform = 'telegram'
                ORDER BY id DESC
                LIMIT 1
            ) AS publication ON TRUE
            WHERE pp.post_id = %s
            """,
            (post_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="post not found")
    return dict(row)


@app.post("/api/content-factory/v1/dev/posts/{post_id}/request-correction", status_code=201)
def request_post_correction(post_id: str, body: RequestPostCorrection) -> dict[str, Any]:
    with connection() as conn:
        with conn.transaction():
            post = conn.execute(
                "SELECT * FROM post_packages WHERE post_id = %s FOR UPDATE", (post_id,)
            ).fetchone()
            if not post:
                raise HTTPException(status_code=404, detail="post not found")
            if post["status"] not in ("REVIEW", "APPROVED"):
                raise HTTPException(status_code=409, detail="only REVIEW or unscheduled APPROVED can be sent for correction")
            if post["status"] == "APPROVED":
                publication = conn.execute(
                    """
                    SELECT 1 FROM publications
                    WHERE post_id = %s AND status IN ('SCHEDULED', 'PUBLISHING', 'PUBLISHED')
                    LIMIT 1
                    """,
                    (post_id,),
                ).fetchone()
                if post["scheduled_at"] is not None or publication:
                    raise HTTPException(status_code=409, detail="scheduled or published post cannot be sent for correction")
            if post["manual_lock"]:
                raise HTTPException(status_code=409, detail="MANUAL_LOCK prevents Codex overwrite")
            revision = post["current_revision"] + 1
            current = post["package_json"]
            job_id = f"JOB_{uuid.uuid4().hex[:12].upper()}"
            brief = current["editorial_brief"]
            payload = {
                "post_id": post_id,
                "revision": revision,
                "sources": current["sources"],
                "editorial_brief": {
                    **brief,
                    "facts": [],
                    "limitations": [],
                },
                "platforms": ["telegram"],
                "additional_instruction": "",
                "correction_comment": body.comment,
                "current_post_json": json.dumps(current, ensure_ascii=False),
            }
            conn.execute(
                """
                INSERT INTO ai_jobs (id, type, status, payload_json, max_attempts, idempotency_key)
                VALUES (%s, 'REVISE_POST', 'PENDING', %s::jsonb, %s, %s)
                """,
                (job_id, json.dumps(payload, ensure_ascii=False), body.max_attempts, f"REVISE_POST:{post_id}:{revision}"),
            )
            row = conn.execute(
                """
                UPDATE post_packages SET status = 'REVISION_REQUESTED',
                    approved_at = null, approved_by = null, updated_at = now()
                WHERE post_id = %s RETURNING post_id, current_revision, status, manual_lock
                """,
                (post_id,),
            ).fetchone()
    return {**dict(row), "job_id": job_id, "requested_revision": revision}


@app.get("/api/content-factory/v1/dev/content-plans/{plan_id}")
def get_content_plan(plan_id: str) -> dict[str, Any]:
    with connection() as conn:
        row = conn.execute("SELECT * FROM content_plans WHERE plan_id = %s", (plan_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="content plan not found")
    return dict(row)


@app.post("/api/content-factory/v1/dev/content-plans/{plan_id}/request-correction", status_code=201)
def request_content_plan_correction(plan_id: str, body: RequestContentPlanCorrection) -> dict[str, Any]:
    with connection() as conn:
        with conn.transaction():
            plan = conn.execute(
                "SELECT * FROM content_plans WHERE plan_id = %s FOR UPDATE", (plan_id,)
            ).fetchone()
            if not plan:
                raise HTTPException(status_code=404, detail="content plan not found")
            if plan["status"] != "REVIEW":
                raise HTTPException(status_code=409, detail="only REVIEW can be sent for correction")
            if plan["manual_lock"]:
                raise HTTPException(status_code=409, detail="MANUAL_LOCK prevents Codex overwrite")
            revision = plan["current_revision"] + 1
            current = plan["plan_json"]
            job_id = f"JOB_{uuid.uuid4().hex[:12].upper()}"
            payload = {
                "plan_id": plan_id,
                "revision": revision,
                "sources": current["sources"],
                "period": current["period"],
                "goal": current["goal"],
                "audience": current["items"][0]["audience"],
                "primary_channel": "telegram",
                "timezone": "Europe/Moscow",
                "item_count": len(current["items"]),
                "additional_instruction": "",
                "correction_comment": body.comment,
                "current_plan_json": json.dumps(current, ensure_ascii=False),
            }
            conn.execute(
                """
                INSERT INTO ai_jobs (id, type, status, payload_json, max_attempts, idempotency_key)
                VALUES (%s, 'REVISE_CONTENT_PLAN', 'PENDING', %s::jsonb, %s, %s)
                """,
                (job_id, json.dumps(payload, ensure_ascii=False), body.max_attempts, f"REVISE_CONTENT_PLAN:{plan_id}:{revision}"),
            )
            row = conn.execute(
                """
                UPDATE content_plans SET status = 'REVISION_REQUESTED', updated_at = now()
                WHERE plan_id = %s RETURNING plan_id, current_revision, status, manual_lock
                """,
                (plan_id,),
            ).fetchone()
    return {**dict(row), "job_id": job_id, "requested_revision": revision}


@app.post("/api/content-factory/v1/dev/content-plans/{plan_id}/manual-edit")
def manual_edit_content_plan(plan_id: str, body: ManualEditContentPlanRequest) -> dict[str, Any]:
    with connection() as conn:
        with conn.transaction():
            plan = conn.execute(
                "SELECT * FROM content_plans WHERE plan_id = %s FOR UPDATE", (plan_id,)
            ).fetchone()
            if not plan:
                raise HTTPException(status_code=404, detail="content plan not found")
            if plan["status"] not in ("REVIEW", "APPROVED"):
                raise HTTPException(status_code=409, detail="content plan cannot be edited in its current status")
            package = plan["plan_json"]
            revision = plan["current_revision"] + 1
            package["revision"] = revision
            package["status"] = "REVIEW"
            package["manual_lock"] = True
            package["goal"] = body.goal
            package["items"] = [item.model_dump(mode="json") for item in body.items]
            conn.execute(
                """
                INSERT INTO content_plan_revisions
                    (plan_id, revision, origin, plan_json, created_by, change_note)
                VALUES (%s, %s, 'MANUAL', %s::jsonb, 'VLAD_LOCAL', %s)
                """,
                (plan_id, revision, json.dumps(package, ensure_ascii=False), body.change_note),
            )
            row = conn.execute(
                """
                UPDATE content_plans SET current_revision = %s, status = 'REVIEW',
                    manual_lock = true, plan_json = %s::jsonb,
                    approved_at = null, approved_by = null, updated_at = now()
                WHERE plan_id = %s
                RETURNING plan_id, current_revision, status, manual_lock
                """,
                (revision, json.dumps(package, ensure_ascii=False), plan_id),
            ).fetchone()
    return dict(row)


@app.post("/api/content-factory/v1/dev/content-plans/{plan_id}/approve")
def approve_content_plan(plan_id: str) -> dict[str, Any]:
    with connection() as conn:
        row = conn.execute(
            """
            UPDATE content_plans SET status = 'APPROVED', approved_at = now(),
                approved_by = 'VLAD_LOCAL', updated_at = now()
            WHERE plan_id = %s AND status = 'REVIEW'
            RETURNING plan_id, current_revision, status, manual_lock, approved_at
            """,
            (plan_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=409, detail="only REVIEW can be approved")
    return dict(row)


@app.post("/api/content-factory/v1/dev/posts/{post_id}/approve")
def approve_post(post_id: str) -> dict[str, Any]:
    with connection() as conn:
        row = conn.execute(
            """
            UPDATE post_packages SET status = 'APPROVED', approved_at = now(),
                approved_by = 'VLAD_LOCAL', updated_at = now()
            WHERE post_id = %s AND status = 'REVIEW'
            RETURNING post_id, status, approved_at
            """,
            (post_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=409, detail="only REVIEW can be approved")
    return dict(row)


@app.post("/api/content-factory/v1/dev/posts/{post_id}/manual-edit")
def manual_edit_post(post_id: str, body: ManualEditRequest) -> dict[str, Any]:
    import html as html_module
    import re

    visible = html_module.unescape(re.sub(r"<[^>]+>", "", body.content))
    if len(visible) > 1024:
        raise HTTPException(status_code=422, detail="Telegram content exceeds 1024 visible characters")
    with connection() as conn:
        with conn.transaction():
            post = conn.execute(
                "SELECT * FROM post_packages WHERE post_id = %s FOR UPDATE", (post_id,)
            ).fetchone()
            if not post:
                raise HTTPException(status_code=404, detail="post not found")
            if post["status"] in ("SCHEDULED", "PUBLISHING", "PUBLISHED"):
                raise HTTPException(status_code=409, detail="scheduled or published post cannot be edited")
            package = post["package_json"]
            revision = post["current_revision"] + 1
            package["revision"] = revision
            package["status"] = "REVIEW"
            package["manual_lock"] = True
            package["master_content"] = {"title": body.title, "content": visible}
            package["platforms"]["telegram"].update(
                {
                    "title": body.title,
                    "content": body.content,
                    "visible_characters": len(visible),
                    "publication_status": "NOT_AUTHORIZED",
                    "scheduled_at": None,
                }
            )
            conn.execute(
                """
                INSERT INTO post_revisions
                    (post_id, revision, origin, package_json, created_by, change_note)
                VALUES (%s, %s, 'MANUAL', %s::jsonb, 'VLAD_LOCAL', %s)
                """,
                (post_id, revision, json.dumps(package, ensure_ascii=False), body.change_note),
            )
            row = conn.execute(
                """
                UPDATE post_packages SET current_revision = %s, status = 'REVIEW',
                    manual_lock = true, package_json = %s::jsonb,
                    approved_at = null, approved_by = null, updated_at = now()
                WHERE post_id = %s
                RETURNING post_id, current_revision, status, manual_lock
                """,
                (revision, json.dumps(package, ensure_ascii=False), post_id),
            ).fetchone()
    return dict(row)


@app.post("/api/content-factory/v1/dev/posts/{post_id}/schedule")
def schedule_post(post_id: str, body: ScheduleRequest) -> dict[str, Any]:
    if body.platform != "telegram":
        raise HTTPException(status_code=422, detail="only telegram is supported in MVP")
    with connection() as conn:
        with conn.transaction():
            post = conn.execute(
                "SELECT * FROM post_packages WHERE post_id = %s FOR UPDATE", (post_id,)
            ).fetchone()
            if not post or post["status"] != "APPROVED":
                raise HTTPException(status_code=409, detail="only APPROVED can be scheduled")
            idem = f"{post_id}:{post['current_revision']}:telegram:{body.scheduled_at.isoformat()}"
            conn.execute(
                """
                INSERT INTO publications (post_id, revision, platform, status, scheduled_at, idempotency_key)
                VALUES (%s, %s, 'telegram', 'SCHEDULED', %s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (post_id, post["current_revision"], body.scheduled_at, idem),
            )
            row = conn.execute(
                """
                UPDATE post_packages SET status = 'SCHEDULED', scheduled_at = %s, updated_at = now()
                WHERE post_id = %s RETURNING post_id, status, scheduled_at
                """,
                (body.scheduled_at, post_id),
            ).fetchone()
    return dict(row)


@app.post("/api/content-factory/v1/dev/publications/simulate")
def simulate_publications() -> dict[str, Any]:
    with connection() as conn:
        with conn.transaction():
            rows = conn.execute(
                """
                SELECT * FROM publications
                WHERE status = 'SCHEDULED' AND scheduled_at <= now()
                ORDER BY scheduled_at FOR UPDATE SKIP LOCKED
                """
            ).fetchall()
            published = []
            for row in rows:
                fake_id = f"LOCAL-{row['id']}"
                fake_url = f"local://telegram/{fake_id}"
                conn.execute(
                    """
                    UPDATE publications SET status = 'PUBLISHED', platform_post_id = %s,
                        published_url = %s, published_at = now(), updated_at = now()
                    WHERE id = %s
                    """,
                    (fake_id, fake_url, row["id"]),
                )
                conn.execute(
                    "UPDATE post_packages SET status = 'PUBLISHED', updated_at = now() WHERE post_id = %s",
                    (row["post_id"],),
                )
                published.append({"post_id": row["post_id"], "url": fake_url})
    return {"published": published, "mode": "simulation"}


@app.post("/api/content-factory/v1/publications/claim")
def claim_publication(body: PublicationClaimRequest) -> dict[str, Any]:
    if body.platform != "telegram":
        raise HTTPException(status_code=422, detail="only telegram is supported in MVP")
    with connection() as conn:
        with conn.transaction():
            row = conn.execute(
                """
                SELECT p.id, p.post_id, p.revision, p.platform, p.scheduled_at,
                       pp.package_json
                FROM publications p
                JOIN post_packages pp ON pp.post_id = p.post_id
                WHERE p.status = 'SCHEDULED'
                  AND p.platform = %s
                  AND p.scheduled_at <= now()
                  AND pp.status = 'SCHEDULED'
                  AND pp.current_revision = p.revision
                ORDER BY p.scheduled_at, p.id
                FOR UPDATE OF p SKIP LOCKED
                LIMIT 1
                """,
                (body.platform,),
            ).fetchone()
            if not row:
                return {"publication": None}
            conn.execute(
                """
                UPDATE publications
                SET status = 'PUBLISHING', attempt = attempt + 1,
                    last_error = null, updated_at = now()
                WHERE id = %s
                """,
                (row["id"],),
            )
            telegram = row["package_json"]["platforms"]["telegram"]
            return {
                "publication": {
                    "id": row["id"],
                    "post_id": row["post_id"],
                    "revision": row["revision"],
                    "platform": row["platform"],
                    "scheduled_at": row["scheduled_at"],
                    "title": telegram["title"],
                    "content": telegram["content"],
                    "format": telegram.get("format", "HTML"),
                }
            }


@app.post("/api/content-factory/v1/publications/{publication_id}/complete")
def complete_publication(publication_id: int, body: PublicationCompleteRequest) -> dict[str, Any]:
    with connection() as conn:
        with conn.transaction():
            row = conn.execute(
                """
                UPDATE publications
                SET status = 'PUBLISHED', platform_post_id = %s,
                    published_url = %s, published_at = now(), updated_at = now()
                WHERE id = %s AND status = 'PUBLISHING'
                RETURNING id, post_id, status, published_url, published_at
                """,
                (body.platform_post_id, body.published_url, publication_id),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=409, detail="only PUBLISHING can be completed")
            conn.execute(
                """
                UPDATE post_packages SET status = 'PUBLISHED', updated_at = now()
                WHERE post_id = %s AND status = 'SCHEDULED'
                """,
                (row["post_id"],),
            )
    return dict(row)


@app.post("/api/content-factory/v1/publications/{publication_id}/fail")
def fail_publication(publication_id: int, body: PublicationFailRequest) -> dict[str, Any]:
    with connection() as conn:
        row = conn.execute(
            """
            UPDATE publications
            SET status = 'FAILED', last_error = %s, updated_at = now()
            WHERE id = %s AND status = 'PUBLISHING'
            RETURNING id, post_id, status, last_error
            """,
            (body.error, publication_id),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=409, detail="only PUBLISHING can be failed")
    return dict(row)

