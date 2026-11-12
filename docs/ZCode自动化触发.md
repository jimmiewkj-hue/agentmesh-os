# ZCode 自动化触发（headless CLI 通道）

- 日期：2026-11-07
- 结论：**ZCode 已从 L2 人工接力升级为 L3 零输入自动执行（verified）**，与 OpenCode 同级的 CLI 通道，
  额外支持 provider+model 双重 pinning 与 `--resume` 会话续接。
- 证据：`verification/zcode-chain-evidence.json`（端到端真实任务）、`verification/reverse-audit.json`
  （zcode.status=verified）、`pytest tests/test_zcode_adapter.py`（10 用例）。

## 1. 触发接口（全部本机实测，ZCode 3.17.2 / zcode CLI 0.17.1）

### 1.1 CLI 核心位置与启动方式

ZCode 桌面安装自带完整 CLI 核心：

```text
C:\Program Files\ZCode\ZCode.exe                 # Electron 宿主
C:\Program Files\ZCode\resources\glm\zcode.cjs   # CLI 核心（15.4 MB bundle，version 0.17.1）
```

以 Node 模式运行（不打开桌面 GUI）：

```powershell
set ELECTRON_RUN_AS_NODE=1
"C:\Program Files\ZCode\ZCode.exe" "C:\Program Files\ZCode\resources\glm\zcode.cjs" --help
```

CLI 与桌面端共享 `~/.zcode`（登录凭据 `v2/credentials.json`、会话库 `cli/db/db.sqlite`、
rollout `cli/rollout/`），因此桌面登录状态对 headless 运行直接生效，无需重复登录。

### 1.2 非交互执行

```text
zcode -p "<任务指令>" --cwd <工作区绝对路径> --json
```

- `-p`：单条 prompt，headless 执行，权限模式默认 `yolo`（`--mode build|edit|plan|yolo|guarded` 可覆盖）；
- `--cwd`：钉住工作区（对应会话库 `session.directory`，与任务 worktree 一致）；
- `--json`：stdout 输出单个 JSON 对象：`sessionId` / `traceId` / `turnId` / `response` / `usage{inputTokens,outputTokens,cacheReadTokens,reasoningTokens?,totalTokens,...}`；
- `--resume <sess_...>`：续接既有会话（链路同 Agent 续跑用）；`-c` 续接当前目录最近会话；
- `--disallowed-tools` / `--attach` / `--target` 等参数与 TUI 共用。

真实示例（本机实测）：

```text
zcode -p "只回复一行文本：AGENTMESH_ZCODE_OK_20261107，不要调用任何工具。" --json
→ {"sessionId":"sess_46ef3e26-…","response":"AGENTMESH_ZCODE_OK_20261107",
   "usage":{"inputTokens":19947,"outputTokens":46,"totalTokens":19993,...}}
```

### 1.3 模型指定（provider + model 双重 pinning）

headless 模式**没有** `--model` 参数，默认选择「合并 provider 注册表中第一个可用模型」。
控制方法（2026-11-07 实测验证）：CLI 要求 `ZCODE_BUILTIN_PROVIDER_CONFIG_FILE` 与
`ZCODE_PERSONAL_PROVIDER_CONFIG_FILE` **成对提供**（`zcode.cjs` 内
`resolveNodeProviderRuntimePaths`：两者必须同时提供，否则按自身规则重新解析）。

适配器的做法：

1. 只读解析用户真实配置 `~/.zcode/v2/provider_config.json`；
2. 深拷贝生成临时副本，写入任务日志目录（`logs/zcode-provider-config.pinned.json`）：
   - `config.providerOrder` → 目标 provider 置顶；
   - 目标 provider 规则 `personalModelIds` / `modelOrder` → 目标模型置顶；
   - `modelConfigRules.providerModelRules` → 目标模型 `enabled:true`，同 provider 其余模型 `enabled:false`；
3. 环境变量指向该副本运行 CLI；**用户的真实 provider_config.json 从不被写入**
   （验证脚本对文件做了 before/after 字节比对）；
4. `ZCODE_MODEL_RETRY_MAX_RETRIES=1` 限制模型重试——否则 provider 业务错误
   （如订阅过期）会重试 11 次、指数退避拖 7 分钟以上。

实测路由证据（每次运行后读 `model_usage` 表核验）：

| 临时配置改动 | 实际请求路由 | 结果 |
| --- | --- | --- |
| providerOrder 置顶 deepseek（a5d4b862…） | `a5d4b862…/deepseek-v4-pro` | completed（2.85s，返回精确预期文本） |
| providerOrder 置顶 tokenplan + modelOrder 置顶 DeepSeek-V4.1-Flash | `83fe12d0…/DeepSeek-V4.1-Flash` | 该 provider 订阅无效（InvalidSubscription），但**路由精确命中** |
| `account:zai-start-plan` 放入 providerOrder | 仍走个人 provider | builtin/account 通道不参与 standalone CLI 的 providerOrder 排序 |

### 1.4 已排除的通道（不猜，逐一证伪）

