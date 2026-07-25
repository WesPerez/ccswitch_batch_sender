from __future__ import annotations

import argparse
import datetime as dt
import json
import msvcrt
import os
import queue
import re
import sqlite3
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


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "config.json"
DEFAULT_LOG_DIR = ROOT / "logs"
DEFAULT_RESULT_PATH = ROOT / "latest-result.json"
DEFAULT_LOCK_PATH = ROOT / "run.lock"
DEFAULT_DB_PATH = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".cc-switch" / "cc-switch.db"


DEFAULT_CONFIG: dict[str, Any] = {
    "provider_id": "current",
    "model": "",
    "base_url": "",
    "message": "1",
    "request_count": 20,
    "max_output_tokens": 1,
    "request_timeout_seconds": 7200,
    "max_wait_seconds": 7200,
    "retry_interval_seconds": 3,
    "poll_interval_seconds": 2,
    "retry_batches": False,
    "user_agent": "CCSWITCH Batch Sender/1.0",
    "originator": "CCSWITCH Batch Sender",
    "db_path": "",
    "endpoint_style": "auto",
    "unique_prompt_cache_key": True,
    "save_full_response": False,
}


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


@dataclass
class AttemptResult:
    index: int
    ok: bool
    status: int | None = None
    text: str = ""
    error: str = ""
    endpoint: str = ""
    latency_ms: int = 0
    payload: Any = None
    provider_name: str = ""
    pending: bool = False
    response_headers: dict[str, str] = field(default_factory=dict)


class RunLogger:
    def __init__(self, log_dir: Path, callback: Callable[[str], None] | None = None) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / f"run-{dt.datetime.now():%Y%m%d}.log"
        self._callback = callback
        self._lock = threading.Lock()

    def log(self, message: str) -> None:
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        line = f"[{stamp}] {message}"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        if self._callback is not None:
            self._callback(line)
        else:
            print(line, flush=True)


