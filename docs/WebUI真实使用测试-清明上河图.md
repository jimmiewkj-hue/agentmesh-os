# WebUI 真实使用测试：《清明上河图》三维网页（2026-11-04）

本记录对应一次从 WebUI 出发的真实使用测试：工作区选择 → Trae CN 主节点规划 → WorkBuddy 子节点执行 → 主节点审阅/汇总 → 浏览器验收。所有结论都来自实际运行；完整原始日志在 `logs/ui-e2e-20261104/`（`server.log`、`test-notes.md`、各任务 `gui.log` 与截图）。

## 1. 测试配置

| 项 | 值 |
| --- | --- |
| 工作目录 | `D:\temp`（预先存在，含 61GB `ai-stack`，未删除、未提交） |
| 主 Agent | Trae CN，模型 `deepseek-v4.1-flash`（界面显示 `DeepSeek-V4.1-Flash`，x0.12） |
| 子 Agent | 仅 WorkBuddy，模型 `GLM-5.3-Flash`（x0.08） |
| 启动方式 | `python -u -m agentloop_hub serve --host 127.0.0.1 --port 8801`，stdout/stderr 追加到 `logs/ui-e2e-20261104/server.log` |
| 零输入 | `AGENTMESH_DISABLE_GUI_INPUT=1`；Trae 走 CDP DOM，WorkBuddy 走深链接 + CDP DOM，未合成 OS 键鼠 |
| 浏览器操作 | browser-use（Qoder 内置浏览器）；桌面观察/权限确认用 computer-use |

计划结构（Trae 生成后由协作契约固化，`plan_995df76a54d9`）：

| 任务 | 角色 | 执行者 | 结果 |
| --- | --- | --- | --- |
| T001 环境与技术基线 | explore | workbuddy / GLM-5.3-Flash | completed（warning：只读任务无测试命令） |
| T002 画面考据与推定边界 | research | workbuddy / GLM-5.3-Flash | completed，产出 `reference-notes.md` |
| T003 最小三维骨架 | implement | workbuddy / GLM-5.3-Flash | 执行完成，但隔离验证因 npm 命令失败而 failed |
| T004 程序化资产 | implement | workbuddy / GLM-5.3-Flash | 取消（模型节流 + 长任务超时） |
| T005 场景装配/细节 | implement | workbuddy | blocked（依赖 T004） |
| T006 文档 | write | workbuddy | 未执行（依赖被阻塞） |
| T007 浏览器验收 | verify | workbuddy | 未执行（依赖被阻塞） |
| T008 主节点终审 | review | trae_cn / deepseek-v4.1-flash | 原运行未执行；后用 `run_3a57e9e6953b` 单独补做 |

主节点负责 plan / review / summarize；子节点只承担 explore / research / implement / write / verify。

## 2. 实测暴露并修复的问题

### 2.1 非空目录初始化没有初始提交

- 现象：`D:\temp` 已存在且非空、非 Git；WebUI「初始化项目」只执行 `git init`，页面显示 `no branch`，Worktree 无法创建。
- 修复：`agentloop_hub/workspace/init.py` 对已有非空目录创建**空初始提交**，不暂存用户内容；`tests/test_workspace_manager.py` 新增回归用例。
- 证据：`715ad9c chore: initialize AgentMesh workspace`；`git ls-files` 为空，`ai-stack` 未被跟踪。

### 2.2 GUI Agent 的规划/汇总结果路径在工作区外

- 现象：Trae 只在 workspace 内写文件，`plan.json`/`summary.json` 放在项目 `.agentloop/` 下，导致规划阶段写不出结果。
- 修复：
  - `control/prompts.py` 的规划/汇总 prompt 支持 GUI 模式，输出路径改为 workspace 内 `plan.json` / `summary.json`，并要求最终聊天回复含 JSON 代码块。
  - `control/planning.py`、`control/runner.py` 在 GUI 模式下使用 workspace 内路径，执行后复制回 canonical 路径。
  - `adapters/gui.py` 的短提示加入“只做规划/汇总、把 JSON 写入工作区”的约束。

### 2.3 TraeReader 过早判定完成

- 现象：Trae 回复仅 97 字符、0 事件；读取器把用户消息回显/进度行当成最终回复。
- 修复：`gui/trae_reader.py` 必须 `assistant_task_status=finished` 且消息增长；新增“结果文件为完整合法 JSON 即完成”的更强信号（`completion_path`）。
- 相关：WorkBuddy 同样支持结果文件完成信号（`gui/readers.py`），避免会话状态长期 `working/pending` 时假死。

