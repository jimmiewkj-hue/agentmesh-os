"""GUI automation package."""

from .base import (
    GUI_ERROR_CODES, GuiAutomationError, GuiDriver, GuiProfile, GuiReply,
    TranscriptReader, read_jsonl, resolve_model_key,
)
from .profiles import GUI_PROFILES, profile_for_app, qoder_cn_profile, workbuddy_profile
from .readers import QoderCnReader, WorkBuddyReader, create_reader
from .windows import WindowsGuiDriver, find_window

__all__ = [
    "GUI_ERROR_CODES", "GuiAutomationError", "GuiDriver", "GuiProfile", "GuiReply",
    "TranscriptReader", "read_jsonl", "resolve_model_key", "GUI_PROFILES",
    "profile_for_app", "qoder_cn_profile", "workbuddy_profile", "QoderCnReader",
    "WorkBuddyReader", "create_reader", "WindowsGuiDriver", "find_window",
]
