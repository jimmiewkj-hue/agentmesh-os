"""Command line interface for AgentMesh OS (package: agentloop_hub)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .errors import HubError
from .hub import Hub


def _print(obj: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, (list, dict)):
                print(f"{key}:")
                print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
            else:
                print(f"{key}: {value}")
    elif isinstance(obj, list):
        for item in obj:
            print(json.dumps(item, ensure_ascii=False, default=str) if isinstance(item, (dict, list)) else item)
    else:
        print(obj)


def _hub(args: argparse.Namespace) -> Hub:
    project = Path(args.project).expanduser() if args.project else Path.cwd()
    # Read-only commands must not silently register real agents on the machine.
    return Hub(project, auto_discover=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentmesh", description="AgentMesh OS — 本地多 Agent 协同操作系统")
    parser.add_argument("--version", action="version", version=f"AgentMesh OS {__version__}")
    parser.add_argument("--project", default=os.environ.get("AGENTLOOP_PROJECT", ""),
                        help="项目根目录（默认当前目录）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="打开或创建项目工作空间")
    sub.add_parser("status", help="显示项目总览")
    sub.add_parser("doctor", help="环境与 Agent 健康检查")
    sub.add_parser("connectors", help="Agent Connector 统一能力清单")
    surface = sub.add_parser("surface", help="统一 5 能力矩阵（零 OS 输入证据）")
    surface.add_argument("--agent", default="", help="只输出指定 Agent")
    models = sub.add_parser("models", help="Agent 支持的模型清单")
    models.add_argument("--refresh", action="store_true", help="重新探测本机 Agent 模型")
    models.add_argument("--sync", action="store_true", help="全量同步真实模型目录（含显示名/倍率）")
    models.add_argument("--agent", default="", help="只刷新指定 Agent")
    versions = sub.add_parser("versions", help="Agent 软件版本与兼容漂移")
    versions.add_argument("--agent", default="")
    versions.add_argument("--status", action="store_true")

    ask = sub.add_parser("ask", help="单一入口：目标进，结构化计划与审批出")
    ask.add_argument("goal")
    ask.add_argument("--agent", default="")
    ask.add_argument("--model", default="")

    solve = sub.add_parser("solve", help="单入口闭环：切分 → 角色选型 → 运行 → 汇总答案")
    solve.add_argument("goal")
    solve.add_argument("--executor", default="", help="执行模型 agent:model（走免费额度）")
    solve.add_argument("--commander", default="", help="统帅模型 agent:model（只做决策与汇总）")
    solve.add_argument("--max-tasks", type=int, default=4)
    solve.add_argument("--max-parallel", type=int, default=1)
    solve.add_argument("--no-start", action="store_true", help="只生成计划与绑定，不运行")
    solve.add_argument("--polish", action="store_true", help="用汇总角色润色最终答案")
    solve.add_argument("--block", action="store_true", help="前台等待运行结束后输出答案")

    segment = sub.add_parser("segment", help="只看任务切分与角色选型（不运行）")
    segment.add_argument("goal")
    segment.add_argument("--executor", default="")
    segment.add_argument("--commander", default="")
    segment.add_argument("--max-tasks", type=int, default=4)

    roles = sub.add_parser("roles", help="角色策略：列出/解析/导入/保存")
    roles_sub = roles.add_subparsers(dest="roles_command", required=True)
    roles_sub.add_parser("list", help="列出所有角色在当前真实模型目录下的候选阶梯")
    rresolve = roles_sub.add_parser("resolve", help="解析单个角色的候选")
    rresolve.add_argument("role")
    rresolve.add_argument("--executor", default="")
    rresolve.add_argument("--commander", default="")
    rresolve.add_argument("--limit", type=int, default=6)
    rimport = roles_sub.add_parser("import", help="从 opencode 配置或 AGENTS.md 导入角色偏好")
    rimport.add_argument("path")
    rimport.add_argument("--save", action="store_true", help="写入 <项目>/.agentloop/roles.json")
    rsave = roles_sub.add_parser("save", help="把当前策略写入 .agentloop/roles.json")
    rsave.add_argument("--path", default="")

    # agents
    sub.add_parser("agents", help="列出已注册 Agent")
    agent = sub.add_parser("agent", help="Agent 管理")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    add = agent_sub.add_parser("add", help="注册 Agent")
    add.add_argument("--file", help="包含 AgentRecord 字段的 JSON/YAML 文件")
    add.add_argument("--id", dest="agent_id")
    add.add_argument("--name")
    add.add_argument("--product")
    add.add_argument("--edition", default="")
    add.add_argument("--adapter", default="file_relay",
                     choices=["cli", "opencode", "file_relay", "manual", "session_only", "mcp"])
    add.add_argument("--level", default="L2", choices=["L0", "L1", "L2", "L3", "L4"])
    add.add_argument("--command", help="CLI 可执行文件")
    add.add_argument("--config-json", help="adapter_config JSON")
    add.add_argument("--models", default="", help="逗号分隔的模型列表")
    rm = agent_sub.add_parser("remove")
    rm.add_argument("agent_id")
    health = agent_sub.add_parser("health")
    health.add_argument("agent_id")
    sub.add_parser("discover", help="重新探测本机安装的 Agent")

    # main
    main = sub.add_parser("main", help="主节点管理")
    main_sub = main.add_subparsers(dest="main_command", required=True)
    main_set = main_sub.add_parser("set")
    main_set.add_argument("agent_id")
    main_set.add_argument("--model", default="")
    main_set.add_argument("--budget", type=int, default=0)
    main_auto = main_sub.add_parser("auto", help="按路由评分自动选择可自动执行的主节点")
    main_auto.add_argument("--agent", default="")
    main_auto.add_argument("--model", default="")
    main_sub.add_parser("show")
    main_sub.add_parser("history")

    # capability router
    route = sub.add_parser("route", help="能力路由")
    route_sub = route.add_subparsers(dest="route_command", required=True)
    rrank = route_sub.add_parser("rank")
    rrank.add_argument("--task-uid", default="")
    rrank.add_argument("--task-type", default="analysis")
    rrank.add_argument("--required", default="")
    rtask = route_sub.add_parser("task")
    rtask.add_argument("uid")
    route_sub.add_parser("scores")
    rdec = route_sub.add_parser("decisions")
    rdec.add_argument("--run", default="")
    rdec.add_argument("--limit", type=int, default=100)

    # context bus
    bus = sub.add_parser("bus", help="Context Bus")
    bus_sub = bus.add_subparsers(dest="bus_command", required=True)
    blast = bus_sub.add_parser("list")
    blast.add_argument("--after", type=int, default=0)
    blast.add_argument("--limit", type=int, default=100)
    blast.add_argument("--topic", default="")
    blast.add_argument("--kind", default="")
    blast.add_argument("--correlation", default="")
    bus_sub.add_parser("stats")
    bus_sub.add_parser("verify")
    bus_sub.add_parser("subscriptions")
    bsub = bus_sub.add_parser("subscribe")
    bsub.add_argument("subscriber")
    bsub.add_argument("--topic", action="append", default=[])
    bsub.add_argument("--kind", action="append", default=[])
    bpoll = bus_sub.add_parser("poll")
    bpoll.add_argument("subscription_id")
    bpoll.add_argument("--limit", type=int, default=100)
    bunsub = bus_sub.add_parser("unsubscribe")
    bunsub.add_argument("subscription_id")
    bpub = bus_sub.add_parser("publish")
    bpub.add_argument("kind")
    bpub.add_argument("--payload", default="{}", help="JSON 字符串或 @file")
    bpub.add_argument("--source", default="user")
    bpub.add_argument("--correlation", default="")
    bpub.add_argument("--topic", default="")
    brep = bus_sub.add_parser("replay")
    brep.add_argument("--kind", default="")
    brep.add_argument("--topic", default="")
    brep.add_argument("--after", type=int, default=0)
    brep.add_argument("--limit", type=int, default=500)

    sub.add_parser("benchmark", help="Agent 基准评测")

    sync = sub.add_parser("sync", help="五级统一同步")
    sync.add_argument("--agent", default="")
    sync.add_argument("--limit", type=int, default=20)
    sync.add_argument("--include-unscoped", action="store_true")
    sync_sub = sync.add_subparsers(dest="sync_command")
    sync_sub.add_parser("status")
    sync_sub.add_parser("bootstrap")

    cache = sub.add_parser("cache", help="语义缓存")
    cache_sub = cache.add_subparsers(dest="cache_command", required=True)
    cache_sub.add_parser("stats")
    cache_sub.add_parser("clear")

    release = sub.add_parser("release", help="发布门禁")
    release.add_argument("run_id")

    budget = sub.add_parser("budget", help="项目预算")
    budget_sub = budget.add_subparsers(dest="budget_command", required=True)
    bset = budget_sub.add_parser("set")
    bset.add_argument("tokens", type=int)
    budget_sub.add_parser("show")

    # plan
    plan = sub.add_parser("plan", help="规划")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)
    preq = plan_sub.add_parser("request")
    preq.add_argument("goal")
    preq.add_argument("--agent", default="")
    preq.add_argument("--model", default="")
    pimp = plan_sub.add_parser("import")
    pimp.add_argument("file")
    pimp.add_argument("--agent", default="")
    pimp.add_argument("--model", default="")
    plan_sub.add_parser("list")
    pshow = plan_sub.add_parser("show")
    pshow.add_argument("plan_id")
    papp = plan_sub.add_parser("approve")
    papp.add_argument("plan_id")
    papp.add_argument("--approval", default="")
    papp.add_argument("--note", default="")
    prej = plan_sub.add_parser("reject")
    prej.add_argument("plan_id")
    prej.add_argument("--approval", default="")
    prej.add_argument("--note", default="")

    # run
    run = sub.add_parser("run", help="运行")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    rstart = run_sub.add_parser("start")
    rstart.add_argument("run_id")
    rstart.add_argument("--parallel", type=int, default=None)
    rstart.add_argument("--background", action="store_true")
    rresume = run_sub.add_parser("resume")
    rresume.add_argument("run_id")
    rresume.add_argument("--parallel", type=int, default=None)
    rresume.add_argument("--background", action="store_true")
    rstatus = run_sub.add_parser("status")
    rstatus.add_argument("run_id")
    run_sub.add_parser("list")

    # task
    task = sub.add_parser("task", help="任务")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    tlist = task_sub.add_parser("list")
    tlist.add_argument("--run", default="")
    tshow = task_sub.add_parser("show")
    tshow.add_argument("uid")
    tshow.add_argument("--level", default="findings", choices=["summary", "findings", "evidence", "raw"])
    tres = task_sub.add_parser("result")
    tres.add_argument("uid")
    tres.add_argument("--file", required=True)
    tsub = task_sub.add_parser("submit-result")
    tsub.add_argument("uid")
    tsub.add_argument("--file", required=True)
    tretry = task_sub.add_parser("retry")
    tretry.add_argument("uid")
    tcancel = task_sub.add_parser("cancel")
    tcancel.add_argument("uid")

    # approval
    approval = sub.add_parser("approval", help="审批")
    approval_sub = approval.add_subparsers(dest="approval_command", required=True)
    approval_sub.add_parser("list")
    aapp = approval_sub.add_parser("approve")
    aapp.add_argument("approval_id")
    aapp.add_argument("--note", default="")
    arej = approval_sub.add_parser("reject")
    arej.add_argument("approval_id")
    arej.add_argument("--note", default="")

    # merge / rollback
    merge = sub.add_parser("merge", help="合并已批准成果")
    merge.add_argument("run_id")
    merge.add_argument("--approval", default="")
    merge.add_argument("--commit", action="store_true")
    rollback = sub.add_parser("rollback", help="回滚")
    rb_sub = rollback.add_subparsers(dest="rollback_command", required=True)
    rb_task = rb_sub.add_parser("task")
    rb_task.add_argument("uid")
    rb_file = rb_sub.add_parser("file")
    rb_file.add_argument("uid")
    rb_file.add_argument("path")
    rb_run = rb_sub.add_parser("run")
    rb_run.add_argument("run_id")

    # memory
    memory = sub.add_parser("memory", help="统一记忆")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    mlist = memory_sub.add_parser("list")
    mlist.add_argument("--type", default="")
    mlist.add_argument("--status", default="")
    madd = memory_sub.add_parser("add")
    madd.add_argument("--type", required=True)
    madd.add_argument("--title", required=True)
    madd.add_argument("--body", required=True)
    madd.add_argument("--source", default="user")
    madd.add_argument("--confidence", type=float, default=0.8)
    msearch = memory_sub.add_parser("search")
    msearch.add_argument("keyword")
    mupdate = memory_sub.add_parser("update")
    mupdate.add_argument("memory_id")
    mupdate.add_argument("--title")
    mupdate.add_argument("--body")
    mupdate.add_argument("--injectable", type=int, choices=[0, 1])
    mupdate.add_argument("--status")
    mdelete = memory_sub.add_parser("delete")
    mdelete.add_argument("memory_id")
    mresolve = memory_sub.add_parser("resolve")
    mresolve.add_argument("memory_id")
    mresolve.add_argument("--keep", action="store_true")
    memory_sub.add_parser("export")
    mboot = memory_sub.add_parser("bootstrap", help="Agent 启动引导包")
    mboot.add_argument("--agent", default="")
    mboot.add_argument("--scope", default="")
    memory_sub.add_parser("sync", help="立即同步项目索引/任务/工作流")

    # sessions
    session = sub.add_parser("session", help="历史会话")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    simp = session_sub.add_parser("import")
    simp.add_argument("--agent", required=True)
    simp.add_argument("--limit", type=int, default=20)
    simp.add_argument("--include-unscoped", action="store_true")
    slist = session_sub.add_parser("list")
    slist.add_argument("--agent", default="")

    # handoff
    handoff = sub.add_parser("handoff", help="主节点交接")
    handoff_sub = handoff.add_subparsers(dest="handoff_command", required=True)
    hbuild = handoff_sub.add_parser("build")
    hbuild.add_argument("--note", default="")
    hswitch = handoff_sub.add_parser("switch")
    hswitch.add_argument("--agent", required=True)
    hswitch.add_argument("--model", default="")
    hswitch.add_argument("--note", default="")

    rec = sub.add_parser("recover", help="故障恢复")
    rec.add_argument("--run", default="")
    rec.add_argument("--auto-resume", action="store_true")

    audit = sub.add_parser("audit", help="审计")
    audit.add_argument("--verify", action="store_true")
    audit.add_argument("--limit", type=int, default=200)

    costs = sub.add_parser("costs", help="成本")
    costs.add_argument("--run", default="")

    serve = sub.add_parser("serve", help="启动本地 Web 控制台")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)

    return parser


def _normalize_global_args(argv: list[str]) -> list[str]:
    """Allow --project/--json after the subcommand (common PowerShell usage)."""
    out = list(argv)
    flags: list[str] = []
    for flag in ("--json",):
        while flag in out:
            out.remove(flag)
            flags.append(flag)
    i = 0
    project_args: list[str] = []
    while i < len(out):
        item = out[i]
        if item == "--project" and i + 1 < len(out):
            project_args = ["--project", out[i + 1]]
            del out[i:i + 2]
            continue
        if item.startswith("--project="):
            project_args = [item]
            out.pop(i)
            continue
        i += 1
    return flags + project_args + out


def main(argv: list[str] | None = None) -> int:
    raw = list(argv if argv is not None else sys.argv[1:])
    argv = _normalize_global_args(raw)
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            from .api.server import serve

            serve(Path(args.project).expanduser() if args.project else Path.cwd(),
                  host=args.host, port=args.port, open_browser=False)
            return 0

        hub = _hub(args)
        result = _dispatch(hub, args)
        _print(result, as_json=args.json)
        hub.close()
        return 0
    except HubError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2) if args.json
              else f"错误[{exc.code}]: {exc.message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def _dispatch(hub: Hub, args: argparse.Namespace) -> Any:
    cmd = args.command
    if cmd == "init":
        hub.discover(merge=True)
        return hub.overview()
    if cmd == "status":
        return hub.overview()
    if cmd == "doctor":
        return hub.doctor()
    if cmd == "connectors":
        return hub.connector_manifest()
    if cmd == "surface":
        from .control.agent_surface import agent_surface, surface_report
        if args.agent:
            return agent_surface(args.agent, hub.project.project_id)
        return surface_report(hub.project.project_id)
    if cmd == "models":
        if args.sync:
            return hub.sync_models(args.agent)
        if args.refresh:
            return hub.refresh_agent_models(args.agent)
        return hub.agent_models()
    if cmd == "versions":
        return hub.agent_version_status() if args.status else hub.agent_versions(args.agent)
    if cmd == "ask":
        return hub.ask(args.goal, main_agent_id=args.agent, model=args.model)
    if cmd == "segment":
        executor_agent, executor_model = _split_pin(args.executor)
        commander_agent, commander_model = _split_pin(args.commander)
        return hub.segment_goal(
            args.goal, max_tasks=args.max_tasks,
            executor_agent=executor_agent, executor_model=executor_model,
            commander_agent=commander_agent, commander_model=commander_model)
    if cmd == "solve":
        return _solve_command(hub, args)
    if cmd == "roles":
        return _roles_command(hub, args)
    if cmd == "route":
        return _route_command(hub, args)
    if cmd == "bus":
        return _bus_command(hub, args)
    if cmd == "benchmark":
        return hub.benchmark()
    if cmd == "sync":
        return _sync_command(hub, args)
    if cmd == "cache":
        if args.cache_command == "clear":
            return hub.clear_semantic_cache()
        return hub.cost_report()["semantic_cache"]
    if cmd == "release":
        return hub.request_release(args.run_id)
    if cmd == "agents":
        return [a.to_dict() for a in hub.list_agents()]
    if cmd == "discover":
        return [a.to_dict() for a in hub.discover(merge=True)]
    if cmd == "agent":
        return _agent_command(hub, args)
    if cmd == "main":
        if args.main_command == "set":
            return hub.set_main(args.agent_id, args.model, budget_tokens=args.budget)
        if args.main_command == "auto":
            return hub.auto_main(agent_id=args.agent, model=args.model)
        if args.main_command == "show":
            return hub.get_main()
        return hub.main_history()
    if cmd == "budget":
        if args.budget_command == "set":
            return hub.set_project_budget(args.tokens)
        return hub.costs()
    if cmd == "plan":
        return _plan_command(hub, args)
    if cmd == "run":
        return _run_command(hub, args)
    if cmd == "task":
        return _task_command(hub, args)
    if cmd == "approval":
        return _approval_command(hub, args)
    if cmd == "merge":
        return hub.merge(args.run_id, approval_id=args.approval, commit=args.commit)
    if cmd == "rollback":
        if args.rollback_command == "task":
            return hub.rollback_task(args.uid)
        if args.rollback_command == "file":
            return hub.rollback_file(args.uid, args.path)
        return hub.rollback_run(args.run_id)
    if cmd == "memory":
        return _memory_command(hub, args)
    if cmd == "session":
        if args.session_command == "import":
            return hub.import_sessions(args.agent, limit=args.limit,
                                      include_unscoped=args.include_unscoped)
        return hub.conversations(agent_id=args.agent)
    if cmd == "handoff":
        if args.handoff_command == "switch":
            return hub.handoff(to_agent_id=args.agent, model=args.model, note=args.note)
        return hub.handoff(note=args.note)
    if cmd == "recover":
        return hub.recover(run_id=args.run, auto_resume=args.auto_resume)
    if cmd == "audit":
        return hub.audit_verify() if args.verify else hub.audit_events(limit=args.limit)
    if cmd == "costs":
        return hub.costs(run_id=args.run)
    raise ValueError(f"未知命令 {cmd}")


def _split_pin(value: str) -> tuple[str, str]:
    """``agent:model`` -> (agent, model). Empty input -> ("", "")."""
    text = str(value or "").strip()
    if not text:
        return "", ""
    agent, sep, model = text.partition(":")
    if not sep or not model:
        raise HubError(f"模型绑定格式应为 agent:model，收到 {text!r}")
    return agent.strip(), model.strip()


def _solve_command(hub: Hub, args: argparse.Namespace) -> Any:
    import time

    executor_agent, executor_model = _split_pin(args.executor)
    commander_agent, commander_model = _split_pin(args.commander)
    result = hub.solve(
        args.goal,
        executor_agent=executor_agent, executor_model=executor_model,
        commander_agent=commander_agent, commander_model=commander_model,
        max_tasks=args.max_tasks, max_parallel=args.max_parallel,
        start=not args.no_start,
        background=False if (args.block or not args.no_start) else True,
        polish=args.polish,
    )
    if args.no_start or not result.get("run_id"):
        return result
    run_id = result["run_id"]
    if not args.block:
        result["answer"] = hub.aggregate_run(run_id).get("conclusion", "")
        return result
    deadline = time.time() + 60 * 30
    settled = {"completed", "failed", "pending_acceptance", "pending_merge",
               "released", "canceled", "paused"}
    while time.time() < deadline:
        status = hub.run_status(run_id)
        if (status.get("run") or {}).get("status") in settled:
            break
        time.sleep(2)
    result["run"] = hub.run_status(run_id)
    result["answer"] = hub.aggregate_run(run_id, polish=args.polish)
    return result


def _roles_command(hub: Hub, args: argparse.Namespace) -> Any:
    if args.roles_command == "list":
        return hub.role_policy()
    if args.roles_command == "resolve":
        executor_agent, executor_model = _split_pin(args.executor)
        commander_agent, commander_model = _split_pin(args.commander)
        return hub.resolve_role(
            args.role, limit=args.limit,
            executor_agent=executor_agent, executor_model=executor_model,
            commander_agent=commander_agent, commander_model=commander_model)
    if args.roles_command == "import":
        return hub.import_role_policy(args.path, save=args.save)
    if args.roles_command == "save":
        return hub.save_role_policy(args.path)
    raise HubError(f"未知 roles 子命令: {args.roles_command}")


def _route_command(hub: Hub, args: argparse.Namespace) -> Any:
    if args.route_command == "task":
        return hub.route_task(args.uid)
    if args.route_command == "scores":
        return hub.router_scores()
    if args.route_command == "decisions":
        return hub.routing_decisions(run_id=args.run, limit=args.limit)
    if args.task_uid:
        return hub.route_task(args.task_uid)
    required = [r for r in args.required.split(",") if r]
    return hub.route_preview(task_type=args.task_type, required=required)


def _bus_command(hub: Hub, args: argparse.Namespace) -> Any:
    if args.bus_command == "list":
        return hub.bus_events(after_seq=args.after, limit=args.limit, topic=args.topic,
                              kind=args.kind, correlation_id=args.correlation)
    if args.bus_command == "stats":
        return hub.bus_stats()
    if args.bus_command == "verify":
        return hub.bus_verify()
    if args.bus_command == "subscriptions":
        return hub.bus_subscriptions()
    if args.bus_command == "subscribe":
        return hub.bus_subscribe(args.subscriber, topics=args.topic, kinds=args.kind)
    if args.bus_command == "poll":
        return hub.bus_poll(args.subscription_id, limit=args.limit)
    if args.bus_command == "unsubscribe":
        return hub.bus_unsubscribe(args.subscription_id)
    if args.bus_command == "publish":
        payload_text = args.payload
        if payload_text.startswith("@"):
            payload_text = Path(payload_text[1:]).read_text(encoding="utf-8")
        payload = json.loads(payload_text)
        if not isinstance(payload, dict):
            raise ValueError("payload 必须是 JSON 对象")
        return hub.bus_publish(args.kind, payload, source=args.source,
                               correlation_id=args.correlation, topic=args.topic)
    return hub.bus.replay(kind=args.kind, topic=args.topic, after_seq=args.after,
                          limit=args.limit)


def _sync_command(hub: Hub, args: argparse.Namespace) -> Any:
    if getattr(args, "sync_command", None) == "status":
        return hub.sync_status()
    if getattr(args, "sync_command", None) == "bootstrap":
        return hub.bootstrap(agent_id=args.agent)
    return hub.run_sync(agent_id=args.agent, limit=args.limit,
                        include_unscoped=args.include_unscoped)


def _agent_command(hub: Hub, args: argparse.Namespace) -> Any:
    if args.agent_command == "add":
        if args.file:
            text = Path(args.file).read_text(encoding="utf-8")
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                import yaml
                data = yaml.safe_load(text)
            return hub.register_agent(data).to_dict()
        if args.config_json:
            config = json.loads(args.config_json)
        elif args.adapter == "mcp":
            config = {"mode": "mcp", "command": args.command or "", "args": []}
        else:
            config = {
                "command": [args.command] if args.command else [],
                "args": ["{prompt_file}"],
                "prompt_mode": "file",
            }
        return hub.register_agent({
            "agent_id": args.agent_id, "name": args.name or args.agent_id,
            "product": args.product or args.name or args.agent_id,
            "edition": args.edition, "adapter_type": args.adapter,
            "access_level": args.level, "adapter_config": config,
            "models": [m for m in args.models.split(",") if m],
        }).to_dict()
    if args.agent_command == "remove":
        hub.remove_agent(args.agent_id)
        return {"status": "removed", "agent_id": args.agent_id}
    return hub.agent_health(args.agent_id)


def _plan_command(hub: Hub, args: argparse.Namespace) -> Any:
    if args.plan_command == "request":
        return hub.request_plan(args.goal, main_agent_id=args.agent, model=args.model)
    if args.plan_command == "import":
        return hub.import_plan(args.file, main_agent_id=args.agent, model=args.model)
    if args.plan_command == "list":
        return hub.plans()
    if args.plan_command == "show":
        return hub.plan(args.plan_id)
    if args.plan_command == "approve":
        return hub.approve_plan(args.plan_id, approval_id=args.approval, note=args.note)
    return hub.reject_plan(args.plan_id, approval_id=args.approval, note=args.note)


def _run_command(hub: Hub, args: argparse.Namespace) -> Any:
    if args.run_command == "start":
        return hub.start_run(args.run_id, max_parallel=args.parallel, background=args.background)
    if args.run_command == "resume":
        return hub.resume_run(args.run_id, max_parallel=args.parallel, background=args.background)
    if args.run_command == "status":
        return hub.run_status(args.run_id)
    return hub.runs()


def _task_command(hub: Hub, args: argparse.Namespace) -> Any:
    if args.task_command == "list":
        return hub.tasks(run_id=args.run)
    if args.task_command == "show":
        return hub.task_detail(args.uid, level=args.level)
    if args.task_command in ("result", "submit-result"):
        return hub.submit_result(args.uid, args.file)
    if args.task_command == "retry":
        return hub.retry_task(args.uid)
    return hub.cancel_task(args.uid)


def _approval_command(hub: Hub, args: argparse.Namespace) -> Any:
    if args.approval_command == "list":
        return hub.approvals_all()
    approve = args.approval_command == "approve"
    return hub.decide_approval(args.approval_id, approve=approve, note=args.note)


def _memory_command(hub: Hub, args: argparse.Namespace) -> Any:
    if args.memory_command == "list":
        return hub.memory_list(type=args.type, status=args.status)
    if args.memory_command == "add":
        return hub.memory_add(type=args.type, title=args.title, body=args.body,
                              source=args.source, confidence=args.confidence)
    if args.memory_command == "search":
        return hub.memory_search(args.keyword)
    if args.memory_command == "update":
        fields = {}
        if args.title:
            fields["title"] = args.title
        if args.body:
            fields["body"] = args.body
        if args.injectable is not None:
            fields["injectable"] = bool(args.injectable)
        if args.status:
            fields["status"] = args.status
        return hub.memory_update(args.memory_id, **fields)
    if args.memory_command == "delete":
        return hub.memory_delete(args.memory_id)
    if args.memory_command == "resolve":
        return hub.memory_resolve(args.memory_id, keep=args.keep)
    if args.memory_command == "bootstrap":
        return hub.bootstrap(agent_id=args.agent, scope=args.scope)
    if args.memory_command == "sync":
        return hub.run_sync()
    return hub.memory_list(limit=10000)


if __name__ == "__main__":
    raise SystemExit(main())
