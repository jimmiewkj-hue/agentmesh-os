# AgentMesh OS

一个统一编排和管理本地多 Agent 的软件中枢：跨 Agent 上下文共享、任务协作、记忆同步、能力路由与成本优化。用户只面对一个入口；其余 Agent 全部降级为可调用资源。

> 本项目的设计基线是 `01-需求整理-v1.docx`。代码包名为 `agentloop_hub`（历史兼容），产品名与 CLI 为 **AgentMesh OS / `agentmesh`**。

## 定位：Controller，而不是主 Agent

```text
用户
 │
 ▼
统一 Web 指挥中心（单一入口：项目 → Prompt → 主/子 Agent 与模型 → 审批/运行 → 实时监控）
 │
 ▼
AgentMesh Controller（确定性软件，不调用 LLM 做控制决策）
 ├── Agent Model Catalog  真实探测 / 用户登记的主、子 Agent 模型清单
 ├── Unified Memory      统一记忆层
 ├── Task Scheduler      依赖 / 优先级 / 重试 / 熔断
 ├── Capability Router   能力 / 成功率 / 验证 / 速度 / 成本
 ├── Cost Optimizer      压缩 / 语义缓存 / 结果复用 / 基准评测
 ├── Version Ledger      Agent 软件版本、指纹与升级兼容漂移
 └── Human Governance    架构 / 验收 / 合并 / 发布审批
 │
 ▼
Context Bus（只追加事件流 + 订阅游标 + 哈希链） ── SSE 实时推送
 │
 ▼
Trae · Qoder · WorkBuddy · ZCode · DeepSeek Harness · OpenCode
Claude Code · Gemini CLI · Codex CLI · Cursor · Aider · REA Studio (MCP) · 任意 CLI/接力 Agent
```

主节点不是“更聪明的 Agent”，而是用户指定的**决策与汇总节点**；执行由路由器按能力与成本自动选择子节点。所有 Agent 的 Prompt、Response、文件变更、任务状态、知识与工作流事件都经过 Context Bus。

## 七个模块与实现位置

| 模块 | 实现 | 关键能力 |
| --- | --- | --- |
| 1 Agent Connector | `agentloop_hub/connectors.py`、`adapters/discovery.py` | 统一 `{agent_id, capability[]}`、14 类本机产物探测（含 REA MCP）、L0-L3 接入、任意 CLI/文件接力注册 |
| 2 Unified Memory | `control/memory.py`、`control/sync.py` | 消息 / 任务状态 / 项目索引 / 代码结构 / 知识 / 决策 / 工作流 / 偏好；冲突裁决；Agent 启动引导包 |
| 3 Context Bus | `control/bus.py` | 事件发布、持久订阅、游标消费、回放、Token 上限、只追加哈希链 |
| 4 Task Scheduler | `control/scheduler.py`、`control/planning.py` | 任务拆分、依赖 DAG、`priority 0..10`、失败重试、熔断 |
| 5 Capability Router | `control/router.py` | 能力硬门槛 + 成功率 / 验证通过率 / 速度 / 成本评分，自动选人并记录决策 |
| 6 Cost Optimizer | `control/cost.py` | 上下文压缩、语义缓存、上游成果复用、Agent Benchmark、节省账本 |
| 7 Human Governance | `control/governance.py` | 架构设计、验收确认、代码合并、发布上线四道人类门禁 |
| 统一控制端 | `api/server.py`、`api/static/`、`workspace/` | 项目初始化/切换、原始 Prompt、主/子 Agent + 模型选配、SSE 实时全量监控 |
| 模型目录 | `control/model_catalog.py`、`adapters/discovery.py` | 从 CLI/会话/provider 配置真实提取模型；支持用户登记，不虚构 |
| 版本台账 | `control/versioning.py`、`storage/repo.py` | Agent 版本/能力/适配器指纹，升级后漂移与兼容提示 |
| REA MCP 接入 | `adapters/mcp.py` | 真实 MCP `initialize` / `tools/list` / `tools/call`，不猜工具参数 |
| GUI 自动化 | `adapters/gui.py`、`gui/` | Qoder CN / WorkBuddy 窗口驱动、模型选择、transcript 回读、错误码与截图留证 |

