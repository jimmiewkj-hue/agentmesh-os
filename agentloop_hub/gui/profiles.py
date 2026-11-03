"""Real GUI profiles for Qoder CN and WorkBuddy, measured on this machine.

Evidence (2026-11-03):
* Qoder CN 0.5.1 / CLI 1.1.79-hotfix.7, window Qoder CN, input at ~(0.524,0.804),
  model shown as "DeepSeek-Flash", transcript runtime key ``dfmodel``.
* WorkBuddy 37.10.3-43, window WorkBuddy, input at ~(0.439,0.395), model menu
  entry GLM-5.3-Flash at ~(0.630,0.060), session DB key ``glm-5.3-flash``.
"""

from __future__ import annotations

import os
from pathlib import Path

from .base import GuiProfile


def _home() -> Path:
    return Path.home()


def _appdata_local() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or _home() / "AppData" / "Local")


def qoder_cn_profile() -> GuiProfile:
    local = _appdata_local()
    return GuiProfile(
        app="qoder_cn",
        product="Qoder CN",
        window_title="Qoder CN",
        executable_candidates=(
            r"C:\Program Files\Qoder\Qoder CN\Qoder CN.exe",
            str(local / "Programs" / "Qoder CN" / "Qoder CN.exe"),
        ),
        transcript_reader="qoder_cn",
        data_root=str(_home() / ".qoder-cn" / "projects"),
        input_ratio=(0.524, 0.804),
        input_double_click=True,
        new_task_ratio=(0.036, 0.072),
        # Re-measured 2026-11-03 against the 1336x808 window: selector sits
        # left of the mic at (0.678, 0.907); the menu opens above it and
        # DeepSeek-Flash / GLM-5.3 are the 6th / 7th rows.
        submit_button_ratio=(0.749, 0.907),
        model_selector_ratio=(0.678, 0.907),
        model_menu_scroll_ratio=(0.584, 0.620),
        model_item_ratio={
            "dfmodel": (0.584, 0.734),
            "gfmodel": (0.584, 0.781),
        },
        workspace_row_ratio=(0.070, 0.282),
        workspace_new_task_ratio=(0.177, 0.282),
        model_aliases={
            "dfmodel": "dfmodel",
            "DeepSeek-Flash": "dfmodel",
            "DeepSeek Flash": "dfmodel",
            "deepseek-flash": "dfmodel",
            "gfmodel": "gfmodel",
            "GLM-5.3-Flash": "gfmodel",
            "GLM 5.3 Flash": "gfmodel",
            "glm-5.3-flash": "gfmodel",
        },
        model_switch_supported=True,
        display_model="DeepSeek-Flash",
        launch_timeout_seconds=120,
        timeout_seconds=1800,
        stall_seconds=90,
    )


def workbuddy_profile() -> GuiProfile:
    local = _appdata_local()
    return GuiProfile(
        app="workbuddy",
        product="WorkBuddy",
        window_title="WorkBuddy",
        executable_candidates=(
            r"C:\Program Files\WorkBuddy\WorkBuddy.exe",
            str(local / "Programs" / "WorkBuddy" / "WorkBuddy.exe"),
        ),
        transcript_reader="workbuddy",
        data_root=str(_home() / ".workbuddy" / "projects"),
        input_ratio=(0.439, 0.395),
        input_double_click=True,
        new_task_ratio=(0.050, 0.120),
        model_selector_ratio=(0.796, 0.519),
        model_menu_scroll_ratio=(0.740, 0.250),
        model_item_ratio={"glm-5.3-flash": (0.720, 0.361)},
        model_aliases={
            "glm-5.3-flash": "glm-5.3-flash",
            "GLM-5.3-Flash": "glm-5.3-flash",
            "GLM 5.3 Flash": "glm-5.3-flash",
        },
        model_switch_supported=True,
        display_model="GLM-5.3-Flash",
        launch_timeout_seconds=120,
        timeout_seconds=1800,
        stall_seconds=90,
    )


GUI_PROFILES = {
    "qoder_cn": qoder_cn_profile,
    "workbuddy": workbuddy_profile,
}


def profile_for_app(app: str) -> GuiProfile | None:
    factory = GUI_PROFILES.get(str(app or "").strip().lower())
    return factory() if factory else None


__all__ = ["GUI_PROFILES", "profile_for_app", "qoder_cn_profile", "workbuddy_profile"]
