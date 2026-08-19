from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

from PIL import Image, ImageGrab


class BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BitmapInfo(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BitmapInfoHeader),
        ("bmiColors", wintypes.DWORD * 3),
    ]


def capture_window_pixels(hwnd: int, width: int, height: int) -> Image.Image | None:
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.GetWindowDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(BitmapInfo),
        wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int

    window_dc = user32.GetWindowDC(hwnd)
    if not window_dc:
        return None
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    previous = gdi32.SelectObject(memory_dc, bitmap)
    try:
        rendered = user32.PrintWindow(hwnd, memory_dc, 0x00000002)
        if not rendered:
            rendered = user32.PrintWindow(hwnd, memory_dc, 0)
        if not rendered:
            return None
        info = BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(BitmapInfoHeader)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        buffer = (ctypes.c_ubyte * (width * height * 4))()
        copied = gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            height,
            ctypes.byref(buffer),
            ctypes.byref(info),
            0,
        )
        if copied != height:
            return None
        return Image.frombuffer(
            "RGBA",
            (width, height),
            buffer,
            "raw",
            "BGRA",
            0,
            1,
        ).convert("RGB")
    finally:
        if previous:
            gdi32.SelectObject(memory_dc, previous)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)


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


def find_window_for_pid(pid: int, *, allow_title_fallback: bool = False) -> int:
    user32 = ctypes.windll.user32
    matches: list[tuple[int, int]] = []
    title_matches: list[tuple[int, int]] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd: int, _lparam: int) -> bool:
        window_pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if "CC Switch 批量请求" in buffer.value:
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
                if area <= 0:
                    return True
                title_matches.append((area, int(hwnd)))
                if window_pid.value == pid:
                    matches.append((area, int(hwnd)))
        return True

    user32.EnumWindows(callback_type(callback), 0)
    if matches:
        return max(matches)[1]
    return max(title_matches)[1] if allow_title_fallback and title_matches else 0


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
    previous_foreground = ctypes.windll.user32.GetForegroundWindow()
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
            hwnd = find_window_for_pid(process.pid, allow_title_fallback=True)
        if not hwnd:
            raise RuntimeError("application window was not found")
        ctypes.windll.user32.ShowWindow(hwnd, 9)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
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
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        image = capture_window_pixels(hwnd, width, height)
        if image is None:
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
        if previous_foreground and ctypes.windll.user32.IsWindow(previous_foreground):
            ctypes.windll.user32.SetForegroundWindow(previous_foreground)


if __name__ == "__main__":
    raise SystemExit(main())
