# Agent 软件接口逆向实测（零 OS 输入）

- 日期：2026-11-04
- 范围：本机**已安装**的全部 Agent（未安装的 4 款只登记，不猜测）
- 原则：
  1. 只走应用**自身**的接口：CLI 参数、deeplink、CDP DOM、本地 SQLite/JSON 配置、stdio；
  2. **不**调用厂商 LLM HTTP API（避免封号）；
  3. **不**合成任何 OS 鼠标/键盘/剪贴板事件（`AGENTMESH_DISABLE_GUI_INPUT=1` 硬锁）；
  4. 没有实证的能力一律标 `partial` / `unsupported`，不写成 `verified`。

复现：`python verification/reverse_audit.py` → `verification/reverse-audit.json`

---

## 1. 总表

| # | Agent | 已安装 | 打开/激活 | 工作区 | 模型 | 开发模式 | 对话 | 回读 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Trae CN | ✅ | 进程启动 + CDP | `state.vscdb`（已验证写入/回读） | **CDP 页面级鼠标点选模型菜单**（已验证 `Seed-Code → DeepSeek-V4.1-Flash`）+ store 持久化 | `work/code/design` 写入 | CDP DOM（已验证插入+点击发送） | CDP 可见文本 + `messageIds`/`assistant_task_status` | **verified** |
| 2 | Trae（国际版） | ✅ | 同上 | 同上（91 个真实模型） | 同上（同一 profile，未单独实测菜单） | 同上 | 同上 | 同上 | **partial**（待国际版机器复跑） |
| 3 | Qoder CN | ✅ | 进程启动 + CDP 9222 | `main.sqlite` + transcript（verified） | `chat_sessions.model` + `runtime-config`（verified，`gfmodel`=GLM-5.3-Flash） | 启动参数固定 | CDP DOM（verified） | transcript `assistant.model` + daemon `open_session cwd` | **verified** |
| 4 | Qoder（国际版） | ✅ | 进程启动 | `com.qoder.app.stable/main.sqlite`（明文，同构） | `chat_model_preferences`（含 `gfmodel`） | 未验证 | 未验证 | transcript | **partial** |
| 5 | WorkBuddy | ✅ | deeplink + 进程 | `workbuddy://task?...&cwd=`（verified） | `...&model=`（verified） | `welcomeMode` → `source_mode=craft` | deeplink 预填 + CDP 点击发送 | `workbuddy.db sessions` | **verified** |
| 6 | OpenCode | ✅ | CLI | 进程 cwd / `--dir` | `-m opencode/glm-5.3-flash-free`（verified） | 非交互无模式 | `opencode run` stdio | stdout JSON 事件 | **verified** |
| 7 | ZCode | ✅ | 进程启动 | `~/.zcode/cli/db/db.sqlite` `session.directory`（明文） | `local_setting` + `model_usage.model_id`（仅读） | `local_setting permission.mode`（仅读） | 未找到零输入发送通道 | sqlite | **partial** |
| 8 | DeepSeek Harness | ✅ | 进程启动 | `~/.dsh/sessions/--<path>--/`（目录名编码） | `settings.yaml.imported` + `.workbuddy-catalog.json`（仅读） | 未找到 | 未找到 | `.jsonl.zstd` | **partial** |
| 9 | Cursor | ✅ | 进程启动 | `--folder-uri` + `workspaceStorage` | `cursor/initialModelState`（仅读） | 未找到 | 未找到 | `conversation-search.db` | **partial** |
| 10 | REA Studio | ✅ | MCP stdio | 不持有工作区 | 无 LLM 模型 | 不适用 | `tools/call` | JSON-RPC | **verified（工具服务）** |
| 11 | Claude Code | ❌ | — | — | — | — | — | — | not_installed |
| 12 | Gemini CLI | ❌ | — | — | — | — | — | — | not_installed |
| 13 | Codex CLI | ❌ | — | — | — | — | — | — | not_installed |
| 14 | Aider | ❌ | — | — | — | — | — | — | not_installed |

---

## 2. Trae CN 逆向细节（本轮新增）