### 五级同步

| Level | 内容 | 落地 |
| --- | --- | --- |
| 1 | 消息 / 历史会话 | `message` 记忆 + conversation 归档 |
| 2 | 任务状态 | `task_state` 记忆 + 总线事件 |
| 3 | 上下文 / 项目索引 / 代码结构 | `project_index`、`code_structure` 记忆 |
| 4 | 知识 / 决策 | `knowledge`、`decision` 记忆 |
| 5 | 工作流状态 | `workflow` 记忆 + 运行阶段 / 进度 |

## 已实现能力

- 本地 Git 项目识别、Agent 自动发现与手动注册。
- 主节点/主模型指定；主节点只负责规划、决策、汇总，不承担高 Token 执行。
- 结构化计划与审批；`plan.json` 校验、一次自动修复。
- 依赖 DAG 调度、优先级、并行、重试、熔断。
- 每个任务独立 Git Worktree；Patch、范围租约、合并预检与回滚。
- 17 字段结构化成果包；控制平面确定性验证 + 可选独立 Agent 验证。
- 项目/任务预算、用量、熔断、成本节省账本。
- 精确缓存 + 语义缓存 + 上游成果摘要复用（result sync）。
- Context Bus 事件流、订阅、回放、哈希链。
- 统一记忆与 Agent 启动引导包。
- 架构 / 验收 / 合并 / 发布审批。
- 检查点、崩溃恢复、追加式审计、主节点交接。
- 统一 Web 指挥中心（单入口五步流程）+ 七个高级视图；SSE 实时事件/日志/快照。
- 本机真实模型清单（2026-11-03 实测 93 个模型 / 14 个 Agent），主/子 Agent 可分别选配。
- **WebUI 真实使用回归（2026-11-04）**：从工作区选择到 Trae 规划、WorkBuddy 执行、主节点审阅，修复非空目录初始化、GUI 结果路径、Trae/WorkBuddy 完成判定、协作契约、上游补丁组合、GUI 串行锁、暂停恢复、预填选择器与补丁污染等问题；详见 [测试记录](docs/WebUI真实使用测试-清明上河图.md)。
- Agent 软件版本台账与兼容漂移检测；REA Studio 以真实 MCP 服务接入（229 个工具）。
- Qoder CN / WorkBuddy 以 `gui_automation` 自动执行：窗口标题 + 相对布局定位、模型切换/校验、transcript 回读、超时/卡死/解析错误码与截图留证。
- 40+ CLI 子命令。
- **多 Agent 链路（2026-11-04）**：WebUI 第 3.5 步可固定「每阶段 Agent + 该 Agent 真实模型清单中的模型」，
  一键 `POST /api/chain` 建计划、批计划、钉住 `assigned_agent` 并按 DAG 运行；单阶段可设时限与重试次数。
- **预检并打开 Agent（`POST /api/preflight`）**：按所选阶段真正启动 Trae/Qoder、写入工作区与模型并回读核验；
  本机实测 `ready 3/3`（Trae `DeepSeek-V4.1-Flash`、Qoder `gfmodel`、OpenCode CLI）。
- **智能选型（`GET /api/models/recommend`）**：质量取 Agent 历史 benchmark，成本取应用自身倍率
  （Trae 目录）或 free 模型；输出 `cost_source` 与理由，支持 `cost_first / balanced / quality_first`。
- **每阶段绑定报告（`GET /api/runs/<id>/bindings`）**：列出每个任务实际使用的 Agent、模型、工作区与核验结果。
- **模型目录每日同步（`POST/GET /api/agents/models/sync`、`agentmesh models --sync`）**：
  Trae/Qoder/WorkBuddy/OpenCode/ZCode/DSH 的真实模型合并去重，下拉显示应用内同名并附真实倍率；
  控制台每小时检查、超过 24 小时自动刷新。实测覆盖：Trae 19、Qoder 10/14、WorkBuddy 30+、OpenCode 41。
