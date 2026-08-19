from __future__ import annotations

import json
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

    def test_success_keepalive_defaults_to_three_minutes(self) -> None:
        config = sender.normalize_config()

        self.assertTrue(config["success_keepalive_enabled"])
        self.assertEqual(config["success_keepalive_interval_seconds"], 180)

    def test_success_keepalive_interval_must_be_at_least_one_minute(self) -> None:
        with self.assertRaises(sender.SenderError):
            sender.normalize_config({"success_keepalive_interval_seconds": 59})

    def test_retry_count_accepts_finite_values_above_ten(self) -> None:
        self.assertEqual(sender.normalize_config({"retry_count": 25})["retry_count"], 25)
        self.assertEqual(
            sender.normalize_config({"retry_count": sender.MAX_FINITE_RETRY_COUNT})["retry_count"],
            sender.MAX_FINITE_RETRY_COUNT,
        )
        with self.assertRaises(sender.SenderError):
            sender.normalize_config({"retry_count": -1})
        with self.assertRaises(sender.SenderError):
            sender.normalize_config({"retry_count": sender.MAX_FINITE_RETRY_COUNT + 1})

    def test_default_uses_random_real_probes(self) -> None:
        config = sender.normalize_config()
        self.assertEqual(config["transport_mode"], sender.TRANSPORT_DIRECT)
        self.assertEqual(config["cli_concurrency"], 10)
        self.assertEqual(config["request_count"], 15)
        self.assertEqual(config["retry_count"], 0)
        self.assertEqual(config["request_timeout_seconds"], 10)
        self.assertEqual(config["max_wait_seconds"], 0)
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

    def test_v2_saved_defaults_use_current_transport(self) -> None:
        migrated = sender.migrate_saved_config({}, 2)
        self.assertEqual(migrated["transport_mode"], sender.TRANSPORT_DIRECT)

    def test_v3_saved_defaults_migrate_to_new_batch_defaults(self) -> None:
        migrated = sender.migrate_saved_config({"request_count": 20, "retry_count": 0}, 3)
        self.assertEqual(migrated["request_count"], 15)
        self.assertEqual(migrated["retry_count"], 0)
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
        self.assertEqual(migrated["request_count"], 15)
        self.assertEqual(migrated["retry_count"], 0)
        self.assertEqual(migrated["cli_concurrency"], 10)

    def test_v5_saved_defaults_migrate_to_current_batch_defaults(self) -> None:
        migrated = sender.migrate_saved_config(
            {"transport_mode": sender.TRANSPORT_CODEX_CLI, "request_count": 10, "retry_count": 2},
            5,
        )
        self.assertEqual(migrated["transport_mode"], sender.TRANSPORT_CODEX_CLI)
        self.assertEqual(migrated["request_count"], 15)
        self.assertEqual(migrated["retry_count"], 0)

    def test_v5_custom_batch_values_and_transport_are_preserved(self) -> None:
        migrated = sender.migrate_saved_config(
            {"transport_mode": sender.TRANSPORT_CODEX_CLI, "request_count": 8, "retry_count": 2},
            5,
        )
        self.assertEqual(migrated["transport_mode"], sender.TRANSPORT_CODEX_CLI)
        self.assertEqual(migrated["request_count"], 8)
        self.assertEqual(migrated["retry_count"], 2)

    def test_v6_saved_default_request_timeout_is_shortened(self) -> None:
        migrated = sender.migrate_saved_config({"request_timeout_seconds": 7200}, 6)
        self.assertEqual(migrated["request_timeout_seconds"], 10)

    def test_v6_custom_request_timeout_is_preserved(self) -> None:
        migrated = sender.migrate_saved_config({"request_timeout_seconds": 300}, 6)
        self.assertEqual(migrated["request_timeout_seconds"], 300)

    def test_current_saved_request_timeout_is_preserved(self) -> None:
        migrated = sender.migrate_saved_config({"request_timeout_seconds": 7200}, 8)
        self.assertEqual(migrated["request_timeout_seconds"], 7200)

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
        self.assertNotIn("originator", payload)
        self.assertNotIn("send_codex_version_header", payload)
        self.assertNotIn("api_key", json.dumps(payload))

    def test_saved_config_uses_readable_versioned_json_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccswitch-config-test-") as temp:
            path = Path(temp) / "settings.json"
            config = sender.normalize_config({"retry_count": 25, "retry_interval_seconds": 7})

            sender.save_saved_config(config, path=path)

            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["schema_version"], sender.CONFIG_SCHEMA_VERSION)
            self.assertEqual(document["settings"]["retry_count"], 25)
            self.assertEqual(document["settings"]["request_timeout_seconds"], 10)
            self.assertEqual(sender.load_saved_config(path=path)["retry_interval_seconds"], 7)
            self.assertEqual(sender.load_json_config(path)["retry_count"], 25)

    def test_saved_config_round_trips_request_timeout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccswitch-config-test-") as temp:
            path = Path(temp) / "settings.json"
            sender.save_saved_config(
                sender.normalize_config({"request_timeout_seconds": 37}),
                path=path,
            )

            loaded = sender.load_saved_config(path=path)

            self.assertEqual(loaded["request_timeout_seconds"], 37)

    def test_saved_config_round_trips_success_keepalive_settings(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccswitch-config-test-") as temp:
            path = Path(temp) / "settings.json"
            sender.save_saved_config(
                sender.normalize_config(
                    {
                        "success_keepalive_enabled": False,
                        "success_keepalive_interval_seconds": 420,
                    }
                ),
                path=path,
            )

            loaded = sender.load_saved_config(path=path)

            self.assertFalse(loaded["success_keepalive_enabled"])
            self.assertEqual(loaded["success_keepalive_interval_seconds"], 420)

    def test_v8_config_enables_default_success_keepalive(self) -> None:
        migrated = sender.migrate_saved_config({}, 8)

        self.assertTrue(migrated["success_keepalive_enabled"])
        self.assertEqual(migrated["success_keepalive_interval_seconds"], 180)

    def test_legacy_registry_config_is_saved_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccswitch-config-migration-") as temp:
            path = Path(temp) / "settings.json"
            legacy = sender.normalize_config({"retry_count": 2})
            with (
                mock.patch.object(sender, "default_saved_config_path", return_value=path),
                mock.patch.object(sender, "_load_legacy_registry_config", return_value=legacy),
                mock.patch.object(sender, "_delete_legacy_registry_config", return_value=True) as delete,
            ):
                loaded = sender.load_saved_config()

            self.assertEqual(loaded["retry_count"], 2)
            self.assertTrue(path.exists())
            delete.assert_called_once_with()

    def test_legacy_registry_is_not_deleted_when_file_write_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccswitch-config-migration-") as temp:
            path = Path(temp) / "settings.json"
            legacy = sender.normalize_config({"retry_count": 2})
            with (
                mock.patch.object(sender, "default_saved_config_path", return_value=path),
                mock.patch.object(sender, "_load_legacy_registry_config", return_value=legacy),
                mock.patch.object(sender, "save_saved_config", side_effect=OSError("disk full")),
                mock.patch.object(sender, "_delete_legacy_registry_config") as delete,
            ):
                loaded = sender.load_saved_config()

            self.assertEqual(loaded["retry_count"], 2)
            delete.assert_not_called()

    def test_invalid_saved_file_falls_back_to_registry_without_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccswitch-config-invalid-") as temp:
            path = Path(temp) / "settings.json"
            path.write_text("{invalid", encoding="utf-8")
            legacy = sender.normalize_config({"retry_count": 3})
            with (
                mock.patch.object(sender, "default_saved_config_path", return_value=path),
                mock.patch.object(sender, "_load_legacy_registry_config", return_value=legacy),
                mock.patch.object(sender, "save_saved_config") as save,
                mock.patch.object(sender, "_delete_legacy_registry_config") as delete,
            ):
                loaded = sender.load_saved_config()

            self.assertEqual(loaded["retry_count"], 3)
            save.assert_not_called()
            delete.assert_not_called()

    def test_invalid_cli_config_does_not_trigger_saved_config_migration(self) -> None:
        mutex = mock.Mock()
        mutex.acquire.return_value = True
        with (
            mock.patch.object(sender, "SingleInstanceMutex", return_value=mutex),
            mock.patch.object(sender, "load_json_config", side_effect=sender.SenderError("bad config")),
            mock.patch.object(sender, "load_saved_config") as load_saved,
            mock.patch.object(sender, "terminate_active_codex_processes"),
            mock.patch.object(sender, "_console_print"),
        ):
            code = sender.main(["--headless", "--config", "bad.json"])

        self.assertEqual(code, 2)
        load_saved.assert_not_called()
        mutex.release.assert_called_once_with()

    def test_v7_default_limits_migrate_to_unlimited(self) -> None:
        migrated = sender.migrate_saved_config(
            {"retry_count": 10, "max_wait_seconds": 7200},
            7,
        )
        self.assertEqual(migrated["retry_count"], 0)
        self.assertEqual(migrated["max_wait_seconds"], 0)

    def test_v6_saved_identity_overrides_are_removed(self) -> None:
        migrated = sender.migrate_saved_config(
            {
                "user_agent": "legacy-agent",
                "originator": "legacy-originator",
                "send_codex_version_header": False,
            },
            6,
        )
        self.assertNotIn("user_agent", migrated)
        self.assertNotIn("originator", migrated)
        self.assertNotIn("send_codex_version_header", migrated)

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

    def test_active_custom_config_key_wins_over_preserved_oauth_material(self) -> None:
        self.db.add(
            "provider-a",
            "Provider A",
            current=True,
            key="",
            config_token="config-secret",
            auth_extra={
                "auth_mode": "chatgpt",
                "last_refresh": "2026-08-06T00:00:00Z",
                "tokens": {"access_token": "oauth-secret", "refresh_token": "refresh-secret"},
            },
        )
        config = sender.normalize_config({"db_path": str(self.db.db_path)})

        catalog = sender.list_codex_providers(config)
        provider = sender.load_provider(config)

        self.assertTrue(catalog.providers[0].available)
        self.assertEqual(provider.api_key, "config-secret")

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
            config_token="must-not-bypass-placeholder",
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
            config_token="stale-config-secret",
            category="official",
        )
        self.db.add(
            "xai",
            "xAI OAuth",
            key="stale-secret",
            config_token="stale-config-secret",
            provider_type="xai_oauth",
        )
        self.db.add(
            "oauth-only",
            "OAuth Only",
            key="",
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

    def test_extract_text_handles_nested_responses_output_text_value(self) -> None:
        payload = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": {"value": '{"sum":9,"parity":"odd"}'},
                        }
                    ],
                }
            ]
        }
        self.assertEqual(sender._extract_text(payload), '{"sum":9,"parity":"odd"}')

    def test_failed_request_log_includes_bounded_response_summary(self) -> None:
        log_lines: list[str] = []

        def fake_sender(index, provider, config, deadline, logger, abort_polling, *, round_no=1):
            return sender.AttemptResult(
                index=index,
                round_no=round_no,
                ok=False,
                status=200,
                text="gateway answer",
                payload={"output_text": "gateway answer", "details": "x" * 5000},
                provider_name=provider.name,
            )

        outcome = sender.run_batch(
            sender.normalize_config(
                {
                    "request_count": 1,
                    "retry_count": 1,
                    "request_timeout_seconds": 1,
                    "max_wait_seconds": 1,
                    "retry_interval_seconds": 0,
                }
            ),
            sender.RunLogger(callback=log_lines.append),
            provider_loader=lambda _config: self.provider,
            sender=fake_sender,
        )

        self.assertEqual(outcome.code, 1)
        failure_lines = [line for line in log_lines if "REQUEST_FAIL" in line]
        self.assertEqual(len(failure_lines), 2)
        self.assertTrue(all("response=" in line for line in failure_lines))
        self.assertTrue(all("gateway answer" in line for line in failure_lines))
        self.assertTrue(all(len(line) < 2200 for line in failure_lines))

    def test_http_request_decompresses_gzip_response(self) -> None:
        expected = {"output_text": '{"sum":9,"parity":"odd"}'}
        response = mock.MagicMock()
        response.status = 200
        response.headers = {
            "Content-Encoding": "gzip",
            "Content-Type": "application/json; charset=utf-8",
        }
        response.read.return_value = gzip.compress(json.dumps(expected).encode("utf-8"))
        response.__enter__.return_value = response

        with mock.patch.object(sender.urllib.request, "urlopen", return_value=response):
            status, payload, headers, error = sender._http_request(
                "POST",
                "https://api.example.test/responses",
                {"input": "probe"},
                self.provider,
                sender.normalize_config(),
                5,
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload, expected)
        self.assertEqual(headers["Content-Encoding"], "gzip")
        self.assertEqual(error, "")

    def test_http_request_detects_gzip_magic_without_header(self) -> None:
        expected = {"output_text": '{"sum":9,"parity":"odd"}'}
        response = mock.MagicMock()
        response.status = 200
        response.headers = {"Content-Type": "application/json"}
        response.read.return_value = gzip.compress(json.dumps(expected).encode("utf-8"))
        response.__enter__.return_value = response

        with mock.patch.object(sender.urllib.request, "urlopen", return_value=response):
            status, payload, _headers, error = sender._http_request(
                "POST",
                "https://api.example.test/responses",
                {"input": "probe"},
                self.provider,
                sender.normalize_config(),
                5,
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload, expected)
        self.assertEqual(error, "")

    def test_http_request_reports_invalid_compressed_response(self) -> None:
        response = mock.MagicMock()
        response.status = 200
        response.headers = {"Content-Encoding": "gzip"}
        response.read.return_value = b"not-gzip"
        response.__enter__.return_value = response

        with mock.patch.object(sender.urllib.request, "urlopen", return_value=response):
            status, payload, headers, error = sender._http_request(
                "POST",
                "https://api.example.test/responses",
                {"input": "probe"},
                self.provider,
                sender.normalize_config(),
                5,
            )

        self.assertEqual(status, 200)
        self.assertIsNone(payload)
        self.assertEqual(headers["Content-Encoding"], "gzip")
        self.assertIn("响应解压或解码失败", error)

    def test_http_request_abort_closes_a_blocked_response_read(self) -> None:
        request_started = threading.Event()
        release_response = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length:
                    self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "1024")
                self.end_headers()
                self.wfile.write(b"{")
                self.wfile.flush()
                request_started.set()
                release_response.wait(5)

            def log_message(self, _format: str, *args: Any) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        abort_event = threading.Event()
        result: list[tuple[int | None, Any, dict[str, str], str]] = []
        caller = threading.Thread(
            target=lambda: result.append(
                sender._http_request(
                    "POST",
                    f"http://127.0.0.1:{server.server_port}/responses",
                    {"input": "local cancellation test"},
                    self.provider,
                    sender.normalize_config(),
                    5,
                    abort_event,
                )
            ),
            daemon=True,
        )
        server_thread.start()
        caller.start()
        try:
            self.assertTrue(request_started.wait(2))
            cancelled_at = time.monotonic()
            abort_event.set()
            caller.join(1)
            self.assertFalse(caller.is_alive())
            self.assertLess(time.monotonic() - cancelled_at, 1)
            self.assertEqual(result[0][0], None)
            self.assertEqual(result[0][3], "请求已取消。")
        finally:
            release_response.set()
            server.shutdown()
            server.server_close()
            server_thread.join(2)
            caller.join(2)

    def test_http_request_discards_a_response_completed_after_abort(self) -> None:
        abort_event = threading.Event()

        class Response:
            status = 200
            headers: dict[str, str] = {}
            fp = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                abort_event.set()
                return b'{"output_text":"late success"}'

        opener = mock.Mock()
        opener.open.return_value = Response()
        with mock.patch.object(sender.urllib.request, "build_opener", return_value=opener):
            status, payload, headers, error = sender._http_request(
                "POST",
                "https://api.example.test/responses",
                {"input": "probe"},
                self.provider,
                sender.normalize_config(),
                5,
                abort_event,
            )

        self.assertIsNone(status)
        self.assertIsNone(payload)
        self.assertEqual(headers, {})
        self.assertEqual(error, "请求已取消。")

    def test_request_headers_use_captured_codex_exec_compatibility_identity(self) -> None:
        headers = sender.build_request_headers(
            self.provider,
            sender.normalize_config(),
            codex_version=sender.CodexCliVersion(version="0.146.0", source="test"),
        )
        expected_user_agent = sender.build_codex_compatibility_user_agent(
            sender.CodexCliVersion(version="0.146.0", source="test"),
            windows_version="10.0.26200",
            machine="AMD64",
        )
        self.assertEqual(
            expected_user_agent,
            "codex_exec/0.146.0 (Windows 10.0.26200; x86_64) unknown (codex_exec; 0.146.0)",
        )
        self.assertTrue(headers["User-Agent"].startswith("codex_exec/0.146.0 (Windows "))
        self.assertEqual(headers["Originator"], "codex_exec")
        self.assertNotIn("CC Switch", json.dumps(headers))
        self.assertFalse(any(name.lower().startswith("x-ccswitch-") for name in headers))

    def test_legacy_identity_overrides_cannot_change_compatibility_headers(self) -> None:
        config = sender.normalize_config(
            {
                "user_agent": "legacy-agent",
                "originator": "legacy-originator",
                "send_codex_version_header": False,
            }
        )
        headers = sender.build_request_headers(
            self.provider,
            config,
            codex_version=sender.CodexCliVersion(version="0.146.0", source="test"),
        )
        self.assertNotIn("user_agent", config)
        self.assertNotIn("originator", config)
        self.assertNotIn("send_codex_version_header", config)
        self.assertEqual(headers["Originator"], "codex_exec")
        self.assertTrue(headers["User-Agent"].startswith("codex_exec/0.146.0 (Windows "))
        self.assertFalse(any(name.lower().startswith("x-ccswitch-") for name in headers))

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

    def test_endpoint_candidates_prefer_standard_openai_path(self) -> None:
        self.assertEqual(
            sender.endpoint_candidates(self.provider, "auto"),
            ["https://api.example.test/v1/responses", "https://api.example.test/responses"],
        )

    def test_send_one_falls_back_when_first_endpoint_returns_html(self) -> None:
        probe = sender.ProbeCase(prompt="自然任务", expected={"sum": 9, "parity": "odd"})
        config = sender.normalize_config({"random_probe_enabled": True})
        responses = [
            (200, "<html><script>var arg1='challenge'</script></html>", {"Content-Type": "text/html"}, ""),
            (200, {"output_text": '{"sum":9,"parity":"odd"}'}, {"Content-Type": "application/json"}, ""),
        ]
        with mock.patch.object(sender, "generate_probe_case", return_value=probe), mock.patch.object(
            sender,
            "endpoint_candidates",
            return_value=[
                "https://api.example.test/responses",
                "https://api.example.test/v1/responses",
            ],
        ), mock.patch.object(sender, "_http_request", side_effect=responses) as request:
            result = sender.send_one(
                1,
                self.provider,
                config,
                time.monotonic() + 5,
                sender.RunLogger(callback=lambda _line: None),
            )

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.endpoint, "https://api.example.test/v1/responses")
        self.assertEqual(request.call_count, 2)

    def test_plain_html_is_not_mislabeled_as_a_security_challenge(self) -> None:
        error = sender._html_response_error(
            "<html><script>console.log('maintenance')</script></html>",
            {"Content-Type": "text/html"},
        )

        self.assertEqual(error, "上游返回 HTML 页面，不是 API 响应。")

    def test_html_challenge_is_non_retryable_after_all_endpoints_fail(self) -> None:
        probe = sender.ProbeCase(prompt="自然任务", expected={"sum": 9, "parity": "odd"})
        config = sender.normalize_config(
            {
                "request_count": 1,
                "retry_count": 0,
                "request_timeout_seconds": 1,
                "max_wait_seconds": 0,
            }
        )
        log_lines: list[str] = []
        html_response = (
            200,
            "<html><script>var arg1='challenge'</script></html>",
            {"Content-Type": "text/html"},
            "",
        )
        with mock.patch.object(sender, "generate_probe_case", return_value=probe), mock.patch.object(
            sender,
            "_http_request",
            side_effect=[html_response, html_response],
        ) as request:
            outcome = sender.run_batch(
                config,
                sender.RunLogger(callback=log_lines.append),
                provider_loader=lambda _config: self.provider,
            )

        self.assertEqual(outcome.code, 1)
        self.assertEqual(outcome.launched, 1)
        self.assertEqual(request.call_count, 2)
        self.assertTrue(any("HARD_FAIL" in line for line in log_lines))
        self.assertFalse(any("RETRY_WAIT" in line for line in log_lines))
        failure_line = next(line for line in log_lines if "REQUEST_FAIL" in line)
        self.assertIn("HTML 安全挑战页", failure_line)
        self.assertEqual(failure_line.count("var arg1="), 1)

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
        self.assertTrue(data["ok"])
        self.assertEqual(data["error"], "")
        self.assertEqual(data["endpoint"], "https://api.example.test/v1/responses")
        self.assertNotIn("secret", json.dumps(data))

    def test_failed_result_export_includes_error(self) -> None:
        result = sender.AttemptResult(
            index=1,
            round_no=3,
            ok=False,
            status=502,
            error="upstream unavailable",
            endpoint="https://api.example.test/v1/responses",
            provider_name="Provider A",
        )

        data = sender.build_result_dict(result, sender.normalize_config())

        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "upstream unavailable")

    def test_result_export_preserves_recorded_completion_time(self) -> None:
        result = sender.AttemptResult(
            index=1,
            round_no=2,
            ok=True,
            status=200,
            text="done",
            completed_at="2026-08-20T09:30:00+08:00",
        )

        data = sender.build_result_dict(result, sender.normalize_config())

        self.assertEqual(data["completed_at"], "2026-08-20T09:30:00+08:00")


