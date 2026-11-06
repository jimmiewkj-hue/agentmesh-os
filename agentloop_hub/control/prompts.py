"""Prompt, task-package and result-template generation.

The protocol is intentionally file-based: every agent receives a small, explicit
task package and must return one JSON object. No adapter parses a free-form
chat answer as an acceptance result.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import (
    RESULT_REQUIRED_FIELDS, VERIFICATION_REQUIRED_FIELDS, AgentRecord, Project, Task,
    TaskPlan, TaskType,
)
from ..util import atomic_write_text, ensure_dir, write_json
from .paths import WorkspacePaths


RESULT_TEMPLATE: dict[str, Any] = {
    "task_id": "",
    "agent_id": "",
    "status": "completed",
    "summary": "",
    "findings": [],
    "files_read": [],
    "files_changed": [],
    "commands_run": [],
    "tool_calls": [],
    "tests": [],
    "evidence": [],
    "risks": [],
    "unresolved_items": [],
    "artifacts": [],
    "token_usage": {"input": 0, "output": 0},
    "cost_estimate": {"amount": 0, "currency": "USD", "estimated": True},
    "recommended_next_action": [],
}

PLAN_TEMPLATE: dict[str, Any] = {
    "plan_version": 1,
    "goal": "",
    "constraints": [],
    "requirements": [],
    "nonfunctional": [],
    "risks": [],
    "open_questions": [],
    "tasks": [
        {
            "task_id": "T001",
            "title": "",
            "type": "analysis",
            "role": "",
            "objective": "",
            "suggested_agent": "",
            "dependencies": [],
            "scope": [],
            "out_of_scope": [],
            "expected_outputs": [],
            "acceptance_criteria": [],
            "verification_commands": [],
            "token_budget": 30000,
            "time_limit_minutes": 30,
            "retry_limit": 2,
            "risk_level": "low",
            "approval_policy": "auto",
            "priority": 5,
        }
    ],
}

SUMMARY_TEMPLATE: dict[str, Any] = {
    "summary_version": 1,
    "completion": "",
    "changes": [],
    "verification": "",
    "risks": [],
    "merge_recommendation": "approve",
    "next_actions": [],
    "goal_status": "complete",
    "blocking_reason": "",
}

VERIFICATION_TEMPLATE: dict[str, Any] = {
    "task_id": "",
    "verifier_agent": "",
    "verification_status": "passed",
    "checks": {"result_schema": "passed", "scope_check": "passed"},
    "warnings": [],
    "summary": "",
    "evidence": [],
}


@dataclass
class TaskPackage:
    task_dir: Path
    runtime_dir: Path
    prompt_path: Path
    result_path: Path
    persistent_result_path: Path
    task_json_path: Path


def _result_schema() -> dict[str, Any]:
    return {
        "required_fields": list(RESULT_REQUIRED_FIELDS),
        "status_allowed": ["completed", "partial", "failed"],
        "field_types": {
            "task_id": "string",
            "agent_id": "string",
            "status": "string",
            "summary": "non-empty string",
            "findings": "array",
            "files_read": "array of paths relative to the task workdir",
            "files_changed": "array of paths relative to the task workdir (e.g. calc.py)",
            "commands_run": "array",
            "tool_calls": "array",
            "tests": "array of {command,result}",
            "evidence": "array of {file,lines,description} (or {log,description})",
            "risks": "array",
            "unresolved_items": "array",
            "artifacts": "array of paths",
            "token_usage": "object",
            "cost_estimate": "object",
            "recommended_next_action": "array",
        },
        "rules": [
            "status=completed 时 files_changed、evidence、tests、unresolved_items 必须存在（可为空数组，但字段不可缺失）",
            "不得只返回自然语言“已完成”；必须写 JSON 文件",
            "evidence 必须能定位到文件/行或日志",
            "evidence[].file 必须是本次工作目录内实际存在的相对文件路径；命令输出请写入 tests/commands_run，"
            "不要把 git diff / git status 等命令名写进 evidence.file",
            "files_read / files_changed / evidence[].file 必须是相对本次工作目录（worktree）的路径，"
            "例如 calc.py；不要写绝对路径，也不要写 .agentloop/runs/... 前缀",
            "未执行测试时在 tests 中写明命令与 skipped 原因",
        ],
    }


def _verification_schema() -> dict[str, Any]:
    return {
        "required_fields": list(VERIFICATION_REQUIRED_FIELDS),
        "verification_status_allowed": ["passed", "failed", "warning", "skipped"],
        "rules": [
            "必须独立检查，不得直接复述执行 Agent 的结论",
            "checks 值必须为 passed/failed/warning/skipped",
            "发现回归或范围越界时必须 failed",
        ],
    }


class PromptBuilder:
    def __init__(self, paths: WorkspacePaths, project: Project):
        self.paths = paths
        self.project = project

    # -- task package ------------------------------------------------------
    def write_task_package(self, task: Task, agent: AgentRecord, plan: TaskPlan,
                           memories: list[dict[str, Any]], *,
                           runtime_dir: str | Path,
                           extra_context: dict[str, Any] | None = None,
                           bootstrap: dict[str, Any] | None = None,
                           agent_profile: dict[str, Any] | None = None) -> TaskPackage:
        task_dir = ensure_dir(self.paths.task_dir(task.uid))
        runtime = ensure_dir(runtime_dir)
        task_json_path = task_dir / "task.json"
        persistent_result = task_dir / "result.json"

        # The agent writes inside its own workdir; the control plane copies the
        # result back to the persistent task ledger afterwards.
        runtime_pkg = ensure_dir(runtime / ".agentloop-task") / task.task_id
        ensure_dir(runtime_pkg)
        result_path = runtime_pkg / "result.json"

        package = {
            "protocol_version": 1,
            "task_id": task.task_id,
            "task_uid": task.uid,
            "run_id": task.run_id,
            "project_id": self.project.project_id,
            "project_root": self.project.root,
            "workdir": str(runtime),
            "goal": plan.goal,
            "constraints": plan.constraints,
            "title": task.spec.title,
            "objective": task.spec.objective,
            "type": task.spec.type,
            "scope": task.spec.scope,
            "out_of_scope": task.spec.out_of_scope,
            "dependencies": task.spec.dependencies,
            "input_artifacts": task.spec.input_artifacts,
            "expected_outputs": task.spec.expected_outputs,
            "acceptance_criteria": task.spec.acceptance_criteria,
            "verification_commands": task.spec.verification_commands,
            "token_budget": task.spec.token_budget,
            "time_limit_minutes": task.spec.time_limit_minutes,
            "retry_limit": task.spec.retry_limit,
            "risk_level": task.spec.risk_level,
            "priority": task.spec.priority,
            "agent": {
                "agent_id": agent.agent_id, "name": agent.name, "product": agent.product,
                "access_level": agent.access_level,
            },
            "memories": memories,
            "bootstrap": bootstrap or {},
            "agent_profile": agent_profile or {},
            "extra_context": extra_context or {},
            "result_path": str(result_path),
            "result_schema": _result_schema(),
        }
        write_json(task_json_path, package)
        write_json(task_dir / "result-schema.json", _result_schema())
        write_json(task_dir / "result-template.json", {
            **RESULT_TEMPLATE, "task_id": task.task_id, "agent_id": agent.agent_id,
        })
        write_json(task_dir / "context.json", {
            "memories": memories, "extra": extra_context or {},
        })
        self._copy_inputs(task, task_dir)

        prompt = self._task_prompt(task, agent, package)
        prompt_path = task_dir / "prompt.md"
        atomic_write_text(prompt_path, prompt)

        # Runtime copy inside the workdir so CLI agents can read it without
        # leaving their sandbox. Excluded from change capture.
        for name in ("task.json", "prompt.md", "result-schema.json",
                     "result-template.json", "context.json"):
            src = task_dir / name
            if src.exists():
                shutil.copy2(src, runtime_pkg / name)
        if (task_dir / "inputs").exists():
            ensure_dir(runtime_pkg / "inputs")
            for item in (task_dir / "inputs").iterdir():
                if item.is_file():
                    shutil.copy2(item, runtime_pkg / "inputs" / item.name)
        runtime_prompt = runtime_pkg / "prompt.md"
        return TaskPackage(
            task_dir=task_dir, runtime_dir=runtime_pkg,
            prompt_path=runtime_prompt, result_path=result_path,
            persistent_result_path=persistent_result, task_json_path=task_json_path,
        )

    def _copy_inputs(self, task: Task, task_dir: Path) -> None:
        if not task.spec.input_artifacts:
            return
        inputs = ensure_dir(task_dir / "inputs")
        for raw in task.spec.input_artifacts:
            p = Path(raw)
            if not p.is_absolute():
                p = Path(self.project.root) / raw
            if p.exists() and p.is_file():
                shutil.copy2(p, inputs / p.name)

    def _task_prompt(self, task: Task, agent: AgentRecord, package: dict[str, Any]) -> str:
        scope = "\n".join(f"- {s}" for s in task.spec.scope) or "- （只读任务，不修改文件）"
        out_scope = "\n".join(f"- {s}" for s in task.spec.out_of_scope) or "- 无额外禁止范围"
        criteria = "\n".join(f"- {s}" for s in task.spec.acceptance_criteria) or "- 按成果包中的证据说明完成情况"
        commands = "\n".join(f"- `{c}`" for c in task.spec.verification_commands) or "- 无"
        expected = "\n".join(f"- {s}" for s in task.spec.expected_outputs) or "- 结构化成果包"
        memory_text = json.dumps(package.get("memories") or [], ensure_ascii=False, indent=2)
        upstream = json.dumps(
            (package.get("extra_context") or {}).get("upstream_results") or [],
            ensure_ascii=False, indent=2,
        )
        bootstrap = json.dumps(package.get("bootstrap") or {}, ensure_ascii=False, indent=2)
        profile = package.get("agent_profile") or {}
        profile_text = (
            f"- 上下文长度配置：{profile.get('context_length') or '默认'}\n"
            f"- 能力等级：{profile.get('capability_level') or '默认'}"
        )
        round_context = json.dumps(
            (package.get("extra_context") or {}).get("round_context") or {},
            ensure_ascii=False, indent=2,
        )
        return f"""# AgentMesh OS 任务包

