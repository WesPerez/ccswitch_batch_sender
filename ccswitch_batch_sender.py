from __future__ import annotations

import argparse
import copy
import ctypes
import datetime as dt
import functools
import json
import os
import queue
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


APP_NAME = "CC Switch Batch Sender"
APP_TITLE = "CC Switch 批量请求"
APP_VERSION = "2.2.5"
ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".cc-switch" / "cc-switch.db"
DIAGNOSTIC_LOG_MAX_BYTES = 512 * 1024
DIAGNOSTIC_LOG_BACKUP_COUNT = 2
REGISTRY_PATH = r"Software\CCSwitchBatchSender"
REGISTRY_VALUE = "SettingsJson"
REGISTRY_SCHEMA_VALUE = "SchemaVersion"
REGISTRY_SCHEMA_VERSION = 7
MUTEX_NAME = r"Local\CCSwitchBatchSender.App"
PROMPT_CACHE_KEY_PLACEHOLDER = "<每个请求唯一>"
RANDOM_TASK_PLACEHOLDER = "<每个请求随机任务>"
LEGACY_RANDOM_PROBE_PLACEHOLDER = "<每个请求随机探针>"
RANDOM_PROBE_PLACEHOLDER = RANDOM_TASK_PLACEHOLDER
RANDOM_TASK_PLACEHOLDERS = (RANDOM_TASK_PLACEHOLDER, LEGACY_RANDOM_PROBE_PLACEHOLDER)
DEFAULT_FIXED_MESSAGE = "请说明批量请求为什么需要超时。"
CODEX_VERSION_HEADER = "X-CCSwitch-Local-Codex-CLI-Version"
TRANSPORT_CODEX_CLI = "codex_cli"
TRANSPORT_DIRECT = "direct"
PROXY_MANAGED_TOKEN = "PROXY_MANAGED"
CODEX_RESERVED_MODEL_PROVIDER_IDS = frozenset(
    {"amazon-bedrock", "openai", "ollama", "lmstudio", "oss", "ollama-chat"}
)


DEFAULT_CONFIG: dict[str, Any] = {
    "transport_mode": TRANSPORT_DIRECT,
    "cli_concurrency": 10,
    "provider_id": "current",
    "model": "",
    "base_url": "",
    "message": DEFAULT_FIXED_MESSAGE,
    "random_probe_enabled": True,
    "request_count": 15,
    "retry_count": 10,
    "max_output_tokens": 64,
    "request_timeout_seconds": 10,
    "max_wait_seconds": 7200,
    "retry_interval_seconds": 3,
    "poll_interval_seconds": 2,
    "user_agent": f"{APP_NAME}/{APP_VERSION} (non-codex)",
    "originator": APP_NAME,
    "db_path": "",
    "endpoint_style": "auto",
    "unique_prompt_cache_key": True,
    "send_codex_version_header": True,
    "save_full_response": False,
    "custom_body_enabled": False,
    "custom_body": None,
}

PERSISTED_KEYS = (
    "transport_mode",
    "cli_concurrency",
    "model",
    "base_url",
    "message",
    "random_probe_enabled",
    "request_count",
    "retry_count",
    "max_output_tokens",
    "request_timeout_seconds",
    "max_wait_seconds",
    "retry_interval_seconds",
    "poll_interval_seconds",
    "endpoint_style",
    "unique_prompt_cache_key",
    "send_codex_version_header",
    "save_full_response",
    "custom_body_enabled",
    "custom_body",
)


class SenderError(RuntimeError):
    """A user-actionable configuration or transport error."""


@dataclass(frozen=True)
class Provider:
    provider_id: str
    name: str
    api_key: str
    base_url: str
    model: str
    api_format: str


@dataclass(frozen=True)
class ProviderSummary:
    provider_id: str
    name: str
    is_current: bool
    model: str
    base_url: str
    api_format: str
    has_api_key: bool
    available: bool
    unavailable_reason: str = ""


@dataclass(frozen=True)
class ProviderCatalog:
    current_provider_id: str
    providers: tuple[ProviderSummary, ...]


@dataclass(frozen=True)
class ProbeCase:
    prompt: str
    expected: dict[str, Any]


@dataclass(frozen=True)
class CodexCliVersion:
    version: str = ""
    source: str = "unavailable"


@dataclass
class AttemptResult:
    index: int
    ok: bool
    round_no: int = 1
    status: int | None = None
    text: str = ""
    error: str = ""
    endpoint: str = ""
    latency_ms: int = 0
    payload: Any = None
    provider_name: str = ""
    request_prompt: str = ""
    pending: bool = False
    cancelled: bool = False
    response_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProgressEvent:
    kind: str
    round_no: int = 0
    max_rounds: int = 0
    request_index: int | None = None
    launched_total: int = 0
    completed_total: int = 0
    failed_total: int = 0
    total_cap: int = 0
    completed_in_round: int = 0
    round_size: int = 0
    winner: AttemptResult | None = None
    message: str = ""
    unfinished: int = 0


@dataclass(frozen=True)
class RunOutcome:
    code: int
    winner: AttemptResult | None
    launched: int
    completed: int
    failed: int
    unfinished: int = 0


def _console_print(message: str, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    if stream is None:
        return
    print(message, file=stream, flush=True)


class RunLogger:
    def __init__(
        self,
        callback: Callable[[str], None] | None = None,
        path: Path | None = None,
    ) -> None:
        self.path = path
        self._callback = callback
        self._lock = threading.Lock()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, message: str) -> None:
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        line = f"[{stamp}] {message}"
        with self._lock:
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        if self._callback is not None:
            self._callback(line)
        elif self.path is None:
            _console_print(line)


def default_provider_diagnostics_path() -> Path:
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if local_app_data:
        root = Path(local_app_data) / "CCSwitchBatchSender"
    else:
        root = Path.home() / ".ccswitch-batch-sender"
    return root / "logs" / "provider-diagnostics.jsonl"


class ProviderDiagnostics:
    _SENSITIVE_FIELD = re.compile(
        r"(?:api.?key|authorization|credential|password|secret|token)",
        re.IGNORECASE,
    )

    def __init__(
        self,
        path: Path | None = None,
        *,
        max_bytes: int = DIAGNOSTIC_LOG_MAX_BYTES,
        backup_count: int = DIAGNOSTIC_LOG_BACKUP_COUNT,
    ) -> None:
        self.path = path or default_provider_diagnostics_path()
        self.max_bytes = max(1, int(max_bytes))
        self.backup_count = max(0, int(backup_count))
        self._lock = threading.Lock()

    @classmethod
    def _is_sensitive_field(cls, field_name: str) -> bool:
        return field_name not in {"credential_source", "has_api_key"} and bool(
            cls._SENSITIVE_FIELD.search(field_name)
        )

    @classmethod
    def _safe_value(cls, value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, dict):
            return {
                str(key): cls._safe_value(item)
                for key, item in value.items()
                if not cls._is_sensitive_field(str(key))
            }
        if isinstance(value, (list, tuple)):
            return [cls._safe_value(item) for item in value]
        return str(value)

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        try:
            current_size = self.path.stat().st_size
        except OSError:
            return
        if current_size <= 0 or current_size + incoming_bytes <= self.max_bytes:
            return
        if self.backup_count <= 0:
            self.path.unlink(missing_ok=True)
            return
        for index in range(self.backup_count, 0, -1):
            source = self.path if index == 1 else self.path.with_name(f"{self.path.name}.{index - 1}")
            target = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                source.replace(target)

    def record(self, event: str, **fields: Any) -> None:
        payload: dict[str, Any] = {
            "timestamp": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": str(event),
            "app_version": APP_VERSION,
            "pid": os.getpid(),
        }
        for key, value in fields.items():
            field_name = str(key)
            if self._is_sensitive_field(field_name):
                continue
            payload[field_name] = self._safe_value(value)
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        encoded_size = len(line.encode("utf-8"))
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed(encoded_size)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
        except OSError:
            return


class SingleInstanceMutex:
    def __init__(self, name: str = MUTEX_NAME) -> None:
        self.name = name
        self.handle: int | None = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.SetLastError(0)
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise ctypes.WinError()
        if kernel32.GetLastError() == 183:
            kernel32.CloseHandle(handle)
            return False
        self.handle = int(handle)
        return True

    def release(self) -> None:
        if self.handle is None or os.name != "nt":
            return
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.handle))
        self.handle = None


class ActiveCodexProcessRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[int, subprocess.Popen[str]] = {}

    def register(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes[process.pid] = process

    def discard(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes.pop(process.pid, None)

    def terminate_all(self) -> None:
        with self._lock:
            processes = list(self._processes.values())
        terminators = [
            threading.Thread(
                target=_terminate_process_tree,
                args=(process,),
                name=f"ccswitch-terminate-{process.pid}",
                daemon=True,
            )
            for process in processes
        ]
        for thread in terminators:
            thread.start()
        for thread in terminators:
            thread.join()
        with self._lock:
            for process in processes:
                if process.poll() is not None:
                    self._processes.pop(process.pid, None)

    def count(self) -> int:
        with self._lock:
            return len(self._processes)


ACTIVE_CODEX_PROCESSES = ActiveCodexProcessRegistry()


def _terminate_process_tree(process: subprocess.Popen[str], timeout: float = 3.0) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill:
            try:
                subprocess.run(
                    [taskkill, "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.SubprocessError):
                pass
    else:
        try:
            process.terminate()
        except OSError:
            pass

    if process.poll() is None:
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


def terminate_active_codex_processes() -> None:
    ACTIVE_CODEX_PROCESSES.terminate_all()


def enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass


def resource_path(relative: str | Path) -> Path:
    base = Path(getattr(sys, "_MEIPASS", ROOT))
    return base / Path(relative)


def _coerce_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SenderError(f"{field_name} 必须是整数。") from exc


def _coerce_float(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise SenderError(f"{field_name} 必须是数字。") from exc


def normalize_config(values: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = dict(DEFAULT_CONFIG)
    if values:
        raw.update(values)

    config: dict[str, Any] = {
        "transport_mode": str(raw.get("transport_mode", TRANSPORT_DIRECT)).strip().lower(),
        "cli_concurrency": _coerce_int(raw.get("cli_concurrency"), "CLI 并发数"),
        "provider_id": str(raw.get("provider_id", "current")).strip() or "current",
        "model": str(raw.get("model", "")).strip(),
        "base_url": str(raw.get("base_url", "")).strip().rstrip("/"),
        "message": str(raw.get("message", "")),
        "random_probe_enabled": bool(raw.get("random_probe_enabled", True)),
        "request_count": _coerce_int(raw.get("request_count"), "请求次数"),
        "retry_count": _coerce_int(raw.get("retry_count"), "重试次数"),
        "max_output_tokens": _coerce_int(raw.get("max_output_tokens"), "最大输出 token"),
        "request_timeout_seconds": _coerce_float(raw.get("request_timeout_seconds"), "单次超时"),
        "max_wait_seconds": _coerce_float(raw.get("max_wait_seconds"), "总等待时间"),
        "retry_interval_seconds": _coerce_float(raw.get("retry_interval_seconds"), "重试间隔"),
        "poll_interval_seconds": _coerce_float(raw.get("poll_interval_seconds"), "轮询间隔"),
        "user_agent": str(raw.get("user_agent", DEFAULT_CONFIG["user_agent"])).strip(),
        "originator": str(raw.get("originator", DEFAULT_CONFIG["originator"])).strip(),
        "db_path": str(raw.get("db_path", "")).strip(),
        "endpoint_style": str(raw.get("endpoint_style", "auto")).strip().lower(),
        "unique_prompt_cache_key": bool(raw.get("unique_prompt_cache_key", True)),
        "send_codex_version_header": bool(raw.get("send_codex_version_header", True)),
        "save_full_response": bool(raw.get("save_full_response", False)),
        "custom_body_enabled": bool(raw.get("custom_body_enabled", False)),
        "custom_body": copy.deepcopy(raw.get("custom_body")),
    }

    if not 1 <= config["request_count"] <= 100:
        raise SenderError("请求次数必须在 1 到 100 之间。")
    if config["transport_mode"] not in {TRANSPORT_CODEX_CLI, TRANSPORT_DIRECT}:
        raise SenderError("请求来源只能是官方 Codex CLI 或直接 API。")
    if not 1 <= config["cli_concurrency"] <= 10:
        raise SenderError("CLI 并发数必须在 1 到 10 之间。")
    if not 0 <= config["retry_count"] <= 10:
        raise SenderError("重试次数必须在 0 到 10 之间。")
    if not 1 <= config["max_output_tokens"] <= 4096:
        raise SenderError("最大输出 token 必须在 1 到 4096 之间。")
    if config["request_timeout_seconds"] <= 0:
        raise SenderError("单次超时必须大于 0。")
    if config["max_wait_seconds"] < 0:
        raise SenderError("总等待时间不能小于 0。")
    if config["retry_interval_seconds"] < 0:
        raise SenderError("重试间隔不能小于 0。")
    if config["poll_interval_seconds"] <= 0:
        raise SenderError("轮询间隔必须大于 0。")
    if config["endpoint_style"] not in {"auto", "ccswitch", "openai"}:
        raise SenderError("Endpoint 模式只能是 auto、ccswitch 或 openai。")

    custom_body = config["custom_body"]
    if isinstance(custom_body, str):
        try:
            custom_body = json.loads(custom_body)
        except json.JSONDecodeError as exc:
            raise SenderError(f"自定义请求体不是有效 JSON：{exc.msg}。") from exc
        config["custom_body"] = custom_body
    if config["custom_body_enabled"]:
        if not isinstance(custom_body, dict):
            raise SenderError("自定义请求体必须是 JSON 对象。")
    elif not config["random_probe_enabled"] and not config["message"].strip():
        raise SenderError("提示词不能为空。")
    elif custom_body is not None and not isinstance(custom_body, dict):
        config["custom_body"] = None
    return config


def load_json_config(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SenderError(f"配置文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise SenderError(f"配置文件不是有效 JSON：{path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise SenderError(f"配置文件必须是 JSON 对象：{path}")
    return normalize_config(loaded)


def migrate_saved_config(values: dict[str, Any], schema_version: int) -> dict[str, Any]:
    migrated = copy.deepcopy(values)
    if schema_version < 2:
        migrated.setdefault("random_probe_enabled", True)
        if str(migrated.get("message", "")).strip() in {"", "1"}:
            migrated["message"] = DEFAULT_FIXED_MESSAGE
        if migrated.get("max_output_tokens") in {None, 1, "1"}:
            migrated["max_output_tokens"] = DEFAULT_CONFIG["max_output_tokens"]
    if schema_version < 3:
        migrated.setdefault("transport_mode", DEFAULT_CONFIG["transport_mode"])
        migrated.setdefault("cli_concurrency", DEFAULT_CONFIG["cli_concurrency"])
    if schema_version < 4:
        if migrated.get("request_count") in {None, 20, "20"}:
            migrated["request_count"] = DEFAULT_CONFIG["request_count"]
        if migrated.get("retry_count") in {None, 0, "0"}:
            migrated["retry_count"] = DEFAULT_CONFIG["retry_count"]
    if schema_version < 5:
        if migrated.get("request_count") in {None, 5, "5"}:
            migrated["request_count"] = DEFAULT_CONFIG["request_count"]
        if migrated.get("retry_count") in {None, 5, "5"}:
            migrated["retry_count"] = DEFAULT_CONFIG["retry_count"]
        if migrated.get("cli_concurrency") in {None, 4, "4"}:
            migrated["cli_concurrency"] = DEFAULT_CONFIG["cli_concurrency"]
    if schema_version < 6:
        old_request_default = migrated.get("request_count") in {None, 10, "10"}
        old_retry_default = migrated.get("retry_count") in {None, 2, "2"}
        if old_request_default and old_retry_default:
            migrated["request_count"] = DEFAULT_CONFIG["request_count"]
            migrated["retry_count"] = DEFAULT_CONFIG["retry_count"]
        migrated.setdefault("transport_mode", DEFAULT_CONFIG["transport_mode"])
    if schema_version < 7 and migrated.get("request_timeout_seconds") in {
        None,
        7200,
        "7200",
        "7200.0",
    }:
        migrated["request_timeout_seconds"] = DEFAULT_CONFIG["request_timeout_seconds"]
    return migrated


def load_saved_config() -> dict[str, Any]:
    if os.name != "nt":
        return normalize_config()
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH) as key:
            raw, _ = winreg.QueryValueEx(key, REGISTRY_VALUE)
            try:
                schema_version, _ = winreg.QueryValueEx(key, REGISTRY_SCHEMA_VALUE)
            except FileNotFoundError:
                schema_version = 0
        loaded = json.loads(str(raw))
        if not isinstance(loaded, dict):
            return normalize_config()
        return normalize_config(migrate_saved_config(loaded, int(schema_version)))
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError, SenderError):
        return normalize_config()


def persistent_config_payload(config: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_config(config)
    return {key: copy.deepcopy(normalized[key]) for key in PERSISTED_KEYS}


def save_saved_config(config: dict[str, Any]) -> None:
    if os.name != "nt":
        return
    import winreg

    payload = persistent_config_payload(config)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REGISTRY_PATH, 0, winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, REGISTRY_SCHEMA_VALUE, 0, winreg.REG_DWORD, REGISTRY_SCHEMA_VERSION)
        winreg.SetValueEx(
            key,
            REGISTRY_VALUE,
            0,
            winreg.REG_SZ,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )


def _config_value(config_obj: Any, key: str) -> str:
    document = config_obj if isinstance(config_obj, dict) else _toml_document(config_obj)
    if not document:
        return ""
    if key in {"base_url", "wire_api"}:
        provider_config = _active_model_provider_config(document)
        value = provider_config.get(key) if key in provider_config else document.get(key)
    else:
        value = document.get(key)
    return value.strip() if isinstance(value, str) else ""


def _normalized_direct_api_key(value: Any) -> tuple[str, str]:
    if not isinstance(value, str):
        return "", "none"
    token = value.strip()
    if not token:
        return "", "none"
    candidate = token[7:].strip() if token.casefold().startswith("bearer ") else token
    folded = candidate.casefold()
    if PROXY_MANAGED_TOKEN.casefold() in folded or "xai_oauth_placeholder" in folded:
        return "", "proxy_managed"
    return candidate, "usable"


def _toml_document(config_blob: Any) -> dict[str, Any]:
    if isinstance(config_blob, dict):
        return config_blob
    if not isinstance(config_blob, str):
        return {}
    text = config_blob.strip()
    if not text:
        return {}
    try:
        parsed = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _active_model_provider_config(document: dict[str, Any]) -> dict[str, Any]:
    raw_provider_id = document.get("model_provider")
    provider_id = raw_provider_id.strip() if isinstance(raw_provider_id, str) else ""
    providers = document.get("model_providers")
    provider_config = providers.get(provider_id) if provider_id and isinstance(providers, dict) else None
    return provider_config if isinstance(provider_config, dict) else {}


def _config_experimental_bearer_token(config_blob: Any) -> tuple[str, str]:
    document = _toml_document(config_blob)
    if not document:
        return "", "none"

    raw_provider_id = document.get("model_provider")
    provider_id = raw_provider_id.strip() if isinstance(raw_provider_id, str) else ""
    top_level, top_level_state = _normalized_direct_api_key(
        document.get("experimental_bearer_token")
    )
    if provider_id and provider_id.casefold() not in CODEX_RESERVED_MODEL_PROVIDER_IDS:
        provider_config = _active_model_provider_config(document)
        if provider_config:
            provider_token, provider_state = _normalized_direct_api_key(
                provider_config.get("experimental_bearer_token")
            )
            if provider_token:
                return provider_token, "active_provider"
            if provider_state == "proxy_managed":
                return "", provider_state
    if top_level:
        return top_level, "top_level"
    return "", top_level_state


def _auth_has_oauth_material(auth: dict[str, Any]) -> bool:
    for key, value in auth.items():
        if str(key) in {"auth_mode", "OPENAI_API_KEY"} or value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                return True
        elif isinstance(value, (list, tuple, dict, set)):
            if value:
                return True
        else:
            return True
    return False


def _provider_identity_block_reason(category: Any, meta: dict[str, Any]) -> str:
    if str(category or "").strip().casefold() == "official":
        return "official"
    provider_type = str(meta.get("providerType") or meta.get("provider_type") or "").strip().casefold()
    if provider_type == "xai_oauth":
        return "managed_oauth"
    return ""


def _provider_api_key(
    settings: dict[str, Any],
    config_blob: Any,
    *,
    category: Any = None,
    meta: dict[str, Any] | None = None,
) -> tuple[str, str]:
    identity_reason = _provider_identity_block_reason(category, meta or {})
    if identity_reason:
        return "", identity_reason
    auth = settings.get("auth")
    auth = auth if isinstance(auth, dict) else {}
    api_key, auth_state = _normalized_direct_api_key(auth.get("OPENAI_API_KEY"))
    if api_key:
        return api_key, "auth"
    if auth_state == "proxy_managed":
        return "", auth_state

    config_key, config_state = _config_experimental_bearer_token(config_blob)
    if config_key:
        return config_key, config_state
    if config_state == "proxy_managed":
        return "", config_state
    if _auth_has_oauth_material(auth):
        return "", "oauth"
    return "", "none"


def resolve_db_path(config: dict[str, Any] | None = None) -> Path:
    value = str((config or {}).get("db_path") or "").strip()
    if not value:
        return DEFAULT_DB_PATH
    return Path(os.path.expandvars(value)).expanduser()


def _settings_path_for_db(db_path: Path) -> Path:
    return db_path.with_name("settings.json")


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SenderError(
            f"CC Switch 数据库不存在：{db_path}\n"
            "请先安装并配置 CC Switch；本工具不会创建数据库或复制 API Key。"
        )
    try:
        connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    except sqlite3.Error as exc:
        raise SenderError(f"无法只读打开 CC Switch 数据库：{exc}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _settings_current_provider_id(settings_path: Path) -> str:
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("currentProviderCodex") or "").strip()


def _resolve_current_provider_id(
    connection: sqlite3.Connection,
    settings_path: Path,
    *,
    strict: bool,
) -> str:
    pointer = _settings_current_provider_id(settings_path)
    if pointer:
        row = connection.execute(
            "SELECT id FROM providers WHERE app_type = 'codex' AND id = ? LIMIT 1",
            (pointer,),
        ).fetchone()
        if row is not None:
            return str(row["id"])

    rows = connection.execute(
        """
        SELECT id
        FROM providers
        WHERE app_type = 'codex' AND is_current = 1
        ORDER BY sort_index, id
        LIMIT 2
        """
    ).fetchall()
    if len(rows) == 1:
        return str(rows[0]["id"])
    if not strict:
        return ""
    if len(rows) > 1:
        raise SenderError("CC Switch 中存在多个当前 Codex provider，无法安全判断。")
    raise SenderError("CC Switch 没有可识别的当前 Codex provider。")


def _decode_provider_row(row: sqlite3.Row) -> tuple[dict[str, Any], dict[str, Any], Any]:
    try:
        settings = json.loads(row["settings_config"] or "{}")
        meta = json.loads(row["meta"] or "{}")
    except json.JSONDecodeError as exc:
        raise SenderError(f"provider 配置 JSON 无法解析：{row['name']}") from exc
    settings = settings if isinstance(settings, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    config_blob = settings.get("config", "")
    return settings, meta, config_blob


def _infer_api_format(meta: dict[str, Any], config_blob: Any) -> str:
    api_format = str(meta.get("apiFormat") or "").strip().lower()
    if api_format in {"openai_chat", "openai_responses"}:
        return api_format
    wire_api = _config_value(config_blob, "wire_api").lower()
    if wire_api == "responses":
        return "openai_responses"
    if wire_api in {"chat", "chat_completions"}:
        return "openai_chat"
    return ""


def list_codex_providers(
    config: dict[str, Any] | None = None,
    *,
    diagnostics: ProviderDiagnostics | None = None,
) -> ProviderCatalog:
    db_path = resolve_db_path(config)
    connection = _connect_readonly(db_path)
    try:
        pointer_id = _settings_current_provider_id(_settings_path_for_db(db_path))
        current_id = _resolve_current_provider_id(connection, _settings_path_for_db(db_path), strict=False)
        rows = connection.execute(
            """
            SELECT id, name, settings_config, meta, category, is_current, sort_index
            FROM providers
            WHERE app_type = 'codex'
            ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END, sort_index, id
            LIMIT 100
            """,
            (current_id,),
        ).fetchall()
        providers: list[ProviderSummary] = []
        for row in rows:
            name = str(row["name"] or row["id"])
            try:
                settings, meta, config_blob = _decode_provider_row(row)
                api_key, credential_source = _provider_api_key(
                    settings,
                    config_blob,
                    category=row["category"],
                    meta=meta,
                )
                has_api_key = bool(api_key)
                base_url = _config_value(config_blob, "base_url").rstrip("/")
                model = _config_value(config_blob, "model")
                api_format = _infer_api_format(meta, config_blob)
                missing = []
                if not has_api_key:
                    if credential_source == "proxy_managed":
                        missing.append("凭据由 CC Switch 代理托管")
                    elif credential_source in {"official", "managed_oauth", "oauth"}:
                        missing.append("OAuth/托管凭据不可直接发送")
                    else:
                        missing.append("无 API Key")
                if not base_url:
                    missing.append("无地址")
                if not model:
                    missing.append("无模型")
                if not api_format:
                    missing.append("API 格式未知")
                providers.append(
                    ProviderSummary(
                        provider_id=str(row["id"]),
                        name=name,
                        is_current=str(row["id"]) == current_id,
                        model=model,
                        base_url=base_url,
                        api_format=api_format,
                        has_api_key=has_api_key,
                        available=not missing,
                        unavailable_reason="、".join(missing),
                    )
                )
            except SenderError as exc:
                providers.append(
                    ProviderSummary(
                        provider_id=str(row["id"]),
                        name=name,
                        is_current=str(row["id"]) == current_id,
                        model="",
                        base_url="",
                        api_format="",
                        has_api_key=False,
                        available=False,
                        unavailable_reason=str(exc),
                    )
                )
        catalog = ProviderCatalog(current_provider_id=current_id, providers=tuple(providers))
        if diagnostics is not None:
            diagnostics.record(
                "PROVIDER_CATALOG",
                pointer_provider_id=pointer_id,
                resolved_current_provider_id=current_id,
                database_current_provider_ids=[
                    str(row["id"]) for row in rows if bool(row["is_current"])
                ],
                provider_count=len(providers),
            )
        return catalog
    finally:
        connection.close()


def load_provider(
    config: dict[str, Any],
    *,
    current_provider_id: str | None = None,
    diagnostics: ProviderDiagnostics | None = None,
) -> Provider:
    db_path = resolve_db_path(config)
    connection = _connect_readonly(db_path)
    try:
        requested_id = str(config.get("provider_id") or "current").strip()
        if requested_id.lower() == "current":
            if current_provider_id is None:
                provider_id = _resolve_current_provider_id(
                    connection,
                    _settings_path_for_db(db_path),
                    strict=True,
                )
            else:
                provider_id = str(current_provider_id).strip()
                if not provider_id:
                    raise SenderError("CC Switch 当前 provider 快照未识别，请刷新后重试。")
        else:
            provider_id = requested_id
        row = connection.execute(
            """
            SELECT id, name, settings_config, meta, category
            FROM providers
            WHERE app_type = 'codex' AND id = ?
            LIMIT 1
            """,
            (provider_id,),
        ).fetchone()
        if row is None:
            if diagnostics is not None:
                diagnostics.record(
                    "PROVIDER_LOAD",
                    requested_provider_id=requested_id,
                    resolved_provider_id=provider_id,
                    current_snapshot_used=current_provider_id is not None,
                    result="missing",
                )
            raise SenderError(f"CC Switch Codex provider 不存在：{provider_id}")

        settings, meta, config_blob = _decode_provider_row(row)
        api_key, credential_source = _provider_api_key(
            settings,
            config_blob,
            category=row["category"],
            meta=meta,
        )
        if not api_key:
            if diagnostics is not None:
                diagnostics.record(
                    "PROVIDER_LOAD",
                    requested_provider_id=requested_id,
                    resolved_provider_id=provider_id,
                    current_snapshot_used=current_provider_id is not None,
                    credential_source=credential_source,
                    has_api_key=False,
                    result="unavailable",
                )
            if credential_source == "proxy_managed":
                raise SenderError(
                    f"provider 的凭据由 CC Switch 本地代理托管：{row['name']}。"
                    "Batch Sender 不能把 PROXY_MANAGED 占位符直接发送给上游。"
                )
            if credential_source in {"official", "managed_oauth", "oauth"}:
                raise SenderError(
                    f"provider 使用 OAuth 或托管登录凭据：{row['name']}。"
                    "这类凭据必须由 Codex 或 CC Switch 代理注入，不能按 API Key 直接发送。"
                )
            raise SenderError(
                f"provider 没有可直接使用的 API Key：{row['name']}。"
                "官方 OAuth provider 不能按 API Key 方式直接发送。"
            )

        base_url = str(config.get("base_url") or "").strip() or _config_value(config_blob, "base_url")
        model = str(config.get("model") or "").strip() or _config_value(config_blob, "model")
        api_format = _infer_api_format(meta, config_blob)
        if not base_url:
            raise SenderError(f"provider 没有 base_url：{row['name']}")
        if not model:
            raise SenderError(f"provider 没有 model：{row['name']}")
        if not api_format:
            raise SenderError(f"无法判断 provider 的 API 格式：{row['name']}")
        provider = Provider(
            provider_id=str(row["id"]),
            name=str(row["name"]),
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            model=model,
            api_format=api_format,
        )
        if diagnostics is not None:
            diagnostics.record(
                "PROVIDER_LOAD",
                requested_provider_id=requested_id,
                resolved_provider_id=provider.provider_id,
                current_snapshot_used=current_provider_id is not None,
                credential_source=credential_source,
                has_api_key=True,
                result="ok",
            )
        return provider
    finally:
        connection.close()


def endpoint_candidates(provider: Provider, style: str) -> list[str]:
    base = provider.base_url.rstrip("/")
    if provider.api_format == "openai_responses":
        if base.endswith("/responses"):
            return [base]
        openai_endpoint = base + "/responses" if base.endswith("/v1") else base + "/v1/responses"
        candidates = (
            [openai_endpoint]
            if style == "openai"
            else [base + "/responses"]
            if style == "ccswitch"
            else [base + "/responses", openai_endpoint]
        )
    else:
        if base.endswith("/chat/completions"):
            return [base]
        openai_endpoint = base + "/chat/completions" if base.endswith("/v1") else base + "/v1/chat/completions"
        candidates = (
            [openai_endpoint]
            if style == "openai"
            else [base + "/v1/chat/completions"]
            if style == "ccswitch"
            else [base + "/v1/chat/completions", openai_endpoint]
        )
    return list(dict.fromkeys(candidates))


def parse_codex_cli_version(raw: str) -> str:
    match = re.search(r"\b(?:codex-cli\s+)?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b", raw or "")
    return match.group(1) if match else ""


def _read_codex_package_version(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return parse_codex_cli_version(str(payload.get("version") or ""))


@functools.lru_cache(maxsize=1)
def detect_codex_cli_version() -> CodexCliVersion:
    command = shutil.which("codex")
    package_candidates: list[Path] = []
    if command:
        package_candidates.append(Path(command).resolve().parent / "node_modules" / "@openai" / "codex" / "package.json")
    appdata = os.environ.get("APPDATA")
    if appdata:
        package_candidates.append(Path(appdata) / "npm" / "node_modules" / "@openai" / "codex" / "package.json")

    for candidate in dict.fromkeys(package_candidates):
        version = _read_codex_package_version(candidate)
        if version:
            return CodexCliVersion(version=version, source="npm-package")

    if command:
        try:
            completed = subprocess.run(
                [command, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            version = parse_codex_cli_version((completed.stdout or "") + "\n" + (completed.stderr or ""))
            if completed.returncode == 0 and version:
                return CodexCliVersion(version=version, source="codex-command")
        except (OSError, subprocess.SubprocessError):
            pass
    return CodexCliVersion()


@functools.lru_cache(maxsize=1)
def resolve_codex_cli_executable() -> Path | None:
    package_roots: list[Path] = []
    command = shutil.which("codex")
    if command:
        package_roots.append(Path(command).resolve().parent / "node_modules" / "@openai" / "codex")
    appdata = os.environ.get("APPDATA")
    if appdata:
        package_roots.append(Path(appdata) / "npm" / "node_modules" / "@openai" / "codex")
    for root in dict.fromkeys(package_roots):
        if not root.is_dir():
            continue
        for candidate in root.glob("node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe"):
            if candidate.is_file():
                return candidate
    executable = shutil.which("codex.exe")
    return Path(executable) if executable else None


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def build_codex_exec_command(
    executable: str | Path,
    provider: Provider,
    config: dict[str, Any],
    base_url: str,
) -> list[str]:
    developer_instructions = (
        "Answer the user's task directly. Do not call tools, inspect files, or modify the environment. "
        "Return only the requested answer."
    )
    return [
        str(executable),
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "-m",
        provider.model,
        "-c",
        f"openai_base_url={_toml_string(base_url)}",
        "-c",
        'approval_policy="never"',
        "-c",
        'model_reasoning_effort="minimal"',
        "-c",
        'model_verbosity="low"',
        "-c",
        f"developer_instructions={_toml_string(developer_instructions)}",
        "-c",
        'otel.exporter="none"',
        "-",
    ]


def _codex_cli_endpoint_candidates(provider: Provider, style: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    for endpoint in endpoint_candidates(provider, style):
        base_url = endpoint[: -len("/responses")] if endpoint.endswith("/responses") else provider.base_url
        candidates.append((base_url.rstrip("/"), endpoint))
    return candidates


def parse_codex_exec_jsonl(stdout: str) -> tuple[str, str, dict[str, Any]]:
    final_text = ""
    errors: list[str] = []
    events: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    thread_id = ""
    for raw_line in (stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        event_type = str(event.get("type") or "")
        if event_type == "thread.started":
            thread = event.get("thread")
            thread_id = str(
                event.get("thread_id")
                or (thread.get("id") if isinstance(thread, dict) else "")
                or ""
            )
        elif event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    final_text = text.strip()
        elif event_type == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = dict(event["usage"])
        elif event_type in {"error", "turn.failed"}:
            error = event.get("error") or event.get("message")
            if isinstance(error, dict):
                error = error.get("message") or error.get("code") or json.dumps(error, ensure_ascii=False)
            if error:
                errors.append(str(error))
    payload: dict[str, Any] = {
        "transport": TRANSPORT_CODEX_CLI,
        "status": "completed" if final_text and not errors else "failed",
        "thread_id": thread_id,
        "usage": usage,
        "events": events,
    }
    return final_text, " | ".join(errors), payload


def _http_status_from_text(text: str) -> int | None:
    match = re.search(r"\b(4\d\d|5\d\d)\b", text or "")
    return int(match.group(1)) if match else None


def _execute_codex_cli(
    command: list[str],
    prompt: str,
    env: dict[str, str],
    deadline: float,
    abort_event: threading.Event | None,
) -> tuple[int, str, str, bool]:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=os.environ.get("TEMP") or str(Path.home()),
        env=env,
        creationflags=creationflags,
    )
    ACTIVE_CODEX_PROCESSES.register(process)
    stopped = False
    input_data: str | None = prompt
    try:
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if (abort_event and abort_event.is_set()) or remaining <= 0:
                stopped = True
                _terminate_process_tree(process)
                break
            try:
                stdout, stderr = process.communicate(input=input_data, timeout=min(0.1, remaining))
                return int(process.returncode or 0), stdout or "", stderr or "", stopped
            except subprocess.TimeoutExpired:
                input_data = None

        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process, timeout=1)
            try:
                stdout, stderr = process.communicate(timeout=1)
            except (OSError, subprocess.SubprocessError):
                stdout, stderr = "", ""
        return int(process.returncode or 0), stdout or "", stderr or "", stopped
    finally:
        if process.poll() is None:
            _terminate_process_tree(process)
        ACTIVE_CODEX_PROCESSES.discard(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def send_one_codex_cli(
    index: int,
    provider: Provider,
    config: dict[str, Any],
    deadline: float,
    logger: RunLogger,
    abort_polling: threading.Event | None = None,
    *,
    round_no: int = 1,
    executor: Callable[
        [list[str], str, dict[str, str], float, threading.Event | None],
        tuple[int, str, str, bool],
    ] = _execute_codex_cli,
) -> AttemptResult:
    started = time.monotonic()
    if provider.api_format != "openai_responses":
        return AttemptResult(
            index=index,
            round_no=round_no,
            ok=False,
            error="官方 Codex CLI 仅支持 Responses API provider。",
            provider_name=provider.name,
        )
    executable = resolve_codex_cli_executable()
    if executable is None:
        return AttemptResult(
            index=index,
            round_no=round_no,
            ok=False,
            error="未找到本机官方 Codex CLI。",
            provider_name=provider.name,
        )
    if bool(config.get("custom_body_enabled")):
        return AttemptResult(
            index=index,
            round_no=round_no,
            ok=False,
            error="官方 Codex CLI 模式不支持自定义 JSON，请切换到直接 API。",
            provider_name=provider.name,
        )

    probe = generate_probe_case() if bool(config.get("random_probe_enabled", True)) else None
    request_prompt = probe.prompt if probe is not None else str(config["message"])
    semaphore = config.get("_cli_semaphore")
    acquired = False
    if isinstance(semaphore, threading.Semaphore):
        while time.monotonic() < deadline and not (abort_polling and abort_polling.is_set()):
            if semaphore.acquire(timeout=0.1):
                acquired = True
                break
        if not acquired:
            cancelled = bool(abort_polling and abort_polling.is_set())
            return AttemptResult(
                index=index,
                round_no=round_no,
                ok=False,
                error="Codex CLI 任务已被取消。" if cancelled else "Codex CLI 任务在等待并发槽位时超时。",
                provider_name=provider.name,
                request_prompt=request_prompt,
                cancelled=cancelled,
            )

    last_error = ""
    last_status: int | None = None
    last_endpoint = ""
    last_payload: Any = None
    try:
        for base_url, endpoint in _codex_cli_endpoint_candidates(provider, str(config["endpoint_style"])):
            if abort_polling and abort_polling.is_set():
                return AttemptResult(
                    index=index,
                    round_no=round_no,
                    ok=False,
                    error="Codex CLI 任务已被取消。",
                    provider_name=provider.name,
                    request_prompt=request_prompt,
                    cancelled=True,
                )
            last_endpoint = endpoint
            env = os.environ.copy()
            env.pop("OPENAI_API_KEY", None)
            env.pop("OPENAI_BASE_URL", None)
            env["CODEX_API_KEY"] = provider.api_key
            command = build_codex_exec_command(executable, provider, config, base_url)
            return_code, stdout, stderr, stopped = executor(command, request_prompt, env, deadline, abort_polling)
            stdout = _redact_secret_text(stdout, provider.api_key)
            stderr = _redact_secret_text(stderr, provider.api_key)
            response_text, event_error, payload = parse_codex_exec_jsonl(stdout)
            last_payload = payload
            last_status = _http_status_from_text(event_error + "\n" + stderr)
            latency_ms = int((time.monotonic() - started) * 1000)
            if stopped:
                cancelled = bool(abort_polling and abort_polling.is_set())
                return AttemptResult(
                    index=index,
                    round_no=round_no,
                    ok=False,
                    status=last_status,
                    error="Codex CLI 任务已被取消。" if cancelled else "Codex CLI 任务已超时。",
                    endpoint=endpoint,
                    latency_ms=latency_ms,
                    payload=payload,
                    provider_name=provider.name,
                    request_prompt=request_prompt,
                    cancelled=cancelled,
                )
            if return_code == 0 and response_text:
                if probe is not None:
                    valid, reason = validate_probe_response(response_text, probe)
                    if not valid:
                        return AttemptResult(
                            index=index,
                            round_no=round_no,
                            ok=False,
                            text=response_text,
                            error=f"随机任务语义校验失败：{reason}。",
                            endpoint=endpoint,
                            latency_ms=latency_ms,
                            payload=payload,
                            provider_name=provider.name,
                            request_prompt=request_prompt,
                        )
                return AttemptResult(
                    index=index,
                    round_no=round_no,
                    ok=True,
                    text=response_text,
                    endpoint=endpoint,
                    latency_ms=latency_ms,
                    payload=payload,
                    provider_name=provider.name,
                    request_prompt=request_prompt,
                )
            detail = event_error or stderr.strip() or f"Codex CLI exit {return_code}"
            last_error = detail[-1000:]
            if last_status not in {404, 405}:
                break
    except (OSError, subprocess.SubprocessError) as exc:
        last_error = f"Codex CLI 启动失败：{exc}"
    finally:
        if acquired:
            semaphore.release()
    return AttemptResult(
        index=index,
        round_no=round_no,
        ok=False,
        status=last_status,
        error=last_error or "Codex CLI 没有返回可用结果。",
        endpoint=last_endpoint,
        latency_ms=int((time.monotonic() - started) * 1000),
        payload=last_payload,
        provider_name=provider.name,
        request_prompt=request_prompt,
    )


def build_request_headers(
    provider: Provider,
    config: dict[str, Any],
    *,
    codex_version: CodexCliVersion | None = None,
) -> dict[str, str]:
    version = codex_version if codex_version is not None else detect_codex_cli_version()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer " + provider.api_key,
        "User-Agent": str(config["user_agent"]),
    }
    if provider.api_format == "openai_responses":
        headers["Originator"] = str(config["originator"])
    if bool(config.get("send_codex_version_header", True)) and version.version:
        headers[CODEX_VERSION_HEADER] = version.version
    return headers


def generate_probe_case(rng: random.Random | random.SystemRandom | None = None) -> ProbeCase:
    randomizer = rng or random.SystemRandom()
    kind = randomizer.randrange(4)
    if kind == 0:
        left = randomizer.randint(12, 89)
        right = randomizer.randint(7, 64)
        total = left + right
        return ProbeCase(
            prompt=(
                f"计算 {left} + {right}，并判断结果是奇数还是偶数。"
                "请只返回 JSON 对象，字段为 sum 和 parity，parity 使用 odd 或 even。"
            ),
            expected={"sum": total, "parity": "even" if total % 2 == 0 else "odd"},
        )
    if kind == 1:
        values = randomizer.sample(range(3, 50), 4)
        return ProbeCase(
            prompt=(
                f"将整数列表 {values} 按升序排列，并给出其中的最大值。"
                "请只返回 JSON 对象，字段为 sorted 和 max。"
            ),
            expected={"sorted": sorted(values), "max": max(values)},
        )
    if kind == 2:
        minutes = randomizer.randint(3, 24)
        threshold = randomizer.randint(4, 20) * 60
        seconds = minutes * 60
        return ProbeCase(
            prompt=(
                f"把 {minutes} 分钟换算成秒，并判断结果是否大于 {threshold} 秒。"
                "请只返回 JSON 对象，字段为 seconds 和 greater_than。"
            ),
            expected={"seconds": seconds, "greater_than": seconds > threshold},
        )
    word = randomizer.choice(("cache", "router", "signal", "thread", "window", "provider"))
    return ProbeCase(
        prompt=(
            f"将英文单词 {word} 转换为大写，并给出它的字母数量。"
            "请只返回 JSON 对象，字段为 uppercase 和 length。"
        ),
        expected={"uppercase": word.upper(), "length": len(word)},
    )


def _contains_exact_placeholder(value: Any, placeholder: str) -> bool:
    if value == placeholder:
        return True
    if isinstance(value, dict):
        return any(_contains_exact_placeholder(item, placeholder) for item in value.values())
    if isinstance(value, list):
        return any(_contains_exact_placeholder(item, placeholder) for item in value)
    return False


def _replace_exact_placeholder(value: Any, placeholder: str, replacement: str) -> Any:
    if value == placeholder:
        return replacement
    if isinstance(value, dict):
        return {key: _replace_exact_placeholder(item, placeholder, replacement) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_exact_placeholder(item, placeholder, replacement) for item in value]
    return value


def request_uses_random_probe(config: dict[str, Any]) -> bool:
    if bool(config.get("custom_body_enabled")):
        return any(
            _contains_exact_placeholder(config.get("custom_body"), placeholder)
            for placeholder in RANDOM_TASK_PLACEHOLDERS
        )
    return bool(config.get("random_probe_enabled", True))


def _parse_json_object_from_text(text: str) -> dict[str, Any] | None:
    candidate = (text or "").strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _strict_json_equal(actual: Any, expected: Any) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _strict_json_equal(actual_item, expected_item)
            for actual_item, expected_item in zip(actual, expected)
        )
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _strict_json_equal(actual[key], expected[key]) for key in expected
        )
    return actual == expected


def validate_probe_response(text: str, probe: ProbeCase) -> tuple[bool, str]:
    payload = _parse_json_object_from_text(text)
    if payload is None:
        return False, "返回内容不是 JSON 对象"
    for key, expected in probe.expected.items():
        if key not in payload:
            return False, f"缺少字段 {key}"
        actual = payload[key]
        if not _strict_json_equal(actual, expected):
            return False, f"字段 {key} 的值不符合预期"
    return True, ""


def build_body(
    provider: Provider,
    config: dict[str, Any],
    *,
    cache_key: str | None = None,
    request_prompt: str | None = None,
) -> dict[str, Any]:
    if bool(config.get("custom_body_enabled")):
        body = config.get("custom_body")
        if not isinstance(body, dict):
            raise SenderError("自定义请求体必须是 JSON 对象。")
        copied = copy.deepcopy(body)
        if copied.get("prompt_cache_key") == PROMPT_CACHE_KEY_PLACEHOLDER:
            copied["prompt_cache_key"] = cache_key or str(uuid.uuid4())
        if any(_contains_exact_placeholder(copied, placeholder) for placeholder in RANDOM_TASK_PLACEHOLDERS):
            prompt = request_prompt or generate_probe_case().prompt
            for placeholder in RANDOM_TASK_PLACEHOLDERS:
                copied = _replace_exact_placeholder(copied, placeholder, prompt)
        return copied

    if request_prompt is not None:
        message = request_prompt
    elif bool(config.get("random_probe_enabled", True)):
        message = generate_probe_case().prompt
    else:
        message = str(config["message"])
    if provider.api_format == "openai_chat":
        return {
            "model": provider.model,
            "messages": [{"role": "user", "content": message}],
            "max_tokens": int(config["max_output_tokens"]),
            "stream": False,
        }
    body: dict[str, Any] = {
        "model": provider.model,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": message}],
            }
        ],
        "reasoning": {"effort": "minimal"},
        "store": False,
        "stream": False,
        "include": ["reasoning.encrypted_content"],
        "text": {"verbosity": "low"},
        "max_output_tokens": int(config["max_output_tokens"]),
    }
    if bool(config.get("unique_prompt_cache_key", True)):
        body["prompt_cache_key"] = cache_key or str(uuid.uuid4())
    return body


def build_preview_body(provider: Provider, config: dict[str, Any]) -> dict[str, Any]:
    custom_body = config.get("custom_body") if bool(config.get("custom_body_enabled")) else None
    custom_uses_key_placeholder = isinstance(custom_body, dict) and custom_body.get("prompt_cache_key") == PROMPT_CACHE_KEY_PLACEHOLDER
    key = (
        PROMPT_CACHE_KEY_PLACEHOLDER
        if bool(config.get("unique_prompt_cache_key", True)) or custom_uses_key_placeholder
        else None
    )
    prompt = RANDOM_TASK_PLACEHOLDER if request_uses_random_probe(config) else None
    return build_body(provider, config, cache_key=key, request_prompt=prompt)


def prepare_request_body(provider: Provider, config: dict[str, Any]) -> tuple[dict[str, Any], str, ProbeCase | None]:
    probe = generate_probe_case() if request_uses_random_probe(config) else None
    if probe is not None:
        request_prompt = probe.prompt
    elif bool(config.get("custom_body_enabled")):
        request_prompt = ""
    else:
        request_prompt = str(config["message"])
    body = build_body(provider, config, request_prompt=request_prompt or None)
    return body, request_prompt, probe


def _redact_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _redact_secret_text(text: str, secret: str) -> str:
    if not text or not secret:
        return text
    redacted = text
    candidates = {
        secret,
        urllib.parse.quote(secret, safe=""),
        urllib.parse.quote_plus(secret, safe=""),
    }
    for candidate in sorted((item for item in candidates if item), key=len, reverse=True):
        redacted = redacted.replace(candidate, "<redacted>")
    return redacted


def _redact_secret_value(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return _redact_secret_text(value, secret)
    if isinstance(value, dict):
        return {key: _redact_secret_value(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secret_value(item, secret) for item in value]
    return value


def _sanitize_attempt_result(result: AttemptResult, secret: str) -> AttemptResult:
    result.text = _redact_secret_text(result.text, secret)
    result.error = _redact_secret_text(result.error, secret)
    result.payload = _redact_secret_value(result.payload, secret)
    result.response_headers = {
        str(key): _redact_secret_text(str(value), secret)
        for key, value in result.response_headers.items()
    }
    return result


def _json_or_text(raw: str) -> Any:
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return raw


def _extract_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return ""
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first, dict) else {}
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                parts = [part.get("text", "") for part in content if isinstance(part, dict)]
                text = "".join(part for part in parts if isinstance(part, str)).strip()
                if text:
                    return text
        text = first.get("text") if isinstance(first, dict) else ""
        if isinstance(text, str) and text.strip():
            return text.strip()
    output = payload.get("output")
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        parts.append(part["text"])
        text = "".join(parts).strip()
        if text:
            return text
    return ""


def _is_pending(payload: Any, status: int) -> bool:
    if status == 202:
        return True
    if isinstance(payload, dict):
        state = str(payload.get("status") or "").lower()
        return state in {"queued", "in_progress", "processing", "running"}
    return False


def _http_request(
    method: str,
    url: str,
    body: dict[str, Any] | None,
    provider: Provider,
    config: dict[str, Any],
    timeout: float,
) -> tuple[int | None, Any, dict[str, str], str]:
    headers = build_request_headers(provider, config)
    data = None if body is None else json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = _redact_secret_text(response.read().decode("utf-8", errors="replace"), provider.api_key)
            response_headers = {
                str(key): _redact_secret_text(str(value), provider.api_key)
                for key, value in response.headers.items()
            }
            return response.status, _json_or_text(raw), response_headers, ""
    except urllib.error.HTTPError as exc:
        raw = _redact_secret_text(exc.read().decode("utf-8", errors="replace")[:2000], provider.api_key)
        response_headers = (
            {
                str(key): _redact_secret_text(str(value), provider.api_key)
                for key, value in exc.headers.items()
            }
            if exc.headers
            else {}
        )
        return exc.code, _json_or_text(raw), response_headers, raw
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, None, {}, _redact_secret_text(str(exc), provider.api_key)


def _request_timeout(config: dict[str, Any], deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 0
    return max(0.1, min(float(config["request_timeout_seconds"]), remaining))


def _poll_pending(
    result: AttemptResult,
    provider: Provider,
    config: dict[str, Any],
    deadline: float,
    logger: RunLogger,
    abort_event: threading.Event | None = None,
    probe: ProbeCase | None = None,
) -> AttemptResult:
    payload = result.payload if isinstance(result.payload, dict) else {}
    location = result.response_headers.get("Location") or result.response_headers.get("location")
    request_id = payload.get("id") if isinstance(payload, dict) else None
    if location:
        poll_url = urllib.parse.urljoin(result.endpoint + "/", location)
    elif request_id and provider.api_format == "openai_responses":
        poll_url = result.endpoint.rstrip("/") + "/" + urllib.parse.quote(str(request_id), safe="")
    else:
        return AttemptResult(**{**result.__dict__, "ok": False, "error": "异步响应缺少轮询地址。"})

    while time.monotonic() < deadline and not (abort_event and abort_event.is_set()):
        wait_for = min(float(config["poll_interval_seconds"]), max(0.0, deadline - time.monotonic()))
        if abort_event and abort_event.wait(wait_for):
            break
        timeout = _request_timeout(config, deadline)
        if timeout <= 0:
            break
        status, body, headers, error = _http_request("GET", poll_url, None, provider, config, timeout)
        if status is None:
            logger.log(f"POLL_WAIT round={result.round_no} request={result.index} error={error[:300]}")
            continue
        if 200 <= status < 300 and not _is_pending(body, status):
            text = _extract_text(body)
            if probe is not None:
                valid, reason = validate_probe_response(text, probe)
                if not valid:
                    return AttemptResult(
                        index=result.index,
                        round_no=result.round_no,
                        ok=False,
                        status=status,
                        text=text,
                        error=f"随机任务语义校验失败：{reason}。",
                        endpoint=poll_url,
                        latency_ms=result.latency_ms,
                        payload=body,
                        provider_name=provider.name,
                        request_prompt=result.request_prompt,
                        response_headers=headers,
                    )
            if text or isinstance(body, dict):
                return AttemptResult(
                    index=result.index,
                    round_no=result.round_no,
                    ok=True,
                    status=status,
                    text=text,
                    endpoint=poll_url,
                    latency_ms=result.latency_ms,
                    payload=body,
                    provider_name=provider.name,
                    request_prompt=result.request_prompt,
                    response_headers=headers,
                )
        if status >= 400:
            return AttemptResult(
                index=result.index,
                round_no=result.round_no,
                ok=False,
                status=status,
                error=f"poll HTTP {status}: {str(body)[:500]}",
                endpoint=poll_url,
                provider_name=provider.name,
                request_prompt=result.request_prompt,
            )
    cancelled = bool(abort_event and abort_event.is_set())
    reason = "轮询已取消。" if cancelled else "轮询超时。"
    return AttemptResult(**{**result.__dict__, "ok": False, "error": reason, "cancelled": cancelled})


def send_one(
    index: int,
    provider: Provider,
    config: dict[str, Any],
    deadline: float,
    logger: RunLogger,
    abort_polling: threading.Event | None = None,
    *,
    round_no: int = 1,
) -> AttemptResult:
    started = time.monotonic()
    body, request_prompt, probe = prepare_request_body(provider, config)
    last_error = ""
    last_status: int | None = None
    last_payload: Any = None
    last_endpoint = ""
    for endpoint in endpoint_candidates(provider, str(config["endpoint_style"])):
        timeout = _request_timeout(config, deadline)
        if timeout <= 0:
            last_error = "请求未发送：已到达本轮截止时间。"
            break
        last_endpoint = endpoint
        status, payload, headers, error = _http_request("POST", endpoint, body, provider, config, timeout)
        last_status = status
        last_payload = payload
        latency_ms = int((time.monotonic() - started) * 1000)
        if status is not None and 200 <= status < 300:
            if isinstance(payload, dict) and payload.get("error"):
                return AttemptResult(
                    index=index,
                    round_no=round_no,
                    ok=False,
                    status=status,
                    error=f"Provider error: {str(payload.get('error'))[:500]}",
                    endpoint=endpoint,
                    latency_ms=latency_ms,
                    payload=payload,
                    provider_name=provider.name,
                    request_prompt=request_prompt,
                    response_headers=headers,
                )
            state = str(payload.get("status") or "").lower() if isinstance(payload, dict) else ""
            if state in {"failed", "cancelled", "canceled", "expired"}:
                return AttemptResult(
                    index=index,
                    round_no=round_no,
                    ok=False,
                    status=status,
                    error=f"Provider returned terminal status: {state}",
                    endpoint=endpoint,
                    latency_ms=latency_ms,
                    payload=payload,
                    provider_name=provider.name,
                    request_prompt=request_prompt,
                    response_headers=headers,
                )
            response_text = _extract_text(payload)
            if not _is_pending(payload, status) and probe is not None:
                valid, reason = validate_probe_response(response_text, probe)
                if not valid:
                    return AttemptResult(
                        index=index,
                        round_no=round_no,
                        ok=False,
                        status=status,
                        text=response_text,
                        error=f"随机任务语义校验失败：{reason}。",
                        endpoint=endpoint,
                        latency_ms=latency_ms,
                        payload=payload,
                        provider_name=provider.name,
                        request_prompt=request_prompt,
                        response_headers=headers,
                    )
            result = AttemptResult(
                index=index,
                round_no=round_no,
                ok=not _is_pending(payload, status),
                status=status,
                text=response_text,
                endpoint=endpoint,
                latency_ms=latency_ms,
                payload=payload,
                provider_name=provider.name,
                request_prompt=request_prompt,
                pending=_is_pending(payload, status),
                response_headers=headers,
            )
            if result.pending:
                return _poll_pending(result, provider, config, deadline, logger, abort_polling, probe)
            if result.text or isinstance(payload, dict):
                return result
            return AttemptResult(**{**result.__dict__, "ok": False, "error": "HTTP 2xx 响应没有可用内容。"})

        last_error = f"HTTP {status}: {str(payload)[:500]}" if status is not None else error
        if status not in {404, 405}:
            break
    return AttemptResult(
        index=index,
        round_no=round_no,
        ok=False,
        status=last_status,
        error=last_error or "没有收到响应。",
        endpoint=last_endpoint,
        latency_ms=int((time.monotonic() - started) * 1000),
        payload=last_payload,
        provider_name=provider.name,
        request_prompt=request_prompt,
    )


def build_result_dict(
    result: AttemptResult,
    config: dict[str, Any],
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "completed_at": (now or dt.datetime.now().astimezone()).isoformat(timespec="seconds"),
        "round": result.round_no,
        "request_index": result.index,
        "transport": config.get("transport_mode", TRANSPORT_DIRECT),
        "provider": result.provider_name,
        "status": result.status,
        "latency_ms": result.latency_ms,
        "endpoint": _redact_url(result.endpoint),
        "text": result.text,
        "message": result.request_prompt
        or ("" if bool(config.get("custom_body_enabled")) else str(config["message"])),
    }
    if isinstance(result.payload, dict):
        data["response_id"] = result.payload.get("id") or result.payload.get("thread_id") or ""
        data["response_status"] = result.payload.get("status", "")
        data["usage"] = result.payload.get("usage") or {}
    if bool(config.get("save_full_response")):
        data["response"] = result.payload
    return data


def save_result(path: Path, result: AttemptResult, config: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(build_result_dict(result, config), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def _emit_progress(callback: Callable[[ProgressEvent], None] | None, **kwargs: Any) -> None:
    if callback is not None:
        callback(ProgressEvent(**kwargs))


def _all_failures_are_non_retryable(results: list[AttemptResult]) -> bool:
    if not results:
        return False
    statuses = [item.status for item in results]
    return all(status in {400, 401, 403, 404, 405, 422} for status in statuses if status is not None) and all(
        status is not None for status in statuses
    )


def run_batch(
    config: dict[str, Any],
    logger: RunLogger,
    stop_event: threading.Event | None = None,
    dry_run: bool = False,
    on_winner: Callable[[AttemptResult], None] | None = None,
    on_progress: Callable[[ProgressEvent], None] | None = None,
    provider_loader: Callable[[dict[str, Any]], Provider] = load_provider,
    sender: Callable[..., AttemptResult] | None = None,
) -> RunOutcome:
    config = normalize_config(config)
    transport_mode = str(config["transport_mode"])
    uses_default_sender = sender is None
    if sender is None:
        sender = send_one_codex_cli if transport_mode == TRANSPORT_CODEX_CLI else send_one
    if transport_mode == TRANSPORT_CODEX_CLI:
        if bool(config.get("custom_body_enabled")):
            raise SenderError("官方 Codex CLI 模式不支持自定义 JSON，请切换到直接 API。")
        if uses_default_sender and resolve_codex_cli_executable() is None:
            raise SenderError("未找到本机官方 Codex CLI，无法使用官方 CLI 模式。")
        config["_cli_semaphore"] = threading.Semaphore(int(config["cli_concurrency"]))
    stop_event = stop_event or threading.Event()
    started = time.monotonic()
    request_count = int(config["request_count"])
    max_rounds = 1 + int(config["retry_count"])
    total_cap = request_count * max_rounds
    max_wait = float(config["max_wait_seconds"])
    run_deadline = started + max_wait if max_wait > 0 else None
    launched_total = 0
    completed_total = 0
    failed_total = 0
    winner: AttemptResult | None = None
    codex_version = detect_codex_cli_version()
    if bool(config.get("custom_body_enabled")):
        task_mode = "custom-random" if request_uses_random_probe(config) else "custom"
    else:
        task_mode = "random" if request_uses_random_probe(config) else "fixed"
    provider = provider_loader(config)
    if transport_mode == TRANSPORT_CODEX_CLI and provider.api_format != "openai_responses":
        raise SenderError("所选 provider 使用 Chat Completions，官方 Codex CLI 仅支持 Responses API。")
    endpoints = endpoint_candidates(provider, str(config["endpoint_style"]))
    if transport_mode == TRANSPORT_CODEX_CLI:
        logger.log(
            "RUN_START count=%s retries=%s task_cap=%s task_mode=%s transport=official-codex-cli "
            "codex_cli=%s cli_concurrency=%s"
            % (
                request_count,
                config["retry_count"],
                total_cap,
                task_mode,
                codex_version.version or "unavailable",
                config["cli_concurrency"],
            )
        )
    else:
        logger.log(
            "RUN_START count=%s retries=%s post_cap=%s task_mode=%s transport=direct-api "
            "codex_cli=%s codex_header=%s client=ccswitch-batch-sender"
            % (
                request_count,
                config["retry_count"],
                total_cap,
                task_mode,
                codex_version.version or "unavailable",
                "on" if bool(config.get("send_codex_version_header", True)) else "off",
            )
        )

    for round_no in range(1, max_rounds + 1):
        if stop_event.is_set():
            _emit_progress(
                on_progress,
                kind="stopped",
                round_no=round_no,
                max_rounds=max_rounds,
                launched_total=launched_total,
                completed_total=completed_total,
                failed_total=failed_total,
                total_cap=total_cap,
                message="已停止。",
            )
            return RunOutcome(130, winner, launched_total, completed_total, failed_total)
        if run_deadline is not None and time.monotonic() >= run_deadline:
            logger.log("TIMEOUT reached before starting the next batch")
            return RunOutcome(1, winner, launched_total, completed_total, failed_total)

        if dry_run:
            if transport_mode == TRANSPORT_CODEX_CLI:
                logger.log(
                    "DRY_RUN transport=official-codex-cli provider=%s model=%s api_format=%s "
                    "tasks=%s retries=%s cli_concurrency=%s endpoints=%s"
                    % (
                        provider.name,
                        provider.model,
                        provider.api_format,
                        request_count,
                        config["retry_count"],
                        config["cli_concurrency"],
                        [_redact_url(item) for item in endpoints],
                    )
                )
            else:
                logger.log(
                    "DRY_RUN transport=direct-api provider=%s model=%s api_format=%s count=%s retries=%s endpoints=%s"
                    % (
                        provider.name,
                        provider.model,
                        provider.api_format,
                        request_count,
                        config["retry_count"],
                        [_redact_url(item) for item in endpoints],
                    )
                )
            _emit_progress(
                on_progress,
                kind="dry_run",
                round_no=round_no,
                max_rounds=max_rounds,
                launched_total=0,
                completed_total=0,
                failed_total=0,
                total_cap=total_cap,
                message=provider.name,
            )
            return RunOutcome(0, None, 0, 0, 0)

        now = time.monotonic()
        round_deadline = run_deadline if run_deadline is not None else now + float(config["request_timeout_seconds"])
        logger.log(
            "BATCH_START batch=%s/%s sending=%s transport=%s provider=%s model=%s api_format=%s"
            % (
                round_no,
                max_rounds,
                request_count,
                "official-codex-cli" if transport_mode == TRANSPORT_CODEX_CLI else "direct-api",
                provider.name,
                provider.model,
                provider.api_format,
            )
        )
        result_queue: queue.Queue[AttemptResult] = queue.Queue()
        threads: list[threading.Thread] = []
        abort_polling = threading.Event()

        def worker(number: int) -> None:
            try:
                item = sender(
                    number,
                    provider,
                    config,
                    round_deadline,
                    logger,
                    abort_polling,
                    round_no=round_no,
                )
                result_queue.put(_sanitize_attempt_result(item, provider.api_key))
            except Exception as exc:
                result_queue.put(
                    AttemptResult(
                        index=number,
                        round_no=round_no,
                        ok=False,
                        error=_redact_secret_text(f"{type(exc).__name__}: {exc}", provider.api_key),
                        provider_name=provider.name,
                    )
                )

        for number in range(1, request_count + 1):
            thread = threading.Thread(
                target=worker,
                args=(number,),
                name=f"ccswitch-{transport_mode}-{round_no}-{number}",
                daemon=True,
            )
            thread.start()
            threads.append(thread)
        launched_total += request_count
        _emit_progress(
            on_progress,
            kind="round_start",
            round_no=round_no,
            max_rounds=max_rounds,
            launched_total=launched_total,
            completed_total=completed_total,
            failed_total=failed_total,
            total_cap=total_cap,
            round_size=request_count,
            message=provider.name,
        )

        completed_in_round = 0
        round_results: list[AttemptResult] = []
        timed_out = False
        stop_noted = False
        while completed_in_round < request_count:
            if stop_event.is_set() and not stop_noted:
                stop_noted = True
                abort_polling.set()
                logger.log("STOPPING no new batch will be started; waiting for dispatched requests")
                _emit_progress(
                    on_progress,
                    kind="stopping",
                    round_no=round_no,
                    max_rounds=max_rounds,
                    launched_total=launched_total,
                    completed_total=completed_total,
                    failed_total=failed_total,
                    total_cap=total_cap,
                    completed_in_round=completed_in_round,
                    round_size=request_count,
                    message="正在等待已发送请求结束。",
                )
            remaining = round_deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                abort_polling.set()
                break
            try:
                item = result_queue.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue
            round_results.append(item)
            completed_in_round += 1
            completed_total += 1
            if item.ok:
                if winner is None:
                    winner = item
                    abort_polling.set()
                    if transport_mode == TRANSPORT_CODEX_CLI:
                        terminate_active_codex_processes()
                    logger.log(
                        "FIRST_RESULT batch=%s request=%s status=%s latency_ms=%s prompt=%s text=%s"
                        % (
                            item.round_no,
                            item.index,
                            item.status,
                            item.latency_ms,
                            (
                                item.request_prompt.replace("\r", " ").replace("\n", " ")[:500]
                                if request_uses_random_probe(config)
                                else "<fixed-or-custom>"
                            ),
                            item.text[:1000],
                        )
                    )
                    if on_winner is not None:
                        on_winner(item)
            elif item.cancelled:
                logger.log(
                    "REQUEST_CANCELLED batch=%s request=%s reason=%s"
                    % (item.round_no, item.index, item.error[:500])
                )
            else:
                failed_total += 1
                logger.log(
                    "REQUEST_FAIL batch=%s request=%s status=%s error=%s"
                    % (item.round_no, item.index, item.status or "", item.error[:500])
                )
            _emit_progress(
                on_progress,
                kind="first_success" if item.ok and item is winner else "request_complete",
                round_no=round_no,
                max_rounds=max_rounds,
                request_index=item.index,
                launched_total=launched_total,
                completed_total=completed_total,
                failed_total=failed_total,
                total_cap=total_cap,
                completed_in_round=completed_in_round,
                round_size=request_count,
                winner=winner,
                message=item.error if not item.ok else item.text,
            )

        if timed_out:
            for thread in threads:
                thread.join(timeout=1.0)
            unfinished = sum(thread.is_alive() for thread in threads)
            logger.log(
                "TIMEOUT batch=%s completed=%s requested=%s unfinished=%s"
                % (round_no, completed_in_round, request_count, unfinished)
            )
            _emit_progress(
                on_progress,
                kind="timeout",
                round_no=round_no,
                max_rounds=max_rounds,
                launched_total=launched_total,
                completed_total=completed_total,
                failed_total=failed_total,
                total_cap=total_cap,
                completed_in_round=completed_in_round,
                round_size=request_count,
                winner=winner,
                message="已达到总等待时间。",
                unfinished=unfinished,
            )
            return RunOutcome(0 if winner else 1, winner, launched_total, completed_total, failed_total, unfinished)

        for thread in threads:
            thread.join(timeout=0.2)
        logger.log(
            "BATCH_COMPLETE batch=%s completed=%s requested=%s success=%s"
            % (round_no, completed_in_round, request_count, bool(winner))
        )
        if winner is not None:
            _emit_progress(
                on_progress,
                kind="done",
                round_no=round_no,
                max_rounds=max_rounds,
                launched_total=launched_total,
                completed_total=completed_total,
                failed_total=failed_total,
                total_cap=total_cap,
                completed_in_round=completed_in_round,
                round_size=request_count,
                winner=winner,
                message="本批已结束。",
            )
            logger.log(
                "RUN_END code=0 launched=%s completed=%s failed=%s"
                % (launched_total, completed_total, failed_total)
            )
            return RunOutcome(0, winner, launched_total, completed_total, failed_total)
        if stop_event.is_set():
            logger.log("STOPPED by user")
            return RunOutcome(130, None, launched_total, completed_total, failed_total)
        if _all_failures_are_non_retryable(round_results):
            logger.log("HARD_FAIL all responses indicate a non-retryable request/configuration error")
            break
        if round_no >= max_rounds:
            break
        if run_deadline is not None and time.monotonic() >= run_deadline:
            logger.log("TIMEOUT no successful response within the configured total wait")
            break
        interval = float(config["retry_interval_seconds"])
        logger.log(f"RETRY_WAIT seconds={interval:.1f} next_batch={round_no + 1}/{max_rounds}")
        _emit_progress(
            on_progress,
            kind="retry_wait",
            round_no=round_no,
            max_rounds=max_rounds,
            launched_total=launched_total,
            completed_total=completed_total,
            failed_total=failed_total,
            total_cap=total_cap,
            completed_in_round=completed_in_round,
            round_size=request_count,
            message=f"{interval:g} 秒后重试。",
        )
        if stop_event.wait(interval):
            logger.log("STOPPED during retry wait")
            return RunOutcome(130, None, launched_total, completed_total, failed_total)

    logger.log(
        "NO_RESULT launched=%s completed=%s failed=%s batches=%s"
        % (launched_total, completed_total, failed_total, max_rounds)
    )
    _emit_progress(
        on_progress,
        kind="no_result",
        round_no=max_rounds,
        max_rounds=max_rounds,
        launched_total=launched_total,
        completed_total=completed_total,
        failed_total=failed_total,
        total_cap=total_cap,
        message="没有成功响应。",
    )
    return RunOutcome(1, None, launched_total, completed_total, failed_total)


def launch_gui(
    initial_config: dict[str, Any],
    *,
    smoke_ui: bool = False,
    diagnostics: ProviderDiagnostics | None = None,
) -> int:
    from ccswitch_batch_sender_ui import launch_gui as _launch_gui

    return _launch_gui(initial_config, smoke_ui=smoke_ui, diagnostics=diagnostics)


def _show_already_running_message() -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, "应用已经在运行。", APP_TITLE, 0x40)
    else:
        _console_print("应用已经在运行。", error=True)


def _apply_cli_overrides(config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    updated = dict(config)
    if args.transport is not None:
        updated["transport_mode"] = args.transport
    if args.cli_concurrency is not None:
        updated["cli_concurrency"] = args.cli_concurrency
    if args.count is not None:
        updated["request_count"] = args.count
    if args.retry_count is not None:
        updated["retry_count"] = args.retry_count
    if args.message is not None:
        updated["message"] = args.message
        if args.random_probes is None:
            updated["random_probe_enabled"] = False
    if args.random_probes is not None:
        updated["random_probe_enabled"] = args.random_probes
    return normalize_config(updated)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="通过 CC Switch Codex provider 批量发送请求。")
    parser.add_argument("--gui", action="store_true", help="启动桌面界面")
    parser.add_argument("--headless", action="store_true", help="无界面发送")
    parser.add_argument("--dry-run", action="store_true", help="只读取并显示脱敏 provider 信息，不发送请求")
    parser.add_argument("--config", type=Path, help="可选的临时 JSON 配置；默认不需要配置文件")
    parser.add_argument(
        "--transport",
        choices=[TRANSPORT_DIRECT, TRANSPORT_CODEX_CLI],
        help="请求来源：direct 使用直接 API；codex_cli 使用官方 Codex CLI",
    )
    parser.add_argument("--cli-concurrency", type=int, help="官方 Codex CLI 模式的最大并发任务数")
    parser.add_argument("--count", type=int, help="覆盖每批请求次数")
    parser.add_argument("--retry-count", type=int, help="覆盖额外批次重试次数")
    parser.add_argument("--message", help="覆盖提示词")
    prompt_mode = parser.add_mutually_exclusive_group()
    prompt_mode.add_argument(
        "--random-tasks",
        "--random-probes",
        dest="random_probes",
        action="store_true",
        help="每个请求生成独立随机任务",
    )
    prompt_mode.add_argument("--fixed-prompt", dest="random_probes", action="store_false", help="使用固定提示词")
    parser.set_defaults(random_probes=None)
    parser.add_argument("--output", type=Path, help="成功后将脱敏结果导出到指定 JSON")
    parser.add_argument(
        "--no-provider-diagnostics",
        action="store_true",
        help="不写入本机 provider 解析诊断日志",
    )
    parser.add_argument("--ui-smoke", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(list(argv) if argv is not None else None)

    config = load_json_config(args.config) if args.config else load_saved_config()
    config = _apply_cli_overrides(config, args)
    gui_mode = args.gui or (not args.headless and not args.dry_run)
    diagnostics = None if args.no_provider_diagnostics else ProviderDiagnostics()
    if diagnostics is not None:
        diagnostics.record(
            "APP_START",
            mode="gui" if gui_mode else "headless",
            frozen=bool(getattr(sys, "frozen", False)),
        )
    mutex = SingleInstanceMutex()
    if not mutex.acquire():
        if gui_mode:
            _show_already_running_message()
        else:
            _console_print("应用已经在运行。", error=True)
        return 0
    try:
        if gui_mode:
            enable_dpi_awareness()
            return launch_gui(config, smoke_ui=args.ui_smoke, diagnostics=diagnostics)

        logger = RunLogger()
        outcome = run_batch(
            config,
            logger,
            dry_run=args.dry_run,
            provider_loader=lambda current: load_provider(current, diagnostics=diagnostics),
        )
        if outcome.winner is not None:
            if args.output:
                save_result(args.output, outcome.winner, config)
            _console_print(
                json.dumps(
                    {
                        "ok": True,
                        "round": outcome.winner.round_no,
                        "request_index": outcome.winner.index,
                        "message": outcome.winner.request_prompt,
                        "text": outcome.winner.text,
                        "output": str(args.output) if args.output else "",
                    },
                    ensure_ascii=False,
                )
            )
        return outcome.code
    except SenderError as exc:
        _console_print(f"ERROR {exc}", error=True)
        return 2
    except KeyboardInterrupt:
        _console_print("STOPPED", error=True)
        return 130
    finally:
        terminate_active_codex_processes()
        mutex.release()


if __name__ == "__main__":
    raise SystemExit(main())