- **预算仅统计、不自动停机（`budget_hard_stop=false`，默认项目预算 0）**：成本页新增
  “分 Agent Token 消耗”（输入/输出/缓存/推理/占比/费用）；运行可用户暂停/继续
  （`POST /api/runs/<id>/pause|resume`），暂停只停止后续派发，不强杀正在执行的任务。
- **Trae / Trae CN 零输入接口（2026-11-04）**：`state.vscdb`（明文 SQLite）写工作区/模式，
  `--remote-debugging-port` + CDP DOM 写入并点击发送；模型写入可回读但**界面不生效**
  （服务端偏好），因此发送前会读取输入框显示模型并在不一致时抛 `MODEL_MISMATCH`。
  详见 [Agent 软件接口逆向实测](docs/Agent软件接口逆向实测.md)。
- **CDP 探活绕过 HTTP_PROXY**：本机 `HTTP_PROXY` 指向本地沙箱代理，`127.0.0.1` 也会被代理成 502；
  `gui/cdp.py` 与两个接口模块已改为显式 `ProxyHandler({})`。
- **Electron 子进程环境清理**：宿主导出的 `ELECTRON_RUN_AS_NODE=1` 会让被拉起的 Electron 应用以 Node 模式
  启动后立即退出；拉起 Qoder/Trae/WorkBuddy 时统一剔除。
- **角色化模型编排（2026-11-04）**：`control/roles.py` 把「一个角色 = 一条 (档位, Agent, 模型) 阶梯」
  变成可执行策略，模型只从本机真实目录解析；`control/segment.py` 用确定性规则把单条需求切成 1–4 个带角色的任务；
  `control/aggregate.py` 把各任务成果包压缩成一份可读答案 + 成本分布。
  一条命令 `agentmesh solve "<需求>" --executor qoder_cn:DeepSeek-Flash --commander workbuddy:<强模型>`
  即可完成「切分 → 选型 → 钉住 → 运行 → 汇总」。
- **任务级模型 + 失败升级（2026-11-04）**：模型从 Agent 属性下移到 `tasks.model`，同一个 Agent 可以在一次运行里
  用两个模型演两个角色；失败重试会沿角色阶梯前进（不回头、不重复同一候选），并写入 `task.escalated` 审计。
- **策略可继承可导入**：`~/.agentloop/roles.json` → `<项目>/.agentloop/roles.json` 逐层覆盖；
  `agentmesh roles import <opencode.json|AGENTS.md>` 直接读取用户现有的 OpenCode `agent` 块或 DSH 路由文档，
  把里面**本机确实存在**的模型映射成角色偏好，不存在的记为 `unmatched`，不虚构。
- **积分/预算前置**：`premium_tier` + `max_premium_tasks_per_run` + 每 Agent `max_tasks_per_run` 在**规划期**
  生效——超预算时先把任务降到更便宜的档位，再谈执行，而不是跑完才发现贵。
- **完全控制（`full_control`，默认勾选）**：WebUI 第 3 步增加「完全控制」开关；勾选后 Trae 切到「完全访问」、
  WorkBuddy 任务 deeplink 使用 `permission_mode=fullAccess`、Qoder 会话写入 `bypassPermissions`，
  运行期间由 CDP DOM watcher 自动接受残留权限弹窗（允许读取/本次会话内始终允许/始终允许等），全程不合成 OS 键鼠。
  详见 [完全控制与权限自动接受](docs/完全控制与权限自动接受.md)。
- **多轮自动续跑（`multi_round`，默认勾选，默认最多 3 轮）**：主节点判定未通过时不再直接结束，
  而是带着上一轮未解决项/风险/下一步重新规划最小增量任务，或使用基于失败证据的回退任务，在同一运行内
  继续执行、复审和汇总；同一阻塞连续出现、轮次用完或计划不可生成时才进入 `waiting_user`。
  参考 opencode 的回合循环与 DSH 的持久 Goal/Goal round driver，详见
  [多轮对话与完成判定](docs/多轮对话与完成判定.md)。