| 通道 | 事实 |
| --- | --- |
| `zcode://` deeplink | 注册表存在（`ZCode.exe -- "%1"`），但 app.asar 内仅 `zcode://workspace/open?path=` 与 `zcode://oauth/callback` 两个路由，无发送能力 |
| 官方命名管道端点（`ZCODE_OFFICIAL_ENDPOINT`） | HTTP-over-named-pipe，token 鉴权通过，但路由白名单仅 `/api/v1/sites`（zcode-site 插件代理），非会话控制接口 |
| 桌面 RPC（`zcode-host` / `zcode-agent-service`） | Electron 主进程↔utility 进程内部 IPC，外部进程不可达 |
| TCP 端口 | ZCode 全部进程无监听端口 |
| `session_input` 表 | UI 发送记录（payload 带 `clientId`/`admissionSeq`），是结果不是注入点 |

### 1.5 与桌面订阅通道的关系

桌面 GUI 会话使用 OAuth 订阅通道 `account:zai-start-plan`（`GLM-5.3-Flash`，本机 58 次 completed）；
standalone CLI 的注册表不含该 OAuth 通道的授权（凭据虽共享，但 account provider 不参与
headless 的 providerOrder 排序）。因此 headless 执行走用户在 ZCode 设置里登记的
**个人 api-key provider**（本机实测可用：deepseek `api.deepseek.com`）。
这不违反「不调用厂商 LLM HTTP API」的原则边界：走的是 ZCode 应用自身的 provider 体系与用户自己的密钥，
不是绕过应用直连厂商接口。

## 2. 回读与核验

| 数据 | 位置 | 用途 |
| --- | --- | --- |
| 会话记录 | `~/.zcode/cli/db/db.sqlite` `session`（`directory` 与任务 workdir 比对） | 工作区绑定核验 |
| 模型请求 | 同库 `model_usage`（`provider_id`/`model_id`/`status`） | 模型 pinning 与完成状态核验 |
| 模型 I/O | `~/.zcode/cli/rollout/model-io-sess_*.jsonl` | 会话内容归档（`sessions/zcode.py` 已有读取器） |

适配器每次执行后做只读 readback（`extras["interface"]`）：
`session_confirmed`（session.directory == workdir）+ `model_completed`（model_usage.status==completed）。

## 3. 控制平面接入

- `agentloop_hub/adapters/zcode.py`：`ZcodeAdapter`（`AdapterType.ZCODE`）——
  模型目标解析（裸 id 按 providerOrder 优先级解析 / `provider/model` 直传）、
  临时 pinning 配置、`--resume` 续接、结果文件缺失时基于 `--resume` 的修复重试（与 OpenCode 同契约）、
  ProviderBusinessError 结构化透传（`error_code=PROVIDER_BUSINESS_ERROR`）、超时/退出码映射。
- `discovery.py`：检测 `resources\glm\zcode.cjs` 存在即升级 `zcode` 为
  `adapter_type=zcode` / `access_level=L3` / `structured` / `auto_executable`；
  不存在时保持 file_relay（L2）不虚构。
- `connectors.py` / `runner.py`：`zcode` 并入 auto-executable 与独立验证者候选（与 cli/opencode 同列）。
- 模型目录：沿用 `_models_from_provider_config` + rollout 提取的裸模型 id；
  执行期由适配器解析为 provider/model，不需要用户登记 provider 前缀。

## 4. 端到端真实任务证据（2026-11-07）

`python verification/verify_zcode_real.py`（真实 Git 仓库 + 真实 deepseek-v4-pro）：

```text
discovery: adapter_type=zcode, access_level=L3, cli_core=…\resources\glm\zcode.cjs
execute:   status=completed, session=sess_a37ffae0-…, duration=42125ms
usage:     input=150976 (cached 130048), output=3354, reasoning=2068
workspace: calc.py 已修复（return a + b），python test_calc.py 通过
result:    结构化成果包（summary/findings/commands_run/evidence）已写入 result.json
readback:  session.directory=任务仓库 ✓, model_usage=deepseek-v4-pro/completed ✓
安全:      用户真实 provider_config.json before/after 字节一致 ✓
```

运行痕迹：`~/.zcode/cli/db/db.sqlite`（sess_a37ffae0…）+
`~/.zcode/cli/rollout/model-io-sess_a37ffae0….jsonl`；任务工作区
`verification/zcode-chain-real/repo/` 保留修复后文件。

## 5. 已知边界

1. 桌面订阅通道（`account:zai-start-plan`）当前只有桌面 GUI 能用；headless 走个人 provider。
   若用户在 ZCode 内为 CLI 登记新的 api-key provider，适配器无需改动即可用（模型目录由每日同步刷新）。
2. `--json` 输出为单对象而非事件流；工具调用明细以 `tool_usage` 表 / rollout 为准，
   适配器仅统计 usage 汇总（与 OpenCode JSONL 事件流的差异点）。
3. ZCode CLI 自身无并发会话锁问题（每任务独立 worktree + 独立 session），但共享 `db.sqlite`
   （WAL 模式），与桌面端并发安全。
4. 无效 provider 会快速失败（`ZCODE_MODEL_RETRY_MAX_RETRIES=1`），由控制平面按角色阶梯升级重试。