class SingleRunLock:
    """Windows-compatible single-instance lock, adapted from the reference task."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            self.handle = handle
            return True
        except OSError:
            handle.close()
            return False

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            self.handle.close()
            self.handle = None


def load_json_config(path: Path) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SenderError(f"配置文件不是有效 JSON：{path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise SenderError(f"配置文件必须是 JSON 对象：{path}")
        config.update(loaded)

    config["provider_id"] = str(config.get("provider_id") or "current").strip()
    config["model"] = str(config.get("model") or "").strip()
    config["base_url"] = str(config.get("base_url") or "").strip().rstrip("/")
    config["message"] = str(config.get("message") or "")
    config["request_count"] = int(config.get("request_count") or 20)
    config["max_output_tokens"] = int(config.get("max_output_tokens") or 1)
    config["request_timeout_seconds"] = float(config.get("request_timeout_seconds") or 7200)
    config["max_wait_seconds"] = float(config.get("max_wait_seconds") or 7200)
    config["retry_interval_seconds"] = float(config.get("retry_interval_seconds") or 3)
    config["poll_interval_seconds"] = float(config.get("poll_interval_seconds") or 2)
    config["retry_batches"] = bool(config.get("retry_batches", False))
    config["user_agent"] = str(config.get("user_agent") or DEFAULT_CONFIG["user_agent"]).strip()
    config["originator"] = str(config.get("originator") or DEFAULT_CONFIG["originator"]).strip()
    config["endpoint_style"] = str(config.get("endpoint_style") or "auto").strip().lower()
    config["unique_prompt_cache_key"] = bool(config.get("unique_prompt_cache_key", True))
    config["db_path"] = str(config.get("db_path") or "").strip()
    config["save_full_response"] = bool(config.get("save_full_response", False))

    if not 1 <= config["request_count"] <= 100:
        raise SenderError("request_count 必须在 1 到 100 之间。")
    if not 1 <= config["max_output_tokens"] <= 64:
        raise SenderError("max_output_tokens 必须在 1 到 64 之间。")
    if config["request_timeout_seconds"] <= 0:
        raise SenderError("request_timeout_seconds 必须大于 0。")
    if config["max_wait_seconds"] < 0:
        raise SenderError("max_wait_seconds 不能小于 0；设为 0 表示仅由 --until-success 无限等待。")
    if config["retry_interval_seconds"] < 0 or config["poll_interval_seconds"] <= 0:
        raise SenderError("retry_interval_seconds 不能小于 0，poll_interval_seconds 必须大于 0。")
    if not config["message"]:
        raise SenderError("message 不能为空。")
    if config["endpoint_style"] not in {"auto", "ccswitch", "openai"}:
        raise SenderError("endpoint_style 只能是 auto、ccswitch 或 openai。")
    return config


def parse_assignment(text: str, key: str) -> str:
    pattern = re.compile(
        rf"^\s*{re.escape(key)}\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^#\r\n]+?))\s*(?:#.*)?$",
        re.MULTILINE,
    )
    match = pattern.search(text or "")
    if not match:
        return ""
    return next((part.strip() for part in match.groups() if part is not None), "")


def _config_value(config_obj: Any, key: str) -> str:
    if isinstance(config_obj, dict):
        value = config_obj.get(key)
        return str(value).strip() if value is not None else ""
    return parse_assignment(str(config_obj or ""), key)


def resolve_db_path(config: dict[str, Any]) -> Path:
    value = str(config.get("db_path") or "").strip()
    if not value:
        return DEFAULT_DB_PATH
    return Path(os.path.expandvars(value)).expanduser()


def load_provider(config: dict[str, Any]) -> Provider:
    db_path = resolve_db_path(config)
    if not db_path.exists():
        raise SenderError(
            f"CCSWITCH 数据库不存在：{db_path}\n"
            "请先安装并配置 CCSWITCH，脚本不会自行创建或复制 API key。"
        )

    provider_id = str(config["provider_id"])
    try:
        connection = sqlite3.connect(str(db_path), timeout=3)
    except sqlite3.Error as exc:
        raise SenderError(f"无法只读打开 CCSWITCH 数据库：{exc}") from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        if provider_id.lower() == "current":
            row = connection.execute(
                """
                SELECT id, name, settings_config, meta
                FROM providers
                WHERE app_type = 'codex' AND is_current = 1
                ORDER BY sort_index, id
                LIMIT 1
                """
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT id, name, settings_config, meta
                FROM providers
                WHERE app_type = 'codex' AND id = ?
                LIMIT 1
                """,
                (provider_id,),
            ).fetchone()
        if row is None:
            raise SenderError(f"CCSWITCH Codex provider 不存在：{provider_id}")

        try:
            settings = json.loads(row["settings_config"] or "{}")
            meta = json.loads(row["meta"] or "{}")
        except json.JSONDecodeError as exc:
            raise SenderError(f"provider 配置 JSON 无法解析：{row['name']}") from exc

        auth = settings.get("auth") if isinstance(settings, dict) else {}
        auth = auth if isinstance(auth, dict) else {}
        api_key = str(auth.get("OPENAI_API_KEY") or "").strip()
        if not api_key:
            raise SenderError(
                f"当前 provider 没有 OPENAI_API_KEY：{row['name']}。"
                "官方 OAuth provider 不能直接按本脚本的 API key 方式发送。"
            )

        config_blob = settings.get("config", "") if isinstance(settings, dict) else ""
        configured_base = str(config.get("base_url") or "").strip()
        configured_model = str(config.get("model") or "").strip()
        base_url = configured_base or _config_value(config_blob, "base_url")
        model = configured_model or _config_value(config_blob, "model")
        api_format = str(meta.get("apiFormat") or "").strip().lower()
        if api_format not in {"openai_chat", "openai_responses"}:
            wire_api = _config_value(config_blob, "wire_api").lower()
            api_format = "openai_responses" if wire_api == "responses" else "openai_chat" if wire_api in {"chat", "chat_completions"} else ""

        if not base_url:
            raise SenderError(f"provider 没有 base_url：{row['name']}")
        if not model:
            raise SenderError(f"provider 没有 model：{row['name']}")
        if not api_format:
            raise SenderError(f"无法判断 provider 的 API 格式：{row['name']}")

        return Provider(
            provider_id=str(row["id"]),
            name=str(row["name"]),
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            model=model,
            api_format=api_format,
        )
    finally:
        connection.close()