- **工作目录可视化选择（2026-11-04）**：参照 DSH web 版的 browse 能力，控制台第 1 步新增
  「浏览…」页面内目录选择器（盘符/面包屑/上一级/隐藏项/新建文件夹/路径直达）与「最近打开」chips；
  后端 `GET /api/fs/list`、`POST /api/fs/mkdir`、`GET /api/workspaces/recent`，
  **仅允许 loopback 访问**（LAN 分享不会暴露磁盘）。

### 多 Agent 链路实测状态（2026-11-04）

- ✅ **Trae CN + `deepseek-v4.1-flash`**：用 CDP **页面级鼠标事件**打开应用自己的模型菜单并点选
  （`Seed-Code → DeepSeek-V4.1-Flash`，回读 `.model-select-area-model` 确认），再发送任务；
  实测产出完整成果包（发现 `add()` 返回 `a - b`、执行了 `python -c` 验证、列出证据与修复建议），
  控制平面验证 `passed`。事件只进入渲染进程，不移动 OS 光标。
- ✅ Qoder CN + `gfmodel`（GLM-5.3-Flash）：CDP 零输入发送，transcript 回读
  `model=gfmodel` / `workspace-directories=<任务 worktree>` / `completed=true`，验证 `passed`。
- ✅ OpenCode + `opencode/glm-5.3-flash-free`：CLI 直连返回预期内容（7.3s）。
- ⚠️ 三段真实链路 `run_e03efb244dcc`：Trae 段与 Qoder 段均 `completed + passed`；
  OpenCode 长任务在免费模型上超过 8 分钟未返回，未在观察窗口内完成（接口本身已由直连验证）。
  链路编排另有 `verification/test_chain.py`（5 个用例，mock CLI Agent）覆盖。

> 与第三方插件 `dsh-connect-trae-plus` 的对比见
> [Trae 接入方案分析](docs/Trae接入方案分析-dsh-connect-trae-plus.md)：该插件走
> `llm_utils_chat` 私有 HTTP + token 桥（有封号风险），本项目不采用；我们改走应用自身 UI。

完整证据与逐条接口见 [Agent 软件接口逆向实测](docs/Agent软件接口逆向实测.md)，
原始运行记录在 `logs/chain-20261104/chain-runs.json`。

### 链路配置、统一执行入口与阻塞恢复（2026-11-05）

- **3.5 只保存配置**：阶段数量跟随第 2 步生成的计划任务（可为 1..N，不再固定 3 个）；
  每一阶段绑定 Agent + 该 Agent 真实模型清单中的模型 + 任务类型。
  配置保存在 `<项目>/.agentloop/chain-draft.json` 与浏览器 `localStorage`，页面刷新不丢。
- **第 4 步是唯一执行入口**：批准计划后才会创建运行，点击「开始执行」才会调度 Agent。
  「配置并运行链路」已改为「保存链路配置（待审批）」，不再自动批准或自动启动。
- **节点阻塞 + 人工处理**：GUI 节点在发送前失败、或需要人工点击发送/审批时，
  任务与运行进入 `waiting_user`，WebUI 右上角弹窗列出节点、原因与处理按钮；
  适配器继续等待同一个会话，用户完成操作后自动恢复并启动下一节点。
- **不再强制重启应用**：Trae CDP 存活时复用原进程，只通过其 CLI 打开目标工作区；
  WorkBuddy 预填失败先重发深链接、再用可见输入框 CDP 兜底，**不会连续杀进程**。
  应用运行但未开放调试接口时返回 `APP_RESTART_REQUIRED`，由用户点击「重启 Agent 接口」
  触发一次优雅关闭（先 WM_CLOSE，超时后才强制），重启方式写入审计与 GUI 日志。

### 链路稳定性加固（2026-11-05 第二轮）

- **WorkBuddy 完成判定**：`result.json` 提前出现不再立即判完成；必须等 WorkBuddy 会话本身
  `completed`（或明确的非 working 状态 + transcript 静默），避免 T001 还在收尾就启动 T002。
