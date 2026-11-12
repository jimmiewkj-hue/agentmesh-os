# 11 类 Agent 能力矩阵与逆向结论（2026-11-03）

> 结论来源：代码注册表 + 本机安装目录 + 应用配置/会话存储 + GUI 实测。  
> 证据目录：`logs/reverse-20261103/`、`logs/gui-adapter-smoke-20261103/`、`logs/gui-adapter-smoke-workbuddy-20261103/`。

## 1. 11 类 Agent 能力矩阵

11 类产品 = Trae、Qoder、WorkBuddy、OpenCode、ZCode、DeepSeek Harness、Cursor、Claude Code、Gemini CLI、Codex CLI、Aider。  
Trae 与 Qoder 各含国际版/CN 版，因此注册表实际有 14 个 Agent ID（另含 REA Studio MCP）。

| # | 类 | 注册 ID | 本机版本 | 可自动执行方式 | 可用模型（真实来源） | 能力标签 | 当前缺口 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Trae | `trae` / `trae_cn` | 已安装（会话库存在） | 人工接力（file_relay） | 模型状态在加密 DB，未解密，不虚构 | read/write/terminal/mcp/analysis? | GUI 自动化未落地；模型清单不可读 |
| 2 | Qoder | `qoder` | 已安装 | 人工接力（file_relay） | `q-model` 等来自 `~/.qoder/projects/**.jsonl` | read/write/terminal/mcp/analysis? | 国际版未做 GUI 适配（CN 版已验证） |
| 3 | Qoder CN | `qoder_cn` | 0.5.1（CLI 1.1.79-hotfix.7） | **GUI 自动化（gui_automation）** | **DeepSeek-Flash**（运行时键 `dfmodel`） | read/write/terminal/analysis/code_generation/testing/review/architecture/gui_control/auto_executable | UI 模型切换未验证；默认 DeepSeek-Flash，执行后用 transcript 校验 |
| 4 | WorkBuddy | `workbuddy` | 37.10.3-43（本地服务 2.165.0） | **GUI 自动化（gui_automation）** | **GLM-5.3-Flash**（`glm-5.3-flash`）；另含 deepseek-v4.1-flash、mimo-v2.6-pro 等 | read/write/terminal/analysis/code_generation/testing/review/architecture/gui_control/auto_executable | 模型菜单依赖相对布局与滚动复位；布局变更时需校准 |
| 5 | OpenCode | `opencode` | 本机 CLI | CLI 非交互（`opencode run --format json`） | CLI `models` 输出 | read/write/terminal/analysis/code_generation/testing/review/architecture/auto_executable | 无（已验证） |
| 6 | ZCode | `zcode` | 3.17.2（CLI core 0.17.1） | **CLI 非交互（`zcode -p`，L3 zcode 适配器）** | `provider_config.json` + rollout（deepseek-v4-pro、GLM-5.3-Flash、DeepSeek-V4.1-Flash 等） | read/write/terminal/analysis/code_generation/testing/review/architecture/auto_executable/structured_output | 桌面订阅通道（account:zai-start-plan）仅桌面 GUI 可用；standalone CLI 走个人 api-key provider（2026-11-07 已验证） |
| 7 | DeepSeek Harness | `dsh` | 0.2.0-rc.2（已安装） | **CLI 非交互（headless profile，L3 dsh 适配器）** | `~/.dsh/settings.yaml` + 会话 `.jsonl.zstd` | read/write/terminal/mcp/analysis/code_generation/testing/review/architecture/web_search/auto_executable/structured_output | 模型经 `--patch` 覆盖 agent-default-model；自动加载凭据服务密钥；2026-11-10 真实端到端跑通（result.json/usage/续接），缺凭据 MISSING_CREDENTIAL 快速失败 |
| 8 | Cursor | `cursor` | 未安装/未验证 | 人工接力（file_relay） | 无真实清单 | read/write/terminal/mcp | 需安装并验证 GUI/CLI |
| 9 | Claude Code | `claude_code` | 安装才启用 | CLI 非交互（`claude -p`） | 未从本机配置提取到模型 | read/write/terminal/code_generation/review/architecture/auto_executable | 登录/凭据与模型清单未验证 |
| 10 | Gemini CLI | `gemini_cli` | 安装才启用 | CLI 非交互（`gemini -p`） | 未从本机配置提取到模型 | read/write/terminal/web_search/code_generation/auto_executable | 登录/凭据与模型清单未验证 |
| 11 | Codex CLI | `codex_cli` | 安装才启用 | CLI 非交互（`codex exec`） | 未从本机配置提取到模型 | read/write/terminal/code_generation/testing/auto_executable | 登录/凭据与模型清单未验证 |
| 12 | Aider | `aider` | 安装才启用 | CLI 非交互（`aider --message`） | 需项目内模型密钥 | read/write/terminal/code_generation/testing/auto_executable | 模型/密钥未验证 |
| + | REA Studio | `rea` | 6.8.0 | MCP stdio（真实 `tools/list`/`tools/call`） | 229 个工具，无 LLM 模型概念 | analysis/code_review/local_mcp | 不写项目文件；需人工发起 |

