# Qoder CN daemon 默认工作区锚点确认（junction 方案安全闸门）

日期：2026-11-03

## 结论

**Qoder CN daemon 的实际默认工作区锚点是 `C:\Users\<user>`（home 根），不是 home 下的专用子目录。**

按用户安全边界：锚点等于 home 根时**立即停止**，不创建任何 junction，不映射 home/AppData/文档/桌面。因此“受控 junction 映射”方案在当前实现下不可用，需要回到路径 1（客户端 workspace/cwd 注入）或路径 3（其他受控方案）。

本次未创建 junction，未修改 home 根、AppData、用户文档/桌面；用户 Qoder 备份未恢复、未覆盖。

## 证据

### 1. daemon 日志逐字记录

日志文件：

`C:\Users\<user>\AppData\Roaming\com.qodercn.app.stable\logs\20261103-100642.669-34080-e5cd4741\qodercli\qoder-agent-sdk.log`

原文（行号 42）：

```text
2026-11-03T10:06:49.434Z [DaemonSession] sessionId=0d7a74fe-f4a0-4099-8df2-1c0941e32dbd session opened cwd=C:\Users\<user> workerArgs=47
```

同一日志行 9/18/30/42 的所有 `open_session` 均为 `cwd=C:\Users\<user>`，与 Qoder UI 显示的 workspace 路径无关。

### 2. daemon 进程结构

```text
"C:\Program Files\Qoder\Qoder CN\Qoder CN.exe" "C:\Program Files\Qoder\Qoder CN\resources\app.asar.unpacked\node_modules\@qoder-ai\qoder-cn-agent-sdk\dist\_worker\qoder-worker-runtime.obf.mjs" --sdk
```

- 版本：`runtime-info.json` → `1.1.79-hotfix.7`
- 父进程：Qoder CN Electron 主进程（当前 PID 34080 → daemon 30192）
- `netstat -ano` 中该 daemon **没有 LISTENING 端口**，只有 443 出站连接；不存在可外部调用的本地 HTTP/RPC 端点。
- 因此 `open_session` 由 Electron 主进程通过 SDK stdio/control 通道发起，AgentMesh 无法从外部直接改请求。

### 3. SDK `open_session` 字段

SDK 源码（`app.asar` 内 `node_modules/@qoder-ai/qoder-cn-agent-sdk/dist/index.js`）：

```text
openSession({worker_args:[...], ...cwd:this.options.cwd, ...config_dir})
```

响应：

```text
case "open_session": { ... return {session_id}; }
```

结论：请求体里**有可选 `cwd` 字段**；本次 daemon 收到的是 `C:\Users\<user>`，即客户端传入了 home 根，而不是 daemon 自行选择。

### 4. 客户端 cwd 回退链

`electronMain.js` 中 `Nce(a,x)` 在 workspace URI 为空时使用：

```text
cwd: e || uY0(x.workspaceId, ...)
```

`uY0(a,x)` 用 `xt()`（用户 home）拼默认路径。本次实际请求的 `cwd` 为 home 根，说明客户端 workspace URI 为空并走了 home 回退。

### 5. 其他状态位置不构成锚点

- `main.sqlite` 的 `chat_sessions.cwd`/`workspace_id`/`execution_target_json` 已写为目标 run workspace；
- `~/.qoder-cn/sites/sessions/*.json` 的 `projectRoot`/`workspaceId` 已写为目标 workspace；
- transcript 的 `workspace-directories` 已重写为目标路径；
- UI 显示目标 workspace，但 daemon 日志仍为 `C:\Users\<user>`。

因此锚点不是 site/session/DB 中的 workspace 路径，而是 **daemon 实际收到的 cwd = home 根**。

## 安全判定

- 锚点 `C:\Users\<user>` 是 home 根：**禁止创建 junction**。
- 未执行任何 junction 创建/删除/覆盖。
- 未触碰 `AppData`、用户文档、桌面或 Qoder 备份。
- 本次没有运行新的 A/B 回归（按安全闸门停止）。

## 可行下一步

1. **路径 1（推荐）**：修改 Qoder 客户端 agent query 的 workspace URI/cwd 构造（`Nce`/`uY0`），让 `open_session` 携带 run workspace；需要改动应用本体，当前约束下未执行。
2. **路径 3（受控替代）**：如果未来确认 daemon 使用 home 下的专用子目录而非 home 根，才可对该专用子目录做 junction；当前证据为 home 根，方案不成立。
3. 在没有修复 `open_session cwd` 前，Qoder 自动化只能视为环境受限，不能用 junction 伪装通过。