- **活动会话串行化**：WorkBuddy 仍有 `working` 会话时，新任务先等待；超时则阻塞为
  `APP_BUSY`，不再静默新开第二个对话。
- **计划 scope 规范化**：把 `新增 qingming-3d/src/scene/assets/**（materials.js…）`
  这类自然语言 scope 提取为可执行 glob；裸文件名按 basename 匹配任意目录。
- **验证命令规范化**：`.agentloop/planning/.../workspace` 绝对路径改写为任务工作区相对路径；
  `git -C <绝对路径>` 改为在当前 Worktree 执行；PowerShell cmdlet 通过 `powershell -Command` 执行。
- **Trae 页面固定 + 登录墙检测**：发送、模型菜单、回读固定同一个 CDP page；
  检测到 “Sign in to use AI Features” 时阻塞为 `APP_LOGIN_REQUIRED` 并提示登录，
  不再误报“菜单中没有模型”。
- **主节点活动可见**：规划/第 N 轮规划/汇总写入 `activity.json`，WebUI 第 4 步显示
  “主节点活动”，用户能明确知道 Trae 当前在跑哪个阶段；汇总使用独立的 `SUMMARY` 节点标识。
- **scope 计划缺口**：默认 `scope_violation_policy=warn`，普通项目内越界显示“验证警告”并继续；
  明确 out_of_scope/越出项目/高风险文件仍失败。
- **重新验证**：失败节点若已有 result.json 与 patch，可点“重新验证”只复跑控制平面验证，
  不重新执行 Agent；`POST /api/tasks/<uid>/reverify`。
- **浏览器验证命令**：PowerShell `&` 自动识别；本地 URL 端口未监听时自动启动 `tools/serve.py`；
  headless Chrome 使用独立临时 profile，`--screenshot` 相对路径自动转绝对路径。
- **Agent 上下文/能力配置（逆向真实选项）**：第 3 步按 Agent+模型保存
  `context_length` 与 `capability_level`；选项不是通用枚举，而是从各应用自身读取：
  WorkBuddy `300k/600k/1M + low/high/max`、Trae `1M + light/high/extra_high`、
  Qoder `none…max` 七档。保存、节点绑定和执行前都按真实选项校验；
  智能选型会按能力等级加权，任务包会把配置带给 Agent，小上下文窗口自动压缩中间上下文。
- **触发时同步写回应用**：WorkBuddy 通过 CDP 写新任务上下文/思考偏好
  （`model-context-window`、`cb-effort`、`cb-thinking-pref`、`cb-newtask`），
  变化时重载渲染器后再发 deeplink；会话入库后补写 `sessions.context_window/thought_level`。
  Trae 写 `state.vscdb` 思考等级键；Qoder 写 `chat_model_preferences`。
  写后回读，失败抛 `PROFILE_SYNC_FAILED`，不会带着错误配置继续跑。
- **同 Agent 会话续接**：链路中同一 Agent 的后续节点默认在该 Agent 上一节点的
  会话与工作树中继续，不再每个节点新开对话；不同 Agent 才新开会话。
  首节点和主节点规划仍是独立会话；模型变化时保留工作树但新开会话。
- **运行中节点绑定编辑**：第 4 步链路节点支持修改 Agent、模型、上下文与能力等级；
  排队/阻塞/等待节点即时生效，正在运行节点等结束或重试后生效。
- **硬停止运行**：第 4 步“停止运行（取消节点）”会取消排队/运行/阻塞节点、置运行 canceled、
  中断 GUI reader 等待并尝试点击 WorkBuddy 自己的停止按钮；取消后不会被自动重试或 resume。

详细设计与真实日志证据见 [链路配置与阻塞恢复](docs/链路配置与阻塞恢复.md)。

## 快速开始

要求：Windows 10/11 x64、Python ≥3.10、Git。依赖：PyYAML、cryptography、zstandard。

