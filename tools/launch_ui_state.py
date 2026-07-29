from __future__ import annotations

import argparse
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ccswitch_batch_sender import enable_dpi_awareness, load_saved_config
from ccswitch_batch_sender_ui import BatchSenderApp


def descendants(widget: tk.Misc):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--view", choices=["default", "advanced", "custom", "log"], default="default")
    args = parser.parse_args()
    enable_dpi_awareness()
    root = tk.Tk()
    app = BatchSenderApp(root, load_saved_config(), smoke_ui=True)
    root.update_idletasks()
    notebooks = [widget for widget in descendants(root) if isinstance(widget, ttk.Notebook)]
    if args.view == "advanced" and notebooks:
        notebooks[0].select(1)
    elif args.view == "custom":
        app.refresh_preview()
        app.custom_body_var.set(True)
        app.toggle_custom_body()
        app.body_text.see("end")
    elif args.view == "log" and len(notebooks) > 1:
        notebooks[1].select(1)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