### 2.4 计划中的角色词被写进 TaskType

- 现象：Trae 输出 `"type": "research"`、`"approval_policy": "manual"`，计划校验失败。
- 修复：`control/collaboration.py` 的 `normalize_plan_payload` 把 research/explore/implement/write/verify 等角色词映射到合法 TaskType，把 `manual` 等审批策略归一为 `required`，非法 risk 归一为 `medium`。
- 注意：子角色的 TaskType 保留规划者的语义；只有主节点的任务被限制为 review/analysis。研究任务写参考文档时仍应保留 `documentation` 类型，否则补丁会丢失该文档。

### 2.5 子 Agent 白名单与角色归属

- 现象：初始计划把实现任务分给主 Agent、OpenCode、Qoder CN，与“只用 WorkBuddy”冲突。
- 修复：`control/collaboration.py` 从 WebUI 已选配模型的 Agent 推导协作契约：
  - 主节点：plan / review / summarize；
  - 子节点：explore / research / implement / write / verify；
  - 任务写入 `role`、`suggested_agent`、`model`，审计 `plan.collaboration_enforced`。
- 当前计划最终为 8 个任务，T001–T007 指向 WorkBuddy，T008 指向 Trae CN。

### 2.6 同一 GUI Agent 的并行任务互相覆盖

- 现象：T003/T004 同时启动，两个深链接争抢同一个 WorkBuddy 输入框。
- 修复：`control/runner.py` 新增按 Agent 的 GUI 串行锁；不同 Agent 仍可并行。新增 `test_downstream_task_sees_upstream_patch_in_worktree` 覆盖工作区组合，不改变并行语义。

### 2.7 上游补丁未进入下游 Worktree

- 现象：每个任务都从 `run.base_commit` 新建 Worktree，T004/T005 看不到 T002/T003 的产物；多步构建链退化为空目录。
- 修复：
  - `runner._apply_upstream_patches` 在任务执行前按依赖顺序应用上游补丁，并提交为 Worktree 基线，任务自身补丁只含本任务改动。
  - `control/verification.py` 在隔离验证 Worktree 中也应用上游补丁，保证验证命令能看到上游文件。
  - 新增 `test_downstream_task_sees_upstream_patch_in_worktree`。

### 2.8 暂停运行的 `TaskStatus.READY` 崩溃

- 现象：暂停时抛 `AttributeError: type object 'TaskStatus' has no attribute 'READY'`，队列任务未标记 paused。
- 修复：只匹配 `QUEUED`；新增 `test_paused_run_marks_queued_tasks_paused`。

### 2.9 高风险审批通过后任务不自动恢复

- 现象：T003/T004 因 `package-lock.json` 高风险等待审批；批准后仍停在 `waiting_user`，需要手动 retry。
- 修复：`hub.decide_approval` 在批准 high_risk 且任务为 `waiting_user` 时自动 requeue；`test_high_risk_task_waits_for_approval_then_resumes` 增加断言。

### 2.10 WorkBuddy 预填校验误判（关键）

- 现象一：深链接已把 211 字符任务文本放进输入框，但 `COMPOSER_TEXT_JS` 选中了欢迎页的另一个 contenteditable（25 字占位符），于是拒绝提交并空点发送。
- 现象二：失败重试只重发深链接，不清理 WorkBuddy 当前任务状态；连续失败。
- 修复：
  - `gui/cdp.py` 收集所有可见 contenteditable/textarea，取文本最长者，并记录 tag/class；
  - `adapters/gui.py` 预填必须包含本次唯一 marker，失败时重发深链接；必要时强制重启 WorkBuddy，等待 3 秒稳定后再发；
  - WorkBuddy 长命令 stall 阈值默认提高到 600s（`workbuddy_stall_seconds`）。
- 证据：T003 重跑 `prefill.chars=211 tag=DIV cls=_editable_1cxd2_1`，发送成功；`test_cdp_rejects_placeholder_prefill_when_marker_expected` 覆盖占位符拒绝。

### 2.11 失败重试自动换模型，违背用户钉住的模型

- 现象：T003/T004 失败重试后，GLM-5.3-Flash 被角色阶梯升级为 `deepseek-v4-flash`。
- 修复：`runner._escalate_task` 对“协作契约计划中已显式指定 model 的角色任务”不再升级；普通 `solve` 角色阶梯仍保留升级。新增协作契约标记注释；`test_solve_escalates_a_failed_task_to_the_next_ladder_rung` 继续通过。

### 2.12 项目补丁被 node_modules/dist 污染

