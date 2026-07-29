from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

from PIL import ImageGrab


def terminate_process_tree(process: subprocess.Popen[bytes], timeout: float = 5.0) -> None:
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
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        process.wait(timeout=timeout)


def find_window_for_pid(pid: int) -> int:
    user32 = ctypes.windll.user32
    matches: list[int] = []
    title_matches: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd: int, _lparam: int) -> bool:
        window_pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if "CC Switch 批量请求" in buffer.value:
                title_matches.append(int(hwnd))
                if window_pid.value == pid:
                    matches.append(int(hwnd))
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return matches[0] if matches else title_matches[0] if title_matches else 0


def main() -> int:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--width", type=int, default=0)
    parser.add_argument("--height", type=int, default=0)
    parser.add_argument("--click-x", type=int, default=-1)
    parser.add_argument("--click-y", type=int, default=-1)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.command:
        parser.error("a command is required after --")
    command = args.command[1:] if args.command[0] == "--" else args.command
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1], creationflags=creation_flags)
    try:
        hwnd = 0
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and process.poll() is None:
            hwnd = find_window_for_pid(process.pid)
            if hwnd:
                break
            time.sleep(0.15)
        if not hwnd:
            raise RuntimeError("application window was not found")
        size_flags = 0x0001 if not (args.width and args.height) else 0
        ctypes.windll.user32.SetWindowPos(
            hwnd,
            ctypes.c_void_p(-1),
            40,
            40,
            args.width,
            args.height,
            size_flags | 0x0010 | 0x0040,
        )
        time.sleep(1.0)
        if args.click_x >= 0 and args.click_y >= 0:
            lparam = (args.click_y << 16) | (args.click_x & 0xFFFF)
            ctypes.windll.user32.PostMessageW(hwnd, 0x0201, 0x0001, lparam)
            ctypes.windll.user32.PostMessageW(hwnd, 0x0202, 0, lparam)
            time.sleep(0.5)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        image = ImageGrab.grab(
            bbox=(rect.left, rect.top, rect.right, rect.bottom),
            include_layered_windows=True,
            all_screens=True,
        )
        image.save(args.output)
        print(f"captured {args.output.resolve()} {image.size} hwnd={hwnd:#x}")
        return 0
    finally:
        terminate_process_tree(process)


if __name__ == "__main__":
    raise SystemExit(main())
