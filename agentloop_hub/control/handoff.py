"""Main-node handoff package.

Switching the main node never forwards the raw chat; it forwards the confirmed
project state (goal, constraints, tasks, decisions, risks, changes, approvals).
"""

from __future__ import annotations

import json
from typing import Any

import yaml

from ..models import AgentRecord, ApprovalStatus, Project, TaskStatus
from ..storage.repo import Store
from ..util import atomic_write_text, ensure_dir, now_iso, safe_slug
from .approvals import ApprovalService
from .memory import MemoryService
from .paths import WorkspacePaths


class HandoffService:
    def __init__(self, store: Store, project: Project, paths: WorkspacePaths,
                 approvals: ApprovalService, memory: MemoryService):
        self.store = store
        self.project = project
        self.paths = paths
        self.approvals = approvals
        self.memory = memory

    def build_package(self, *, from_agent_id: str = "", to_agent_id: str = "",
                      to_model: str = "", phase: str = "",
                      note: str = "") -> dict[str, Any]:
        run = self.store.latest_run(self.project.project_id)
        plan = self.store.get_plan(run["plan_id"]) if run else None
        tasks = self.store.list_tasks(run["run_id"]) if run else []
        open_tasks = [self._task_row(t) for t in tasks if t.status not in (
            TaskStatus.COMPLETED.value, TaskStatus.REJECTED.value, TaskStatus.CANCELED.value,
        )]
        completed = [self._task_row(t) for t in tasks if t.status == TaskStatus.COMPLETED.value]
        decisions = [
            {"content": d.content, "agent": d.agent_id, "model": d.model,
             "approved": d.approved, "created_at": d.created_at}
            for d in self.store.list_decisions(self.project.project_id, limit=30)
        ]
        rejected = [
            {"subject": a.subject, "details": a.details, "decided_at": a.decided_at, "note": a.note}
            for a in self.store.list_approvals(self.project.project_id, limit=200)
            if a.status == ApprovalStatus.REJECTED.value
        ]
        risks: list[str] = []
        changed_files: set[str] = set()
        verification_status: dict[str, str] = {}
        for task in tasks:
            check = self.store.latest_verification(task.uid)
            if check:
                risks.extend(str(w) for w in check.get("warnings", []))
                verification_status[task.task_id] = check["status"]
            result = self.store.get_result(task.uid)
            if result:
                for risk in result["payload"].get("risks", []):
                    risks.append(str(risk)[:300])
                for file in result["payload"].get("files_changed", []):
                    changed_files.add(str(file))
        pending = [
            {"approval_id": a.approval_id, "kind": a.kind, "subject": a.subject,
             "requested_at": a.requested_at}
            for a in self.approvals.pending()
        ]
        recommended: list[str] = []
        if pending:
            recommended.append("先处理待审批事项")
        if any(t.status == TaskStatus.FAILED.value for t in tasks):
            recommended.append("检查失败任务并决定是否返工")
        if run and run["status"] in ("pending_merge", "completed"):
            recommended.append("审阅最终汇总与合并结果")
        if not recommended:
            recommended.append("继续执行未完成任务" if open_tasks else "开始新的规划")

        package = {
            "generated_at": now_iso(),
            "project": {
                "project_id": self.project.project_id, "name": self.project.name,
                "root": self.project.root, "branch": self.project.branch,
                "commit": self.project.commit, "tech_stack": self.project.tech_stack,
            },
            "project_goal": plan.goal if plan else "",
            "confirmed_requirements": plan.constraints if plan else [],
            "constraints": plan.constraints if plan else [],
            "current_phase": phase or (run["status"] if run else "no_run"),
            "open_tasks": open_tasks,
            "completed_tasks": completed,
            "decisions": decisions,
            "rejected_options": rejected,
            "known_risks": sorted(set(risks)),
            "changed_files": sorted(changed_files),
            "verification_status": verification_status,
            "pending_approvals": pending,
            "recommended_next_action": recommended,
            "from_main_agent": from_agent_id,
            "to_main_agent": to_agent_id,
            "model": to_model,
            "note": note,
        }
        return package

    @staticmethod
    def _task_row(task) -> dict[str, Any]:
        return {
            "task_id": task.task_id, "title": task.spec.title, "status": task.status,
            "agent": task.assigned_agent, "attempts": task.attempts,
            "objective": task.spec.objective, "scope": task.spec.scope,
            "dependencies": task.spec.dependencies, "verification_status": task.verification_status,
            "error": task.error,
        }

    def write(self, package: dict[str, Any], *, prefix: str = "") -> dict[str, str]:
        stamp = now_iso().replace(":", "").replace("-", "")[:15]
        name = safe_slug(f"{prefix}-{stamp}-handoff", max_len=80)
        directory = ensure_dir(self.paths.handoffs)
        yaml_path = directory / f"{name}.yaml"
        md_path = directory / f"{name}.md"
        atomic_write_text(yaml_path, yaml.safe_dump(package, allow_unicode=True, sort_keys=False))
        atomic_write_text(md_path, self.to_markdown(package))
        return {"yaml": str(yaml_path), "markdown": str(md_path)}

    def switch(self, *, to_agent: AgentRecord, model: str, note: str = "") -> dict[str, Any]:
        current = self.store.get_main_node(self.project.project_id)
        from_agent = current["agent_id"] if current else ""
        package = self.build_package(
            from_agent_id=from_agent, to_agent_id=to_agent.agent_id,
            to_model=model, note=note,
        )
        files = self.write(package, prefix=f"switch-{to_agent.agent_id}")
        self.store.set_main_node(
            self.project.project_id, to_agent.agent_id, model,
            budget_tokens=self.project.extra.get("token_budget", 0),
            permissions=to_agent.permissions, note=note,
        )
        self.store.append_audit(
            self.project.project_id, "user", "main_node.switched",
            subject=to_agent.agent_id,
            payload={"from": from_agent, "model": model, "handoff": files},
        )
        self.memory.record_decision(
            f"主节点由 {from_agent or '未设置'} 切换为 {to_agent.name}（模型 {model}）",
            approved=True,
        )
        return {"package": package, "files": files}

    @staticmethod
    def to_markdown(package: dict[str, Any]) -> str:
        def bullets(items: list[Any]) -> str:
            if not items:
                return "- （无）"
            return "\n".join(f"- {item}" if not isinstance(item, (dict, list))
                             else f"- `{json.dumps(item, ensure_ascii=False)}`" for item in items)

        parts = [
            "# 主节点交接包",
            f"生成时间：{package.get('generated_at')}",
            f"项目：{package['project']['name']} ({package['project']['root']})",
            f"分支/提交：{package['project']['branch']} / {package['project']['commit']}",
            f"当前阶段：{package.get('current_phase')}",
            "",
            "## 项目目标",
            package.get("project_goal") or "（未设置）",
            "",
            "## 已确认约束",
            bullets(package.get("confirmed_requirements", [])),
            "",
            "## 未完成任务",
            bullets(package.get("open_tasks", [])),
            "",
            "## 已完成任务",
            bullets(package.get("completed_tasks", [])),
            "",
            "## 关键决策",
            bullets(package.get("decisions", [])),
            "",
            "## 被否决方案",
            bullets(package.get("rejected_options", [])),
            "",
            "## 已知风险",
            bullets(package.get("known_risks", [])),
            "",
            "## 变更文件",
            bullets(package.get("changed_files", [])),
            "",
            "## 验证状态",
            bullets([f"{k}: {v}" for k, v in (package.get("verification_status") or {}).items()]),
            "",
            "## 待审批事项",
            bullets(package.get("pending_approvals", [])),
            "",
            "## 推荐下一步",
            bullets(package.get("recommended_next_action", [])),
        ]
        return "\n".join(parts) + "\n"
