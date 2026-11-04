"""Real-machine audit: what the control plane actually sees on THIS box.

Read-only: creates a throw-away git repo, runs the real discovery path, then
prints the connector manifest, auto-executable share, routing outcome per task
type and the five-capability surface summary. No task is executed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentloop_hub.control.agent_surface import surface_report  # noqa: E402
from agentloop_hub.hub import Hub  # noqa: E402


def _repo(base: Path) -> Path:
    repo = base / "audit"
    repo.mkdir(parents=True)
    for args in (["init", "-q"], ["config", "user.email", "a@b.c"],
                 ["config", "user.name", "audit"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# audit\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="agentmesh-audit-"))
    hub = Hub(_repo(base), auto_discover=True)
    manifest = hub.connector_manifest()
    agents = []
    for agent in manifest["agents"]:
        agents.append({
            "agent_id": agent["agent_id"],
            "product": agent["product"],
            "status": agent["status"],
            "adapter": agent["adapter_type"],
            "level": agent["access_level"],
            "auto": agent["auto_executable"],
            "capabilities": len(agent["capability"]),
            "models": len(agent["models"]),
        })
    surface = surface_report(hub.project.project_id)
    routing = {task_type: (hub.route_preview(task_type=task_type)["selected"] or {}).get("agent_id", "")
               for task_type in ("analysis", "review", "implementation", "documentation",
                                 "verification", "architecture")}
    report = {
        "root": str(base),
        "agents_total": len(agents),
        "auto_executable": sorted(a["agent_id"] for a in agents if a["auto"]),
        "manual_only": sorted(a["agent_id"] for a in agents if not a["auto"]),
        "agents": agents,
        "surface_summary": surface["summary"],
        "zero_input": surface["zero_input"],
        "routing_selected": routing,
        "models_total": sum(a["models"] for a in agents),
    }
    out = ROOT / "verification" / "audit-real-machine.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in (
        "agents_total", "auto_executable", "manual_only", "surface_summary",
        "routing_selected", "models_total")}, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
