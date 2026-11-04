# dsh-connect-trae-plus 实现思路分析 与 本项目 Trae 接入方案

- 分析对象：`https://dsh-plugin.org/plugins/dph-1121/dsh-connect-trae-plus`
- 上游仓库：`DPH-1121/dsh-connect-trae-plus`（MIT，TypeScript，fork 自 `dingminhua/dsh-connect-trae` v2.0.4）
- 分析日期：2026-11-04
- 结论先行：**它的思路是"把 Trae 账号的 token 变成一个本地 OpenAI 兼容端点"，属于纯 HTTP API 路线；
  本项目不采用这条路线（封号风险 + 与既定零输入约束冲突），但从中提取了两个关键事实，并用应用自身 UI 打通了 Trae 的模型切换。**

---

## 1. 插件做了什么

| 环节 | 实现 |
| --- | --- |
| 账号发现 | 扫描 `%APPDATA%\Trae CN` / `TRAE SOLO CN` / `Trae` / `TRAE SOLO` 下的 `User\globalStorage\storage.json` |
| 区域判定 | 读凭证中的 `userRegion`，再用 host 后缀与 edition 兜底 |
| 应用版本头 | 读 `resources\app\product.json` 的 `appVersion`，请求带 `x-app-version` / `x-ide-version` |
| Token 隔离 | 每个区域一个随机端口 + 进程内随机 secret 的 **loopback shim**；真实 token 不下发给 DSH 适配层 |
| 模型目录 | `POST /api/ide/v1/get_detail_param`，`function=solo_work_lite` |
| 对话上游 | `POST https://trae-api-cn.mchost.guru/api/agent/v3/llm_utils_chat`（国际版 `coresg-normal.trae.ai`），`function=solo_work_lite` |
| 协议转换 | Trae SSE（`metadata` / `timing_cost` / `output` / `extra_info` / `token_usage` / `done`）→ OpenAI SSE `tool_calls` |
| 用量查询 | `https://api.trae.cn/trae/api/v2/pay/*`、`/api/v2/ug/*`，只读不消耗额度 |
| 注册方式 | DSH 内注册两个 provider：`trae`（国内）、`trae-global`（国际） |

请求体核心字段：

```json
{
  "messages": [{"role": "user", "content": [{"type": "text", "text": "..."}]}],
  "function": "solo_work_lite",
  "stream": true,
  "config_name": "glm-5.2",
  "model": "glm-5.2"
}
```

---

## 2. 它的关键事实（对我们有用）

1. **模型分通道**：插件只覆盖 **Trae SOLO** 通道；`deepseek-v4.1-flash`、`glm-5.3-flash`、
   `kimi-k2.8-preview`、`qwen3.8-flash` 被标注为 **TraeCode / Trae IDE 通道**，SOLO 通道"未开放、无调用路由"。
2. **鉴权素材位置**：`%APPDATA%\<Trae>\User\globalStorage\storage.json` 里的
   `iCubeAuthInfo://icube.cloudide`（JWT）等字段。
3. **实际可用模型**（插件口径）：`DeepSeek-V4-Flash-Official`、`DeepSeek-V4-Pro-Official`、`GLM-5.3`、
   `GLM-5.2`、`Kimi-K3`、`MiniMax-M3`、`Qwen3.8-Max`、`Doubao-Seed-*`。
4. **风险点**：直接以客户端身份调用厂商私有端点；一旦被风控识别为"非官方客户端/异常频率"，
   账号可能被限流或封禁；token 也进入了第三方插件的内存。

---

## 3. 为什么本项目不照搬

| 约束 | 说明 |
| --- | --- |
| 用户既定要求 | "不要将 agent 软件转为纯 HTTP API，会遭到供应商封号" |
| 项目零输入原则 | 现有执行链只允许应用自身接口（CLI / deeplink / CDP DOM / 本地 store / stdio），不代理厂商推理 API |
| 可维护性 | 私有端点随时变更，且 `function` / header 需要跟随客户端版本漂移 |

因此：**不实现 token→`llm_utils_chat` 的桥**，也不把 Trae 账号当成本项目的"模型供应商"。

---

## 4. 从插件线索到本项目打通（实测）

### 4.1 插件信息已过时：SOLO 菜单里其实有 DeepSeek-V4.1-Flash

用 CDP 打开 Trae CN 1.107.1 的 SOLO 输入框模型菜单，实际列出：

```text
内置模型
Auto Mode
Seed-2.1-Turbo        0.20x
Seed-2.1-Pro-0915     0.80x
Seed-Evolving         0.80x
Seed-Code             0.06x
GLM-5.3-Flash         0.08x
GLM-5.3-FlashX        0.31x
GLM-5.3               0.78x
DeepSeek-V4.1-Flash   0.12x
DeepSeek-V4-Pro       0.72x
Kimi-K3.1-Pro         1.20x
Kimi-K3               1.83x
MiMo-V2.6-Flash       0.06x
MiMo-V2.6-Pro         0.15x
MiniMax-M3.1-Flash    0.09x
MiniMax-M3            0.26x
Step-5-Preview        0.48x
Qwen3.8-Flash         0.08x
Qwen3.8-Max           1.50x
```