```powershell
cd j:\qoder\agent-hub
python -m pip install -r requirements.txt
python -m pip install -e .          # 让任意项目目录可直接运行 python -m agentloop_hub

# 方式 A：单一 Web 入口（推荐）
python -m agentloop_hub serve --port 8787    # http://127.0.0.1:8787
# 在“指挥中心 → 项目目录”输入路径并点击“初始化项目”：
# 自动创建目录、git init、生成初始 commit，然后探测 Agent 与模型清单。

# 方式 B：CLI
cd <你的Git项目>
python -m agentloop_hub init                 # 创建工作空间，探测本机 Agent
python -m agentloop_hub connectors           # 统一能力清单
python -m agentloop_hub models               # 本机 Agent 模型清单
python -m agentloop_hub versions --status    # 版本台账与兼容漂移
python -m agentloop_hub main set opencode --model opencode/deepseek-v4.1-flash-free
python -m agentloop_hub ask "修复 calc.py 中的加法错误并补充测试"
python -m agentloop_hub plan approve <plan_id>
python -m agentloop_hub run start <run_id> --parallel 2
python -m agentloop_hub approval list        # 验收确认 → 合并审批 → 发布审批
```

说明：

- Web 控制台可以初始化空目录（自动 `git init` + 初始 commit）或打开已有 Git 项目；`init` CLI 只支持 Git 项目，因为 Worktree/Patch 隔离依赖 Git。
- GUI Agent：Trae / Trae CN、Qoder CN、WorkBuddy 为 `gui_automation`（经应用自身接口自动执行）；
  ZCode/DSH/Cursor 仍为 L2 接力模式，控制平面生成任务包，用户在软件中执行并回填成果。
- OpenCode / Claude Code / Gemini CLI / Codex CLI / Aider 探测到可执行文件时以 L3 CLI 模式注册；未安装时在清单中显示为离线，不虚构能力。
- 未在本机实测的 CLI 产品使用探测画像默认参数；若其版本参数不同，请用 `agent add --config-json` 覆盖，不把默认参数当作已验证事实。
- 计划包含 `architecture` 任务时，必须先批准架构审批，再批准计划。
- 运行完成后依次经过：验收确认 → 合并审批 → 发布审批。发布门禁只记录人类决定，不自动部署。

## 本机 Agent 接入矩阵（2026-11-03 审计）

| Agent | 存储/CLI | 等级 | 适配方式 |
| --- | --- | --- | --- |
| Trae / Trae CN | `state.vscdb` + `ModularData/ai-agent/database.db` 会话 | **L3 GUI** | CDP DOM 发送 + 状态库工作区/模型写入；会话只读解密 |
| Qoder / Qoder CN | `~/.qoder{,-cn}/projects/**/*.jsonl` | L2 / **L3 GUI** | 文件接力 + 会话读取；CN 版 `gui_automation` |
| ZCode | `~/.zcode/cli/rollout/model-io-sess_*.jsonl` | L2 | 文件接力 + 会话读取 |
| WorkBuddy | `~/.workbuddy/projects/**/*.jsonl` + `workbuddy.db` | **L3 GUI** | `gui_automation`：模型菜单 + transcript/DB 回读 |
| DeepSeek Harness | `~/.dsh/sessions/**/*.jsonl.zstd` | L2 | 文件接力 + 会话读取 |
| OpenCode | `opencode run --format json` | L3 | CLI 非交互执行 |
| Claude Code | `claude -p` | L3（安装才启用） | CLI 非交互执行 |
| Gemini CLI | `gemini -p` | L3（安装才启用） | CLI 非交互执行 |
| Codex CLI | `codex exec` | L3（安装才启用） | CLI 非交互执行 |
| Aider | `aider --message` | L3（安装才启用） | CLI 非交互执行 |
| Cursor | `cursor` / 安装目录 | L1-L2 | 文件接力；不读取私有状态 |
| REA Studio | 项目级 `.mcp.json`（`rea-agents` 6.8.0） | L2 | MCP stdio：真实 `tools/list` / `tools/call`，229 个工具 |

等级：L0 人工接力 / L1 文件协同 / L2 结构化任务协同 / L3 会话级协同 / L4 原生双向（本期不承诺）。

