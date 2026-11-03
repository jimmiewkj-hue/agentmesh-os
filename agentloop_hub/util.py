"""Small, dependency-free helpers used across the codebase."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def now_iso() -> str:
    """UTC timestamp with millisecond precision and a trailing Z."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def epoch_ms() -> int:
    return int(time.time() * 1000)


def iso_from_epoch_ms(value: int | float | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# IDs and hashing
# ---------------------------------------------------------------------------

_ID_ALPHABET = "0123456789abcdef"


def new_id(prefix: str, length: int = 12) -> str:
    return f"{prefix}_{''.join(secrets.choice(_ID_ALPHABET) for _ in range(length))}"


def sha1_hex(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | os.PathLike[str], *, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def hash_obj(value: Any) -> str:
    return sha256_text(stable_json(value))


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def tree_hash(paths: Iterable[str], *, base: Path | None = None) -> str:
    """Hash of relative path + file content for a list of paths."""
    digest = hashlib.sha256()
    base = base or Path.cwd()
    for raw in sorted(set(paths)):
        p = Path(raw)
        if not p.is_absolute():
            p = base / p
        try:
            rel = p.relative_to(base).as_posix()
        except ValueError:
            rel = p.as_posix()
        digest.update(rel.encode("utf-8"))
        if p.is_file():
            digest.update(b"\0")
            digest.update(sha256_file(p).encode("utf-8"))
        else:
            digest.update(b"\1")
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def ensure_dir(path: str | os.PathLike[str]) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def atomic_write_text(path: str | os.PathLike[str], text: str, *, encoding: str = "utf-8") -> None:
    p = Path(path)
    ensure_dir(p.parent)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def read_text(path: str | os.PathLike[str], *, default: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return default


def read_json(path: str | os.PathLike[str], default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def write_json(path: str | os.PathLike[str], value: Any, *, indent: int = 2) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=indent))


def tail_text(path: str | os.PathLike[str], *, max_chars: int = 20000) -> str:
    try:
        data = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return data[-max_chars:]


def safe_slug(value: str, *, max_len: int = 60) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-._")
    return (cleaned or "item")[:max_len]


def truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)] + "\n...[truncated]"


def estimate_tokens(text: str) -> int:
    """Conservative token estimate when an agent does not report usage.

    English/code averages ~4 chars per token; CJK averages ~1.6 chars per token.
    We use 3.2 chars/token as a middle ground and never return 0 for non-empty text.
    """
    if not text:
        return 0
    return max(1, int(len(text) / 3.2))


def split_lines(text: str, *, limit: int | None = None) -> list[str]:
    lines = text.splitlines()
    if limit is not None:
        lines = lines[-limit:]
    return lines
