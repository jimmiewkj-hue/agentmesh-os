# 独立需求验证套件（verification/）

本目录是**独立于 `tests/` 的需求验收套件**：只依据 `01-需求整理-v1.docx` 的需求条文编写，
不 import 项目自带的测试夹具，避免"用项目自己的脚手架证明项目自己合格"。

## 运行

```bash
cd D:/qoder/agent-hub

# 需求级验证（32 个用例，约 31s）
python -m pytest verification -q

# 真实机能力审计（发现本机 Agent / 模型 / 路由结果，只读，不执行任务）
python verification/audit_real_machine.py
```

## 用例命名约定

| 前缀 | 含义 | 判定 |
| --- | --- | --- |
| `test_Rn_...` | 需求第 n 条应当成立 | 通过 = 需求被满足 |
| `test_GAP_...` | 缺口探针 | **通过 = 缺口被证实**（不是缺陷） |

## 零输入接口与新链路测试（2026-11-04）

| 文件 | 覆盖 |
| --- | --- |
| `test_trae_interface.py` | Trae `state.vscdb` 明文校验、真实模型目录（含 `deepseek-v4.1-flash` / `glm-5.3-flash`）、未知模型拒绝、模型/工作区/模式写入与回读、工作区不一致检测、deeplink 形状、`ELECTRON_RUN_AS_NODE` 清理、零 OS 输入守卫 |
| `test_chain.py` | 每阶段 Agent+模型钉住、未知模型拒绝、三段 DAG 顺序、成果落在隔离 worktree、`/api/chain` HTTP 端点 |
| `reverse_audit.py` | 本机全部已安装 Agent 的只读逆向审计 → `reverse-audit.json` |
| `audit_real_machine.py` | 需求基线视角的真机 Agent/模型/路由快照 |

Trae 测试写入的是真实 `state.vscdb` 的**临时副本**，不会改动用户状态库；真实应用重启与 CDP 只在
`AGENTMESH_ALLOW_APP_RESTART=1` 且人工确认的 E2E 中发生。

## 需求映射

| 需求（01-需求整理-v1） | 用例 |
| --- | --- |
| 主节点不是 Agent，是 Controller | `test_R1_main_node_is_never_routed_as_executor`、`test_R1_control_plane_contains_no_llm_client` |
| 模块1 统一 `{agent_id, capability[]}`、12 类产品 | `test_R2_manifest_exposes_unified_agent_and_capability`、`test_R2_all_products_named_by_requirement_are_profiled`、`test_R2_offline_relay_agent_reports_no_capability` |
| 模块2 Level 1-5 同步 + Agent 启动引导 | `test_R3_*`（3 个） |
| 模块3 Prompt/Response/FileChange/TaskState/Knowledge 全量过总线、只追加 | `test_R4_*`（3 个） |
| 模块4 依赖 / 优先级 / 重试 / 熔断 | `test_R5_*`（3 个） |
| 模块5 能力门槛 + 成功率/验证/速度/成本评分 | `test_R6_*`（3 个） |
| 模块6 压缩 / 语义缓存 / 结果复用 / Benchmark | `test_R7_*`（3 个） |
| 模块7 架构 / 验收 / 合并 / 发布四道门禁 | `test_R8_*`（3 个） |
| 用户只面对一个入口 | `test_R9_one_goal_is_enough_to_generate_a_plan`、`test_R9_http_single_entry_accepts_one_goal` |
| 可观测性（审计链、版本台账） | `test_R11_*` |
| 缺口探针 | `test_GAP_router_drops_all_rejection_reasons...`、`test_GAP_most_products_still_need_a_human...`、`test_GAP_offline_cli_agent_is_still_marked_auto_executable`、`test_GAP_independent_verifier_executes_in_the_live_workspace`、`test_GAP_overview_module_status_is_a_hardcoded_literal`、`test_GAP_cost_in_usd_cannot_be_measured...` |

## 夹具说明

- `conftest.py` 自建 Git 仓库、自建 Hub、自注册 mock CLI Agent（`adapter_type=cli`，真实子进程）。
- `mock_agent.py` 只实现 README 公开的文件契约：读 `AGENTLOOP_*` 环境变量、写 17 字段 `result.json`；
  `plan.json` / `summary.json` / `verification.json` 三种路径分别模拟主节点规划、主节点汇总、独立验证报告。
- 每个用例使用独立临时仓库，互不污染，也不触碰本仓库的 `.agentloop/`。

产物：`audit-real-machine.json` 为本机真实发现结果（14 Agent / 107 模型 / 路由结果）。
