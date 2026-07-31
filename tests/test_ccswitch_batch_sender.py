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
import ccswitch_batch_sender_ui as sender_ui


def provider_settings(
    *,
    key: str = "test-key",
    base_url: str = "https://api.example.test",
    model: str = "gpt-test",
    config_token: str = "",
    auth_extra: dict[str, Any] | None = None,
    config_text: str | None = None,
) -> str:
    auth = {"OPENAI_API_KEY": key}
    if auth_extra:
        auth.update(auth_extra)
    token_line = f'experimental_bearer_token = "{config_token}"\n' if config_token else ""
    return json.dumps(
        {
            "auth": auth,
            "config": config_text
            if config_text is not None
            else (
                'model_provider = "custom"\n'
                f'model = "{model}"\n'
                "[model_providers.custom]\n"
                f'base_url = "{base_url}"\n'
                'wire_api = "responses"\n'
                f"{token_line}"
            ),
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
                category TEXT,
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
        config_token: str = "",
        auth_extra: dict[str, Any] | None = None,
        config_text: str | None = None,
        app_type: str = "codex",
        api_format: str = "openai_responses",
        category: str | None = None,
        provider_type: str = "",
        sort_index: int = 0,
    ) -> None:
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            """
            INSERT INTO providers (
                id, app_type, name, settings_config, meta, category, is_current, sort_index
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider_id,
                app_type,
                name,
                provider_settings(
                    key=key,
                    base_url=base_url,
                    model=model,
                    config_token=config_token,
                    auth_extra=auth_extra,
                    config_text=config_text,
                ),
                json.dumps(
                    {
                        "apiFormat": api_format,
                        **({"providerType": provider_type} if provider_type else {}),
                    }
                ),
                category,
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

    def test_provider_diagnostics_drop_sensitive_fields_and_rotate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccswitch-diagnostics-test-") as temp:
            path = Path(temp) / "provider-diagnostics.jsonl"
            diagnostics = sender.ProviderDiagnostics(path=path, max_bytes=180, backup_count=1)
            diagnostics.record(
                "PROVIDER_LOAD",
                provider_id="provider-a",
                credential_source="config",
                has_api_key=True,
                api_key="must-not-be-written",
                authorization="Bearer must-not-be-written",
                credential="must-not-be-written",
                details={"api_key": "nested-must-not-be-written", "result": "ok"},
            )
            diagnostics.record("PROVIDER_LOAD", provider_id="provider-b", note="x" * 200)

            rendered = path.read_text(encoding="utf-8")
            rotated = path.with_name(path.name + ".1").read_text(encoding="utf-8")
            combined = rendered + rotated
            self.assertIn("PROVIDER_LOAD", combined)
            self.assertIn("credential_source", combined)
            self.assertNotIn("must-not-be-written", combined)
            self.assertNotIn("nested-must-not-be-written", combined)
            self.assertNotIn('"api_key"', combined)
            self.assertNotIn('"authorization"', combined)

    def test_zero_request_count_is_rejected_instead_of_replaced(self) -> None:
        with self.assertRaises(sender.SenderError):
            sender.normalize_config({"request_count": 0})

    def test_retry_count_sets_finite_post_cap(self) -> None:
        config = sender.normalize_config({"request_count": 7, "retry_count": 2})
        self.assertEqual(config["request_count"] * (1 + config["retry_count"]), 21)

    def test_default_uses_random_real_probes(self) -> None:
        config = sender.normalize_config()
        self.assertEqual(config["transport_mode"], sender.TRANSPORT_CODEX_CLI)
        self.assertEqual(config["cli_concurrency"], 10)
        self.assertEqual(config["request_count"], 10)
        self.assertEqual(config["retry_count"], 2)
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
        self.assertEqual(migrated["cli_concurrency"], 10)

    def test_v3_saved_defaults_migrate_to_new_batch_defaults(self) -> None:
        migrated = sender.migrate_saved_config({"request_count": 20, "retry_count": 0}, 3)
        self.assertEqual(migrated["request_count"], 10)
        self.assertEqual(migrated["retry_count"], 2)
        self.assertEqual(migrated["cli_concurrency"], 10)

    def test_v3_custom_batch_values_are_preserved(self) -> None:
        migrated = sender.migrate_saved_config({"request_count": 8, "retry_count": 2}, 3)
        self.assertEqual(migrated["request_count"], 8)
        self.assertEqual(migrated["retry_count"], 2)

    def test_v4_saved_defaults_migrate_to_current_defaults(self) -> None:
        migrated = sender.migrate_saved_config(
            {"request_count": 5, "retry_count": 5, "cli_concurrency": 4},
            4,
        )
        self.assertEqual(migrated["request_count"], 10)
        self.assertEqual(migrated["retry_count"], 2)
        self.assertEqual(migrated["cli_concurrency"], 10)

    def test_cli_concurrency_accepts_ten_and_rejects_more(self) -> None:
        self.assertEqual(sender.normalize_config({"cli_concurrency": 10})["cli_concurrency"], 10)
        with self.assertRaises(sender.SenderError):
            sender.normalize_config({"cli_concurrency": 11})

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

    def test_config_bearer_token_matches_cc_switch_credential_fallback(self) -> None:
        self.db.add(
            "provider-a",
            "Provider A",
            current=True,
            key="",
            config_token="config-secret",
        )
        config = sender.normalize_config({"db_path": str(self.db.db_path), "provider_id": "current"})

        catalog = sender.list_codex_providers(config)
        provider = sender.load_provider(config)

        self.assertTrue(catalog.providers[0].has_api_key)
        self.assertTrue(catalog.providers[0].available)
        self.assertEqual(provider.api_key, "config-secret")

    def test_provider_load_diagnostics_record_source_without_key(self) -> None:
        self.db.add(
            "provider-a",
            "Provider A",
            current=True,
            key="",
            config_token="config-secret",
        )
        config = sender.normalize_config({"db_path": str(self.db.db_path), "provider_id": "current"})
        log_path = self.db.root / "provider-diagnostics.jsonl"
        diagnostics = sender.ProviderDiagnostics(path=log_path)

        sender.load_provider(config, diagnostics=diagnostics)

        rendered = log_path.read_text(encoding="utf-8")
        self.assertIn('"credential_source":"active_provider"', rendered)
        self.assertIn('"resolved_provider_id":"provider-a"', rendered)
        self.assertNotIn("config-secret", rendered)

    def test_auth_key_takes_precedence_over_config_token(self) -> None:
        self.db.add(
            "provider-a",
            "Provider A",
            current=True,
            key="auth-secret",
            config_token="config-secret",
        )
        config = sender.normalize_config({"db_path": str(self.db.db_path)})
        self.assertEqual(sender.load_provider(config).api_key, "auth-secret")

    def test_bearer_prefixed_auth_key_is_normalized_before_transport_use(self) -> None:
        self.db.add(
            "provider-a",
            "Provider A",
            current=True,
            key="Bearer real-secret",
        )
        config = sender.normalize_config({"db_path": str(self.db.db_path)})

        provider = sender.load_provider(config)
        headers = sender.build_request_headers(
            provider,
            config,
            codex_version=sender.CodexCliVersion(version="", source="test"),
        )

        self.assertEqual(provider.api_key, "real-secret")
        self.assertEqual(headers["Authorization"], "Bearer real-secret")

    def test_only_active_provider_section_can_supply_config_token(self) -> None:
        config_text = """model_provider = "active"
model = "gpt-test"

[model_providers.active]
base_url = "https://api.example.test"
wire_api = "responses"

[model_providers.inactive]
experimental_bearer_token = "inactive-secret"
"""
        self.db.add(
            "provider-a",
            "Provider A",
            current=True,
            key="",
            config_text=config_text,
        )
        config = sender.normalize_config({"db_path": str(self.db.db_path)})
        with self.assertRaises(sender.SenderError):
            sender.load_provider(config)

    def test_provider_endpoint_and_wire_api_come_from_active_toml_section(self) -> None:
        config_text = """model_provider = "active"
model = "active-model"

[model_providers.inactive]
base_url = "https://inactive.example.test"
wire_api = "chat"

[model_providers.active]
base_url = "https://active.example.test"
wire_api = "responses"
"""
        self.db.add(
            "provider-a",
            "Provider A",
            current=True,
            config_text=config_text,
            api_format="",
        )
        config = sender.normalize_config({"db_path": str(self.db.db_path)})

        catalog = sender.list_codex_providers(config)
        provider = sender.load_provider(config)

        self.assertEqual(catalog.providers[0].base_url, "https://active.example.test")
        self.assertEqual(catalog.providers[0].api_format, "openai_responses")
        self.assertEqual(provider.base_url, "https://active.example.test")
        self.assertEqual(provider.api_format, "openai_responses")

    def test_model_is_read_only_from_the_toml_top_level(self) -> None:
        config_text = """model_provider = "active"

[model_providers.inactive]
model = "inactive-model"
base_url = "https://inactive.example.test"

[model_providers.active]
model = "active-section-model"
base_url = "https://active.example.test"
wire_api = "responses"
"""
        self.db.add(
            "provider-a",
            "Provider A",
            current=True,
            config_text=config_text,
            api_format="",
        )
        config = sender.normalize_config({"db_path": str(self.db.db_path)})

        catalog = sender.list_codex_providers(config)

        self.assertFalse(catalog.providers[0].available)
        self.assertIn("无模型", catalog.providers[0].unavailable_reason)
        with self.assertRaisesRegex(sender.SenderError, "没有 model"):
            sender.load_provider(config)

    def test_legacy_top_level_toml_endpoint_remains_supported(self) -> None:
        config_text = """model = "legacy-model"
base_url = "https://legacy.example.test"
wire_api = "responses"
"""
        self.db.add(
            "provider-a",
            "Provider A",
            current=True,
            config_text=config_text,
            api_format="",
        )
        config = sender.normalize_config({"db_path": str(self.db.db_path)})

        provider = sender.load_provider(config)

        self.assertEqual(provider.model, "legacy-model")
        self.assertEqual(provider.base_url, "https://legacy.example.test")
        self.assertEqual(provider.api_format, "openai_responses")

    def test_top_level_token_is_fallback_for_active_custom_provider(self) -> None:
        config_text = """model_provider = "active"
model = "gpt-test"
experimental_bearer_token = "top-level-secret"

[model_providers.active]
base_url = "https://api.example.test"
wire_api = "responses"
"""
        self.db.add(
            "provider-a",
            "Provider A",
            current=True,
            key="",
            config_text=config_text,
        )
        config = sender.normalize_config({"db_path": str(self.db.db_path)})
        self.assertEqual(sender.load_provider(config).api_key, "top-level-secret")

    def test_reserved_provider_ignores_its_section_token(self) -> None:
        config_text = """model_provider = "OpenAI"
model = "gpt-test"
experimental_bearer_token = "top-level-secret"

[model_providers.OpenAI]
base_url = "https://api.example.test"
wire_api = "responses"
experimental_bearer_token = "stale-section-secret"
"""
        self.db.add(
            "provider-a",
            "Provider A",
            current=True,
            key="",
            config_text=config_text,
        )
        config = sender.normalize_config({"db_path": str(self.db.db_path)})
        self.assertEqual(sender.load_provider(config).api_key, "top-level-secret")

    def test_non_string_config_token_is_rejected(self) -> None:
        config_text = """model_provider = "custom"
model = "gpt-test"

[model_providers.custom]
base_url = "https://api.example.test"
wire_api = "responses"
experimental_bearer_token = 123
"""
        self.db.add(
            "provider-a",
            "Provider A",
            current=True,
            key="",
            config_text=config_text,
        )
        config = sender.normalize_config({"db_path": str(self.db.db_path)})
        with self.assertRaises(sender.SenderError):
            sender.load_provider(config)

    def test_non_string_model_provider_does_not_select_a_provider_table(self) -> None:
        config_text = """model_provider = 123
model = "gpt-test"

[model_providers.123]
base_url = "https://api.example.test"
wire_api = "responses"
experimental_bearer_token = "wrong-section-secret"
"""
        self.db.add(
            "provider-a",
            "Provider A",
            current=True,
            key="",
            config_text=config_text,
        )
        config = sender.normalize_config({"db_path": str(self.db.db_path)})
        with self.assertRaises(sender.SenderError):
            sender.load_provider(config)

    def test_managed_placeholders_are_never_treated_as_api_keys(self) -> None:
        self.db.add(
            "provider-a",
            "Provider A",
            current=True,
            key="Bearer abc-PROXY_MANAGED-value",
        )
        config = sender.normalize_config({"db_path": str(self.db.db_path)})

        catalog = sender.list_codex_providers(config)
        self.assertFalse(catalog.providers[0].available)
        self.assertIn("代理托管", catalog.providers[0].unavailable_reason)
        with self.assertRaisesRegex(sender.SenderError, "代理托管"):
            sender.load_provider(config)

    def test_official_and_oauth_providers_cannot_bypass_direct_key_gate(self) -> None:
        self.db.add(
            "official",
            "OpenAI Official",
            current=True,
            key="stale-secret",
            category="official",
        )
        self.db.add(
            "xai",
            "xAI OAuth",
            key="stale-secret",
            provider_type="xai_oauth",
        )
        self.db.add(
            "oauth-only",
            "OAuth Only",
            key="",
            config_token="stale-config-secret",
            auth_extra={"tokens": {"access_token": "oauth-secret"}},
        )

        for provider_id in ("official", "xai", "oauth-only"):
            config = sender.normalize_config(
                {"db_path": str(self.db.db_path), "provider_id": provider_id}
            )
            with self.subTest(provider_id=provider_id), self.assertRaises(sender.SenderError):
                sender.load_provider(config)

    def test_catalog_current_pin_prevents_preview_from_switching_provider(self) -> None:
        self.db.add("provider-a", "Provider A", current=True, key="key-a")
        self.db.add("provider-b", "Provider B", current=False, key="")
        self.db.set_pointer("provider-a")
        config = sender.normalize_config({"db_path": str(self.db.db_path), "provider_id": "current"})
        catalog = sender.list_codex_providers(config)

        self.db.set_pointer("provider-b")
        provider = sender.load_provider(config, current_provider_id=catalog.current_provider_id)

        self.assertEqual(provider.provider_id, "provider-a")
        self.assertEqual(provider.api_key, "key-a")

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
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        sender.ACTIVE_CODEX_PROCESSES.register(process)
        try:
            sender.terminate_active_codex_processes()
            process.wait(timeout=3)
            self.assertIsNotNone(process.returncode)
            self.assertIsNone(unrelated.poll())
            self.assertEqual(sender.ACTIVE_CODEX_PROCESSES.count(), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            if unrelated.poll() is None:
                unrelated.kill()
                unrelated.wait(timeout=3)
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


class GuiGuardTests(unittest.TestCase):
    def test_start_run_respects_disabled_preview_state_for_hotkey_calls(self) -> None:
        app = mock.Mock()
        app.running = False
        app.blocked_by_unfinished = False
        app.can_start = False

        sender_ui.BatchSenderApp.start_run(app)

        app._selected_provider_id.assert_not_called()


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

    def test_provider_snapshot_is_reused_across_retry_batches(self) -> None:
        provider_loads: list[str] = []

        def provider_loader(_config):
            provider_loads.append("load")
            return self.provider

        def fake_sender(index, provider, config, deadline, logger, abort_polling, *, round_no=1):
            return sender.AttemptResult(
                index=index,
                round_no=round_no,
                ok=False,
                status=502,
                error="bad gateway",
                provider_name=provider.name,
            )

        outcome = sender.run_batch(
            sender.normalize_config(
                {"request_count": 1, "retry_count": 2, "retry_interval_seconds": 0}
            ),
            sender.RunLogger(callback=lambda _line: None),
            provider_loader=provider_loader,
            sender=fake_sender,
        )

        self.assertEqual(outcome.code, 1)
        self.assertEqual(provider_loads, ["load"])

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
        self.assertIn("cli_concurrency=10", run_start)
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

    def test_first_cli_success_cancels_the_other_tasks(self) -> None:
        cancelled: list[int] = []
        lock = threading.Lock()

        def fake_sender(index, provider, config, deadline, logger, abort_polling, *, round_no=1):
            if index == 1:
                return sender.AttemptResult(
                    index=index,
                    round_no=round_no,
                    ok=True,
                    status=200,
                    text="ok",
                    provider_name=provider.name,
                )
            if abort_polling.wait(timeout=2):
                with lock:
                    cancelled.append(index)
                return sender.AttemptResult(
                    index=index,
                    round_no=round_no,
                    ok=False,
                    error="Codex CLI 任务已被取消。",
                    provider_name=provider.name,
                    cancelled=True,
                )
            return sender.AttemptResult(index=index, round_no=round_no, ok=False, status=504, error="timeout")

        config = sender.normalize_config(
            {
                "transport_mode": sender.TRANSPORT_CODEX_CLI,
                "request_count": 3,
                "retry_count": 0,
            }
        )
        with mock.patch.object(sender, "terminate_active_codex_processes") as terminate:
            outcome = sender.run_batch(
                config,
                sender.RunLogger(callback=lambda _line: None),
                provider_loader=lambda _config: self.provider,
                sender=fake_sender,
            )
        self.assertEqual(outcome.code, 0)
        self.assertEqual(outcome.failed, 0)
        self.assertEqual(set(cancelled), {2, 3})
        terminate.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
