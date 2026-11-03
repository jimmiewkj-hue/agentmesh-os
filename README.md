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
- Agent 软件版本台账与兼容漂移检测；REA Studio 以真实 MCP 服务接入（229 个工具）。
- Qoder CN / WorkBuddy 以 `gui_automation` 自动执行：窗口标题 + 相对布局定位、模型切换/校验、transcript 回读、超时/卡死/解析错误码与截图留证。
- 40+ CLI 子命令。

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
- GUI Agent：Qoder CN 与 WorkBuddy 为 `gui_automation`（自动执行）；Trae/ZCode/DSH/Cursor 仍为 L2 接力模式，控制平面生成任务包，用户在软件中执行并回填成果。
- OpenCode / Claude Code / Gemini CLI / Codex CLI / Aider 探测到可执行文件时以 L3 CLI 模式注册；未安装时在清单中显示为离线，不虚构能力。
- 未在本机实测的 CLI 产品使用探测画像默认参数；若其版本参数不同，请用 `agent add --config-json` 覆盖，不把默认参数当作已验证事实。
- 计划包含 `architecture` 任务时，必须先批准架构审批，再批准计划。
- 运行完成后依次经过：验收确认 → 合并审批 → 发布审批。发布门禁只记录人类决定，不自动部署。

## 本机 Agent 接入矩阵（2026-11-03 审计）

| Agent | 存储/CLI | 等级 | 适配方式 |
| --- | --- | --- | --- |
| Trae / Trae CN | `%APPDATA%/<Trae\|Trae CN>/ModularData/ai-agent/database.db` | L2 | 文件接力 + 解密会话读取 |
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
2. **原始 Prompt**：直接输入需求，主 Agent 生成结构化计划。
3. **主/子 Agent 与模型**：主 Agent 负责规划；每个子 Agent 可从真实模型清单中选配模型并持久化。
4. **计划审批与运行**：架构 → 计划 → 执行 → 验收 → 合并 → 发布。
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

本机结果：**146 passed**（2026-11-03）。覆盖 Connector 清单、路由评分、总线订阅/回放/只追加、调度优先级、语义缓存、节省账本、Benchmark、五级记忆同步、架构/验收/发布门禁、Worktree/Patch、DAG、重试、熔断、预算、审批、验证、合并、回滚、恢复、交接、会话读取、HTTP API、CLI、项目初始化/切换、模型目录选配、版本漂移、MCP 工具发现/调用、GUI 适配器（stub 驱动）与 SSE 实时流。

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
- 需求基线：`01-需求整理-v1.docx`（原始需求与更早版本保留在根目录）

## 许可

MIT（仅本项目代码；第三方 Agent 的使用须遵守其各自服务条款）。