说明：
- “analysis?” 表示该 GUI 产品具备读取/分析能力，但当前 file_relay 模式需要人工操作，能力清单不把 `auto_executable` 打开。
- 未安装/未登录的产品保持 offline 或仅登记，不虚构模型与能力。
- 11 类齐全：14 个 ID 覆盖 11 类；Qoder CN 与 WorkBuddy 已从 `file_relay` 升级为 `gui_automation`。

## 2. Qoder CN 逆向结论

### 架构与安装
- 可执行文件：`C:\Program Files\Qoder\Qoder CN\Qoder CN.exe`
- 产品版本：`0.5.1`（`~/.qoder-cn/.qoder-app-status.json`，`product=qodercn`）
- Agent CLI 版本：`1.1.79-hotfix.7`（`~/.qoder-cn/logs/runs/<run>/manifest.json`）
- 内置 SDK worker：`resources/app.asar.unpacked/node_modules/@qoder-ai/qoder-cn-agent-sdk/dist/_worker/qoder-worker-runtime.obf.mjs`
- manifest 中真实启动参数包含：`--print --output-format stream-json --model dfmodel --permission-mode auto --tools Read,Write,Edit,Glob,Grep,Bash,WebSearch,WebFetch,ImageGen,NotebookEdit,TaskCreate,TaskList,TaskGet,TaskUpdate,TaskStop,ExitPlanMode,CreateGoal,GetGoal,UpdateGoal,AskUserQuestion,Skill,Agent`

### 数据流
1. GUI 输入 → 应用内 Agent runtime（qoder worker）执行。
2. 会话持久化到 `~/.qoder-cn/projects/<project>/<session>.jsonl`。
3. 子 Agent 会话：`.../<session>/subagents/agent-a<type>-*.jsonl`。
4. 运行日志：`~/.qoder-cn/logs/runs/<run>/qodercli.log`、`~/.qoder-cn/logs/sessions/...`。
5. 模型选择写入 transcript 的 `runtime-config.model`，消息事件 `message.model` 同步记录。
6. 模型目录从 `https://gateway.qoder.com.cn/api/v2/model/list` 拉取，缓存到 `~/.qoder-cn/.models/<uid>/catalog-v7`（QMC 头加密；日志显示 11 个模型）。

### Loop / 任务规划
- 工具调用统计（本机 transcript）：Bash 710、Edit 178、Read 139、mcp_call 124、Write 27、TaskCreate 18、TaskUpdate 18、WebSearch 12、WebFetch 7、Agent 7、TaskStop 5、Skill 3、AskUserQuestion 1。
- `TaskCreate/TaskUpdate/TaskStop` 是 todo/plan 机制；`ExitPlanMode` 表示计划确认；`Agent` 表示子 Agent 拆分；`AskUserQuestion` 表示人工确认环节。
- 完成标志：assistant 事件 `stop_reason="end_turn"`。

### 模型标识证据
- 运行时/配置键：`dfmodel`（`runtime-config` 与所有 assistant `message.model`）。
- 显示名：`DeepSeek-Flash`（GUI 底部模型条，见 `qoder-gui-ok.png` / `qoder-model-open.png`）。
- 日志：`[catalog] Loaded 11 models`、`modelCatalogFetch ... /api/v2/model/list status=200`。
- 会话实测：`7e99b559-a5a9-4fa4-ab61-932f8ef8e836.jsonl` 中 `runtime-config.model=dfmodel`，回复 `QODER_GUI_OK_20261103`。

### GUI 实测
- 窗口标题：`Qoder CN`（`Chrome_WidgetWin_1`）。
- 输入区：窗口相对坐标约 `(0.524, 0.804)`；新任务按钮约 `(0.036, 0.072)`。
- 实测：打开应用（WMI 脱离宿主进程启动，避免工具宿主清理进程树）→ 当前模型 DeepSeek-Flash → 粘贴最小指令 → 回车 → 从 transcript 读取回复，耗时约 7.6s。
- 截图：`logs/reverse-20261103/qoder-gui-ok.png`。

## 3. WorkBuddy 逆向结论

### 架构与安装
- 可执行文件：`C:\Program Files\WorkBuddy\WorkBuddy.exe`
- 产品版本：`37.10.3-43`（`C:\Program Files\WorkBuddy\version`）
- 本地会话服务版本：`2.165.0`（`~/.workbuddy/sessions/<pid>.json`，字段 `endpoint=http://127.0.0.1:<port>`）
- Electron 主进程监听本地端口（实测 `127.0.0.1:18488` 返回 404，说明存在 HTTP 服务但路由未公开）。

### 数据流
1. GUI 输入 → 本地 agent runtime（`mode=craft`）执行。
2. 会话元数据写入 `~/.workbuddy/workbuddy.db` 的 `sessions` 表（含 `model`、`permission_mode`、`thought_level`、`status`）。
3. 会话事件写入 `~/.workbuddy/projects/<cwd>/<session>.jsonl`，事件类型：`message`、`reasoning`、`function_call`、`function_call_result`、`credit-usage`。
4. 子 Agent：`.../<session>/subagents/agent-*.jsonl`。
5. Todo/任务：`~/.workbuddy/tasks/<task_id>/<n>.json`，字段 `subject/description/activeForm/status/createdAt/updatedAt`。
6. 计划：`~/.workbuddy/plans/`。