你正在作为子节点执行任务，Agent：{agent.name}（{agent.agent_id}）。

{profile_text}

## 目标
{task.spec.objective}

## 项目与工作目录
- 项目根目录：{self.project.root}
- 本次工作目录：{package['workdir']}
- 任务编号：{task.task_id}
- 任务类型：{task.spec.type}

## 允许修改的范围（scope）
{scope}

## 禁止修改的范围（out_of_scope）
{out_scope}

## 必须遵守
- 只能修改 scope 内文件；不得访问或写入项目目录之外的路径。
- 所有文件读写都以“本次工作目录”为根：项目根目录仅作只读参考，禁止把修改直接写到项目根；控制平面只采集本次工作目录内的变更。
- 不得读取、输出或写入密钥、凭据、令牌；如任务疑似涉及，立即停止并在结果中标记 unresolved。
- 不得删除文件、修改依赖锁文件、执行发布/部署/强制推送等高风险操作；如确有必要，写入 unresolved 并说明。
- 不得把完整日志或大量源码粘贴到结果；结果只保留摘要、证据定位和必要片段。
- 不得只回复“已完成”。必须写入 JSON 成果包。

## 验收条件
{criteria}

## 建议验证命令
{commands}

## 期望产物
{expected}

## 可用记忆（只作为背景，不确定时以当前代码为准）
```json
{memory_text}
```

