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
        "import http from 'node:http';\n"
        "const server = http.createServer((req, res) => {\n"
        "  res.writeHead(200); res.end('ok');\n"
        "});\n"
        "server.listen(5199, '127.0.0.1', () => {\n"
        "  console.log('本地静态服务器 http://127.0.0.1:5199/');\n"
        "});\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    result = run_command("node scripts/serve.mjs", cwd=str(tmp_path),
                         timeout=60, long_running_timeout=5)
    assert result.long_running is True
    assert result.timed_out is True
    assert result.server_ready is True
    assert result.process_alive_before_stop is True
    assert result.probe_url.endswith(":5199/")
    assert time.monotonic() - started < 20


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_log_text_alone_is_not_server_readiness(tmp_path):
    """A fake 'listening' line followed by exit 1 must never pass."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "serve-fake.mjs").write_text(
        "console.log('listening on http://127.0.0.1:5198/');\n"
        "process.exit(1);\n",
        encoding="utf-8",
    )
    result = run_command("node scripts/serve-fake.mjs", cwd=str(tmp_path),
                         timeout=30, long_running_timeout=3)
    assert result.server_ready is False
    assert result.ok is False
    assert "进程在探测前退出" in result.probe_note


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_listening_word_without_a_port_is_not_ready(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "server.mjs").write_text(
        "console.log('listening, waiting for connections');\n"
        "setInterval(() => {}, 1000);\n", encoding="utf-8")
    result = run_command("node scripts/server.mjs", cwd=str(tmp_path),
                         timeout=10, long_running_timeout=1)
    assert result.server_ready is False
    assert "未探测到监听地址" in result.probe_note
