"""GUI automation foundation: error codes, profiles and transcript replies."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


GUI_ERROR_CODES: dict[str, str] = {
    "GUI_UNSUPPORTED_PLATFORM": "当前平台不支持 GUI 自动化（仅 Windows）",
    "GUI_APP_NOT_CONFIGURED": "未配置 GUI 应用标识",
    "GUI_LAUNCH_FAILED": "应用启动失败",
    "GUI_WINDOW_NOT_FOUND": "未找到目标窗口",
    "GUI_FOCUS_FAILED": "窗口无法获得前台焦点",
    "GUI_INPUT_NOT_FOUND": "未找到输入区域",
    "GUI_SEND_FAILED": "任务发送失败（输入框未接收或回车未触发）",
    "GUI_MODEL_SWITCH_FAILED": "模型切换失败或切换后未生效",
    "GUI_TIMEOUT": "等待 Agent 回复超时",
    "GUI_STALLED": "Agent 会话长时间无新增事件（疑似卡死）",
    "GUI_REPLY_PARSE_FAILED": "无法从回复中解析结构化 JSON",
    "GUI_RESULT_MISSING": "Agent 未产出成果包",
    "GUI_SCREENSHOT_FAILED": "截图失败",
    "GUI_DRIVER_ERROR": "GUI 驱动内部错误",
    "GUI_INPUT_FORBIDDEN": "零 OS 输入策略禁止键鼠/剪贴板合成",
    "WORKSPACE_MISMATCH": "实际工作区与期望工作区不一致",
    "WORKSPACE_SWITCH_FAILED": "无法通过程序接口创建工作区/切换会话",
    "GUI_MODE_MISMATCH": "实际模式或权限与期望值不一致",
    "MODEL_UNAVAILABLE": "Agent 的真实模型清单中没有请求的模型",
    "MODEL_SWITCH_UNSUPPORTED": "没有可用的程序化模型切换接口",
    "MODEL_GUI_FORBIDDEN": "零鼠标模式禁止使用 GUI 模型切换",
    "MODEL_MISMATCH": "回读的实际模型与请求不一致",
    "INTERFACE_WRITE_FAILED": "程序接口写入失败",
    "APP_RESTART_REQUIRED": "应用正在运行但没有开放程序接口，需要用户确认重启",
    "MANUAL_ACTION_REQUIRED": "节点需要用户在 Agent 应用内手动提交或审批",
    "APP_BUSY": "Agent 应用仍有其他任务在运行，需要等待或先停止",
    "APP_LOGIN_REQUIRED": "Agent 应用未登录，需要先在应用内登录",
    "MODEL_STREAM_ERROR": "模型流在应用内中断（上游返回错误/空流）",
    "GUI_CANCELED": "任务在 Agent 应用内被用户中断",
    "PROFILE_SYNC_FAILED": "模型上下文/能力等级未能写入 Agent 应用",
}


class GuiAutomationError(Exception):
    """Stable, diagnosable GUI automation error.

    ``code`` is one of :data:`GUI_ERROR_CODES`; adapters include it verbatim in
    ``ExecutionOutcome.error`` and log the same code to the task GUI log.
    """

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None,
                 screenshot: str = ""):
        self.code = code if code in GUI_ERROR_CODES else "GUI_UNSUPPORTED_PLATFORM"
        self.message = message
        self.details = details or {}
        self.screenshot = screenshot
        super().__init__(f"{self.code}: {message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "message": self.message,
            "details": self.details, "screenshot": self.screenshot,
        }


@dataclass(frozen=True)
class GuiProfile:
    """App-specific GUI layout and transcript binding.

    Coordinates are **relative ratios** of the window rect, never absolute
    screen positions. Keeping them in config lets a window resize or move
    without breaking the adapter.
    """

    app: str
    product: str
    window_title: str
    executable_candidates: tuple[str, ...]
    transcript_reader: str
    data_root: str = ""
    input_ratio: tuple[float, float] = (0.5, 0.8)
    input_double_click: bool = True
    new_task_ratio: tuple[float, float] | None = None
    submit_key: str = "ENTER"
    submit_button_ratio: tuple[float, float] | None = None
    model_selector_ratio: tuple[float, float] | None = None
    model_selector_ratio_home: tuple[float, float] | None = None
    model_menu_scroll_ratio: tuple[float, float] | None = None
    model_item_ratio: dict[str, tuple[float, float]] = field(default_factory=dict)
    model_item_ratio_home: dict[str, tuple[float, float]] = field(default_factory=dict)
    workspace_row_ratio: tuple[float, float] | None = None
    workspace_new_task_ratio: tuple[float, float] | None = None
    model_aliases: dict[str, str] = field(default_factory=dict)
    model_switch_supported: bool = False
    display_model: str = ""
    launch_timeout_seconds: int = 120
    timeout_seconds: int = 1800
    stall_seconds: int = 60
    poll_seconds: float = 2.0


@dataclass
class GuiReply:
    session_id: str
    text: str
    model: str = ""
    transcript_path: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    events: int = 0
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id, "text": self.text, "model": self.model,
            "transcript_path": self.transcript_path, "tool_calls": self.tool_calls,
            "usage": self.usage, "events": self.events, "finished_at": self.finished_at,
        }


class GuiDriver(Protocol):
    def attach(self) -> dict[str, Any]: ...
    def select_model(self, model_key: str) -> bool: ...
    def send_prompt(self, text: str) -> None: ...
    def screenshot(self, path: str | Path) -> str: ...
    def close(self) -> None: ...


class TranscriptReader(Protocol):
    def wait_reply(self, marker: str, *, started_at: float, timeout: float,
                   stall_seconds: float) -> GuiReply: ...


def resolve_model_key(model: str, aliases: dict[str, str]) -> str:
    """Map a display/model name to the runtime key used by the agent."""
    text = str(model or "").strip()
    if not text:
        return ""
    if text in aliases:
        return aliases[text]
    lower = text.lower()
    for key, value in aliases.items():
        if key.lower() == lower:
            return value
    return text


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    events.append(item)
    except OSError:
        return []
    return events


__all__ = [
    "GUI_ERROR_CODES", "GuiAutomationError", "GuiProfile", "GuiReply",
    "GuiDriver", "TranscriptReader", "resolve_model_key", "read_jsonl",
]