## 统一记忆引导包（项目状态）
```json
{bootstrap}
```

## 多轮任务上下文
若 `round` 大于 1，当前工作区已通过依赖补丁包含上一轮成果；只修复 listed unresolved 项，不要重复已完成工作。
```json
{round_context}
```

## 上游成果摘要（result sync，不是完整上下文）
只接收依赖任务的压缩摘要；不要假设未列出的细节。
```json
{upstream}
```

## 输出要求（强制）
将符合 `result-schema.json` 的 JSON 对象写入：

`{package['result_path']}`

成果包必须包含以下字段：
`{", ".join(RESULT_REQUIRED_FIELDS)}`

字段语义：
- status：completed / partial / failed
- summary：一句话结论
- findings：发现列表（含严重级别与定位）
- files_read / files_changed：相对**本次工作目录（worktree）**的路径，例如 `calc.py`；
  不要写绝对路径，也不要写 `.agentloop/runs/...` 前缀
- commands_run：实际执行的命令
- tool_calls：工具调用记录
- tests：`[{{"command": "...", "result": "passed|failed|skipped", "output": "..."}}]`
- evidence：`[{{"file": "...", "lines": "10-20", "description": "..."}}]`，
  其中 `file` 必须是**本次工作目录内**实际存在的相对文件路径（例如 `calc.py`）；
  命令输出请放在 `tests` 或 `commands_run`，不要把 `git diff`、`git status` 等命令字符串写进 `file`。