## 常用 CLI

| 命令 | 说明 |
| --- | --- |
| `agentmesh init` / `status` / `doctor` | 项目、总览、健康检查（含总线哈希链） |
| `agentmesh connectors` | 统一 Connector Manifest |
| `agentmesh models [--refresh]` | 本机真实模型清单（主/子 Agent 选配依据） |
| `agentmesh versions [--status]` | Agent 软件版本台账、升级漂移 |
| `agentmesh ask "<目标>"` | 单一入口：生成计划与审批 |
| `agentmesh solve "<目标>"` | 单入口闭环：切分 → 角色选型 → 运行 → 汇总答案（`--executor/--commander` 指定模型） |
| `agentmesh segment "<目标>"` | 只看切分与选型，不运行 |
| `agentmesh roles list/resolve/import/save` | 角色策略：候选阶梯、导入 OpenCode/AGENTS.md、落盘 |
| `agentmesh main set <agent> --model <model>` | 指定主节点与主模型 |
| `agentmesh plan request/import/approve/reject` | 规划与批准 |
| `agentmesh run start/resume/status` | 执行任务图 |
| `agentmesh task list/show/result/retry/cancel` | 任务与成果 |
| `agentmesh route rank/task/scores/decisions` | 能力路由 |
| `agentmesh bus list/stats/verify/subscribe/poll/publish/replay` | Context Bus |
| `agentmesh benchmark` | Agent 基准评测 |
| `agentmesh costs` / `cache stats/clear` | 成本、节省、语义缓存 |
| `agentmesh approval list/approve/reject` | 人类治理 |
| `agentmesh release <run_id>` | 发布审批（合并后可用） |
| `agentmesh sync [--agent ID]` / `memory bootstrap` | 五级同步、启动引导包 |
| `agentmesh session import/list` | 历史会话 |
| `agentmesh memory list/add/search/update/delete/export` | 统一记忆 |
| `agentmesh merge/rollback` | 合并与回滚 |
| `agentmesh recover` / `audit --verify` | 恢复与审计 |
| `agentmesh serve` | Web 控制台 |

所有查询命令支持 `--json`；`--project` 可放在子命令前后。

## 统一 Web 控制台

默认进入**指挥中心**，按五步完成一次闭环：

1. **项目目录**：输入路径，一键初始化（创建目录 + `git init` + 初始 commit）或打开已有 Git 项目。
2. **原始 Prompt**：直接输入需求，主 Agent 生成结构化计划（N 个任务，进入待审批）。
3. **主/子 Agent 与模型**：主 Agent 负责规划；每个子 Agent 可从真实模型清单中选配模型并持久化。
3.5 **多 Agent 链路配置**：从计划载入 N 个阶段，逐阶段锁定 Agent / 模型 / 任务类型；
   点击「保存链路配置（待审批）」，**这里不执行**。配置刷新不丢。
4. **计划审批与运行（唯一执行入口）**：批准计划 → 开始执行 → 验收 → 合并 → 发布；
   节点需要人工提交/审批时阻塞并弹窗，节点完成后自动继续下一节点。
5. **实时全量监控**：SSE 推送 Context Bus 事件、Agent stdout 日志、任务状态、成本、审批、版本漂移。

高级视图：Agent 管理（含 MCP 工具台）/ Agent 版本 / 任务图 / 任务详情 / 人类治理 / Context Bus / 成本与审计。

模型清单来源只有四种：CLI 输出、Agent 会话事件、Agent 自己的 provider/settings 配置、用户手动登记；不会内置或猜测模型名，也不会读取密钥。

## 目录与数据

```text
<项目>/.agentloop/
├── project.yaml
├── state.db                 # 任务/审批/预算/审计/记忆/会话/总线/路由/缓存/基准
├── tasks/<run>_<task>/      # 任务包、提示词、成果包、日志
├── runs/<run>/worktrees/    # 每个子任务独立 Worktree
├── patches/                 # 统一 Patch
├── reports/                 # 验证报告
├── checkpoints/             # 检查点快照
├── handoffs/                # 主节点交接包
├── relay/<task>/inbox|outbox# L2 接力文件
├── conversations/           # 归一化历史会话归档
└── audit/                   # 审计在 state.db，追加式
```