def endpoint_candidates(provider: Provider, style: str) -> list[str]:
    base = provider.base_url.rstrip("/")
    if provider.api_format == "openai_responses":
        if base.endswith("/responses"):
            return [base]
        openai_endpoint = base + "/responses" if base.endswith("/v1") else base + "/v1/responses"
        if style == "openai":
            candidates = [openai_endpoint]
        elif style == "ccswitch":
            candidates = [base + "/responses"]
        else:
            candidates = [base + "/responses", openai_endpoint]
    else:
        if base.endswith("/chat/completions"):
            return [base]
        openai_endpoint = base + "/chat/completions" if base.endswith("/v1") else base + "/v1/chat/completions"
        if style == "openai":
            candidates = [openai_endpoint]
        elif style == "ccswitch":
            candidates = [base + "/v1/chat/completions"]
        else:
            candidates = [base + "/v1/chat/completions", openai_endpoint]
    return list(dict.fromkeys(candidates))


def build_body(provider: Provider, config: dict[str, Any]) -> dict[str, Any]:
    message = str(config["message"])
    if provider.api_format == "openai_chat":
        return {
            "model": provider.model,
            "messages": [{"role": "user", "content": message}],
            "max_tokens": int(config["max_output_tokens"]),
            "stream": False,
        }
    body = {
        "model": provider.model,
        "instructions": f"Return {message}.",
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
        body["prompt_cache_key"] = str(uuid.uuid4())
    return body


def _redact_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


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
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer " + provider.api_key,
        # This identifies the real sender instead of impersonating Codex Desktop.
        "User-Agent": str(config["user_agent"]),
    }
    if provider.api_format == "openai_responses":
        headers["Originator"] = str(config["originator"])
    data = None if body is None else json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, _json_or_text(raw), dict(response.headers.items()), ""
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")[:2000]
        return exc.code, _json_or_text(raw), dict(exc.headers.items()) if exc.headers else {}, raw
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, None, {}, str(exc)


def _poll_pending(
    result: AttemptResult,
    provider: Provider,
    config: dict[str, Any],
    deadline: float,
    logger: RunLogger,
    abort_event: threading.Event | None = None,
) -> AttemptResult:
    payload = result.payload if isinstance(result.payload, dict) else {}
    location = result.response_headers.get("Location") or result.response_headers.get("location")
    request_id = payload.get("id") if isinstance(payload, dict) else None
    if location:
        poll_url = urllib.parse.urljoin(result.endpoint + "/", location)
    elif request_id and provider.api_format == "openai_responses":
        # Try the same endpoint family used for the POST.
        poll_url = result.endpoint.rstrip("/") + "/" + urllib.parse.quote(str(request_id), safe="")
    else:
        return AttemptResult(**{**result.__dict__, "ok": False, "error": "Provider returned an async response without a poll URL."})

    while time.monotonic() < deadline and not (abort_event and abort_event.is_set()):
        time.sleep(min(float(config["poll_interval_seconds"]), max(0.0, deadline - time.monotonic())))
        status, body, headers, error = _http_request("GET", poll_url, None, provider, config, float(config["request_timeout_seconds"]))
        if status is None:
            continue
        if 200 <= status < 300 and not _is_pending(body, status):
            text = _extract_text(body)
            if text or isinstance(body, dict):
                return AttemptResult(
                    index=result.index,
                    ok=True,
                    status=status,
                    text=text,
                    endpoint=poll_url,
                    latency_ms=result.latency_ms,
                    payload=body,
                    provider_name=provider.name,
                    response_headers=headers,
                )
        if status >= 400:
            return AttemptResult(
                index=result.index,
                ok=False,
                status=status,
                error=f"poll HTTP {status}: {str(body)[:500]}",
                endpoint=poll_url,
                provider_name=provider.name,
            )
    reason = "Polling stopped after another request succeeded." if abort_event and abort_event.is_set() else "Polling deadline exceeded."
    return AttemptResult(**{**result.__dict__, "ok": False, "error": reason})