class GuiGuardTests(unittest.TestCase):
    @staticmethod
    def _summary(provider_id: str, name: str) -> sender.ProviderSummary:
        return sender.ProviderSummary(
            provider_id=provider_id,
            name=name,
            is_current=False,
            model="gpt-test",
            base_url="https://api.example.test",
            api_format="openai_responses",
            has_api_key=True,
            available=True,
        )

    def test_default_provider_prefers_first_name_containing_any(self) -> None:
        catalog = sender.ProviderCatalog(
            current_provider_id="provider-current",
            providers=(
                self._summary("provider-any-1", "Primary Any"),
                self._summary("provider-any-2", "Backup ANY"),
                self._summary("provider-current", "Current"),
            ),
        )

        self.assertEqual(sender_ui._default_provider_id(catalog), "provider-any-1")

    def test_default_provider_falls_back_to_current_without_any_name(self) -> None:
        catalog = sender.ProviderCatalog(
            current_provider_id="provider-current",
            providers=(self._summary("provider-current", "Current"),),
        )

        self.assertEqual(sender_ui._default_provider_id(catalog), "provider-current")

    def test_start_run_respects_disabled_preview_state_for_hotkey_calls(self) -> None:
        app = mock.Mock()
        app.running = False
        app.blocked_by_unfinished = False
        app.can_start = False

        sender_ui.BatchSenderApp.start_run(app)

        app._selected_provider_id.assert_not_called()

    def test_success_keepalive_gate_requires_a_fully_reclaimed_success(self) -> None:
        winner = sender.AttemptResult(index=1, ok=True, status=200, text="ok")
        success = sender.RunOutcome(0, winner, 1, 1, 0, 0)
        success_with_unfinished = sender.RunOutcome(0, winner, 2, 2, 0, 1)
        enabled = sender.normalize_config({"success_keepalive_enabled": True})
        disabled = sender.normalize_config({"success_keepalive_enabled": False})

        self.assertTrue(sender_ui._should_start_success_keepalive(success, enabled, stopped=False))
        self.assertFalse(
            sender_ui._should_start_success_keepalive(
                success_with_unfinished,
                enabled,
                stopped=False,
            )
        )
        self.assertFalse(sender_ui._should_start_success_keepalive(success, disabled, stopped=False))
        self.assertFalse(sender_ui._should_start_success_keepalive(success, enabled, stopped=True))

    def test_success_notification_is_still_allowed_when_keepalive_cannot_start(self) -> None:
        winner = sender.AttemptResult(index=1, ok=True, status=200, text="ok")
        enabled = sender.normalize_config({"success_keepalive_enabled": True})

        self.assertTrue(
            sender_ui._should_notify_success_without_keepalive(
                sender.RunOutcome(0, winner, 2, 2, 0, 1),
                enabled,
                stopped=False,
            )
        )
        self.assertTrue(
            sender_ui._should_notify_success_without_keepalive(
                sender.RunOutcome(0, winner, 1, 1, 0, 0),
                enabled,
                stopped=True,
            )
        )
        self.assertFalse(
            sender_ui._should_notify_success_without_keepalive(
                sender.RunOutcome(0, winner, 1, 1, 0, 0),
                enabled,
                stopped=False,
            )
        )

    def test_windows_notification_uses_hidden_powershell_and_environment_text(self) -> None:
        completed = mock.Mock(returncode=0)
        with (
            mock.patch.object(sender_ui.os, "name", "nt"),
            mock.patch.object(sender_ui.Path, "exists", return_value=True),
            mock.patch.object(sender_ui.subprocess, "run", return_value=completed) as run,
        ):
            shown = sender_ui.show_windows_notification("成功", "已进入定时保持")

        self.assertTrue(shown)
        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertIn("-WindowStyle", command)
        self.assertIn("Hidden", command)
        self.assertEqual(environment["CCSWITCH_NOTIFICATION_TITLE"], "成功")
        self.assertEqual(environment["CCSWITCH_NOTIFICATION_BODY"], "已进入定时保持")


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

    def test_first_success_does_not_wait_for_a_stuck_sibling(self) -> None:
        release_sender = threading.Event()
        stuck_started = threading.Event()
        logs: list[str] = []

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
            stuck_started.set()
            release_sender.wait()
            return sender.AttemptResult(
                index=index,
                round_no=round_no,
                ok=False,
                error="late",
                provider_name=provider.name,
            )

        started = time.monotonic()
        try:
            with mock.patch.object(sender, "SUCCESS_CANCELLATION_GRACE_SECONDS", 0.02), mock.patch.object(
                sender, "REQUEST_COMPLETION_GRACE_SECONDS", 0.02
            ):
                outcome = sender.run_batch(
                    sender.normalize_config(
                        {
                            "request_count": 2,
                            "retry_count": 1,
                            "request_timeout_seconds": 60,
                            "max_wait_seconds": 0,
                            "retry_interval_seconds": 0,
                        }
                    ),
                    sender.RunLogger(callback=logs.append),
                    provider_loader=lambda _config: self.provider,
                    sender=fake_sender,
                )
        finally:
            release_sender.set()

        self.assertTrue(stuck_started.is_set())
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(outcome.code, 0)
        self.assertIsNotNone(outcome.winner)
        self.assertEqual(outcome.failed, 0)
        self.assertGreaterEqual(outcome.unfinished, 1)
        first_result = next(line for line in logs if "FIRST_RESULT" in line)
        success_result = next(line for line in logs if "SUCCESS_RESULT" in line)
        batch_complete = next(line for line in logs if "BATCH_COMPLETE" in line)
        self.assertIn("success_at=", first_result)
        self.assertIn("success_at=", success_result)
        self.assertIn("success_at=", batch_complete)
        self.assertTrue(any("reason=success_received" in line for line in logs))

    def test_cancelled_result_is_not_reclassified_as_timeout_after_deadline(self) -> None:
        logs: list[str] = []

        def fake_sender(index, provider, config, deadline, logger, abort_polling, *, round_no=1):
            time.sleep(0.03)
            return sender.AttemptResult(
                index=index,
                round_no=round_no,
                ok=False,
                error="请求已取消。",
                provider_name=provider.name,
                cancelled=True,
            )

        outcome = sender.run_batch(
            sender.normalize_config(
                {
                    "request_count": 1,
                    "retry_count": 1,
                    "request_timeout_seconds": 0.01,
                    "retry_interval_seconds": 0,
                }
            ),
            sender.RunLogger(callback=logs.append),
            provider_loader=lambda _config: self.provider,
            sender=fake_sender,
        )

        self.assertEqual(outcome.code, 1)
        self.assertEqual(outcome.failed, 0)
        self.assertEqual(sum("REQUEST_CANCELLED" in line for line in logs), 2)
        self.assertFalse(any("REQUEST_TIMEOUT" in line for line in logs))

    def test_direct_api_success_aborts_a_sibling_blocked_in_response_read(self) -> None:
        request_number = 0
        request_lock = threading.Lock()
        blocked_started = threading.Event()
        release_blocked = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                nonlocal request_number
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length:
                    self.rfile.read(length)
                with request_lock:
                    request_number += 1
                    current = request_number
                if current == 1:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "1024")
                    self.end_headers()
                    self.wfile.write(b"{")
                    self.wfile.flush()
                    blocked_started.set()
                    release_blocked.wait(5)
                    return
                body = json.dumps({"output_text": "ok"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: Any) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        provider = sender.Provider(
            provider_id="local-provider",
            name="Local Provider",
            api_key="local-key",
            base_url=f"http://127.0.0.1:{server.server_port}",
            model="gpt-test",
            api_format="openai_responses",
        )
        logs: list[str] = []
        try:
            outcome = sender.run_batch(
                sender.normalize_config(
                    {
                        "request_count": 2,
                        "retry_count": 1,
                        "request_timeout_seconds": 5,
                        "retry_interval_seconds": 0,
                        "random_probe_enabled": False,
                        "message": "local cancellation test",
                        "endpoint_style": "openai",
                    }
                ),
                sender.RunLogger(callback=logs.append),
                provider_loader=lambda _config: provider,
            )
        finally:
            release_blocked.set()
            server.shutdown()
            server.server_close()
            server_thread.join(2)

        self.assertTrue(blocked_started.is_set())
        self.assertEqual(outcome.code, 0)
        self.assertEqual(outcome.unfinished, 0)
        self.assertEqual(outcome.failed, 0)
        self.assertTrue(any("REQUEST_CANCELLED" in line for line in logs))

    def test_success_keepalive_sends_one_request_per_interval_and_continues_after_failure(self) -> None:
        calls: list[int] = []
        waits: list[float] = []
        logs: list[str] = []
        progress: list[sender.ProgressEvent] = []
        wait_results = iter((False, False, True))

        def fake_wait(seconds: float) -> bool:
            waits.append(seconds)
            return next(wait_results)

        def fake_sender(index, provider, config, deadline, logger, abort_polling, *, round_no=1):
            calls.append(round_no)
            if round_no == 1:
                return sender.AttemptResult(
                    index=index,
                    round_no=round_no,
                    ok=False,
                    status=502,
                    error="temporary",
                    provider_name=provider.name,
                )
            return sender.AttemptResult(
                index=index,
                round_no=round_no,
                ok=True,
                status=200,
                text="kept",
                provider_name=provider.name,
            )

        outcome = sender.run_success_keepalive(
            self.provider,
            sender.normalize_config({"success_keepalive_interval_seconds": 180}),
            sender.RunLogger(callback=logs.append),
            threading.Event(),
            on_progress=progress.append,
            sender=fake_sender,
            waiter=fake_wait,
        )

        self.assertEqual(calls, [1, 2])
        self.assertEqual(waits, [180, 180, 180])
        self.assertEqual(outcome, sender.KeepaliveOutcome(sent=2, succeeded=1, failed=1, stopped=True))
        self.assertEqual(sum("KEEPALIVE_OK" in line for line in logs), 1)
        self.assertEqual(sum("KEEPALIVE_FAIL" in line for line in logs), 1)
        self.assertEqual([event.kind for event in progress].count("keepalive_start"), 2)
        self.assertEqual([event.kind for event in progress].count("keepalive_result"), 2)
        keepalive_results = [event.result for event in progress if event.kind == "keepalive_result"]
        self.assertEqual([item.ok for item in keepalive_results if item is not None], [False, True])
        self.assertEqual(progress[-1].kind, "keepalive_stopped")

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

    def test_request_timeout_does_not_terminate_the_batch(self) -> None:
        calls: list[tuple[int, int]] = []
        lock = threading.Lock()

        def fake_sender(index, provider, config, deadline, logger, abort_polling, *, round_no=1):
            with lock:
                calls.append((round_no, index))
            time.sleep(0.06)
            return sender.AttemptResult(
                index=index,
                round_no=round_no,
                ok=False,
                status=502,
                error="bad gateway",
            )

        outcome = sender.run_batch(
            sender.normalize_config(
                {
                    "request_count": 2,
                    "retry_count": 1,
                    "request_timeout_seconds": 0.02,
                    "max_wait_seconds": 0,
                    "retry_interval_seconds": 0,
                }
            ),
            sender.RunLogger(callback=lambda _line: None),
            provider_loader=lambda _config: self.provider,
            sender=fake_sender,
        )

        self.assertEqual(outcome.code, 1)
        self.assertEqual(outcome.launched, 4)
        self.assertEqual(outcome.completed, 4)
        self.assertEqual(outcome.unfinished, 0)
        self.assertEqual(sorted(calls), [(1, 1), (1, 2), (2, 1), (2, 2)])

    def test_each_request_deadline_uses_request_timeout_not_total_wait(self) -> None:
        remaining_deadlines: list[float] = []

        def fake_sender(index, provider, config, deadline, logger, abort_polling, *, round_no=1):
            remaining_deadlines.append(deadline - time.monotonic())
            return sender.AttemptResult(
                index=index,
                round_no=round_no,
                ok=False,
                status=502,
                error="bad gateway",
            )

        outcome = sender.run_batch(
            sender.normalize_config(
                {
                    "request_count": 1,
                    "retry_count": 1,
                    "request_timeout_seconds": 0.2,
                    "max_wait_seconds": 1,
                    "retry_interval_seconds": 0,
                }
            ),
            sender.RunLogger(callback=lambda _line: None),
            provider_loader=lambda _config: self.provider,
            sender=fake_sender,
        )

        self.assertEqual(outcome.code, 1)
        self.assertEqual(len(remaining_deadlines), 2)
        self.assertTrue(all(0 <= remaining <= 0.3 for remaining in remaining_deadlines))

    def test_sender_ignoring_deadline_cannot_block_run_forever(self) -> None:
        release_sender = threading.Event()
        sender_finished = threading.Event()
        progress: list[sender.ProgressEvent] = []

        def fake_sender(index, provider, config, deadline, logger, abort_polling, *, round_no=1):
            release_sender.wait()
            sender_finished.set()
            return sender.AttemptResult(
                index=index,
                round_no=round_no,
                ok=False,
                status=502,
                error="bad gateway",
            )

        started = time.monotonic()
        try:
            with mock.patch.object(sender, "REQUEST_COMPLETION_GRACE_SECONDS", 0.02):
                outcome = sender.run_batch(
                    sender.normalize_config(
                        {
                            "request_count": 1,
                            "retry_count": 1,
                            "request_timeout_seconds": 0.02,
                            "max_wait_seconds": 0,
                            "retry_interval_seconds": 0,
                        }
                    ),
                    sender.RunLogger(callback=lambda _line: None),
                    on_progress=progress.append,
                    provider_loader=lambda _config: self.provider,
                    sender=fake_sender,
                )
        finally:
            release_sender.set()

        self.assertTrue(sender_finished.wait(0.5))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(outcome.code, 1)
        self.assertEqual(outcome.launched, 2)
        self.assertEqual(outcome.completed, 2)
        self.assertEqual(outcome.failed, 2)
        self.assertEqual(outcome.unfinished, 1)
        self.assertFalse(any(event.kind == "timeout" for event in progress))
        request_timeouts = [
            event for event in progress if event.kind == "request_complete" and "单请求超时" in event.message
        ]
        blocked_requests = [
            event for event in progress if event.kind == "request_complete" and "发送槽位" in event.message
        ]
        self.assertEqual(len(request_timeouts), 1)
        self.assertEqual(len(blocked_requests), 1)

    def test_stop_does_not_wait_for_a_stuck_sender_deadline(self) -> None:
        release_sender = threading.Event()
        sender_started = threading.Event()
        stop_event = threading.Event()
        outcome_holder: list[sender.RunOutcome] = []

        def fake_sender(index, provider, config, deadline, logger, abort_polling, *, round_no=1):
            sender_started.set()
            release_sender.wait()
            return sender.AttemptResult(index=index, round_no=round_no, ok=False, error="stuck")

        def run() -> None:
            outcome_holder.append(
                sender.run_batch(
                    sender.normalize_config(
                        {
                            "request_count": 1,
                            "retry_count": 1,
                            "request_timeout_seconds": 60,
                            "max_wait_seconds": 0,
                            "retry_interval_seconds": 0,
                        }
                    ),
                    sender.RunLogger(callback=lambda _line: None),
                    stop_event=stop_event,
                    provider_loader=lambda _config: self.provider,
                    sender=fake_sender,
                )
            )

        runner = threading.Thread(target=run, daemon=True)
        try:
            with mock.patch.object(sender, "REQUEST_COMPLETION_GRACE_SECONDS", 0.02):
                runner.start()
                self.assertTrue(sender_started.wait(0.5))
                stopped_at = time.monotonic()
                stop_event.set()
                runner.join(0.5)
        finally:
            release_sender.set()
            runner.join(0.5)

        self.assertFalse(runner.is_alive())
        self.assertLess(time.monotonic() - stopped_at, 0.5)
        self.assertEqual(len(outcome_holder), 1)
        self.assertEqual(outcome_holder[0].code, 130)
        self.assertEqual(outcome_holder[0].unfinished, 1)

    def test_unlimited_run_waits_for_stuck_sender_until_stopped(self) -> None:
        release_sender = threading.Event()
        sender_started = threading.Event()
        stop_event = threading.Event()
        outcome_holder: list[sender.RunOutcome] = []

        def fake_sender(index, provider, config, deadline, logger, abort_polling, *, round_no=1):
            sender_started.set()
            release_sender.wait()
            return sender.AttemptResult(index=index, round_no=round_no, ok=False, error="stuck")

        def run() -> None:
            outcome_holder.append(
                sender.run_batch(
                    sender.normalize_config(
                        {
                            "request_count": 1,
                            "retry_count": 0,
                            "request_timeout_seconds": 0.02,
                            "max_wait_seconds": 0,
                            "retry_interval_seconds": 0,
                        }
                    ),
                    sender.RunLogger(callback=lambda _line: None),
                    stop_event=stop_event,
                    provider_loader=lambda _config: self.provider,
                    sender=fake_sender,
                )
            )

        runner = threading.Thread(target=run, daemon=True)
        try:
            with mock.patch.object(sender, "REQUEST_COMPLETION_GRACE_SECONDS", 0.02):
                runner.start()
                self.assertTrue(sender_started.wait(0.5))
                time.sleep(0.1)
                self.assertTrue(runner.is_alive())
                stop_event.set()
                runner.join(0.5)
        finally:
            release_sender.set()
            runner.join(0.5)

        self.assertFalse(runner.is_alive())
        self.assertEqual(len(outcome_holder), 1)
        self.assertEqual(outcome_holder[0].code, 130)
        self.assertEqual(outcome_holder[0].unfinished, 1)

    def test_zero_retries_runs_until_success(self) -> None:
        calls: list[int] = []
        progress: list[sender.ProgressEvent] = []

        def fake_sender(index, provider, config, deadline, logger, abort_polling, *, round_no=1):
            calls.append(round_no)
            if round_no == 3:
                return sender.AttemptResult(index=index, round_no=round_no, ok=True, status=200, text="ok")
            return sender.AttemptResult(index=index, round_no=round_no, ok=False, status=502, error="bad gateway")

        outcome = sender.run_batch(
            sender.normalize_config(
                {"request_count": 1, "retry_count": 0, "retry_interval_seconds": 0}
            ),
            sender.RunLogger(callback=lambda _line: None),
            on_progress=progress.append,
            provider_loader=lambda _config: self.provider,
            sender=fake_sender,
        )
        self.assertEqual(outcome.code, 0)
        self.assertEqual(outcome.launched, 3)
        self.assertEqual(calls, [1, 2, 3])
        self.assertTrue(all(event.max_rounds == 0 for event in progress))
        self.assertTrue(all(event.total_cap == 0 for event in progress))

    def test_unlimited_retries_stop_on_user_request(self) -> None:
        stop_event = threading.Event()
        calls: list[int] = []

        def fake_sender(index, provider, config, deadline, logger, abort_polling, *, round_no=1):
            calls.append(round_no)
            if round_no == 2:
                stop_event.set()
            return sender.AttemptResult(index=index, round_no=round_no, ok=False, status=502, error="bad gateway")

        outcome = sender.run_batch(
            sender.normalize_config(
                {"request_count": 1, "retry_count": 0, "retry_interval_seconds": 0}
            ),
            sender.RunLogger(callback=lambda _line: None),
            stop_event=stop_event,
            provider_loader=lambda _config: self.provider,
            sender=fake_sender,
        )

        self.assertEqual(outcome.code, 130)
        self.assertEqual(calls, [1, 2])

    def test_non_retryable_batch_stops_before_configured_retries(self) -> None:
        calls = 0
        lock = threading.Lock()

        def fake_sender(index, provider, config, deadline, logger, abort_polling, *, round_no=1):
            nonlocal calls
            with lock:
                calls += 1
            return sender.AttemptResult(index=index, round_no=round_no, ok=False, status=401, error="unauthorized")

        outcome = sender.run_batch(
            sender.normalize_config({"request_count": 2, "retry_count": 0, "retry_interval_seconds": 0}),
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
            sender.normalize_config(
                {
                    "transport_mode": sender.TRANSPORT_CODEX_CLI,
                    "request_count": 2,
                    "retry_count": 2,
                }
            ),
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

    def test_direct_transport_logs_codex_compatibility_simulation(self) -> None:
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
        self.assertIn("client=codex-compatibility-simulation", run_start)
        self.assertIn("originator=codex_exec", run_start)
        self.assertIn("post_cap=unlimited", run_start)

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
                    "retry_count": 1,
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
