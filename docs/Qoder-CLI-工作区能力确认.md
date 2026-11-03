# Qoder CN 命令行 / 深链接打开工作区能力确认

日期：2026-11-03

## 结论

**Qoder CN 当前安装版本不具备“通过 CLI 参数或深链接指定工作区”的可编程能力。**

实测的路径参数、`--new-window`、`--reuse-window`、`--folder-uri`、`--user-data-dir`、`--workspace`、`--folder`、`--cwd` 以及 `qoder-cn://` 深链接均未打开新窗口或改变 daemon 的 `open_session cwd`。daemon 日志仍为：

```text
[DaemonSession] sessionId=... session opened cwd=C:\Users\<user> workerArgs=49
```

按用户约束：**立即停止，不修改 app.asar，不跑两轮回归**。需要用户另行授权路径 1 的 app.asar 修改（客户端 workspace/cwd 注入），或接受 Qoder 当前仅 GUI 可用的现实。

## 证据

### 1. `--help` / `--version`

```powershell
Qoder CN.exe --version   # stdout/stderr 为空，进程立即退出
Qoder CN.exe --help      # stdout/stderr 为空，进程立即退出
Qoder CN.exe -h          # stdout/stderr 为空，进程立即退出
```

说明该应用不提供 CLI 帮助输出；参数被单实例转发/忽略。

### 2. app.asar 参数解析搜索

对 `app.asar` 搜索 `--workspace|--folder|--project|--cwd|--open`：
- 未找到 Qoder 自有解析分支；仅命中 pdfjs 等第三方文本。
- 搜索 `process.argv` 只找到内嵌 helper（local-bootstrap、SSH askpass、manifest 读取），未见“打开工作区”分支。
- 搜索 `setAsDefaultProtocolClient|open-url|qoder-cn://` 未找到工作区深链接处理器；仅 `product.json` 有 `"protocolScheme": "qoder-cn"`。

### 3. 协议注册（只读）

注册表：

```text
HKCU\Software\Classes\qoder-cn\shell\open\command
  "C:\ProgramData\Qoder CN\Qoder CN Launcher\Qoder CN Launcher.exe" "%1"

HKCU\Software\Classes\qoder\shell\open\command
  "C:\ProgramData\Qoder\Qoder Launcher\Qoder Launcher.exe" "%1"
```

协议存在，但只是把 URL 交给 Launcher；未找到把 URL 中的 `cwd/path` 转为 workspace 的代码。

### 4. 实测参数与 daemon 回读

测试工作区：`D:\qoder\agent-hub\logs\cli-workspace-20261103\ws_arg_test`（独立 git clone）。

| 启动方式 | 结果 | daemon 最后一次 `open_session cwd` |
|---|---|---|
| `Qoder CN.exe <path>` | Launcher 立即退出，无新窗口 | `C:\Users\<user>`（未新增会话） |
| `Qoder CN.exe --new-window <path>` | 同上 | `C:\Users\<user>` |
| `Qoder CN.exe --reuse-window <path>` | 同上 | `C:\Users\<user>` |
| `Qoder CN.exe --folder-uri file:///...` | 同上 | `C:\Users\<user>` |
| `Qoder CN.exe --user-data-dir <profile> --new-window <path>` | 未产生新进程/新窗口 | `C:\Users\<user>` |
| `Qoder CN.exe --workspace <path>` | 立即退出 | `C:\Users\<user>` |
| `Qoder CN.exe --folder <path>` | 立即退出 | `C:\Users\<user>` |
| `Qoder CN.exe --cwd <path>` | 立即退出 | `C:\Users\<user>` |
| `Qoder CN Launcher.exe --new-window <path>` | 立即退出，仅原窗口 | `C:\Users\<user>` |
| `qoder-cn://open?path=...` | 无新窗口/无新会话 | `C:\Users\<user>` |

原文日志：

```text
2026-11-03T13:04:36.394Z [DaemonSession] sessionId=6c19564f-ae10-4d32-805e-d00f734c003f session opened cwd=C:\Users\<user> workerArgs=49
```

日志路径：

`C:\Users\<user>\AppData\Roaming\com.qodercn.app.stable\logs\20261103-100642.669-34080-e5cd4741\qodercli\qoder-agent-sdk.log`

### 5. 单实例影响

Qoder 是单实例应用：上述 CLI 启动器均立即退出，参数被转发给已有实例，但已有实例没有打开目标 workspace，也没有新增 daemon 会话。未关闭用户当前窗口，未修改用户会话。

## 安全边界执行情况

- 未修改 Qoder 应用本体 / app.asar。
- 未修改注册表（仅只读查询 `qoder-cn`/`qoder` 协议命令）。
- 未安装证书。
- 未恢复/覆盖 Qoder 备份，未删除用户数据。
- 未创建 junction（上一条任务已确认锚点为 home 根并拒绝）。
- 未运行 A/B 回归（CLI 能力不成立）。

## 下一步选项

1. **申请修改 app.asar（路径 1）**：在客户端 agent query 的 workspace URI/cwd 构造处注入 run workspace；需要用户明确授权，并保留可回退备份。
2. **等待 Qoder 官方支持**：`--workspace`/`qoder-cn://` 工作区深链接或 CLI 打开目录能力。
3. 若只做验证性工作，可继续使用 WorkBuddy 深链接；Qoder 侧标记为环境限制。