### Loop / 任务规划
- 工具调用统计：Bash 249、ViewImage 171、Write 43、Read 30、Agent 11、Edit 5、present_files 3、PowerShell 2、ToolSearch 2、Skill 1、AskUserQuestion 1、TaskOutput 1、Grep 1。
- `tasks/*.json` 是显式 todo 列表；`Agent` 表示子 Agent；`AskUserQuestion` 表示人工确认；`status=completed` 是回合完成标志。
- 会话默认 `permission_mode=fullAccess`、`thought_level=max`、`mode=craft`。

### 模型标识证据
- 会话 DB：`sessions.model = glm-5.3-flash`（实测新会话 `7024d933-...`、`4ce2ef6a-...`）。
- transcript：`providerData.model = glm-5.3-flash`，`providerData.requestModelName = GLM-5.3-Flash`。
- UI：模型条显示 `GLM-5.3-Flash`（`workbuddy-glm-selected4.png`、`workbuddy-gui-ok.png`）。
- 应用内置目录：`zai-org/GLM-5.3-Flash`（`app.asar` 提供者注册表）；本地存储 `cb-effort:by-model` 含 `"glm-5.3-flash":"max"`。
- 其他真实会话模型：`deepseek-v4.1-flash`、`mimo-v2.6-pro`。

### GUI 实测
- 窗口标题：`WorkBuddy`（`Chrome_WidgetWin_1`）。
- 新任务：`(0.050, 0.120)`；首页输入区：`(0.439, 0.395)`；模型选择器：`(0.796, 0.519)`；GLM-5.3-Flash 菜单项：`(0.720, 0.361)`（打开菜单后先向滚轮上方复位）。
- 实测：新建任务 → 模型菜单选择 GLM-5.3-Flash → 粘贴最小指令 → 回车 → 从 transcript + sessions DB 校验模型并读取回复，耗时约 17.3s，usage 31281/43 tokens，credits 0.47。
- 截图：`logs/reverse-20261103/workbuddy-gui-ok.png`、`workbuddy-glm-selected4.png`。

## 4. 零 OS 输入接口（2026-11-04 实测）

以下通道全部不产生 OS 输入事件；`agentloop_hub/gui/windows.py` 的坐标点击/剪贴板路径已加入零输入守卫，默认拒绝，仅 `AGENTMESH_ALLOW_OS_INPUT=1` 显式诊断时可用。

| Agent | 打开/激活 | 工作区注入 | 模型 | 开发模式 | 发起对话 | 回读证据 |
| --- | --- | --- | --- | --- | --- | --- |
| Qoder CN | 程序拉起 `Qoder CN.exe`，`/json/list` 就绪 | `main.sqlite` `chat_sessions.cwd` + transcript `workspace-directories` | `runtime-config.model`（`dfmodel`/`gfmodel`） | 会话恢复即继承 | CDP `Runtime.evaluate` 插入文本 + 点击“发送消息”按钮 | daemon `open_session` cwd、transcript `assistant.model` |
| WorkBuddy | `--remote-debugging-port=9223` 拉起/复用 | `workbuddy://task?...cwd=` 预填 + DB `sessions.cwd` | `workbuddy://...&model=` + DB `sessions.model` / `providerData.requestModelName` | `welcomeMode=code`+`permissionMode=fullAccess` → DB `source_mode=craft` | deeplink 仅预填（`prepareInput`，无 autoSend）→ CDP 点击“发送”按钮 | `sessions` 行 `cwd/model/source_mode/permission_mode/status` |

关键结论：
- WorkBuddy 深链接**不会自动发送**：asar 中 `task-deeplink-coordinator` 只调用 `prepareInput`；提交必须点击渲染器“发送”按钮（CDP DOM，零按键）。旧文档“深链接自动提交”的说法作废。
- Qoder CN 发送同样走 CDP DOM：`execCommand('insertText')` + 按钮点击；不使用回车键、不用剪贴板。
- 两者均实测成功跑通跨 Agent 闭环，详见 `logs/zero-input-e2e-20261104/`。

## 5. 结论

- 11 类 Agent 在注册表中齐全（14 个 ID）；`/api/surface` 与 CLI `surface` 输出逐 Agent 的 5 能力矩阵（`verified / partial / unsupported / not_installed`），无证据的能力不标 verified。
- Qoder CN、WorkBuddy 已具备**零 OS 输入**的打开、工作区、模型、模式与对话五能力；其余 GUI 产品仍为 file_relay 或未安装，原因与缺口逐条列在 matrix 的 `gap` 字段。
- 2026-11-04 跨 Agent 闭环：Qoder CN 主 + WorkBuddy 子、WorkBuddy 主 + Qoder CN 子，均在 `AGENTMESH_DISABLE_GUI_INPUT=1` 下完成并发布，日志在 `logs/zero-input-e2e-20261104/<combo>/`。

