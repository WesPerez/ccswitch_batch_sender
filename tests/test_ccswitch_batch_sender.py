from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

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
    def test_zero_request_count_is_rejected_instead_of_replaced(self) -> None:
        with self.assertRaises(sender.SenderError):
            sender.normalize_config({"request_count": 0})

    def test_retry_count_sets_finite_post_cap(self) -> None:
        config = sender.normalize_config({"request_count": 7, "retry_count": 2})
        self.assertEqual(config["request_count"] * (1 + config["retry_count"]), 21)

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
        config = sender.normalize_config({"message": "probe", "max_output_tokens": 3})
        body = sender.build_preview_body(self.provider, config)
        self.assertEqual(body["model"], "gpt-test")
        self.assertEqual(body["input"][0]["content"][0]["text"], "probe")
        self.assertEqual(body["prompt_cache_key"], "<每个请求唯一>")
        self.assertEqual(body["max_output_tokens"], 3)

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
        def fail_sender(*args, **kwargs):
            raise AssertionError("sender must not be called")

        outcome = sender.run_batch(
            sender.normalize_config({"request_count": 2, "retry_count": 2}),
            sender.RunLogger(callback=lambda _line: None),
            dry_run=True,
            provider_loader=lambda _config: self.provider,
            sender=fail_sender,
        )
        self.assertEqual(outcome.code, 0)
        self.assertEqual(outcome.launched, 0)


if __name__ == "__main__":
    unittest.main()