- risks / unresolved_items：剩余风险与未解决项
- artifacts：产物路径（补丁、报告、日志）
- token_usage / cost_estimate：可填你实际知道的值；不知道时如实写 0
- recommended_next_action：建议的下一步

完成后先自检：任务目标、范围、验收条件是否逐项满足；证据是否可定位；未解决的问题是否列出。
"""

    # -- planning ----------------------------------------------------------
    def write_plan_request(self, *, plan_id: str, goal: str, main_agent: AgentRecord,
                           model: str, context: dict[str, Any],
                           output_path: Path | None = None,
                           workspace: str = "", gui_mode: bool = False,
                           round_context: dict[str, Any] | None = None) -> tuple[Path, Path]:
        directory = ensure_dir(self.paths.planning / plan_id)
        prompt_path = directory / "plan_request.md"
        plan_path = directory / "plan.json"
        target_path = Path(output_path) if output_path else plan_path
        write_json(directory / "plan-template.json", PLAN_TEMPLATE)
        write_json(directory / "plan-schema.json", {
            "required": ["plan_version", "goal", "tasks"],
            "optional": ["constraints", "requirements", "nonfunctional", "risks", "open_questions"],
            "task_required": [
                "task_id", "title", "type", "objective", "dependencies", "scope",
                "out_of_scope", "expected_outputs", "acceptance_criteria",
                "verification_commands", "token_budget", "time_limit_minutes",
                "retry_limit", "priority",
            ],
            "task_types": [t.value for t in TaskType],
            "rules": [
                "task_id 唯一且无环依赖",
                "type=implementation/documentation 的任务必须有非空 scope",
                "type=architecture 只产出架构判断；若需落地文档请另建 documentation 任务",
                "每个任务只做一件事，可独立验收",
                "role 取 explore/research/implement/review/write/verify；必须与 suggested_agent 的协作契约一致",
                "priority 0..10，越大越先执行；依赖仍优先于优先级",
                "不得把需要人工审批的高风险操作写成自动执行",
            ],
        })
        agents_text = json.dumps(context.get("agents", []), ensure_ascii=False, indent=2)
        memory_text = json.dumps(context.get("memories", []), ensure_ascii=False, indent=2)
        conversations = json.dumps(context.get("recent_conversations", []), ensure_ascii=False, indent=2)
        collaboration = context.get("collaboration") or {}
        collab_text = json.dumps(collaboration, ensure_ascii=False, indent=2)
        collab_block = ""
        if collaboration.get("sub_agents"):
            sub_names = ", ".join(
                f"{a.get('agent_id')}（{a.get('model')}）"
                for a in collaboration.get("sub_agents") or []
            )
            collab_block = f"""## 协作契约（强制）
用户已明确本次只用下列执行者，请严格按角色分配 `suggested_agent` 与 `role`：

```json
{collab_text}
```

- 主节点 `{collaboration.get('main_agent_id')}` 只负责 plan / review / summarize；
  不要给它分配 implementation / documentation / verification / analysis 任务。
- 子 Agent 只允许：{sub_names}。
- role=explore / research / implement / write / verify 的任务必须交给子 Agent；
  role=review 的任务交给主节点。
- 每个任务都要填写 `role`（explore/research/implement/review/write/verify），
  并把 `suggested_agent` 写成上面真实存在的 agent_id。
