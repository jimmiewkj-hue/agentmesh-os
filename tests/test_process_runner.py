"""Long-running server commands must not block verification for 15 minutes."""

from __future__ import annotations

import shutil
import time

import pytest

from agentloop_hub.control.process_runner import is_long_running_command, run_command


def test_long_running_command_detection():
    assert is_long_running_command("npm run start")
    assert is_long_running_command("npm run dev")
    assert is_long_running_command("node scripts/serve.mjs")
    assert is_long_running_command("python -m http.server 8000")
    assert not is_long_running_command("node scripts/check-scene.mjs")
    assert not is_long_running_command("npm install")
    assert not is_long_running_command("git status --short")
    assert not is_long_running_command(
        "chrome --headless http://127.0.0.1:5173/")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_static_server_is_probed_then_stopped(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "serve.mjs").write_text(
        "console.log('本地静态服务器 http://127.0.0.1:5199/');\n"
        "setInterval(() => {}, 1000);\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    result = run_command("node scripts/serve.mjs", cwd=str(tmp_path),
                         timeout=60, long_running_timeout=2)
    assert result.long_running is True
    assert result.timed_out is True
    assert result.server_ready is True
    assert time.monotonic() - started < 20
