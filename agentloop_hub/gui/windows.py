"""Windows GUI driver for Electron agents.

The driver intentionally uses only relative window ratios + clipboard/keyboard
events and the agent's own transcript for read-back. It never relies on fixed
screen coordinates and never writes to the agent's private store.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from ..util import atomic_write_text, ensure_dir, now_iso, read_json
from .base import GuiAutomationError, GuiProfile

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_WHEEL = 0x0800
VK_CONTROL = 0x11
VK_V = 0x56
VK_A = 0x41
VK_RETURN = 0x0D
KEYEVENTF_KEYUP = 0x0002
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
HWND_TOP = 0

user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, wintypes.UINT]

kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]

SUBMIT_KEYS = {"ENTER": VK_RETURN}


def _window_text(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def find_window(title_part: str) -> tuple[int, tuple[int, int, int, int]] | None:
    matches: list[tuple[int, tuple[int, int, int, int]]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_text(hwnd)
        if title_part.lower() in title.lower():
            rect = _window_rect(hwnd)
            # Ignore zero-size helper windows.
            if rect[2] - rect[0] > 200 and rect[3] - rect[1] > 200:
                matches.append((int(hwnd), rect))
        return True

    user32.EnumWindows(callback, 0)
    if not matches:
        return None
    matches.sort(key=lambda item: (item[1][2] - item[1][0]) * (item[1][3] - item[1][1]),
                 reverse=True)
    return matches[0]


def _set_clipboard(text: str) -> None:
    data = text.encode("utf-16-le") + b"\x00\x00"
    if not user32.OpenClipboard(0):
        raise GuiAutomationError("GUI_SEND_FAILED", "打开剪贴板失败")
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise GuiAutomationError("GUI_SEND_FAILED", "剪贴板内存分配失败")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise GuiAutomationError("GUI_SEND_FAILED", "剪贴板内存锁定失败")
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise GuiAutomationError("GUI_SEND_FAILED", "写入剪贴板失败")
    finally:
        user32.CloseClipboard()


def _key_event(vk: int, flags: int = 0) -> None:
    user32.keybd_event(vk, 0, flags, 0)
    time.sleep(0.04)


def _press(vk: int) -> None:
    _key_event(vk, 0)
    _key_event(vk, KEYEVENTF_KEYUP)


def _launch_detached(executable: str) -> int:
    """Launch outside the current process job so the GUI survives tool hosts.

    WMI creates the process under WmiPrvSE, which also makes this reliable when
    AgentMesh itself runs inside a short-lived CLI invocation.
    """
    exe = str(Path(executable))
    if os.name != "nt":
        raise GuiAutomationError("GUI_UNSUPPORTED_PLATFORM", "GUI 自动化仅支持 Windows")
    command_line = f'"{exe}"'
    script = (
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        f"-Arguments @{{CommandLine='{command_line.replace(chr(39), chr(39) + chr(39))}'}}; "
        "if ($r.ReturnValue -eq 0) { $r.ProcessId }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
        pid = (proc.stdout or "").strip().splitlines()
        if pid and pid[-1].isdigit():
            return int(pid[-1])
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        os.startfile(exe)  # type: ignore[attr-defined]
    except OSError as exc:
        raise GuiAutomationError("GUI_LAUNCH_FAILED", f"无法启动 {exe}: {exc}") from exc
    return 0


class WindowsGuiDriver:
    def __init__(self, profile: GuiProfile, config: dict[str, Any] | None = None, *,
                 log_path: str | Path | None = None):
        self.profile = profile
        self.config = dict(config or {})
        self.log_path = Path(log_path) if log_path else None
        self.hwnd = 0
        self.rect = (0, 0, 0, 0)
        self.launched = False
        self._log_events: list[dict[str, Any]] = []

    # -- diagnostics -----------------------------------------------------
    def log(self, event: str, **data: Any) -> None:
        item = {"ts": now_iso(), "event": event, **data}
        self._log_events.append(item)
        if self.log_path:
            ensure_dir(self.log_path.parent)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    def window_info(self) -> dict[str, Any]:
        return {"title": self.profile.window_title, "hwnd": self.hwnd,
                "rect": list(self.rect), "launched": self.launched}

    # -- lifecycle -------------------------------------------------------
    def attach(self) -> dict[str, Any]:
        if os.name != "nt":
            raise GuiAutomationError("GUI_UNSUPPORTED_PLATFORM",
                                     f"GUI 自动化仅在 Windows 可用，当前 {sys.platform}")
        found = find_window(self.profile.window_title)
        if found:
            self.hwnd, self.rect = found
            self._restore()
            self.log("attach", **self.window_info())
            return self.window_info()
        exe = self._executable()
        if not exe:
            raise GuiAutomationError(
                "GUI_WINDOW_NOT_FOUND",
                f"未找到窗口“{self.profile.window_title}”，且未定位到可执行文件",
                details={"candidates": list(self.profile.executable_candidates)},
            )
        self.log("launch", executable=exe)
        _launch_detached(exe)
        self.launched = True
        deadline = time.monotonic() + float(
            self.config.get("launch_timeout_seconds") or self.profile.launch_timeout_seconds
        )
        while time.monotonic() < deadline:
            time.sleep(1.0)
            found = find_window(self.profile.window_title)
            if found:
                self.hwnd, self.rect = found
                self._restore()
                self.log("attached_after_launch", **self.window_info())
                return self.window_info()
        raise GuiAutomationError(
            "GUI_WINDOW_NOT_FOUND",
            f"已启动 {exe}，但在 {int(deadline - time.monotonic())}s 内未出现窗口“"
            f"{self.profile.window_title}”",
            details={"executable": exe},
        )

    def _executable(self) -> str:
        configured = str(self.config.get("executable") or "").strip()
        if configured and Path(configured).exists():
            return configured
        for candidate in self.profile.executable_candidates:
            if Path(candidate).exists():
                return candidate
        return ""

    def _restore(self) -> None:
        if self.hwnd:
            user32.ShowWindow(self.hwnd, 9)  # SW_RESTORE
            time.sleep(0.2)
            self.rect = _window_rect(self.hwnd)

    def close(self) -> None:
        # Never terminate a user's running app by default.
        self.log("detach", **self.window_info())

    # -- input -----------------------------------------------------------
    def _focus(self) -> None:
        if not self.hwnd:
            raise GuiAutomationError("GUI_WINDOW_NOT_FOUND", "尚未 attach 窗口")
        user32.ShowWindow(self.hwnd, 9)
        user32.SetWindowPos(self.hwnd, HWND_TOP, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.SetForegroundWindow(self.hwnd)
        user32.BringWindowToTop(self.hwnd)
        time.sleep(0.5)
        foreground = int(user32.GetForegroundWindow())
        if foreground != self.hwnd:
            # Windows may reject SetForegroundWindow from a background process.
            # Attach to the current foreground thread to bypass the lock, then
            # raise the target above all other Electron windows.
            current_tid = int(kernel32.GetCurrentThreadId())
            fg_tid = int(user32.GetWindowThreadProcessId(wintypes.HWND(foreground), None))
            attached = False
            try:
                attached = bool(user32.AttachThreadInput(current_tid, fg_tid, True))
                user32.SetWindowPos(self.hwnd, HWND_TOP, 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
                user32.SetForegroundWindow(self.hwnd)
                user32.BringWindowToTop(self.hwnd)
                time.sleep(0.5)
            finally:
                if attached:
                    user32.AttachThreadInput(current_tid, fg_tid, False)
        foreground = int(user32.GetForegroundWindow())
        if foreground != self.hwnd:
            # A single click on the title bar activates the window even when
            # Windows' foreground lock rejects SetForegroundWindow.
            left, top, right, _bottom = self.rect
            self._click(int((left + right) / 2), top + 20)
            time.sleep(0.4)

    def _click(self, x: int, y: int) -> None:
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.15)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.08)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.25)

    def click_ratio(self, ratio: tuple[float, float], *, times: int = 1) -> None:
        left, top, right, bottom = self.rect
        x = left + int((right - left) * float(ratio[0]))
        y = top + int((bottom - top) * float(ratio[1]))
        for _ in range(max(1, times)):
            self._click(x, y)

    def prepare_new_task(self) -> bool:
        """Return to the app's home layout so the input ratio is stable.

        WorkBuddy and Qoder CN both switch from a large home composer to a
        bottom-docked session composer after a conversation exists. Clicking
        "新建任务" makes every adapter invocation start from the same layout.
        """
        if not self.profile.new_task_ratio:
            return False
        self._focus()
        self.click_ratio(self.profile.new_task_ratio, times=1)
        time.sleep(1.0)
        self.log("new_task", ratio=list(self.profile.new_task_ratio))
        return True

    def select_workspace(self, workspace: str = "") -> bool:
        """Open a new task inside the workspace row prepared for this run.

        ``prepare_qoder_session`` inserts the run workspace and the desktop
        lists the most recently updated workspace first, so the row sits at
        a stable ratio.  The row must be activated before the inline ``+``
        creates a task in that workspace.
        """
        row = self.profile.workspace_row_ratio
        plus = self.profile.workspace_new_task_ratio
        if not row or not plus:
            return False
        self._focus()
        self.click_ratio(row, times=1)
        time.sleep(0.8)
        self.click_ratio(plus, times=1)
        time.sleep(1.5)
        self.log("select_workspace", workspace=workspace,
                 row=list(row), new_task=list(plus))
        return True

    def scroll_ratio(self, ratio: tuple[float, float], notches: int) -> None:
        left, top, right, bottom = self.rect
        x = left + int((right - left) * float(ratio[0]))
        y = top + int((bottom - top) * float(ratio[1]))
        user32.SetCursorPos(x, y)
        time.sleep(0.15)
        # Positive delta scrolls up (towards the first item).
        for _ in range(abs(int(notches))):
            user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, 120 if notches > 0 else -120, 0)
            time.sleep(0.08)
        time.sleep(0.3)

    def _composer_layout(self) -> str:
        """Detect the wide home composer vs the narrower open-chat composer."""
        if not self.hwnd:
            return ""
        try:
            from PIL import ImageGrab
            image = ImageGrab.grab(bbox=_window_rect(self.hwnd), all_screens=True).convert("RGB")
            width, height = image.size
            pixels = image.load()
            xs: list[int] = []
            for y in range(int(height * 0.87), int(height * 0.94)):
                for x in range(int(width * 0.6), width):
                    red, green, blue = pixels[x, y]
                    if green > 90 and green > red + 15 and green > blue + 15:
                        xs.append(x)
            if xs:
                return "home" if max(xs) / width > 0.82 else "chat"
        except Exception:
            pass
        return ""

    def select_model(self, model_key: str) -> bool:
        if not self.profile.model_switch_supported:
            return False
        selector = self.profile.model_selector_ratio
        items = self.profile.model_item_ratio
        layout = self._composer_layout()
        if layout == "home":
            selector = self.profile.model_selector_ratio_home or selector
            items = self.profile.model_item_ratio_home or items
        if not selector:
            return False
        item = items.get(model_key)
        if not item:
            raise GuiAutomationError(
                "GUI_MODEL_SWITCH_FAILED",
                f"未配置模型 {model_key} 的菜单项位置",
                details={"known": sorted(items)},
            )
        self._focus()
        self.click_ratio(selector, times=1)
        time.sleep(1.5)
        # The menu auto-scrolls to the currently selected model. Reset to the
        # top so the measured row is stable regardless of the previous
        # selection (e.g. after a mistaken Kimi/Hy click).
        scroll = self.profile.model_menu_scroll_ratio
        if scroll:
            if layout == "home":
                scroll = (item[0] - 0.08, scroll[1])
            self.scroll_ratio(scroll, 8)
        self.click_ratio(item, times=1)
        time.sleep(0.8)
        self.log("select_model", model=model_key, layout=layout or "unknown",
                 selector=list(selector), item=list(item))
        return True

    def send_prompt(self, text: str) -> None:
        self._focus()
        # First click activates the window, second click focuses the editor.
        self.click_ratio(self.profile.input_ratio,
                         times=2 if self.profile.input_double_click else 1)
        foreground = int(user32.GetForegroundWindow())
        if foreground != self.hwnd:
            raise GuiAutomationError(
                "GUI_FOCUS_FAILED", "点击输入区后窗口仍未获得前台焦点",
                details={"hwnd": self.hwnd, "foreground": foreground},
            )
        _key_event(VK_CONTROL, 0)
        _press(VK_A)  # replace any draft / stale text
        _key_event(VK_CONTROL, KEYEVENTF_KEYUP)
        time.sleep(0.1)
        _set_clipboard(text)
        _key_event(VK_CONTROL, 0)
        _press(VK_V)
        _key_event(VK_CONTROL, KEYEVENTF_KEYUP)
        time.sleep(0.8)
        submit = SUBMIT_KEYS.get(str(self.config.get("submit_key") or self.profile.submit_key).upper(),
                                 VK_RETURN)
        _press(submit)
        if self.profile.submit_button_ratio and \
                str(self.config.get("submit_mode") or "").lower() == "button":
            # Long pastes become an attachment chip and Enter does not submit;
            # button mode is opt-in because the same spot is the "stop"
            # control while a reply is streaming.
            time.sleep(0.8)
            self.click_ratio(self.profile.submit_button_ratio, times=1)
        self.log("send_prompt", chars=len(text), input_ratio=list(self.profile.input_ratio),
                 submit_button=list(self.profile.submit_button_ratio or []))
        time.sleep(0.8)

    # -- evidence --------------------------------------------------------
    def screenshot(self, path: str | Path) -> str:
        target = Path(path)
        ensure_dir(target.parent)
        if not self.hwnd:
            return ""
        rect = _window_rect(self.hwnd)
        try:
            from PIL import ImageGrab  # optional dependency
            image = ImageGrab.grab(bbox=rect, all_screens=True)
            image.save(target)
            self.log("screenshot", path=str(target), size=list(image.size))
            return str(target)
        except Exception:
            pass
        try:
            left, top, right, bottom = rect
            script = (
                "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
                f"$bmp = New-Object System.Drawing.Bitmap {right - left},{bottom - top}; "
                "$g = [System.Drawing.Graphics]::FromImage($bmp); "
                f"$g.CopyFromScreen({left},{top},0,0,$bmp.Size); "
                f"$bmp.Save('{str(target).replace(chr(39), chr(39) + chr(39))}', "
                "[System.Drawing.Imaging.ImageFormat]::Png); "
                "$g.Dispose(); $bmp.Dispose()"
            )
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, timeout=30)
            if target.exists():
                self.log("screenshot", path=str(target), backend="powershell")
                return str(target)
        except Exception:
            pass
        self.log("screenshot_failed", path=str(target))
        return ""


__all__ = ["WindowsGuiDriver", "find_window", "WindowsGuiDriver"]