### 2.1 状态库不是加密的那一个

- `%APPDATA%\Trae CN\ModularData\ai-agent\database.db`：文件头随机字节 → **加密**，全程不读不写。
- `%APPDATA%\Trae CN\User\globalStorage\state.vscdb`：**明文 SQLite**（`ItemTable`），这才是模型 / 工作区 / 模式的真实来源。

### 2.2 关键键（`<uid>` = 登录账号 id）

| 键 | 作用 | 实测值 |
| --- | --- | --- |
| `<uid>:AI.agent.model.model_list_map` | 真实模型目录（按 agent label 分组） | `solo_agent` 23 个，含 `deepseek-v4.1-flash`、`glm-5.3-flash` |
| `<uid>:AI.agent.model.recent_user_selection_by_agent_label` | 新建会话的权威选择 | `{"solo_agent":{"modelId":"solo_agent_1__deepseek-v4.1-flash","mode":0}}` |
| `<uid>:AI.agent.model.session_selected_model` | 每会话选择 | 启动时被应用**整表重写** |
| `<uid>_AI.agent.model.selected_model` | 完整模型对象 | 写入后仍在，但界面不读 |
| `localModelData_<uid>_<agentRecordId>` | 当前 Agent 的模型对象 | 写入后界面仍显示默认 |
| `solo-lite-mode-state-map-<uid>` | `work/code/design` + 当前 session | `session.workspacePath/local_folder/mainFolder/mode` |
| `solo-lite.local-project-folders` / `project-workspace-cache` | 工作区登记 | `{id: "盘符:\路径"}` |
| `solo-lite:route:<uid>` | 当前路由 | `/session/<id>?env=local` |

模型 id 组成：`<agentLabel>_<config_source>__<name>`，例如
`solo_agent_1__deepseek-v4.1-flash`。解析器只接受目录中真实存在的条目，未知模型直接 `MODEL_UNAVAILABLE`。

### 2.3 零输入对话（已验证）

1. `Trae CN.exe --remote-debugging-port=9342 --remote-allow-origins=* --new-window --folder-uri=file:///...`
   - 必须先移除继承的 `ELECTRON_RUN_AS_NODE` / `NODE_OPTIONS`，否则 Electron 以 Node 模式启动并立即退出。
2. CDP `Runtime.evaluate`：`execCommand('insertText')` 写入 composer（`[contenteditable=true]`）。
3. 点击 `.chat-input-v2-send-button`（源码 `workbench.desktop.main.js:3777` 的发送类）。
4. 实测：提示词出现在会话中，界面进入「思考中」→ 出现「排队提醒：当前模型请求量较高，你目前排在 826 位」。

### 2.4 模型切换：先验证失败，后改用 CDP 页面级鼠标事件**通过**

**失败路径（已证伪，保留记录）**

- 写 `recent_user_selection_by_agent_label`、`session_selected_model`、`selected_model`、`localModelData_*`，并**先停应用再写、再启动**：输入框仍显示 `Seed-Code` / `Auto`。
- `Runtime.evaluate` 合成 `click` / `mousedown` / `PointerEvent` 到 `.model-select-area`、`.model-select-area-model`、`.icd-model-select-trigger`：菜单**不展开**（组件忽略非受信任事件）。

**成功路径（2026-11-04 实测）**

`Input.dispatchMouseEvent` 在渲染进程内派发页面级鼠标事件（不移动 OS 光标、不调用 `SendInput`）：

1. 取 `.model-select-area` 的矩形中心；
2. `mouseMoved` → `mousePressed` → `mouseReleased`，菜单展开；
3. 按行文本匹配目标模型（如 `DeepSeek-V4.1-Flash`），点击该行中心；
4. 回读 `.model-select-area-model` 校验。

实测：

```text
visible before: Seed-Code
select ok: True
actual: {"visible_model": "DeepSeek-V4.1-Flash", "clicked": "DeepSeek-V4.1-Flash"}
visible after: DeepSeek-V4.1-Flash
```