- 任务数量控制在 6-8 个；不要把同一目标拆成多个重复实现任务。
"""
        gui_block = ""
        if gui_mode:
            gui_block = f"""## GUI 执行方式（重要）
- 你正在通过 GUI Agent 执行本规划请求；工作区是 `{workspace}`。
- 本步骤只做规划：不要创建实现文件，不要运行构建或测试。
- 把计划 JSON 写入 `{target_path}`（该路径位于工作区内）。
- 在最终聊天回复中再给出一个完整 JSON 代码块（以 ```json 开始），不要输出其他解释文字。
"""
        round_block = ""
        if round_context:
            round_block = f"""## 多轮续跑上下文（强制）
这是第 {round_context.get('round', '?')} / {round_context.get('max_rounds', '?')} 轮。
上一轮没有满足目标，不要重复已经完成的工作。只规划解决下列未解决项所需的最小任务图；
可写任务必须给出最小必要 scope，并依赖上一轮相关任务（系统会自动补上上轮补丁依赖）。

```json
{json.dumps(round_context, ensure_ascii=False, indent=2)}
```
"""
        prompt = f"""# AgentMesh OS 主节点规划请求

你是用户指定的主节点 Agent：{main_agent.name}（{main_agent.agent_id}），模型：{model or "用户指定"}。

## 用户目标
{goal}

## 项目事实
```json
{json.dumps(context.get("project", {}), ensure_ascii=False, indent=2)}
```

## 可用记忆
```json
{memory_text}
```

## 最近会话摘要（只读，可能不完整）
```json
{conversations}
```

## 可调度 Agent
```json
{agents_text}
```

{collab_block}
{round_block}
## 规划规则
- 只输出任务图，不要直接执行任务。
- 任务粒度应可独立验收；实现类任务必须写清允许修改的 scope。
- 依赖必须引用已存在的 task_id，禁止环。
- 每个任务给出 token_budget、time_limit_minutes、retry_limit、acceptance_criteria 与 verification_commands。
- analysis 任务只做只读检查：verification_commands 只能包含只读命令（如 grep/git status）或留空；禁止携带 pytest/unittest/npm test 等测试类命令，测试命令只能放在 implementation 或 verification 任务。
- 优先把大规模扫描/检索/试错交给子节点，把判断与验收留在主节点。
- 不虚构项目事实；不确定时在任务中写明“先核实”。

## 输出（强制）
{gui_block}
将符合计划的 JSON 写入：

`{target_path}`

必填结构：
```json
{json.dumps(PLAN_TEMPLATE, ensure_ascii=False, indent=2)}
```

完成后不要修改任何项目代码，只写上述文件。
"""
        atomic_write_text(prompt_path, prompt)
        return prompt_path, plan_path

    # -- summary -----------------------------------------------------------
    def write_summary_request(self, *, run_id: str, main_agent: AgentRecord, model: str,
                              context: dict[str, Any], output_path: Path | None = None,
                              workspace: str = "", gui_mode: bool = False) -> tuple[Path, Path]:
        directory = ensure_dir(self.paths.run_dir(run_id) / "summary")
        prompt_path = directory / "summary_request.md"
        summary_path = directory / "summary.json"
        target_path = Path(output_path) if output_path else summary_path
        write_json(directory / "summary-template.json", SUMMARY_TEMPLATE)
        gui_block = ""
        if gui_mode:
            gui_block = f"""## GUI 执行方式（重要）
- 你正在通过 GUI Agent 执行汇总；工作区是 `{workspace}`。
- 把汇总 JSON 写入 `{target_path}`（该路径位于工作区内）。
- 在最终聊天回复中再给出一个完整 JSON 代码块（以 ```json 开始），不要输出其他解释文字。
"""
        prompt = f"""# AgentMesh OS 主节点汇总请求

你是主节点 Agent：{main_agent.name}（{main_agent.agent_id}），模型：{model or "用户指定"}。

你只收到以下经过压缩的上下文，禁止假设未提供的内容：

```json
{json.dumps(context, ensure_ascii=False, indent=2)}
```

