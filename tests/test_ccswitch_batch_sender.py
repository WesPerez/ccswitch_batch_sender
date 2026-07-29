from __future__ import annotations

import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

import ccswitch_batch_sender as sender


def provider_settings(*, key: str = "test-key", base_url: str = "https://api.example.test", model: str = "gpt-test") -> str:
    return json.dumps(
        {
            "auth": {"OPENAI_API_KEY": key},
            "config": f'model = "{model}"\nbase_url = "{base_url}"\nwire_api = "responses"\n',
        }
    )


class TempCcSwitchDb:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ccswitch-batch-test-")
        self.root = Path(self.temp.name)
        self.db_path = self.root / "cc-switch.db"
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            """
            CREATE TABLE providers (
                id TEXT NOT NULL,
                app_type TEXT NOT NULL,
                name TEXT NOT NULL,
                settings_config TEXT,
                meta TEXT,
                is_current INTEGER DEFAULT 0,
                sort_index INTEGER DEFAULT 0
            )
            """
        )
        connection.commit()
        connection.close()

    def add(
        self,
        provider_id: str,
        name: str,
        *,
        current: bool = False,
        key: str = "test-key",
        model: str = "gpt-test",
        base_url: str = "https://api.example.test",
        app_type: str = "codex",
        api_format: str = "openai_responses",
        sort_index: int = 0,
    ) -> None:
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            """
            INSERT INTO providers (id, app_type, name, settings_config, meta, is_current, sort_index)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider_id,
                app_type,
                name,
                provider_settings(key=key, base_url=base_url, model=model),
                json.dumps({"apiFormat": api_format}),
                int(current),
                sort_index,
            ),
        )
        connection.commit()
        connection.close()

    def set_pointer(self, provider_id: str) -> None:
        (self.root / "settings.json").write_text(
            json.dumps({"currentProviderCodex": provider_id}),
            encoding="utf-8",
        )

    def close(self) -> None:
        self.temp.cleanup()


class ConfigTests(unittest.TestCase):
    def test_windowed_logger_tolerates_missing_console_streams(self) -> None:
        logger = sender.RunLogger()
        with mock.patch.object(sender.sys, "stdout", None), mock.patch.object(sender.sys, "stderr", None):
            logger.log("windowed dry run")

    def test_zero_request_count_is_rejected_instead_of_replaced(self) -> None:
        with self.assertRaises(sender.SenderError):
            sender.normalize_config({"request_count": 0})

    def test_retry_count_sets_finite_post_cap(self) -> None:
        config = sender.normalize_config({"request_count": 7, "retry_count": 2})
        self.assertEqual(config["request_count"] * (1 + config["retry_count"]), 21)

    def test_default_uses_random_real_probes(self) -> None:
        config = sender.normalize_config()
        self.assertEqual(config["transport_mode"], sender.TRANSPORT_CODEX_CLI)
        self.assertEqual(config["cli_concurrency"], 4)
        self.assertTrue(config["random_probe_enabled"])
        self.assertGreaterEqual(config["max_output_tokens"], 32)

    def test_v1_saved_defaults_are_migrated_for_real_probes(self) -> None:
        migrated = sender.migrate_saved_config({"message": "1", "max_output_tokens": 1}, 1)
        self.assertTrue(migrated["random_probe_enabled"])
        self.assertEqual(migrated["message"], sender.DEFAULT_FIXED_MESSAGE)
        self.assertEqual(migrated["max_output_tokens"], sender.DEFAULT_CONFIG["max_output_tokens"])

    def test_v2_saved_custom_json_stays_on_direct_transport(self) -> None:
        migrated = sender.migrate_saved_config({"custom_body_enabled": True}, 2)
        self.assertEqual(migrated["transport_mode"], sender.TRANSPORT_DIRECT)
        self.assertEqual(migrated["cli_concurrency"], 4)

    def test_custom_body_can_replace_the_prompt(self) -> None:
        config = sender.normalize_config(
            {
                "message": "",
                "custom_body_enabled": True,
                "custom_body": {"model": "x", "input": "custom"},
            }
        )
        self.assertEqual(config["custom_body"]["input"], "custom")

    def test_persisted_payload_excludes_provider_and_sensitive_runtime_fields(self) -> None:
        config = sender.normalize_config({"provider_id": "provider-a", "db_path": "X:/private.db"})
        payload = sender.persistent_config_payload(config)
        self.assertNotIn("provider_id", payload)
        self.assertNotIn("db_path", payload)
        self.assertNotIn("user_agent", payload)
        self.assertNotIn("api_key", json.dumps(payload))

    @unittest.skipUnless(os.name == "nt", "Windows named mutex")
    def test_named_mutex_prevents_a_second_instance(self) -> None:
        name = rf"Local\CCSwitchBatchSender.Test.{uuid.uuid4()}"
        first = sender.SingleInstanceMutex(name)
        second = sender.SingleInstanceMutex(name)
        third = sender.SingleInstanceMutex(name)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
        finally:
            first.release()
            second.release()
        try:
            self.assertTrue(third.acquire())
        finally:
            third.release()


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = TempCcSwitchDb()

    def tearDown(self) -> None:
        self.db.close()

    def test_settings_pointer_wins_over_is_current(self) -> None:
        self.db.add("provider-a", "Provider A", current=True, sort_index=1)
        self.db.add("provider-b", "Provider B", current=False, sort_index=2, model="gpt-b")
        self.db.set_pointer("provider-b")
        config = sender.normalize_config({"db_path": str(self.db.db_path), "provider_id": "current"})

        catalog = sender.list_codex_providers(config)
        provider = sender.load_provider(config)

        self.assertEqual(catalog.current_provider_id, "provider-b")
        self.assertEqual(provider.provider_id, "provider-b")
        self.assertEqual(provider.model, "gpt-b")

    def test_falls_back_to_unique_is_current(self) -> None:
        self.db.add("provider-a", "Provider A", current=True)
        config = sender.normalize_config({"db_path": str(self.db.db_path), "provider_id": "current"})
        self.assertEqual(sender.load_provider(config).provider_id, "provider-a")

    def test_catalog_marks_unavailable_provider_without_exposing_key(self) -> None:
        self.db.add("provider-a", "Provider A", current=True, key="")
        config = sender.normalize_config({"db_path": str(self.db.db_path)})
        catalog = sender.list_codex_providers(config)
        self.assertEqual(len(catalog.providers), 1)
        self.assertFalse(catalog.providers[0].available)
        self.assertIn("无 API Key", catalog.providers[0].unavailable_reason)

    def test_non_codex_rows_are_not_listed(self) -> None:
        self.db.add("provider-a", "Provider A", current=True)
        self.db.add("claude-a", "Claude A", app_type="claude")
        catalog = sender.list_codex_providers(sender.normalize_config({"db_path": str(self.db.db_path)}))
        self.assertEqual([item.provider_id for item in catalog.providers], ["provider-a"])


class ProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = sender.Provider(
            provider_id="provider-a",
            name="Provider A",
            api_key="secret",
            base_url="https://api.example.test",
            model="gpt-test",
            api_format="openai_responses",
        )

    def test_response_body_shape_and_preview_cache_key(self) -> None:
        config = sender.normalize_config(
            {"message": "probe", "random_probe_enabled": False, "max_output_tokens": 3}
        )
        body = sender.build_preview_body(self.provider, config)
        self.assertEqual(body["model"], "gpt-test")
        self.assertEqual(body["input"][0]["content"][0]["text"], "probe")
        self.assertNotIn("instructions", body)
        self.assertEqual(body["prompt_cache_key"], sender.PROMPT_CACHE_KEY_PLACEHOLDER)
        self.assertEqual(body["max_output_tokens"], 3)

    def test_random_preview_uses_readable_placeholders(self) -> None:
        body = sender.build_preview_body(self.provider, sender.normalize_config())
        self.assertEqual(body["input"][0]["content"][0]["text"], sender.RANDOM_PROBE_PLACEHOLDER)
        self.assertEqual(body["prompt_cache_key"], sender.PROMPT_CACHE_KEY_PLACEHOLDER)

    def test_custom_body_is_copied_exactly(self) -> None:
        config = sender.normalize_config(
            {
                "custom_body_enabled": True,
                "custom_body": {"model": "custom", "input": [{"role": "user", "content": "x"}]},
            }
        )
        body = sender.build_body(self.provider, config)
        body["model"] = "changed"
        self.assertEqual(config["custom_body"]["model"], "custom")

    def test_custom_body_replaces_exact_placeholders_per_request(self) -> None:
        template = {
            "model": "custom",
            "input": [{"role": "user", "content": sender.RANDOM_PROBE_PLACEHOLDER}],
            "prompt_cache_key": sender.PROMPT_CACHE_KEY_PLACEHOLDER,
        }
        config = sender.normalize_config({"custom_body_enabled": True, "custom_body": template})
        body = sender.build_body(
            self.provider,
            config,
            cache_key="cache-123",
            request_prompt="自然任务",
        )
        self.assertEqual(body["input"][0]["content"], "自然任务")
        self.assertEqual(body["prompt_cache_key"], "cache-123")
        self.assertEqual(config["custom_body"], template)

    def test_legacy_random_probe_placeholder_still_works(self) -> None:
        config = sender.normalize_config(
            {
                "custom_body_enabled": True,
                "custom_body": {"input": sender.LEGACY_RANDOM_PROBE_PLACEHOLDER},
            }
        )
        body = sender.build_body(self.provider, config, request_prompt="兼容任务")
        self.assertEqual(body["input"], "兼容任务")

    def test_custom_preview_keeps_cache_placeholder_readable(self) -> None:
        config = sender.normalize_config(
            {
                "custom_body_enabled": True,
                "unique_prompt_cache_key": False,
                "custom_body": {"model": "custom", "prompt_cache_key": sender.PROMPT_CACHE_KEY_PLACEHOLDER},
            }
        )
        preview = sender.build_preview_body(self.provider, config)
        self.assertEqual(preview["prompt_cache_key"], sender.PROMPT_CACHE_KEY_PLACEHOLDER)

    def test_custom_body_preserves_explicit_cache_key(self) -> None:
        config = sender.normalize_config(
            {
                "custom_body_enabled": True,
                "custom_body": {"model": "custom", "input": "x", "prompt_cache_key": "fixed-key"},
            }
        )
        body = sender.build_body(self.provider, config, cache_key="replacement")
        self.assertEqual(body["prompt_cache_key"], "fixed-key")

    def test_placeholders_require_an_exact_json_string_value(self) -> None:
        prompt_value = f"前缀 {sender.RANDOM_TASK_PLACEHOLDER}"
        cache_value = f"前缀 {sender.PROMPT_CACHE_KEY_PLACEHOLDER}"
        config = sender.normalize_config(
            {
                "custom_body_enabled": True,
                "custom_body": {"input": prompt_value, "prompt_cache_key": cache_value},
            }
        )
        body = sender.build_body(self.provider, config, cache_key="replacement", request_prompt="替换任务")
        self.assertEqual(body["input"], prompt_value)
        self.assertEqual(body["prompt_cache_key"], cache_value)

    def test_each_prepared_request_gets_its_own_prompt_and_cache_key(self) -> None:
        probes = [
            sender.ProbeCase(prompt="任务 A", expected={"value": 1}),
            sender.ProbeCase(prompt="任务 B", expected={"value": 2}),
        ]
        config = sender.normalize_config()
        with mock.patch.object(sender, "generate_probe_case", side_effect=probes), mock.patch.object(
            sender.uuid,
            "uuid4",
            side_effect=["cache-a", "cache-b"],
        ):
            first_body, first_prompt, _ = sender.prepare_request_body(self.provider, config)
            second_body, second_prompt, _ = sender.prepare_request_body(self.provider, config)
        self.assertEqual((first_prompt, second_prompt), ("任务 A", "任务 B"))
        self.assertEqual(first_body["prompt_cache_key"], "cache-a")
        self.assertEqual(second_body["prompt_cache_key"], "cache-b")

    def test_probe_generation_and_semantic_validation(self) -> None:
        probe = sender.generate_probe_case(random.Random(7))
        valid, reason = sender.validate_probe_response(json.dumps(probe.expected), probe)
        self.assertTrue(valid, reason)
        invalid, _ = sender.validate_probe_response("{}", probe)
        self.assertFalse(invalid)

    def test_request_headers_report_codex_version_without_impersonating_codex(self) -> None:
        headers = sender.build_request_headers(
            self.provider,
            sender.normalize_config(),
            codex_version=sender.CodexCliVersion(version="0.144.5", source="test"),
        )
        self.assertEqual(headers[sender.CODEX_VERSION_HEADER], "0.144.5")
        self.assertIn("non-codex", headers["User-Agent"])
        self.assertNotIn("codex_cli_rs", json.dumps(headers))

    def test_codex_version_header_can_be_disabled(self) -> None:
        config = sender.normalize_config({"send_codex_version_header": False})
        headers = sender.build_request_headers(
            self.provider,
            config,
            codex_version=sender.CodexCliVersion(version="0.144.5", source="test"),
        )
        self.assertNotIn(sender.CODEX_VERSION_HEADER, headers)

    def test_send_one_rejects_semantically_wrong_probe_response(self) -> None:
        probe = sender.ProbeCase(prompt="自然任务", expected={"sum": 9, "parity": "odd"})
        config = sender.normalize_config({"random_probe_enabled": True})
        with mock.patch.object(sender, "generate_probe_case", return_value=probe), mock.patch.object(
            sender,
            "_http_request",
            return_value=(200, {"output_text": '{"sum":8,"parity":"even"}'}, {}, ""),
        ):
            result = sender.send_one(
                1,
                self.provider,
                config,
                time.monotonic() + 5,
                sender.RunLogger(callback=lambda _line: None),
            )
        self.assertFalse(result.ok)
        self.assertIn("语义校验失败", result.error)
        self.assertEqual(result.request_prompt, "自然任务")

    def test_codex_version_parser_accepts_cli_output(self) -> None:
        self.assertEqual(sender.parse_codex_cli_version("codex-cli 0.144.5"), "0.144.5")

    def test_codex_exec_command_uses_official_cli_without_exposing_key(self) -> None:
        config = sender.normalize_config({"transport_mode": sender.TRANSPORT_CODEX_CLI})
        command = sender.build_codex_exec_command(
            Path("C:/tools/codex.exe"),
            self.provider,
            config,
            "https://api.example.test/v1",
        )
        rendered = " ".join(command)
        self.assertIn("codex.exe", command[0])
        self.assertIn("exec", command)
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn('openai_base_url="https://api.example.test/v1"', command)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("CC Switch Batch Sender", rendered)

    def test_parse_codex_exec_jsonl_extracts_final_message_and_usage(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"id": "item-1", "type": "agent_message", "text": "完成"},
                    },
                    ensure_ascii=False,
                ),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 3}}),
            ]
        )
        text, error, payload = sender.parse_codex_exec_jsonl(stdout)
        self.assertEqual(text, "完成")
        self.assertEqual(error, "")
        self.assertEqual(payload["thread_id"], "thread-1")
        self.assertEqual(payload["usage"]["output_tokens"], 3)

    def test_codex_cli_sender_passes_key_only_through_child_environment(self) -> None:
        captured: dict[str, Any] = {}

        def fake_executor(command, prompt, env, deadline, abort_event):
            captured.update({"command": command, "prompt": prompt, "env": env})
            stdout = json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "固定回答"},
                },
                ensure_ascii=False,
            )
            return 0, stdout, "", False

        config = sender.normalize_config(
            {
                "transport_mode": sender.TRANSPORT_CODEX_CLI,
                "random_probe_enabled": False,
                "message": "固定任务",
            }
        )
        with mock.patch.object(sender, "resolve_codex_cli_executable", return_value=Path("C:/tools/codex.exe")):
            result = sender.send_one_codex_cli(
                1,
                self.provider,
                config,
                time.monotonic() + 5,
                sender.RunLogger(callback=lambda _line: None),
                executor=fake_executor,
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "固定回答")
        self.assertEqual(captured["prompt"], "固定任务")
        self.assertEqual(captured["env"]["CODEX_API_KEY"], "secret")
        self.assertNotIn("secret", " ".join(captured["command"]))

    def test_codex_cli_timeout_terminates_registered_process(self) -> None:
        before = sender.ACTIVE_CODEX_PROCESSES.count()
        return_code, stdout, stderr, stopped = sender._execute_codex_cli(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            "small prompt",
            os.environ.copy(),
            time.monotonic() + 0.15,
            None,
        )
        self.assertTrue(stopped)
        self.assertNotEqual(return_code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        self.assertEqual(sender.ACTIVE_CODEX_PROCESSES.count(), before)

    def test_active_registry_terminates_registered_process(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        sender.ACTIVE_CODEX_PROCESSES.register(process)
        try:
            sender.terminate_active_codex_processes()
            process.wait(timeout=3)
            self.assertIsNotNone(process.returncode)
            self.assertEqual(sender.ACTIVE_CODEX_PROCESSES.count(), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            sender.ACTIVE_CODEX_PROCESSES.discard(process)

    def test_codex_cli_errors_redact_provider_key(self) -> None:
        def fake_executor(command, prompt, env, deadline, abort_event):
            stdout = json.dumps(
                {"type": "error", "message": f"Authorization: Bearer {self.provider.api_key}"}
            )
            return 1, stdout, f"upstream echoed {self.provider.api_key}", False

        config = sender.normalize_config(
            {
                "transport_mode": sender.TRANSPORT_CODEX_CLI,
                "random_probe_enabled": False,
                "message": "固定任务",
            }
        )
        with mock.patch.object(sender, "resolve_codex_cli_executable", return_value=Path("C:/tools/codex.exe")):
            result = sender.send_one_codex_cli(
                1,
                self.provider,
                config,
                time.monotonic() + 5,
                sender.RunLogger(callback=lambda _line: None),
                executor=fake_executor,
            )
        rendered = json.dumps(result.__dict__, ensure_ascii=False)
        self.assertNotIn(self.provider.api_key, rendered)
        self.assertIn("<redacted>", rendered)

    def test_endpoint_candidates_keep_existing_fallback_order(self) -> None:
        self.assertEqual(
            sender.endpoint_candidates(self.provider, "auto"),
            ["https://api.example.test/responses", "https://api.example.test/v1/responses"],
        )

    def test_result_export_redacts_query_string(self) -> None:
        result = sender.AttemptResult(
            index=2,
            round_no=1,
            ok=True,
            status=200,
            endpoint="https://api.example.test/v1/responses?token=secret",
            provider_name="Provider A",
            text="done",
        )
        data = sender.build_result_dict(result, sender.normalize_config())
        self.assertEqual(data["endpoint"], "https://api.example.test/v1/responses")
        self.assertNotIn("secret", json.dumps(data))


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = sender.Provider(
            provider_id="provider-a",
            name="Provider A",
            api_key="secret",
            base_url="https://api.example.test",
            model="gpt-test",
            api_format="openai_responses",
        )

    def test_retries_are_additional_batches(self) -> None:
        calls: list[tuple[int, int]] = []
        lock = threading.Lock()

        def fake_sender(index, provider, config, deadline, logger, abort_polling, *, round_no=1):
            with lock:
                calls.append((round_no, index))
            if round_no == 2 and index == 1:
                return sender.AttemptResult(index=index, round_no=round_no, ok=True, status=200, text="ok", provider_name=provider.name)
            return sender.AttemptResult(index=index, round_no=round_no, ok=False, status=502, error="bad gateway", provider_name=provider.name)

        outcome = sender.run_batch(
            sender.normalize_config({"request_count": 2, "retry_count": 1, "retry_interval_seconds": 0}),
            sender.RunLogger(callback=lambda _line: None),
            provider_loader=lambda _config: self.provider,
            sender=fake_sender,
        )
        self.assertEqual(outcome.code, 0)
        self.assertEqual(outcome.launched, 4)
        self.assertEqual(outcome.completed, 4)
        self.assertEqual(outcome.winner.round_no, 2)
        self.assertEqual(len(calls), 4)

    def test_zero_retries_runs_one_batch(self) -> None:
        calls = 0
        lock = threading.Lock()

        def fake_sender(index, provider, config, deadline, logger, abort_polling, *, round_no=1):
            nonlocal calls
            with lock:
                calls += 1
            return sender.AttemptResult(index=index, round_no=round_no, ok=False, status=502, error="bad gateway")

        outcome = sender.run_batch(
            sender.normalize_config({"request_count": 3, "retry_count": 0}),
            sender.RunLogger(callback=lambda _line: None),
            provider_loader=lambda _config: self.provider,
            sender=fake_sender,
        )
        self.assertEqual(outcome.code, 1)
        self.assertEqual(outcome.launched, 3)
        self.assertEqual(calls, 3)

    def test_non_retryable_batch_stops_before_configured_retries(self) -> None:
        calls = 0
        lock = threading.Lock()

        def fake_sender(index, provider, config, deadline, logger, abort_polling, *, round_no=1):
            nonlocal calls
            with lock:
                calls += 1
            return sender.AttemptResult(index=index, round_no=round_no, ok=False, status=401, error="unauthorized")

        outcome = sender.run_batch(
            sender.normalize_config({"request_count": 2, "retry_count": 3, "retry_interval_seconds": 0}),
            sender.RunLogger(callback=lambda _line: None),
            provider_loader=lambda _config: self.provider,
            sender=fake_sender,
        )
        self.assertEqual(outcome.code, 1)
        self.assertEqual(outcome.launched, 2)
        self.assertEqual(calls, 2)

    def test_dry_run_never_calls_sender(self) -> None:
        lines: list[str] = []

        def fail_sender(*args, **kwargs):
            raise AssertionError("sender must not be called")

        outcome = sender.run_batch(
            sender.normalize_config({"request_count": 2, "retry_count": 2}),
            sender.RunLogger(callback=lines.append),
            dry_run=True,
            provider_loader=lambda _config: self.provider,
            sender=fail_sender,
        )
        self.assertEqual(outcome.code, 0)
        self.assertEqual(outcome.launched, 0)
        run_start = next(line for line in lines if "RUN_START" in line)
        self.assertIn("task_mode=random", run_start)
        self.assertIn("transport=official-codex-cli", run_start)
        self.assertIn("cli_concurrency=4", run_start)
        self.assertNotIn("third-party", run_start)
        self.assertNotIn("random-probe", run_start)

    def test_direct_transport_keeps_transparent_client_identity(self) -> None:
        lines: list[str] = []
        outcome = sender.run_batch(
            sender.normalize_config(
                {
                    "transport_mode": sender.TRANSPORT_DIRECT,
                    "request_count": 1,
                    "retry_count": 0,
                }
            ),
            sender.RunLogger(callback=lines.append),
            dry_run=True,
            provider_loader=lambda _config: self.provider,
            sender=lambda *args, **kwargs: None,
        )
        self.assertEqual(outcome.code, 0)
        run_start = next(line for line in lines if "RUN_START" in line)
        self.assertIn("transport=direct-api", run_start)
        self.assertIn("client=ccswitch-batch-sender", run_start)
        self.assertIn("post_cap=1", run_start)

    def test_runner_redacts_provider_key_before_logging(self) -> None:
        lines: list[str] = []

        def fake_sender(index, provider, config, deadline, logger, abort_polling, *, round_no=1):
            return sender.AttemptResult(
                index=index,
                round_no=round_no,
                ok=False,
                status=500,
                error=f"Authorization: Bearer {provider.api_key}",
                payload={"error": provider.api_key},
                provider_name=provider.name,
            )

        outcome = sender.run_batch(
            sender.normalize_config(
                {
                    "transport_mode": sender.TRANSPORT_DIRECT,
                    "request_count": 1,
                    "retry_count": 0,
                }
            ),
            sender.RunLogger(callback=lines.append),
            provider_loader=lambda _config: self.provider,
            sender=fake_sender,
        )
        self.assertEqual(outcome.code, 1)
        self.assertNotIn(self.provider.api_key, "\n".join(lines))
        self.assertIn("<redacted>", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