菜单实测列出 `DeepSeek-V4.1-Flash 0.12x`、`GLM-5.3-Flash 0.08x`、`GLM-5.3 0.78x`、
`DeepSeek-V4-Pro 0.72x` 等（第三方插件所称"flash 仅 IDE 通道"在本机版本已不成立）。

适配器流程改为：写工作区 → 起 CDP（等 page target）→ **在渲染器里选模型** → 回读可见模型 → 再发送；
任何不一致抛 `MODEL_MISMATCH` / `MODEL_SWITCH_UNSUPPORTED`，绝不把任务发给错误模型。

实测记录：`run_c13ccdd526e5`、`run_e03efb244dcc` 的 `CHAIN01` 均以
`deepseek-v4.1-flash` 完成，成果包含真实缺陷结论（`add()` 返回 `a - b`）与验证 `passed`。

---

## 3. 实跑链路记录

### 3.1 用户指定链路（Trae → Qoder → OpenCode）

**早期失败记录（保留以说明模型切换问题的发现过程）**

```text
run_ce89f6bc3836  status=failed
CHAIN01 trae_cn   deepseek-v4.1-flash            failed  MODEL_MISMATCH（显示 Auto）
CHAIN02 qoder_cn  gfmodel                        blocked 前置任务失败
CHAIN03 opencode  opencode/glm-5.3-flash-free    queued
```

**模型切换打通后的重跑**

```text
run_e03efb244dcc
CHAIN01 trae_cn   deepseek-v4.1-flash            completed / passed
CHAIN02 qoder_cn  gfmodel                        completed / passed
CHAIN03 opencode  opencode/glm-5.3-flash-free    running（免费模型长任务，未在观察窗口内返回）
```

Trae 段成果包（真实模型输出，节选）：

```json
{"summary":"calc.py 的 add 函数实现为减法（return a - b）而非加法，属于逻辑错误级缺陷",
 "findings":[{"id":"F1","severity":"high","title":"add 函数误用减号，返回差值而非和值",
              "location":{"file":"calc.py","lines":"1-2","function":"add"}}],
 "commands_run":["python -c \"...print('add(2,3)=', calc.add(2,3))...\""]}
```

**结论：Trae + DeepSeek-V4.1-Flash 已能零 OS 输入完成真实任务并产出结构化成果；**
整条三段链的第 1、2 段已 `completed + passed`，第 3 段受 OpenCode 免费模型长任务延迟影响，
在观察窗口内未返回（接口本身已由单次直连验证）。

### 3.2 已验证的**单段**真实执行（Qoder CN + GLM-5.3-Flash）

```text
run_2775f49a9756 / run_184d3757f03b / run_573da792c8fb
CHAIN01 qoder_cn gfmodel  ->  completed, verification=passed
```

真实证据（`logs/chain-20261104/final-result.json`、`final-gui.log`）：
- `chat_sessions.model=gfmodel`；transcript `runtime-config.model=gfmodel`、`assistant.model=gfmodel`
- transcript `workspace-directories=<任务 worktree>`；daemon `open_session cwd=<任务 worktree>`
- 回复内容（真实模型输出）：`calc.py 存在 1 个高严重度缺陷（第 2 行 add 函数返回 a - b，与 test_calc.py::test_add 期望的 add(2, 3) == 5 矛盾）`
- 控制平面确定性验证：`passed`

OpenCode 单次直连验证：

```text
opencode run -m opencode/glm-5.3-flash-free "只回复一行..."
-> AGENTMESH_OPENCODE_OK_20261104   (7.3s, exit 0)
```

### 3.3 两段真实链路：未完成（如实记录）

| run | 阶段 1（Qoder） | 阶段 2（OpenCode） | 终止原因 |
| --- | --- | --- | --- |
| `run_184d3757f03b` | completed / passed | blocked | 上一次中断残留的 `file_leases` 把 `tests/**` 占住（已修复：`release_stale` + `recover` 释放） |
| `run_573da792c8fb` | completed / passed | running > 9 分钟，未写任何文件 | 免费模型长任务延迟/排队；已带 `--auto`，未出现权限等待，但模型侧未返回 |

