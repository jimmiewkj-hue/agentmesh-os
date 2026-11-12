"""Control-plane web search: parsing, honest errors, capability, CLI."""

from __future__ import annotations

import io
import json

import pytest

from agentloop_hub.control import tools
from agentloop_hub.connectors import derive_capability_list

FIXTURE = """
<table border="0">
  <tr><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=abc" class='result-link'>Example A</a></td></tr>
  <tr><td class='result-snippet'>Snippet <b>A</b> text.</td></tr>
  <tr><td><a rel="nofollow" href="https://direct.example.org/b" class='result-link'>Example B</a></td></tr>
  <tr><td class='result-snippet'>Snippet B.</td></tr>
</table>
"""


def test_parse_decodes_redirect_and_snippet():
    results = tools.parse_duckduckgo_lite(FIXTURE, max_results=5)
    assert len(results) == 2
    assert results[0]["url"] == "https://example.com/a"
    assert results[0]["title"] == "Example A"
    assert results[0]["snippet"] == "Snippet A text."
    assert results[1]["url"] == "https://direct.example.org/b"


def test_parse_respects_max_results():
    assert len(tools.parse_duckduckgo_lite(FIXTURE, max_results=1)) == 1


def test_real_url_variants():
    assert tools._real_url("//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.y%2Fz") == "https://x.y/z"
    assert tools._real_url("//cdn.example.org/page") == "https://cdn.example.org/page"
    assert tools._real_url("https://already.example.org/") == "https://already.example.org/"


def test_empty_query_is_error():
    out = tools.web_search("   ")
    assert out["results"] == [] and out["error"]


def test_network_failure_is_honest(monkeypatch):
    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(tools.urllib.request, "urlopen", boom)
    out = tools.web_search("anything")
    assert out["results"] == []
    assert "network down" in out["error"]


def test_success_parses_response(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return FIXTURE.encode("utf-8")
    monkeypatch.setattr(tools.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    out = tools.web_search("example", max_results=2)
    assert out["error"] == ""
    assert len(out["results"]) == 2
    assert out["results"][0]["url"] == "https://example.com/a"


def test_terminal_agents_derive_web_search(tmp_path):
    from agentloop_hub.models import AgentRecord

    agent = AgentRecord(agent_id="t", name="T", product="T", project_id="prj",
                        adapter_type="cli", access_level="L3", status="online",
                        adapter_config={"command": "x"},
                        capabilities={"read_files": True, "write_files": True, "terminal": True})
    assert "web_search" in derive_capability_list(agent)
    # An explicit user disable still wins over the hub-tool derivation.
    agent.capabilities = {**agent.capabilities, "web_search": False}
    assert "web_search" not in derive_capability_list(agent)


def test_cli_search_outputs_json(monkeypatch, capsys):
    from agentloop_hub.cli import main
    monkeypatch.setattr(tools, "web_search",
                        lambda q, **k: {"query": q, "engine": "duckduckgo-lite",
                                        "results": [{"title": "T", "url": "u", "snippet": "s"}],
                                        "error": ""})
    code = main(["search", "hello", "--json"])
    assert code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["query"] == "hello"
    assert payload["results"][0]["url"] == "u"