即：**用户要求的 `DeepSeek-V4.1-Flash` 与 `GLM-5.3-Flash` 在本机 SOLO 通道就是可选的**，
只是第三方插件没有覆盖到（或版本落后）。

### 4.2 之前为什么切不动

- 写 `state.vscdb` 的 `recent_user_selection_by_agent_label` / `session_selected_model`：启动后被应用整表重写。
- 用 `Runtime.evaluate` 合成 `click` / `PointerEvent`：该 React 组件不响应非受信任事件，菜单不展开。

### 4.3 现在怎么切（已验证）

用 **CDP 页面级鼠标事件** `Input.dispatchMouseEvent`：

1. 取 `.model-select-area` 的 `getBoundingClientRect()`；
2. 依次派发 `mouseMoved` / `mousePressed` / `mouseReleased`；
3. 菜单渲染后，按可见行文本匹配目标模型，取该行中心坐标再点击；
4. 回读 `.model-select-area-model` 文本确认。

> 这不是 OS 键鼠合成：没有 `SetCursorPos`、没有 `SendInput`、没有移动系统光标，事件只进入该渲染进程自己的事件循环。
> 项目对 **OS 输入** 的硬锁（`AGENTMESH_DISABLE_GUI_INPUT=1`）保持不变。

实测：

```text
visible before: Seed-Code
select ok: True
actual: {"visible_model": "DeepSeek-V4.1-Flash", "clicked": "DeepSeek-V4.1-Flash"}
visible after: DeepSeek-V4.1-Flash
```

### 4.4 落地代码

- `agentloop_hub/control/trae_interface.py`
  - `open_trae_model_menu(port)`：打开菜单
  - `list_trae_model_items(port)`：解析可见模型行（含坐标）
  - `select_trae_model_via_cdp(model, port)`：点击 + 回读校验
  - `set_trae_model(..., cdp_port=...)`：**先 UI 切换，再写 store 持久化**
  - `ensure_trae_cdp(...)`：等待 **page target** 就绪（不再只等 `/json/version`）
- `agentloop_hub/adapters/gui.py`
  - Trae 分支改为：写工作区 → 起 CDP → **在渲染器里选模型** → 读取可见模型 → 再发送
  - 任何一步不一致直接抛 `MODEL_MISMATCH` / `MODEL_SWITCH_UNSUPPORTED`，不发送错误模型

---

## 5. 与插件方案的对比

| 维度 | dsh-connect-trae-plus | 本项目 |
| --- | --- | --- |
| 通道 | 厂商私有 HTTP（`llm_utils_chat`） | 应用自身 UI（CDP 页面事件 + 本地 store） |
| 模型 | SOLO 通道，插件口径不含 flash | SOLO 菜单实测全部可选，含 `DeepSeek-V4.1-Flash` / `GLM-5.3-Flash` |
| 凭证 | 读 `storage.json` 的 JWT，经 loopback shim 使用 | 不读 token、不调用厂商端点 |
| 封号风险 | 存在（非官方客户端调用） | 不增加（就是本人在用客户端） |
| 工具执行 | DSH 本地执行，模型只出 tool_calls | Trae 自己执行工具，控制平面只收发任务 |
| 适用 | 想让任意 OpenAI 客户端用 Trae 额度 | 想让 Trae 作为本项目的一个 Agent |

---

## 6. 复现

```bash
export AGENTMESH_DISABLE_GUI_INPUT=1
export AGENTMESH_ALLOW_APP_RESTART=1
export AGENTMESH_TRAE_CDP_PORT=9342

python - <<'PY'
from agentloop_hub.control import trae_interface as ti
ti.ensure_trae_cdp(app="trae_cn", port=9342)
print(ti.trae_visible_model(9342))
print(ti.select_trae_model_via_cdp("DeepSeek-V4.1-Flash", port=9342).actual)
PY
```

预期输出：切换前 `Seed-Code`，切换后 `DeepSeek-V4.1-Flash`。

---

## 7. 仍未采用/未验证

- 未实现 `llm_utils_chat` 桥；若将来要做，应放在**可选插件**里并默认关闭，且明确告知封号风险。
- 国际版 Trae 的菜单结构未单独实测（代码按同一 profile 支持，需在装有国际版的机器上复跑）。
- 模型菜单若被应用改版（类名变化），`open_trae_model_menu` 会返回 `NO_MODEL_SELECTOR`，
  适配器会中止而不是盲点。