- 现象：`git add -N` 会把 `node_modules`、`dist`、`.tmp-pkg` 纳入任务补丁，导致范围校验失败/补丁巨大。
- 修复：`generate_patch` 默认排除控制目录、`**/node_modules`、`**/dist`、`**/.tmp-pkg`、`**/.vite`，并在生成前 `git reset` 清理陈旧 intent-to-add；测试 Worktree 另在 `.git/info/exclude` 忽略这些目录。T003 补丁从 1564 个文件降为 14 个源码/配置文件。

### 2.13 WorkBuddy 长任务与模型节流

- T001/T002 正常（T002 约 9 分钟产出 24KB 考据笔记）。
- T004 在 `common.py` 后长时间“生成回复中”，20 分钟无新文件；为保证测试收口，取消 T004，T005 因此 blocked。
- T003 补齐骨架后在 WorkBuddy 内完成 Vite 构建、无头 Chrome 截图、修复 THREE.Clock/PCFSoftShadowMap 警告；但控制平面隔离验证执行 `npm install / npm run build / npx vite preview` 仍因 Node 24 + rolldown 原生绑定与长驻 preview 失败，任务被标记 failed。
- 结论：Agent 自测通过不等于隔离验证通过；验证器需要支持“复制依赖/跳过安装与服务型命令”，这是后续改进项。

## 3. 交付物与浏览器验收

交付目录：`D:\temp\qingming-3d`（Git 提交 `208ec70`）。

```text
qingming-3d/
├── dist/              # 已构建静态页面（JS 571,990 B / CSS 2,865 B）
├── src/               # 渲染器/OrbitControls/机位/画质/地形/虹桥/市井/HUD
├── docs/reference-notes.md
├── docs/THREE-LICENSE.txt
├── README.md
├── serve.py           # 本地静态服务器（127.0.0.1:8098）
├── package.json / package-lock.json / vite.config.js
└── index.html
```

浏览器（browser-use）实测：

```text
URL http://127.0.0.1:8098/
✓ 首帧可见：汴河、两岸地形、编木虹桥拱架、屋舍占位体块、树、HUD
✓ 预设机位：全景俯瞰 / 虹桥近观 / 汴河顺流 / 南岸市井
✓ 画质档位：高 / 中 / 低，HUD 同步
✓ OrbitControls 环绕/缩放/平移
✓ 控制台无 error / warn
```

主节点终审：Trae CN 在 `run_3a57e9e6953b` 中打开 `http://localhost:8098/`，用 Browser Use Agent 实际查看页面并读取 22 个文件。R001 成功完成且控制平面 verification=passed，但主节点 summary 给出 **merge_recommendation=reject**：

- R001 的 `result.json` 中 `findings=[]`、`files_read=[]`、`tests=[]`，summary 字段是 GUI 聊天转录（“--> 展示更多 / 已读取 22 个文件…”），没有可定位的代码问题与“通过/有条件通过/不通过”结论；
- evidence 使用了 `state.vscdb` 与 `.agentloop` 绝对路径，不是交付项目内的文件/行号；
- 因而主节点不能仅凭“Agent 声称完成”判定通过。

这是一个**有效的否定审阅**：页面本身已由 browser-use 独立验证可运行，但 Trae 的审阅成果包不满足证据契约。该结果按原样保留，不伪装为通过；改进方向是让 GUI task 结果也强制写结构化 `findings`/`files_read`/`tests`。

## 3.1 完全控制（后续修复）

本轮真实使用中，GUI Agent 对工作区外文件/命令/写入的确认会阻塞对话。后续已新增 WebUI「完全控制」复选框（默认勾选），并在展示层通过各 Agent 自身接口自动接受：见 [完全控制与权限自动接受](完全控制与权限自动接受.md)。

## 4. 如实说明的边界

- 交付物是**骨架/最小可运行场景**，不是“完美还原”或“考古级复原”。
- 精细资产（船、人物、窗棂、瓦缝、幌子、竹篮、陶器、街巷层次）未完成；对应 T004/T005 因模型节流与超时取消/阻塞。
- T003 的隔离验证失败（npm 命令环境）与原运行 `run_ec2d66ccffb0` 的失败状态均已保留，未伪装为通过。
- 后续建议：
  1. 验证器支持复制任务 Worktree 的 `node_modules`，并把 `npm install` 与 `vite preview` 作为 setup/长驻命令跳过；
  2. 为 GUI 任务增加“取消即终止 Agent 会话”的硬中断；
  3. 增加 asset build 的断点与增量执行，避免 60 分钟单任务；
  4. 将静态 import map 作为无构建回退，减少对 Vite 原生绑定的依赖。