def send_one(
    index: int,
    provider: Provider,
    config: dict[str, Any],
    deadline: float,
    logger: RunLogger,
    abort_polling: threading.Event | None = None,
) -> AttemptResult:
    started = time.monotonic()
    body = build_body(provider, config)
    last_error = ""
    for endpoint in endpoint_candidates(provider, str(config["endpoint_style"])):
        status, payload, headers, error = _http_request(
            "POST", endpoint, body, provider, config, float(config["request_timeout_seconds"])
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        if status is not None and 200 <= status < 300:
            if isinstance(payload, dict) and payload.get("error"):
                return AttemptResult(
                    index=index,
                    ok=False,
                    status=status,
                    error=f"Provider error: {str(payload.get('error'))[:500]}",
                    endpoint=endpoint,
                    latency_ms=latency_ms,
                    payload=payload,
                    provider_name=provider.name,
                    response_headers=headers,
                )
            state = str(payload.get("status") or "").lower() if isinstance(payload, dict) else ""
            if state in {"failed", "cancelled", "canceled", "expired"}:
                return AttemptResult(
                    index=index,
                    ok=False,
                    status=status,
                    error=f"Provider returned terminal status: {state}",
                    endpoint=endpoint,
                    latency_ms=latency_ms,
                    payload=payload,
                    provider_name=provider.name,
                    response_headers=headers,
                )
            result = AttemptResult(
                index=index,
                ok=not _is_pending(payload, status),
                status=status,
                text=_extract_text(payload),
                endpoint=endpoint,
                latency_ms=latency_ms,
                payload=payload,
                provider_name=provider.name,
                pending=_is_pending(payload, status),
                response_headers=headers,
            )
            if result.pending:
                return _poll_pending(result, provider, config, deadline, logger, abort_polling)
            if result.text or isinstance(payload, dict):
                return result
            return AttemptResult(**{**result.__dict__, "ok": False, "error": "HTTP 2xx response contained no usable body."})

        last_error = f"HTTP {status}: {str(payload)[:500]}" if status is not None else error
        # Only try the alternate endpoint when the path itself was not found/method was rejected.
        if status not in {404, 405}:
            break
    return AttemptResult(
        index=index,
        ok=False,
        status=status,
        error=last_error or "No response",
        endpoint=endpoint if 'endpoint' in locals() else "",
        latency_ms=int((time.monotonic() - started) * 1000),
        provider_name=provider.name,
    )


def save_result(path: Path, result: AttemptResult, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "completed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "request_index": result.index,
        "provider": result.provider_name,
        "status": result.status,
        "latency_ms": result.latency_ms,
        "endpoint": _redact_url(result.endpoint),
        "text": result.text,
        "message": str(config["message"]),
    }
    if isinstance(result.payload, dict):
        data["response_id"] = result.payload.get("id", "")
        data["response_status"] = result.payload.get("status", "")
        data["usage"] = result.payload.get("usage") or {}
    if bool(config.get("save_full_response")):
        data["response"] = result.payload
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def run_batch(
    config: dict[str, Any],
    logger: RunLogger,
    result_path: Path,
    stop_event: threading.Event | None = None,
    until_success: bool = False,
    dry_run: bool = False,
    on_winner: Callable[[AttemptResult], None] | None = None,
) -> tuple[int, AttemptResult | None]:
    stop_event = stop_event or threading.Event()
    started = time.monotonic()
    round_no = 0
    while not stop_event.is_set():
        round_no += 1
        provider = load_provider(config)
        endpoints = endpoint_candidates(provider, str(config["endpoint_style"]))
        if dry_run:
            logger.log(
                "DRY_RUN provider=%s model=%s api_format=%s count=%s endpoints=%s"
                % (provider.name, provider.model, provider.api_format, config["request_count"], [_redact_url(x) for x in endpoints])
            )
            return 0, None

        logger.log(
            "ROUND %s sending=%s provider=%s model=%s api_format=%s"
            % (round_no, config["request_count"], provider.name, provider.model, provider.api_format)
        )
        result_queue: queue.Queue[AttemptResult] = queue.Queue()
        threads: list[threading.Thread] = []
        abort_polling = threading.Event()
        if not until_success and float(config["max_wait_seconds"]) > 0:
            round_deadline = started + float(config["max_wait_seconds"])
        else:
            round_deadline = time.monotonic() + float(config["request_timeout_seconds"]) + 5

        def worker(number: int) -> None:
            try:
                # Every worker always performs its initial POST. abort_polling only stops
                # follow-up polling after another worker has already won.
                result_queue.put(send_one(number, provider, config, round_deadline, logger, abort_polling))
            except Exception as exc:  # defensive: one worker must not kill the whole batch
                result_queue.put(AttemptResult(index=number, ok=False, error=f"{type(exc).__name__}: {exc}", provider_name=provider.name))

        for number in range(1, int(config["request_count"]) + 1):
            thread = threading.Thread(target=worker, args=(number,), name=f"ccswitch-{number}", daemon=True)
            thread.start()
            threads.append(thread)

        failures = 0
        winner: AttemptResult | None = None
        while failures < len(threads) and time.monotonic() < round_deadline and not stop_event.is_set():
            try:
                item = result_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if item.ok:
                winner = item
                break
            failures += 1
            logger.log("request=%s failed status=%s error=%s" % (item.index, item.status or "", item.error[:500]))

        if winner is not None:
            logger.log(
                "FIRST_RESULT request=%s status=%s latency_ms=%s text=%s"
                % (winner.index, winner.status, winner.latency_ms, winner.text[:1000])
            )
            save_result(result_path, winner, config)
            if on_winner is not None:
                on_winner(winner)
            # Keep the first result visible immediately, while every other request
            # stays connected so Sub2API can finish pool retries/account failover.
            drain_deadline = time.monotonic() + float(config["request_timeout_seconds"]) + 2
            for thread in threads:
                thread.join(timeout=max(0.0, drain_deadline - time.monotonic()))
            completed = sum(not thread.is_alive() for thread in threads)
            logger.log("DISPATCH_COMPLETE completed=%s requested=%s" % (completed, len(threads)))
            return 0, winner

        if stop_event.is_set():
            abort_polling.set()
            break
        elapsed = time.monotonic() - started
        if not until_success and not bool(config.get("retry_batches")):
            logger.log("NO_RESULT the one 20-request batch completed without a successful response.")
            return 1, None
        if not until_success and float(config["max_wait_seconds"]) > 0 and elapsed >= float(config["max_wait_seconds"]):
            logger.log("TIMEOUT no successful response within %.1fs" % float(config["max_wait_seconds"]))
            return 1, None
        logger.log(
            "No successful response; waiting %.1fs before another %s-request round."
            % (float(config["retry_interval_seconds"]), config["request_count"])
        )
        stop_event.wait(float(config["retry_interval_seconds"]))
    return 130, None


def launch_gui(config_path: Path) -> int:
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        print("当前 Python 没有 tkinter，请改用命令行模式。", file=sys.stderr)
        return 2

    root = tk.Tk()
    root.title("CCSWITCH 并发 20 请求")
    root.geometry("820x560")
    root.minsize(700, 460)
    stop_event = threading.Event()
    running = {"thread": None}

    frame = ttk.Frame(root, padding=12)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="CCSWITCH 并发 20 请求", font=("Segoe UI", 13, "bold")).pack(anchor="w")
    ttk.Label(frame, text="窗口打开后自动发送；首个结果立即显示，其余连接继续等待服务端完成。").pack(anchor="w", pady=(2, 0))
    status = tk.StringVar(value="准备启动…")
    result_summary = tk.StringVar(value="等待首个返回结果")
    ttk.Label(frame, textvariable=status).pack(anchor="w", pady=(8, 2))
    ttk.Label(frame, textvariable=result_summary, font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 8))
    output = tk.Text(frame, height=23, wrap="word")
    output.pack(fill="both", expand=True)
    buttons = ttk.Frame(frame)
    buttons.pack(fill="x", pady=(8, 0))

    def append(line: str) -> None:
        root.after(0, lambda: (output.insert("end", line + "\n"), output.see("end")))

    def start() -> None:
        if running["thread"] is not None and running["thread"].is_alive():
            return
        stop_event.clear()
        status.set("正在并发发送 20 个请求…")
        result_summary.set("等待首个返回结果")
        output.delete("1.0", "end")
        start_button.state(["disabled"])

        def show_winner(winner: AttemptResult) -> None:
            payload = winner.payload if isinstance(winner.payload, dict) else {}
            response_id = str(payload.get("id") or "")
            response_status = str(payload.get("status") or winner.status or "")
            visible = winner.text or f"已收到响应：status={response_status} id={response_id}"

            def update() -> None:
                status.set(f"第 {winner.index} 个请求最先返回；结果已保存")
                result_summary.set(visible[:500])

            root.after(0, update)

        def job() -> None:
            lock = SingleRunLock(DEFAULT_LOCK_PATH)
            try:
                if not lock.acquire():
                    append("已有一批请求正在运行，本次未重复发送。")
                    root.after(0, lambda: status.set("已有任务正在运行"))
                    return
                cfg = load_json_config(config_path)
                logger = RunLogger(ROOT / "logs", append)
                code, winner = run_batch(
                    cfg,
                    logger,
                    ROOT / "latest-result.json",
                    stop_event=stop_event,
                    on_winner=show_winner,
                )
                if winner is not None:
                    root.after(0, lambda: status.set("本批 20 个请求均已结束；首个结果已保存"))
                else:
                    root.after(0, lambda: result_summary.set("本批没有成功响应，请查看下方错误。"))
                    root.after(0, lambda: status.set(f"已结束，状态码 {code}"))
            except Exception as exc:
                append(f"ERROR {type(exc).__name__}: {exc}")
                root.after(0, lambda: status.set("失败"))
                root.after(0, lambda: result_summary.set(str(exc)[:500]))
            finally:
                lock.release()
                root.after(0, lambda: start_button.state(["!disabled"]))

        running["thread"] = threading.Thread(target=job, daemon=True)
        running["thread"].start()

    def stop() -> None:
        stop_event.set()
        status.set("正在停止…")

    def open_result() -> None:
        if DEFAULT_RESULT_PATH.exists():
            os.startfile(DEFAULT_RESULT_PATH)
        else:
            messagebox.showinfo("暂无结果", "还没有生成 latest-result.json。")

    def open_logs() -> None:
        DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(DEFAULT_LOG_DIR)

    start_button = ttk.Button(buttons, text="重新发送 20 个", command=start)
    start_button.pack(side="left")
    ttk.Button(buttons, text="停止", command=stop).pack(side="left", padx=8)
    ttk.Button(buttons, text="打开结果", command=open_result).pack(side="left")
    ttk.Button(buttons, text="打开日志", command=open_logs).pack(side="left", padx=8)
    ttk.Button(buttons, text="关闭", command=root.destroy).pack(side="right")
    root.after(250, start)
    root.mainloop()
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send 20 minimal requests through the current CCSWITCH Codex provider.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--gui", action="store_true", help="启动简易可视化界面")
    parser.add_argument("--dry-run", action="store_true", help="只读取 provider 并打印脱敏配置，不发送请求")
    parser.add_argument("--until-success", action="store_true", help="无成功响应时持续轮询/重试，直到成功或手动停止")
    parser.add_argument("--count", type=int, help="覆盖 config.json 的 request_count")
    parser.add_argument("--message", help="覆盖 config.json 的 message")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.gui:
        return launch_gui(args.config)

    config = load_json_config(args.config)
    if args.count is not None:
        config["request_count"] = args.count
    if args.message is not None:
        config["message"] = args.message
    if not 1 <= int(config["request_count"]) <= 100:
        raise SenderError("--count/request_count 必须在 1 到 100 之间。")

    logger = RunLogger(ROOT / "logs")
    lock = SingleRunLock(DEFAULT_LOCK_PATH)
    if not lock.acquire():
        logger.log("Previous run is still active; exiting without sending another batch.")
        return 0
    try:
        code, winner = run_batch(
            config,
            logger,
            DEFAULT_RESULT_PATH,
            until_success=args.until_success,
            dry_run=args.dry_run,
        )
        if winner is not None:
            print(json.dumps({"ok": True, "request_index": winner.index, "text": winner.text, "result_path": str(DEFAULT_RESULT_PATH)}, ensure_ascii=False))
        return code
    except SenderError as exc:
        logger.log(f"ERROR {exc}")
        return 2
    except KeyboardInterrupt:
        logger.log("STOPPED by user")
        return 130
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
