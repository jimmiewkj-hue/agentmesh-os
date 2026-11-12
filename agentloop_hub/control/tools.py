"""Control-plane tools callable by sub-nodes.

Web search is provided by the Controller itself (no per-Agent native browsing
required): any terminal-capable Agent can call it through the CLI
(``python -m agentloop_hub search "<query>"``) or the HTTP endpoint
(``GET /api/tools/web_search?q=``). The default engine is DuckDuckGo Lite,
which needs no API key; results are parsed from its HTML and the redirect
target (``uddg``) is decoded to the real URL. Network failure is reported
honestly, never fabricated.
"""

from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_ENGINE = "duckduckgo-lite"
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AgentMesh/1.0"

_LINK_RE = re.compile(r"<a\b([^>]*\bresult-link\b[^>]*)>(.*?)</a>", re.S | re.I)
_HREF_RE = re.compile(r"href=['\"]([^'\"]+)['\"]", re.I)
_SNIPPET_RE = re.compile(r"class=['\"]result-snippet['\"]\s*>(.*?)</td>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text or "")).strip()


def _real_url(href: str) -> str:
    href = html.unescape(href or "")
    if "uddg=" in href:
        query = urllib.parse.urlsplit(href).query
        values = urllib.parse.parse_qs(query).get("uddg")
        if values:
            return values[0]
    if href.startswith("//"):
        return "https:" + href
    return href


def parse_duckduckgo_lite(html_text: str, max_results: int = 5) -> list[dict[str, str]]:
    """Parse result links + snippets from a DuckDuckGo Lite HTML page."""
    snippets = [_clean(m) for m in _SNIPPET_RE.findall(html_text or "")]
    results: list[dict[str, str]] = []
    for index, match in enumerate(_LINK_RE.finditer(html_text or "")):
        attrs, inner = match.group(1), match.group(2)
        href_match = _HREF_RE.search(attrs)
        if not href_match:
            continue
        url = _real_url(href_match.group(1))
        title = _clean(inner)
        if not url or not title:
            continue
        snippet = snippets[index] if index < len(snippets) else ""
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results


def web_search(query: str, *, max_results: int = 5, timeout: float = 20.0,
               engine: str = DEFAULT_ENGINE) -> dict[str, Any]:
    """Run a keyless web search; return structured results or an honest error."""
    query = str(query or "").strip()
    if not query:
        return {"query": query, "engine": engine, "results": [], "error": "查询为空"}
    url = "https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode({"q": query})
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            html_text = response.read().decode("utf-8", "replace")
    except Exception as exc:  # network/DNS/TLS — report, never fabricate
        return {"query": query, "engine": engine, "results": [],
                "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    results = parse_duckduckgo_lite(html_text, max_results=max_results)
    error = "" if results else "未解析到结果（可能被限流或页面结构变化）"
    return {"query": query, "engine": engine, "results": results, "error": error}


__all__ = ["web_search", "parse_duckduckgo_lite", "DEFAULT_ENGINE"]