**没有拿到“Trae → Qoder → OpenCode 三段全绿”的结果**。链路编排本身由 14 个自动化用例（mock CLI Agent）证明；真实执行只完成了 Qoder 段。

### 3.4 本轮修复的真实缺陷（均为实跑暴露）

| 缺陷 | 现象 | 修复 |
| --- | --- | --- |
| 继承 `ELECTRON_RUN_AS_NODE=1` | 拉起的 Qoder/Trae 以 Node 模式启动后立即退出，CDP 永不就绪 | `electron_env()` / `clean_launch_env()` 剔除此变量 |
| `HTTP_PROXY` 劫持 loopback | `/json/version` 返回 502，误判应用未启动 | `cdp.py`、`interfaces.py`、`trae_interface.py` 全部显式 `ProxyHandler({})` |
| OpenCode 非交互缺 `--auto` | 需要写文件时卡在权限确认，无任何输出 | 默认加 `--auto`（可显式关闭） |
| 中断后遗留 `file_leases` | 后续可写任务被“文件范围与其他任务冲突”永久阻塞 | `LeaseManager.release_stale()` + `recover` 清理 |
| 同一 Agent 多阶段不同模型 | 最后一次 `set_agent_model` 覆盖全局，前序阶段静默用错模型 | `configure_chain` 显式拒绝并报错 |
| Trae 模型写入 ≠ 生效 | 界面仍显示 Auto/Seed-Code | 发送前读取 `.model-select-area-model`，不一致即 `MODEL_MISMATCH` |

---

## 4. 每款 Agent 的“指定工作区 / 指定模型”接口速查

| Agent | 指定工作区 | 指定模型 |
| --- | --- | --- |
| Trae / Trae CN | `state.vscdb`: `solo-lite.local-project-folders` + `project-workspace-cache` + `mode-state-map.session.workspacePath`；启动 `--folder-uri` | `state.vscdb`: `recent_user_selection_by_agent_label.modelId`（写入成功，**生效未通过**） |
| Qoder CN | `main.sqlite`: `workspaces.root_paths_json` + `chat_sessions.cwd/workspace_id/execution_target_json`；transcript `workspace-directories`；重启 | `chat_sessions.model` + transcript `runtime-config.model`（`dfmodel`/`gfmodel`/`gmodel`） |
| Qoder 国际版 | 同上（`%APPDATA%\com.qoder.app.stable\main.sqlite`） | 同上（`chat_model_preferences` 含 `gfmodel`） |
| WorkBuddy | `workbuddy://task?action=start&cwd=<path>` | `...&model=glm-5.3-flash` |
| OpenCode | 进程 cwd / `--dir` | `-m opencode/glm-5.3-flash-free` |
| ZCode | `cli/db/db.sqlite` `session.directory/path`（仅读） | `local_setting(namespace=model)`（仅读） |
| DSH | `~/.dsh/sessions/--<path>--/`（仅读） | `settings.yaml`（仅读） |
| Cursor | `--folder-uri` | `cursor/initialModelState`（仅读） |

---

## 5. 已知限制

1. **Trae 模型切换依赖 CDP 页面级事件**：`Runtime.evaluate` 的合成点击无效，必须用
   `Input.dispatchMouseEvent`（只进渲染进程，不碰 OS）。若应用改版导致 `.model-select-area`
   消失，会返回 `NO_MODEL_SELECTOR` 并中止。
2. **CDP 需要重启应用**：Trae/Qoder 只有在带 `--remote-debugging-port` 启动时才可编程；系统会在 `AGENTMESH_ALLOW_APP_RESTART=1` 且先备份状态库后才重启。
3. **GUI 子进程生命周期**：从工具会话直接启动的 GUI 进程会随父进程结束；生产使用应由常驻的 `agentmesh serve` 进程拉起。
4. **ZCode / DSH / Cursor**：只做到状态库读取与工作区事实确认，未找到零输入发送通道，未做任何输入合成。
5. **未安装的 4 款 CLI**：`claude_code` / `gemini_cli` / `codex_cli` / `aider` 仅登记公开 CLI 参数，未在本机验证。
