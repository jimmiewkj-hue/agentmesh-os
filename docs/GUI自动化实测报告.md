# GUI 自动化实测报告（2026-11-03）

## 1. 环境与对象

- OS：Windows 11 x64；工作区：`j:\qoder\agent-hub`
- 主 Agent：`qoder_cn` / `DeepSeek-Flash`（Qoder CN 0.5.1，CLI 1.1.79-hotfix.7）
- 子 Agent：`workbuddy` / `GLM-5.3-Flash`（WorkBuddy 37.10.3-43，本地服务 2.165.0）
- 独立验证：控制平面确定性验证；可选 opencode 独立验证
- 测试项目：`logs/gui-e2e-20261103/project`（独立 git 仓库，初始 `calc.py` 的 `add` 返回 `a-b`）

## 2. 成功闭环

| 阶段 | 执行者 | 结果 | 证据 |
| --- | --- | --- | --- |
| 选择 | 用户/控制面 | 主 `qoder_cn` + `DeepSeek-Flash`；子 `workbuddy` + `GLM-5.3-Flash` | `main.json`、`model-selection.json` |
| 规划 | `qoder_cn` / `DeepSeek-Flash` | `plan_f17e57c9eb65`，1 个 implementation 任务，`suggested_agent=workbuddy` | `plan-response.json`、`planning/plan_f17e.../plan.json` |
| 分发 | 控制面 | `run_6eb19690a433:T001` → `workbuddy` | `run-result.json` |
| 执行 | `workbuddy` / `GLM-5.3-Flash` | worktree 内 `calc.py` 单行修复；`python -m pytest -q` 2 passed | 任务 `result.json`、`gui/before.png`、`gui/after.png` |
| 验证 | 控制平面 | `verification_status=passed`；范围/证据/测试复跑通过 | `verifications` 表、`final-summary.json` |
| 验收 | 用户门禁 | `apr_7e03de8bb86e` approved | `final-summary.json` |
| 合并 | 控制面 | Patch 应用到干净基线；commit `e9c35b52b2d109768b876f56a022272cacd19f82` | `merge-result.json`、fixture `git log` |
| 发布 | 用户门禁 | `apr_c9621553813b` approved；run `released` | `release.json`、`final-summary.json` |

### 用量与耗时

| 环节 | Agent / 模型 | Tokens | Cost (credits) | 耗时 |
| --- | --- | ---: | ---: | ---: |
| 规划 | qoder_cn / DeepSeek-Flash | 2,310 | 0.168067 | 49.7s |
| 执行 | workbuddy / GLM-5.3-Flash | 54,311（输入 53,694 / 输出 617 / 缓存 52,864 / 推理 149） | 0.27 | 273.9s |
| 独立验证（未产出报告） | opencode / opencode-go/beam-free | 829 | 0 | 4.2s |
| 汇总 | qoder_cn / DeepSeek-Flash | 982 | 0.105753 | 22.0s |
| **运行合计** | — | **56,122** | **0.375753** | **300.0s** |

- 任务工具调用 14 次；运行总 tool_calls 16。
- 最终项目工作区 `git status` 干净；`calc.py` 为 `return a + b`；`python -m pytest -q` 2 passed。
- 运行状态：`released`；合并 commit：`e9c35b52b2d109768b876f56a022272cacd19f82`。

## 3. 过程中发现并修复的问题

1. **WorkBuddy 回合完成判定过早**
   - 现象：第一条 assistant message `status=completed` 后仍有 function_call/result；适配器提前 detach，任务被判无文件变更。
   - 修复：`WorkBuddyReader` 改为“最后一条有意义事件是 assistant completed 且静默 10s”或 `sessions.status=completed` 才结束；否则继续等待。GUI 日志显示回合实际持续约 4.5 分钟、14 次工具调用。
   - 证据：`gui-adapter-smoke-workbuddy-20261103`、`run_5911de837224_T001`。
2. **成果包路径语义不清**
   - 现象：WorkBuddy 把 `.agentloop/runs/.../worktrees/T001/calc.py` 写入 `files_changed`；验证器判越界。
   - 修复：任务提示明确 `files_read/files_changed/evidence.file` 必须是**本次 worktree 相对路径**（如 `calc.py`），禁止 `.agentloop/...` 前缀与绝对路径。
3. **evidence 混入命令字符串**
   - 现象：`evidence.file` 写成 `git diff -- calc.py`，验证器判“文件不存在”。
   - 修复：schema/任务提示要求 `evidence.file` 只能是真实存在的相对文件；命令输出放 `tests`/`commands_run`。
4. **路由评分压过 suggested_agent**
   - 现象：历史失败统计让 opencode 分数高于 WorkBuddy；`suggested_agent` 仅 +0.03 不足以稳定选中。
   - 处理：测试按用户显式选择的子 Agent，将任务的 `assigned_agent` 固定为 `workbuddy`；Agent 池与预算保持默认，未裁剪、未手工加预算。
5. **工作区脏状态阻塞合并**
   - 现象：前一轮 GUI 任务误改到项目主工作区，导致 merge preflight 报 `用户工作区中存在与 Patch 重叠的未提交修改，拒绝覆盖`。
   - 处理：恢复 fixture 到基线后重试合并成功；这是测试环境前置状态，不是合并逻辑缺陷。
6. **独立验证 Agent 未产出报告**
   - opencode 独立验证调用失败（exit 1），控制平面确定性验证仍 `passed`；不伪造独立验证结论。

## 4. 证据路径

- 总日志：`logs/gui-e2e-20261103/e2e.log`
- 运行汇总：`logs/gui-e2e-20261103/final-summary.json`、`merge-result.json`、`release.json`
- 任务成果：`logs/gui-e2e-20261103/project/.agentloop/tasks/run_6eb19690a433_T001/result.json`
- 任务 GUI 截图：同目录 `gui/before.png`、`gui/after.png`、`gui/gui.log`
- 规划 GUI 截图：`.agentloop/planning/plan_f17e57c9eb65/gui/`
- 汇总 GUI 截图：`.agentloop/runs/run_6eb19690a433/summary/gui/`
- 逆向证据：`logs/reverse-20261103/`
- 适配器 smoke：`logs/gui-adapter-smoke-20261103/`、`logs/gui-adapter-smoke-workbuddy-20261103/`

## 5. 结论

- 11 类 Agent 注册齐全；Qoder CN 与 WorkBuddy 已按真实能力登记为 `gui_automation`，`auto_executable=true`。
- Qoder CN 真实模型键 `dfmodel` / 显示名 `DeepSeek-Flash`；WorkBuddy 真实模型键 `glm-5.3-flash` / 显示名 `GLM-5.3-Flash`。
- `qoder_cn` 主 + `workbuddy` 子组合已真实跑通：选择 → 分发 → 执行 → 验证 → 验收 → 合并 → 发布，最终 run `released`。
- 工程测试：**146 passed**（139 基线 + 7 个 GUI/reader 测试）。