`state.db` 的 v3 schema 额外包含 `agent_versions`（软件版本/能力指纹台账）与 `agent_models`（模型目录，来源可追溯）。

`.agentloop/` 与 `.agentloop-task/` 会写入 `.git/info/exclude`，不进入项目 Git 状态。

## 测试

```powershell
python -m pytest
```

本机结果：**310 passed**（2026-11-05）。覆盖 Connector 清单、路由评分、总线订阅/回放/只追加、调度优先级、语义缓存、节省账本、Benchmark、五级记忆同步、架构/验收/发布门禁、Worktree/Patch、DAG、重试、熔断、预算、审批、验证、合并、回滚、恢复、交接、会话读取、HTTP API、CLI、项目初始化/切换、模型目录选配、版本漂移、MCP 工具发现/调用、GUI 适配器（stub 驱动）、SSE 实时流，以及本轮新增的
**角色策略解析 / 确定性切分 / 任务级模型派发 / 失败升级 / 预算降级 / 结果汇总 / 多轮续跑判定 / 增量规划与回退任务 / 跨轮补丁合并 / 链路绑定计划 / 配置草稿持久化 / 启动器重启保护 / 手动提交阻塞与自动续跑 / WorkBuddy 完成终态校验 / 活动会话串行化 / 计划 scope 与验证命令规范化 / PowerShell 验证 / Trae 页面固定与登录墙检测 / 主节点活动可视化 / 长思考存活检测 / 模型流中断上报 / OpenCode 结构化错误透传**。

## 安全边界与已知限制

- GUI 自动化仅用于用户本机已安装、已登录的 Qoder CN / WorkBuddy：通过窗口标题与相对布局操作，不破解应用、不修改其私有存储；截图与 transcript 仅作执行证据。其他 GUI Agent 仍为文件/人工接力。
- 不承诺读取第三方 Agent 的私有隐藏状态或写入其会话；会话读取与 GUI 回读均为只读归档。
- 子任务在独立 Worktree 中执行，控制平面在合并前检查范围、补丁可应用性、命令验证与证据；但 Agent 进程本身仍运行在用户账户下，不是强沙箱。
- 运行仍要求 Git（Worktree/Patch 隔离）；非 Git 目录可在 Web 控制台一键初始化，CLI `init` 会明确拒绝非 Git 目录。
- 发布审批只是人类门禁记录，不自动执行部署/推送；部署脚本由用户自行控制。
- 语义缓存只对只读任务、同一文件基线、相似意图生效；写任务不会自动复用成果。
- 本期 GUI 自动化只覆盖 Qoder CN 与 WorkBuddy；其余 GUI 产品未做同等强度定位验证，仍为接力模式。
- 本期未实现多电脑同步、团队权限、云端调度、自动选择“全球最强模型”等 P2 能力。

## 文档

- [用户使用手册](docs/用户使用手册.md)
- [概要设计](docs/概要设计.md)
- [实现与测试报告](docs/实现与测试报告.md)
- [验证日志](docs/验证日志.md)
- [11 类 Agent 能力矩阵](docs/11类Agent能力矩阵.md)
- [GUI 自动化实测报告](docs/GUI自动化实测报告.md)
- [角色路由与成本编排](docs/角色路由与成本编排.md)
- [Web 控制台的工作目录选择器](docs/工作目录选择器.md)
- [WebUI 真实使用测试：《清明上河图》三维网页](docs/WebUI真实使用测试-清明上河图.md)
- [完全控制与权限自动接受](docs/完全控制与权限自动接受.md)
- [多轮对话与完成判定](docs/多轮对话与完成判定.md)
- 需求基线：`01-需求整理-v1.docx`（原始需求与更早版本保留在根目录）

## 许可

MIT（仅本项目代码；第三方 Agent 的使用须遵守其各自服务条款）。
