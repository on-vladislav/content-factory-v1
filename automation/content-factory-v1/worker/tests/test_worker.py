from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WORKER_DIR = Path(__file__).resolve().parents[1]
MODULE_ROOT = WORKER_DIR.parent
sys.path.insert(0, str(WORKER_DIR))

from content_factory_worker import (  # noqa: E402
    InvalidResultError,
    PermanentWorkerError,
    WorkerConfig,
    build_codex_command,
    build_prompt,
    process_job,
    run_codex,
    read_primary_source,
    simulate_codex,
)
from schema_validator import SchemaValidationError, validate  # noqa: E402


def sample_job() -> dict:
    return {
        "schema_version": "content-factory/ai-job-v1",
        "job_id": "JOB_000001",
        "type": "GENERATE_POST",
        "status": "CLAIMED",
        "attempt": 1,
        "max_attempts": 3,
        "lease_token": "lease-token-00000001",
        "lease_expires_at": "2026-10-02T12:00:00Z",
        "payload": {
            "post_id": "test-post-0001",
            "revision": 1,
            "sources": {"primary": "reports/2026-08-30-source-events-registry.md", "supporting": []},
            "editorial_brief": {
                "audience": "Собственники малого бизнеса",
                "problem": "Ручная работа",
                "main_idea": "Сначала разобрать процесс",
                "goal": "Показать подход",
                "cta": "Написать Владу",
                "facts": [],
                "limitations": [],
            },
            "platforms": ["telegram"],
            "additional_instruction": "",
            "correction_comment": "",
            "current_post_json": "",
        },
    }


def sample_result() -> dict:
    return {
        "schema_version": "content-factory/post-package-v1",
        "post_id": "test-post-0001",
        "job_id": "JOB_000001",
        "revision": 1,
        "status": "REVIEW",
        "manual_lock": False,
        "sources": {"primary": "reports/2026-08-30-source-events-registry.md", "supporting": []},
        "editorial_brief": {
            "audience": "Собственники малого бизнеса",
            "problem": "Ручная работа",
            "main_idea": "Сначала разобрать процесс",
            "goal": "Показать подход",
            "cta": "Написать Владу",
        },
        "master_content": {"title": "Тест", "content": "Тестовый материал"},
        "platforms": {
            "telegram": {
                "title": "Тест",
                "content": "Тестовый материал",
                "format": "HTML",
                "visible_characters": 17,
                "hashtags": [],
                "publication_status": "NOT_AUTHORIZED",
                "scheduled_at": None,
            }
        },
        "review": {
            "facts_verified": True,
            "needs_clarification": False,
            "clarification_questions": [],
            "warnings": [],
        },
    }


def sample_content_plan_job() -> dict:
    return {
        "schema_version": "content-factory/ai-job-v1",
        "job_id": "JOB_PLAN01",
        "type": "GENERATE_CONTENT_PLAN",
        "status": "CLAIMED",
        "attempt": 1,
        "max_attempts": 3,
        "lease_token": "lease-token-plan-0001",
        "lease_expires_at": "2026-10-02T12:00:00Z",
        "payload": {
            "plan_id": "october-plan-2026",
            "revision": 1,
            "sources": {"primary": "reports/2026-08-30-source-events-registry.md", "supporting": []},
            "period": {"start": "2026-10-05", "end": "2026-10-31"},
            "goal": "Собрать очередь подтвержденных тем",
            "audience": "Собственники малого бизнеса",
            "primary_channel": "telegram",
            "timezone": "Europe/Moscow",
            "item_count": 8,
            "additional_instruction": "",
            "correction_comment": "",
            "current_plan_json": "",
        },
    }


