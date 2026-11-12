"""Real local-embedding verification for the true-semantic cache.

Runs the user's on-disk EmbeddingGemma-2 (no mocks) and exercises the real
CostOptimizer semantic cache against a temporary git project. Prints JSON
evidence; exits non-zero on failure.

    python verification/verify_embeddings_real.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentloop_hub.control.embeddings import LocalEmbedder  # noqa: E402
from agentloop_hub.models import Task, TaskSpec  # noqa: E402


def main() -> int:
    embedder = LocalEmbedder()
    evidence: dict = {"model_path": embedder.model_path, "available": embedder.available()}
    if not embedder.available():
        evidence["error"] = "embedding model or torch/transformers unavailable"
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
        return 1

    texts = ["修复 calc.py 的加法函数错误",
             "修正 calc.py 中 add 函数的缺陷",
             "为项目生成 README 文档"]
    vectors = embedder.embed(texts)
    if vectors is None:
        evidence["error"] = embedder.status().get("error") or "embedding failed"
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
        return 1
    from agentloop_hub.control.embeddings import cosine
    paraphrase = cosine(vectors[0], vectors[1])
    different = cosine(vectors[0], vectors[2])
    evidence.update({
        "dim": len(vectors[0]),
        "sim_paraphrase": round(paraphrase, 4),
        "sim_different_task": round(different, 4),
        "separation": round(paraphrase - different, 4),
    })

    # End-to-end through the real CostOptimizer + SQLite semantic_cache table.
    tmp = Path(tempfile.mkdtemp(prefix="semcache_"))
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    (tmp / "calc.py").write_text("def add(a,b):\n    return a-b\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=tmp, check=True)
    from agentloop_hub.hub import Hub
    hub = Hub(tmp, auto_discover=False)
    try:
        def task(objective: str, task_id: str) -> Task:
            spec = TaskSpec(task_id=task_id, title=task_id, objective=objective,
                            type="analysis", scope=["calc.py"], token_budget=1000)
            return Task(uid=f"verify:{task_id}", project_id=hub.project.project_id,
                        plan_id="plan", run_id="run", spec=spec, status="completed",
                        workdir=str(tmp), task_dir=str(tmp), base_commit="abc")

        stored = task("修复 calc.py 的加法函数错误", "T1")
        hub.cost.semantic_store(stored, file_hash="H", payload={"summary": "fixed add"})
        hit = hub.cost.semantic_reference(task("修正 calc.py 中 add 函数的缺陷", "T2"), "H")
        miss = hub.cost.semantic_reference(task("为项目生成 README 文档", "T3"), "H")
        evidence["cache_hit"] = hit
        evidence["cache_miss_is_none"] = miss is None
        ok = (paraphrase >= 0.90 and different <= paraphrase - 0.15
              and hit is not None and hit.get("method") == "embedding"
              and miss is None)
        evidence["ok"] = ok
        print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
        return 0 if ok else 1
    finally:
        hub.close()


if __name__ == "__main__":
    raise SystemExit(main())