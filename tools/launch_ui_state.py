from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ccswitch_batch_sender import (
    AttemptResult,
    ProgressEvent,
    TRANSPORT_DIRECT,
    enable_dpi_awareness,
    load_saved_config,
)
from ccswitch_batch_sender_ui import BatchSenderApp


def descendants(widget: tk.Misc):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--view",
        choices=["default", "advanced", "custom", "log", "keepalive"],
        default="default",
    )
    args = parser.parse_args()
    enable_dpi_awareness()
    root = tk.Tk()
    app = BatchSenderApp(root, load_saved_config(), smoke_ui=True)
    root.update_idletasks()
    notebooks = [widget for widget in descendants(root) if isinstance(widget, ttk.Notebook)]
    if args.view == "advanced" and notebooks:
        notebooks[0].select(1)
    elif args.view == "custom":
        app.transport_mode_var.set(TRANSPORT_DIRECT)
        app._on_transport_changed()
        app.refresh_preview()
        app.custom_body_var.set(True)
        app.toggle_custom_body()
        app.body_text.see("end")
    elif args.view == "log" and len(notebooks) > 1:
        notebooks[1].select(1)
    elif args.view == "keepalive":
        app.running = True
        app.run_started_at = time.monotonic() - 179.4
        # The keepalive event normally triggers a real Windows toast. A visual
        # smoke fixture must stay side-effect free, so mark it as already sent.
        app.success_notification_sent = True
        app.last_run_config = {
            **app.collect_config(),
            "success_keepalive_enabled": True,
            "success_keepalive_interval_seconds": 180,
        }
        keepalive_result = AttemptResult(
            index=1,
            round_no=2,
            ok=True,
            status=200,
            text="定时请求返回正常",
            latency_ms=1240,
            provider_name="UI smoke provider",
            completed_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        app.apply_progress(
            ProgressEvent(
                kind="keepalive_wait",
                keepalive_sequence=2,
                keepalive_successes=2,
                keepalive_failures=0,
                next_run_at=time.time() + 177,
            )
        )
        app.apply_progress(
            ProgressEvent(
                kind="keepalive_result",
                keepalive_sequence=2,
                keepalive_successes=2,
                keepalive_failures=0,
                winner=keepalive_result,
                result=keepalive_result,
            )
        )
        app.apply_progress(
            ProgressEvent(
                kind="keepalive_wait",
                keepalive_sequence=2,
                keepalive_successes=2,
                keepalive_failures=0,
                next_run_at=time.time() + 177,
            )
        )
        app._set_editing_enabled(False)
        app.stop_button.state(["!disabled"])
        app._start_elapsed_clock()
        if len(notebooks) > 1:
            notebooks[1].select(1)
        app.append_log(
            f"[{keepalive_result.completed_at}] KEEPALIVE_OK sequence=2 status=200 "
            f"latency_ms={keepalive_result.latency_ms} success_at={keepalive_result.completed_at}"
        )
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