class FakeApi:
    def __init__(self):
        self.calls = []

    def post(self, path, payload):
        self.calls.append((path, payload))
        return {"ok": True}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.job_schema = json.loads(
            (MODULE_ROOT / "schemas" / "ai-job.schema.json").read_text(encoding="utf-8")
        )
        self.result_schema_path = MODULE_ROOT / "schemas" / "codex-post-result.schema.json"
        self.config = WorkerConfig(
            api_base_url="https://example.test",
            worker_id="VLAD-PC",
            project_root=MODULE_ROOT.parents[1],
            skip_git_repo_check=True,
        )

    def test_job_schema_accepts_mvp_job(self):
        validate(sample_job(), self.job_schema)

    def test_job_schema_rejects_unknown_platform(self):
        job = sample_job()
        job["payload"]["platforms"] = ["vk"]
        with self.assertRaises(SchemaValidationError):
            validate(job, self.job_schema)

    def test_codex_command_uses_read_only_and_schema(self):
        command = build_codex_command(self.config, self.result_schema_path, Path("result.json"))
        self.assertIn("read-only", command)
        self.assertIn("--output-schema", command)
        self.assertIn('model_reasoning_effort="medium"', command)
        self.assertIn("--skip-git-repo-check", command)
        self.assertEqual(command[-1], "-")

    def test_run_codex_validates_and_returns_result(self):
        result = sample_result()

        def runner(command, **kwargs):
            output_path = Path(command[command.index("-o") + 1])
            output_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        actual = run_codex(self.config, sample_job(), self.result_schema_path, runner=runner)
        self.assertEqual(actual["status"], "REVIEW")

    def test_run_codex_rejects_result_for_another_job(self):
        result = sample_result()
        result["job_id"] = "JOB_999999"

        def runner(command, **kwargs):
            output_path = Path(command[command.index("-o") + 1])
            output_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with self.assertRaises(InvalidResultError):
            run_codex(self.config, sample_job(), self.result_schema_path, runner=runner)

    def test_run_codex_normalizes_incorrect_visible_length(self):
        result = sample_result()
        result["platforms"]["telegram"]["visible_characters"] = 999

        def runner(command, **kwargs):
            output_path = Path(command[command.index("-o") + 1])
            output_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        normalized = run_codex(
            self.config, sample_job(), self.result_schema_path, runner=runner
        )
        self.assertEqual(
            normalized["platforms"]["telegram"]["visible_characters"],
            len("Тестовый материал"),
        )

    def test_config_rejects_disabled_tls_verification(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "factory.json"
            config_path.write_text(
                json.dumps(
                    {
                        "api_base_url": "https://example.test",
                        "worker_id": "VLAD-PC",
                        "project_root": str(MODULE_ROOT),
                        "verify_tls": False,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(Exception):
                WorkerConfig.load(config_path)

    def test_process_job_starts_then_completes(self):
        api = FakeApi()
        process_job(
            api,
            self.config,
            sample_job(),
            self.job_schema,
            self.result_schema_path,
            codex_runner=lambda *_: sample_result(),
        )
        self.assertTrue(api.calls[0][0].endswith("/start"))
        self.assertTrue(api.calls[1][0].endswith("/complete"))
        self.assertEqual(api.calls[1][1]["result"]["status"], "REVIEW")

    def test_simulation_returns_schema_valid_package(self):
        result = simulate_codex(self.config, sample_job(), self.result_schema_path)
        self.assertEqual(result["status"], "REVIEW")
        self.assertIn("SIMULATION", result["review"]["warnings"][0])

    def test_content_plan_job_schema_and_simulation(self):
        job = sample_content_plan_job()
        job_schema = json.loads(
            (MODULE_ROOT / "schemas" / "ai-job-content-plan.schema.json").read_text(encoding="utf-8")
        )
        result_schema_path = MODULE_ROOT / "schemas" / "codex-content-plan-result.schema.json"
        validate(job, job_schema)
        result = simulate_codex(self.config, job, result_schema_path)
        self.assertEqual(result["status"], "REVIEW")
        self.assertFalse(result["manual_lock"])
        self.assertEqual(result["plan_id"], "october-plan-2026")

    def test_primary_source_cannot_escape_project_root(self):
        job = sample_job()
        job["payload"]["sources"]["primary"] = "..\\outside.md"
        with self.assertRaises(PermanentWorkerError):
            read_primary_source(self.config, job)

    def test_inline_primary_source_is_supported(self):
        job = sample_job()
        job["payload"]["sources"]["primary"] = "inline:Идея из Telegram"
        self.assertEqual(read_primary_source(self.config, job), ("inline:notion", "Идея из Telegram"))

    def test_inline_primary_source_cannot_be_empty(self):
        job = sample_job()
        job["payload"]["sources"]["primary"] = "inline:   "
        with self.assertRaises(PermanentWorkerError):
            read_primary_source(self.config, job)

    def test_prompt_embeds_rules_and_supporting_sources(self):
        job = sample_job()
        job["payload"]["sources"]["supporting"] = ["content-registry.yaml"]
        prompt = build_prompt(self.config, job)
        self.assertIn("ПРАВИЛА ПРОЕКТА (AGENTS.md)", prompt)
        self.assertIn("ИСТОЧНИК (content-registry.yaml)", prompt)


if __name__ == "__main__":
    unittest.main()
