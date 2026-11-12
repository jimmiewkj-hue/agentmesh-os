"""True-semantic cache: local embeddings (fake in unit tests) + honest fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from hub_test_support import make_project

from agentloop_hub.control import cost as cost_mod
from agentloop_hub.control.embeddings import LocalEmbedder, cosine
from agentloop_hub.models import Task, TaskSpec


class FakeEmbedder:
    """Deterministic 3-D embedder keyed by topic; simulates cosine separation."""
    def available(self) -> bool:
        return True

    def embed(self, texts):
        out = []
        for text in texts:
            low = str(text).lower()
            # The signature always carries the shared scope ("calc.py"), so the
            # topic keyword in the objective must be checked first.
            if "readme" in low:
                out.append([0.0, 1.0, 0.0])
            elif "calc" in low:
                out.append([1.0, 0.0, 0.0])
            else:
                out.append([0.0, 0.0, 1.0])
        return out


def _task(tmp_path, objective, *, type="analysis", writable=False, task_id="T1"):
    spec = TaskSpec(task_id=task_id, title=task_id, objective=objective, type=type,
                    scope=["calc.py"], token_budget=1000)
    return Task(uid=f"run:{task_id}", project_id="prj", plan_id="plan", run_id="run",
                spec=spec, status="completed", workdir=str(tmp_path), task_dir=str(tmp_path),
                base_commit="abc")


def test_cosine_and_unavailable_embedder(tmp_path):
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine([], [1]) == 0.0
    missing = LocalEmbedder(str(tmp_path / "absent-model"))
    assert missing.available() is False
    assert missing.embed(["x"]) is None


def test_embedding_backend_hits_paraphrase_and_misses_other_topic(tmp_path, monkeypatch):
    repo, hub, _ = make_project(tmp_path)
    monkeypatch.setattr(cost_mod.CostOptimizer, "_embedder", lambda self: FakeEmbedder())
    hub.config.semantic_cache_embedding_threshold = 0.85

    stored = _task(tmp_path, "fix calc add function", task_id="T1")
    hub.cost.semantic_store(stored, file_hash="H", payload={"summary": "fixed add"})

    paraphrase = _task(tmp_path, "fix calc add bug", task_id="T2")
    ref = hub.cost.semantic_reference(paraphrase, file_hash="H")
    assert ref is not None
    assert ref["method"] == "embedding"
    assert ref["similarity"] == pytest.approx(1.0)

    other = _task(tmp_path, "write readme docs", task_id="T3")
    assert hub.cost.semantic_reference(other, file_hash="H") is None
    hub.close()


def test_embedding_persisted_and_reused_without_recompute(tmp_path, monkeypatch):
    from agentloop_hub.util import read_json  # noqa: F401
    repo, hub, _ = make_project(tmp_path)
    monkeypatch.setattr(cost_mod.CostOptimizer, "_embedder", lambda self: FakeEmbedder())
    stored = _task(tmp_path, "fix calc add function", task_id="T1")
    hub.cost.semantic_store(stored, file_hash="H", payload={"summary": "ok"})
    rows = hub.store.semantic_candidates(hub.project.project_id, "analysis", "H")
    assert rows and rows[0]["embedding"]
    # A later reference finds it via the stored vector.
    ref = hub.cost.semantic_reference(_task(tmp_path, "fix calc bug", task_id="T2"), "H")
    assert ref is not None and ref["method"] == "embedding"
    hub.close()


def test_forced_jaccard_backend_marks_method(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    # No monkeypatch: the real _embedder honors the forced backend.
    hub.config.semantic_cache_backend = "jaccard"
    stored = _task(tmp_path, "fix calc add function", task_id="T1")
    hub.cost.semantic_store(stored, file_hash="H", payload={"summary": "ok"})
    ref = hub.cost.semantic_reference(_task(tmp_path, "fix calc add function", task_id="T2"), "H")
    assert ref is not None and ref["method"] == "jaccard"
    hub.close()


def test_writable_task_never_uses_semantic_cache(tmp_path, monkeypatch):
    repo, hub, _ = make_project(tmp_path)
    monkeypatch.setattr(cost_mod.CostOptimizer, "_embedder", lambda self: FakeEmbedder())
    task = _task(tmp_path, "fix calc", type="implementation", writable=True)
    assert hub.cost.semantic_store(task, file_hash="H", payload={"x": 1}) is None
    assert hub.cost.semantic_reference(task, file_hash="H") is None
    hub.close()