## 要求
- 判断每个任务是否真正满足验收条件，不以“Agent 声称完成”为依据。
- 汇总修改内容、验证结论、剩余风险、是否建议合并。
- 这是合并前的预汇总：不要因为“主工作区尚未应用补丁/仍处于基线”而判定失败；补丁应用与后置验证由合并阶段负责，合并后控制平面会重写最终汇总。
- 若有未解决项或任务验证失败，merge_recommendation 必须为 reject。
- `goal_status` 只能取 complete / continue / blocked：
  * 只有全部验收条件真正满足、证据充分时才写 complete；
  * 目标尚未完成、还有可执行修复工作，写 continue，并把修复项逐条写入 next_actions；
  * 只有同一阻塞条件已经连续多轮无法推进时才写 blocked，并在 blocking_reason 写具体阻塞条件。
    难度、不确定、还有剩余工作都不算 blocked。
- next_actions 会被控制平面用于自动规划下一轮；必须是可执行、最小、带文件/行为定位的动作，不要写“继续优化”这类空泛描述。
- 多轮上下文会以 `round` 字段提供；同一阻塞条件重复时控制平面会提前停止，不要重复上一轮已经验证失败的方案。

## 输出（强制）
{gui_block}
将以下结构的 JSON 写入：

`{target_path}`

```json
{json.dumps(SUMMARY_TEMPLATE, ensure_ascii=False, indent=2)}
```
"""
        atomic_write_text(prompt_path, prompt)
        return prompt_path, summary_path

    # -- independent verification -----------------------------------------
    def write_verification_request(self, *, run_id: str, task: Task, verifier: AgentRecord,
                                   model: str, patch_path: str, result_summary: dict[str, Any],
                                   verification_commands: list[str]) -> tuple[Path, Path]:
        directory = ensure_dir(self.paths.run_dir(run_id) / "verification" / task.task_id)
        prompt_path = directory / "verification_request.md"
        report_path = directory / "verification.json"
        write_json(directory / "verification-template.json", VERIFICATION_TEMPLATE)
        write_json(directory / "verification-schema.json", _verification_schema())
        commands = "\n".join(f"- `{c}`" for c in verification_commands) or "- 无（请按验收条件人工检查）"
        prompt = f"""# AgentMesh OS 独立验证请求

你是独立验证节点：{verifier.name}（{verifier.agent_id}），模型：{model or "用户指定"}。
你不是执行节点；执行节点是另一个 Agent。必须独立检查，不得直接采信执行结论。

## 待验证任务
- task_id：{task.task_id}
- 目标：{task.spec.objective}
- 执行 Agent：{task.assigned_agent}
- 修改范围：{json.dumps(task.spec.scope, ensure_ascii=False)}
- 禁止范围：{json.dumps(task.spec.out_of_scope, ensure_ascii=False)}
- 验收条件：{json.dumps(task.spec.acceptance_criteria, ensure_ascii=False)}
- Patch：{patch_path or "（无补丁）"}

## 执行 Agent 的成果摘要（不可作为验收依据）
```json
{json.dumps(result_summary, ensure_ascii=False, indent=2)}
```

## 建议验证命令
{commands}

## 检查边界
- 只检查任务执行 worktree / 提供的 Patch 与其基线；不要用主工作区当前状态判断（合并前主工作区本来未应用补丁）。
- 忽略 `__pycache__`、`*.pyc`、`.pytest_cache` 等被 `.gitignore`/excluded 的文件，它们可能是验证命令自身产生的副产物。
- 只读分析任务如果建议验证命令导致失败，应按“信息性”记录，不要把“修复前测试失败”当成执行缺陷。

## 必须检查
1. Patch 是否能应用到基线？
2. 修改是否超出范围？
3. 构建/测试是否通过？
4. 是否满足验收条件？
5. 是否存在明显回归？
6. 成果包中的证据是否可定位？

## 输出（强制）
将验证报告 JSON 写入：

`{report_path}`

字段：{", ".join(VERIFICATION_REQUIRED_FIELDS)}，
verification_status ∈ passed/failed/warning/skipped，checks 为对象，warnings 为数组。
"""
        atomic_write_text(prompt_path, prompt)
        return prompt_path, report_path
