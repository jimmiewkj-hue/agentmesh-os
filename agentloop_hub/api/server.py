"""Local HTTP API and web console server (stdlib only)."""

from __future__ import annotations

import json
import mimetypes
import secrets
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from ..errors import (
    ConfigError, HubError, NotFoundError, PermissionDeniedError, ValidationError,
)
from ..control.fsbrowser import (
    create_directory as fs_create_directory,
    is_loopback_address,
    list_level as fs_list_level,
    recent_projects as fs_recent_projects,
    remember_project as fs_remember_project,
)
from ..hub import Hub
from ..util import new_id, now_iso, tail_text
from ..workspace.manager import WorkspaceManager


MAX_REQUEST_BODY_BYTES = 8 * 1024 * 1024
LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}


def _host_header_name(value: str) -> str:
    try:
        return str(urlparse("http://" + str(value or "")).hostname or "").lower()
    except ValueError:
        return ""


STATUS_MAP = {
    "not_found": HTTPStatus.NOT_FOUND,
    "validation_error": HTTPStatus.BAD_REQUEST,
    "config_error": HTTPStatus.BAD_REQUEST,
    "conflict": HTTPStatus.CONFLICT,
    "permission_denied": HTTPStatus.FORBIDDEN,
    "budget_exceeded": HTTPStatus.CONFLICT,
    "approval_required": HTTPStatus.CONFLICT,
    "adapter_error": HTTPStatus.BAD_GATEWAY,
    "adapter_timeout": HTTPStatus.GATEWAY_TIMEOUT,
    "result_invalid": HTTPStatus.UNPROCESSABLE_ENTITY,
    "state_transition_error": HTTPStatus.CONFLICT,
    "git_operation_error": HTTPStatus.CONFLICT,
}


class JobManager:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            for job in (data.get("jobs") if isinstance(data, dict) else []) or []:
                if not isinstance(job, dict) or not job.get("job_id"):
                    continue
                if job.get("status") == "running":
                    # The worker thread died with the previous process.
                    job["status"] = "interrupted"
                    job["error"] = {
                        "code": "service_restarted",
                        "message": "控制服务重启，任务是否完成需查看计划/运行记录",
                    }
                    job["business_status"] = "unknown"
                self._jobs[str(job["job_id"])] = job

    def _persist(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"jobs": sorted(self._jobs.values(),
                                      key=lambda j: j.get("created_at", ""), reverse=True)[:100]}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str),
                           encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass

    def submit(self, label: str, fn: Callable[[], Any]) -> str:
        job_id = new_id("job")
        job = {"job_id": job_id, "label": label, "phase": "running",
               "status": "running", "created_at": now_iso(),
               "result": None, "error": None, "business_status": "running"}
        with self._lock:
            self._jobs[job_id] = job
            self._persist()

        def runner() -> None:
            try:
                result = fn()
                job["result"] = result
                job["status"] = "completed"
                # Thread completion is not business completion: keep both facts.
                business = ""
                if isinstance(result, dict):
                    business = str(result.get("status") or "")
                job["business_status"] = business or "completed"
                job["phase"] = business or "completed"
            except Exception as exc:
                job["error"] = exc.to_dict() if isinstance(exc, HubError) else {
                    "code": "internal_error", "message": str(exc)
                }
                job["status"] = "failed"
                job["business_status"] = "failed"
                job["phase"] = "failed"
            finally:
                job["finished_at"] = now_iso()
                with self._lock:
                    self._persist()

        threading.Thread(target=runner, name=f"job-{job_id}", daemon=True).start()
        return job_id

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j["created_at"], reverse=True)
            return [dict(j) for j in jobs[:limit]]


class FixedHubProvider:
    """Compatibility wrapper used by tests that pass a ready Hub."""

    def __init__(self, hub: Hub):
        self._hub = hub

    @property
    def hub(self) -> Hub:
        return self._hub

    def require(self) -> Hub:
        return self._hub

    def info(self) -> dict[str, Any]:
        project = self._hub.project.to_dict()
        return {"open": True, "root": project["root"], "project": project, "error": ""}

    def open(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise ValidationError("该控制台绑定固定项目，不能切换工作区")

    def close(self) -> None:
        return None


class HubServer:
    def __init__(self, project_root: Path | None = None, *, host: str = "127.0.0.1",
                 port: int = 8787):
        self.project_root = Path(project_root) if project_root else None
        self.host = host
        self.port = port
        self.manager = WorkspaceManager(self.project_root)
        jobs_path = (Path(self.project_root) / ".agentloop" / "jobs.json"
                     if self.project_root else None)
        self.jobs = JobManager(jobs_path)
        handler = _make_handler(None, self.jobs, self.manager)
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self._sync_stop = threading.Event()
        threading.Thread(target=self._catalog_sync_loop, daemon=True,
                         name="agentmesh-catalog-sync").start()

    def _catalog_sync_loop(self) -> None:
        """Daily model-catalog sync while the console stays open."""
        while not self._sync_stop.wait(3600):
            hub = self.hub
            if not hub:
                continue
            try:
                if hub.models_sync_status()["stale"]:
                    hub.sync_models()
            except Exception:
                continue

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def hub(self) -> Hub | None:
        return self.manager._hub

    def serve_forever(self, *, open_browser: bool = False) -> None:
        if open_browser:
            threading.Timer(0.6, lambda: webbrowser.open(self.url)).start()
        print(f"AgentMesh OS Web Console: {self.url}")
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.httpd.server_close()
            self.manager.close()

    def shutdown(self) -> None:
        self._sync_stop.set()
        self.httpd.shutdown()


def serve(project_root: Path | None, *, host: str = "127.0.0.1", port: int = 8787,
          open_browser: bool = False) -> None:
    HubServer(project_root, host=host, port=port).serve_forever(open_browser=open_browser)


def _sync_models_if_stale(hub: Hub) -> None:
    try:
        if hub.models_sync_status()["stale"]:
            hub.sync_models()
    except Exception:
        pass


def _make_handler(hub: Hub | None, jobs: JobManager,
                  manager: WorkspaceManager | FixedHubProvider | None = None):
    provider = manager if manager is not None else (FixedHubProvider(hub) if hub else None)
    if provider is None:
        raise ConfigError("需要 Hub 或 WorkspaceManager")
    static_dir = Path(__file__).parent / "static"
    # Per-process control token.  The UI reads it from /api/session; a
    # cross-origin page cannot read that response (no CORS headers) and every
    # mutating request must carry it.
    auth_token = secrets.token_urlsafe(32)

    def current_hub() -> Hub:
        return provider.require()  # type: ignore[union-attr]

    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentLoopHub/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            # Keep the console quiet; jobs and errors are in the audit log.
            return

        # -- helpers ---------------------------------------------------
        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > MAX_REQUEST_BODY_BYTES:
                raise ValidationError(
                    f"请求体过大（{length} 字节，上限 {MAX_REQUEST_BODY_BYTES}）")
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValidationError(f"请求体不是有效 JSON: {exc}")
            return data if isinstance(data, dict) else {"value": data}

        def _drain_body(self) -> None:
            """Read and discard a rejected request body before replying.

            On Windows a client still writing a body can otherwise see
            WSAECONNABORTED instead of the intended 403/415 response.
            """
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            if 0 < length <= MAX_REQUEST_BODY_BYTES:
                try:
                    self.rfile.read(length)
                except OSError:
                    pass

        def _error(self, exc: Exception) -> None:
            if isinstance(exc, HubError):
                status = STATUS_MAP.get(exc.code, HTTPStatus.INTERNAL_SERVER_ERROR)
                self._json(status, {"error": exc.to_dict()})
            else:
                # The public response carries an actionable id; the traceback
                # stays in the local console log only.
                error_id = uuid.uuid4().hex[:12]
                print(f"[api-error {error_id}] {type(exc).__name__}: {exc}\n"
                      f"{traceback.format_exc()}", file=sys.stderr)
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                           {"error": {"code": "internal_error",
                                      "message": "服务器内部错误，请用错误编号查看本地日志",
                                      "error_id": error_id}})

        def _require_local(self) -> None:
            """All control endpoints are loopback-only and host/origin checked."""
            peer = ""
            try:
                peer = str(self.client_address[0] or "")
            except (AttributeError, IndexError, TypeError):
                peer = ""
            if not is_loopback_address(peer):
                raise PermissionDeniedError(
                    f"控制接口仅允许本机访问（请求来自 {peer or '未知地址'}）")
            host = _host_header_name(str(self.headers.get("Host") or ""))
            if host and host not in LOCAL_HOSTNAMES:
                raise PermissionDeniedError(f"Host 不是本机地址: {host}")
            origin = str(self.headers.get("Origin") or "").strip()
            if origin:
                parsed = urlparse(origin)
                if (parsed.hostname or "").lower() not in LOCAL_HOSTNAMES:
                    raise PermissionDeniedError(f"跨源请求被拒绝: {origin}")

        def _guard_write(self, *, require_json: bool = True) -> None:
            try:
                self._require_local()
            except Exception:
                self._drain_body()
                raise
            token = str(self.headers.get("X-AgentMesh-Token") or "")
            if not secrets.compare_digest(token, auth_token):
                self._drain_body()
                raise PermissionDeniedError("缺少或错误的控制令牌")
            if require_json:
                ctype = str(self.headers.get("Content-Type") or "").split(";")[0]
                if ctype.strip().lower() != "application/json":
                    self._drain_body()
                    raise PermissionDeniedError("写入请求必须使用 application/json")
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_REQUEST_BODY_BYTES:
                    raise ValidationError(
                        f"请求体过大（{length} 字节，上限 {MAX_REQUEST_BODY_BYTES}）")

        def _require_loopback(self) -> None:
            """Filesystem browsing is a local-only capability.

            The console binds 127.0.0.1 by default, but a user can pass
            ``--host 0.0.0.0`` to share it on a LAN. Directory listing must not
            follow that flag: it would expose the whole disk to the network.
            """
            peer = ""
            try:
                peer = str(self.client_address[0] or "")
            except (AttributeError, IndexError, TypeError):
                peer = ""
            if is_loopback_address(peer):
                return
            raise PermissionDeniedError(
                f"文件浏览仅允许本机访问（请求来自 {peer or '未知地址'}）")

        def _static(self, path: str) -> bool:
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (static_dir / rel).resolve()
            if static_dir.resolve() not in target.parents and target != static_dir.resolve():
                return False
            if not target.exists() or not target.is_file():
                return False
            body = target.read_bytes()
            mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text") or mime.endswith("javascript") else ""))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True

        # -- routing ---------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
            try:
                if path == "/api/stream":
                    self._require_local()
                    self._stream(query)
                    return
                if path == "/api/session":
                    self._require_local()
                    self._json(200, {"token": auth_token,
                                     "host": self.headers.get("Host", "")})
                    return
                if path.startswith("/api/"):
                    self._require_local()
                    self._route_get(path, query)
                elif not self._static(path):
                    self.send_error(404)
            except Exception as exc:
                self._error(exc)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                self._guard_write()
                self._route_post(unquote(parsed.path), self._body())
            except Exception as exc:
                self._error(exc)

        def do_DELETE(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                self._guard_write(require_json=False)
                hub = current_hub()
                parts = unquote(parsed.path).strip("/").split("/")
                if parts[:2] == ["api", "agents"] and len(parts) == 3:
                    hub.remove_agent(parts[2])
                    self._json(200, {"status": "removed", "agent_id": parts[2]})
                else:
                    self._json(404, {"error": {"code": "not_found", "message": "接口不存在"}})
            except Exception as exc:
                self._error(exc)

        def _sse(self, event: str, data: Any) -> bool:
            payload = data if isinstance(data, str) else json.dumps(
                data, ensure_ascii=False, default=str)
            try:
                self.wfile.write(f"event: {event}\n".encode("utf-8"))
                for line in payload.splitlines() or [""]:
                    self.wfile.write(f"data: {line}\n".encode("utf-8"))
                self.wfile.write(b"\n")
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
                return False

        def _stream(self, query: dict[str, str]) -> None:
            """Server-Sent Events: bus events + task logs + periodic snapshot."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True
            after_seq = int(query.get("after_seq", "0") or 0)
            interval = min(10.0, max(0.4, float(query.get("interval", "1.5") or 1.5)))
            run_id = query.get("run_id", "")
            project_id = current_hub().project.project_id
            offsets: dict[str, int] = {}
            last_snapshot = 0.0
            last_ping = time.monotonic()
            if not self._sse("hello", {"status": "connected", "time": now_iso(),
                                       "project_id": project_id}):
                return
            try:
                while True:
                    hub = current_hub()
                    if hub.project.project_id != project_id:
                        # A cursor from another project must never be reused.
                        self._sse("project_changed", {
                            "from": project_id, "to": hub.project.project_id})
                        return
                    events = hub.bus_events(after_seq=after_seq, limit=400)["events"]
                    for event in events:
                        after_seq = int(event["seq"])
                        if not self._sse("bus", event):
                            return
                    now = time.monotonic()
                    active_run = run_id or ((hub.latest_run() or {}).get("run_id", ""))
                    if now - last_snapshot >= 3.0:
                        last_snapshot = now
                        tasks = hub.tasks(run_id=active_run, limit=500) if active_run else []
                        counts: dict[str, int] = {}
                        for task in tasks:
                            counts[task["status"]] = counts.get(task["status"], 0) + 1
                        snapshot = {
                            "time": now_iso(),
                            "run": hub.run_status(active_run) if active_run else None,
                            "task_counts": counts,
                            "tasks": [
                                {"uid": t["uid"], "task_id": t["task_id"], "title": t["title"],
                                 "status": t["status"], "agent": t["assigned_agent"],
                                 "verification": t["verification_status"]}
                                for t in tasks
                            ],
                            "pending_approvals": len(hub.approvals_pending()),
                            "costs": hub.budget.snapshot(run_id=active_run),
                            "agents": [
                                {"agent_id": a.agent_id, "status": a.status,
                                 "version": a.version,
                                 "selected_model": (a.extra or {}).get("selected_model", "")}
                                for a in hub.list_agents()
                            ],
                            "drift_count": hub.versions.status()["drift_count"],
                        }
                        if not self._sse("snapshot", snapshot):
                            return
                        # Tail stdout logs of active tasks as they grow.
                        for task in tasks:
                            if task["status"] not in ("running", "waiting_agent", "verifying"):
                                continue
                            task_dir = task.get("task_dir") or ""
                            log_path = Path(task_dir) / "logs" / "agent.stdout.log"
                            if not log_path.exists():
                                continue
                            try:
                                size = log_path.stat().st_size
                            except OSError:
                                continue
                            previous = offsets.get(task["uid"], 0)
                            if size < previous:
                                previous = 0
                            if size > previous:
                                try:
                                    with log_path.open("rb") as handle:
                                        handle.seek(previous)
                                        chunk = handle.read(min(size - previous, 65536))
                                except OSError:
                                    continue
                                offsets[task["uid"]] = previous + len(chunk)
                                text = chunk.decode("utf-8", errors="replace")
                                if not self._sse("logs", {
                                    "task_uid": task["uid"], "task_id": task["task_id"],
                                    "agent_id": task["assigned_agent"], "chunk": text,
                                }):
                                    return
                    if now - last_ping >= 15:
                        last_ping = now
                        if not self._sse("ping", {"time": now_iso()}):
                            return
                    time.sleep(interval)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            except Exception as exc:
                self._sse("error", {"message": str(exc)})

        def _route_get(self, path: str, query: dict[str, str]) -> None:
            parts = [p for p in path.strip("/").split("/") if p]
            if path == "/api/health":
                from ..control.zero_input import status as zero_status
                self._json(200, {"status": "ok", "time": now_iso(),
                                 "zero_input": zero_status()})
                return
            if path == "/api/workspace":
                self._json(200, provider.info())  # type: ignore[union-attr]
                return
            # Directory browsing is served before a workspace exists: the
            # picker is exactly how the user creates/opens their first project.
            if path == "/api/fs/list":
                self._require_loopback()
                self._json(200, fs_list_level(query.get("path", "")))
                return
            if path == "/api/workspaces/recent":
                self._require_loopback()
                self._json(200, {"projects": fs_recent_projects()})
                return
            hub = current_hub()
            if path == "/api/settings":
                self._json(200, hub.settings())
                return
            if path == "/api/overview":
                self._json(200, hub.overview())
                return
            if path == "/api/doctor":
                self._json(200, hub.doctor())
                return
            if path == "/api/agents":
                self._json(200, {"agents": [a.to_dict() for a in hub.list_agents()]})
                return
            if path == "/api/agents/models":
                self._json(200, hub.agent_models(
                    refresh=query.get("refresh", "false") == "true"))
                return
            if path == "/api/agents/models/sync":
                self._json(200, hub.models_sync_status())
                return
            if path == "/api/agents/versions":
                self._json(200, hub.agent_versions(
                    agent_id=query.get("agent_id", ""),
                    limit=int(query.get("limit", "200"))))
                return
            if path == "/api/agents/version-status":
                self._json(200, hub.agent_version_status())
                return
            if len(parts) == 4 and parts[1] == "agents" and parts[3] == "models":
                self._json(200, hub.agent_models()["by_agent"].get(parts[2], {}))
                return
            if len(parts) == 5 and parts[1] == "agents" and parts[3] == "mcp" and parts[4] == "tools":
                self._json(200, hub.mcp_tools(parts[2]))
                return
            if path == "/api/connectors":
                self._json(200, hub.connector_manifest())
                return
            if path == "/api/surface":
                from ..control.agent_surface import surface_report
                self._json(200, surface_report(hub.project.project_id,
                                               records=hub.list_agents()))
                return
            if len(parts) == 3 and parts[1] == "surface":
                from ..control.agent_surface import agent_surface
                try:
                    self._json(200, agent_surface(parts[2], hub.project.project_id,
                                                  records=hub.list_agents()))
                except KeyError as exc:
                    self._json(404, {"error": {"code": "not_found",
                                               "message": str(exc)}})
                return
            if len(parts) == 4 and parts[1] == "agents" and parts[3] == "health":
                self._json(200, hub.agent_health(parts[2]))
                return
            if len(parts) == 4 and parts[1] == "agents" and parts[3] == "ready":
                self._json(200, hub.agent_ready(parts[2]))
                return
            if path == "/api/main":
                self._json(200, {"main": hub.get_main(), "history": hub.main_history()})
                return
            if path == "/api/chain":
                self._json(200, hub.chain_status(query.get("run_id", "")))
                return
            if path == "/api/chain/draft":
                self._json(200, {"draft": hub.chain_draft()})
                return
            if path == "/api/models/recommend":
                self._json(200, hub.recommend_models(
                    task_type=query.get("task_type", "analysis"),
                    limit=int(query.get("limit", "8") or 8),
                    profile=query.get("profile", "balanced"),
                    objective=query.get("objective", ""),
                    heavy=str(query.get("heavy", "")).lower() in ("1", "true", "yes")))
                return
            if len(parts) == 4 and parts[1] == "runs" and parts[3] == "bindings":
                self._json(200, hub.run_bindings(parts[2]))
                return
            if path == "/api/roles":
                self._json(200, hub.role_policy())
                return
            if path == "/api/solve":
                self._json(200, hub.solve_status(query.get("run_id", "")))
                return
            if len(parts) == 3 and parts[1] == "roles":
                self._json(200, hub.resolve_role(parts[2]))
                return
            if path == "/api/plans":
                self._json(200, {"plans": hub.plans(limit=int(query.get("limit", "50")))})
                return
            if path == "/api/plans/watches":
                self._json(200, {"watches": hub.plan_watches()})
                return
            if len(parts) == 3 and parts[1] == "plans":
                self._json(200, hub.plan(parts[2]))
                return
            if path == "/api/runs":
                self._json(200, {"runs": hub.runs(limit=int(query.get("limit", "30")))})
                return
            if len(parts) == 3 and parts[1] == "runs":
                self._json(200, hub.run_status(parts[2]))
                return
            if path == "/api/tasks":
                run_id = query.get("run_id", "")
                self._json(200, {"tasks": hub.tasks(run_id=run_id,
                                                    limit=int(query.get("limit", "200")))})
                return
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "logs":
                task = hub.task(parts[2])
                logs = tail_text(Path(task.task_dir) / "logs" / "agent.stdout.log") if task.task_dir else ""
                self._json(200, {"task_uid": task.uid, "logs": logs})
                return
            if len(parts) == 3 and parts[1] == "patches":
                task = hub.task(parts[2])
                patch_path = Path(task.patch_path)
                if not patch_path.exists():
                    raise NotFoundError("该任务没有 Patch")
                text = patch_path.read_text(encoding="utf-8", errors="replace")
                self._json(200, {"task_uid": task.uid, "patch": text})
                return
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "attempts":
                task = hub.task(parts[2])
                attempts = []
                for row in hub.store.list_attempts(task.uid):
                    result_path = Path(str(row.get("result_path") or ""))
                    attempts.append({
                        "attempt_id": row.get("attempt_id"),
                        "attempt_number": row.get("attempt_number"),
                        "status": row.get("status"),
                        "purpose": row.get("purpose"),
                        "request_id": row.get("request_id"),
                        "session_id": row.get("session_id"),
                        "result_path": str(result_path),
                        "result_exists": bool(result_path.is_file()
                                              and result_path.stat().st_size > 0),
                        "error": str(row.get("error") or "")[:300],
                        "created_at": row.get("created_at"),
                        "finished_at": row.get("finished_at"),
                        "active": row.get("attempt_id") == task.active_attempt_id,
                    })
                self._json(200, {"task_uid": task.uid,
                                 "active_attempt_id": task.active_attempt_id,
                                 "attempts": attempts})
                return
            if len(parts) == 3 and parts[1] == "tasks":
                level = query.get("level", "findings")
                self._json(200, hub.task_detail(parts[2], level=level))
                return
            if path == "/api/approvals":
                pending = query.get("pending", "false") == "true"
                self._json(200, {"approvals": hub.approvals_pending() if pending else hub.approvals_all()})
                return
            if path == "/api/budget":
                self._json(200, hub.costs())
                return
            if path == "/api/costs":
                self._json(200, {"costs": hub.costs(run_id=query.get("run_id", "")),
                                 "usage": hub.usage()})
                return
            if path == "/api/audit":
                self._json(200, {"events": hub.audit_events(limit=int(query.get("limit", "200")),
                                                            offset=int(query.get("offset", "0")))})
                return
            if path == "/api/audit/verify":
                self._json(200, hub.audit_verify())
                return
            if path == "/api/memory":
                self._json(200, {"memories": hub.memory_list(
                    type=query.get("type", ""), status=query.get("status", ""))})
                return
            if path == "/api/conversations":
                self._json(200, {"conversations": hub.conversations(
                    agent_id=query.get("agent_id", ""))})
                return
            if path == "/api/bus":
                self._json(200, hub.bus_events(
                    after_seq=int(query.get("after_seq", "0")),
                    limit=int(query.get("limit", "100")),
                    topic=query.get("topic", ""), kind=query.get("kind", ""),
                    correlation_id=query.get("correlation_id", "")))
                return
            if path == "/api/bus/stats":
                self._json(200, hub.bus_stats())
                return
            if path == "/api/bus/verify":
                self._json(200, hub.bus_verify())
                return
            if path == "/api/bus/subscriptions":
                self._json(200, {"subscriptions": hub.bus_subscriptions()})
                return
            if path == "/api/router/rank":
                if query.get("task_uid"):
                    self._json(200, hub.route_task(query["task_uid"]))
                else:
                    required = [r for r in query.get("required", "").split(",") if r]
                    self._json(200, hub.route_preview(
                        task_type=query.get("task_type", "analysis"), required=required))
                return
            if path == "/api/router/scores":
                self._json(200, hub.router_scores())
                return
            if path == "/api/router/decisions":
                self._json(200, {"decisions": hub.routing_decisions(
                    run_id=query.get("run_id", ""), limit=int(query.get("limit", "100")))})
                return
            if path == "/api/benchmark":
                self._json(200, hub.cost.report())
                return
            if path == "/api/cost/report":
                self._json(200, hub.cost_report(run_id=query.get("run_id", "")))
                return
            if path == "/api/sync":
                self._json(200, hub.sync_status())
                return
            if path == "/api/memory/bootstrap":
                self._json(200, hub.bootstrap(agent_id=query.get("agent_id", ""),
                                              scope=query.get("scope", "")))
                return
            if path == "/api/tools/web_search":
                from ..control.tools import web_search
                self._json(200, web_search(
                    query.get("q", "") or query.get("query", ""),
                    max_results=int(query.get("max_results", "5") or 5)))
                return
            if len(parts) == 3 and parts[1] == "governance":
                self._json(200, hub.governance_status(parts[2]))
                return
            if path == "/api/jobs":
                self._json(200, {"jobs": jobs.list()})
                return
            if len(parts) == 3 and parts[1] == "jobs":
                job = jobs.get(parts[2])
                if not job:
                    raise NotFoundError("任务作业不存在")
                self._json(200, job)
                return
            if path == "/api/recovery":
                self._json(200, hub.recovery_report())
                return
            self._json(404, {"error": {"code": "not_found", "message": f"接口不存在: {path}"}})

        def _route_post(self, path: str, body: dict[str, Any]) -> None:
            parts = [p for p in path.strip("/").split("/") if p]
            if path == "/api/fs/mkdir":
                self._require_loopback()
                self._json(200, fs_create_directory(
                    str(body.get("path", "")), str(body.get("name", ""))))
                return
            if path in ("/api/workspace/init", "/api/workspace/open"):
                target = str(body.get("path", "")).strip()
                if not target:
                    raise ValidationError("请提供项目目录 path")
                result = provider.open(  # type: ignore[union-attr]
                    target,
                    create=bool(body.get("create", path.endswith("/init"))),
                    git_init=bool(body.get("git_init", path.endswith("/init"))),
                    name=str(body.get("name", "")),
                )
                hub = current_hub()
                # Daily catalog sync: refresh in the background when stale so
                # opening the console does not wait on model probing.
                threading.Thread(target=_sync_models_if_stale, args=(hub,),
                                 daemon=True).start()
                info = provider.info()  # type: ignore[union-attr]
                if info.get("open") and info.get("root"):
                    fs_remember_project(str(info["root"]),
                                        str(body.get("name", "") or ""))
                self._json(200, {
                    "workspace": info,
                    "result": result,
                    "agents": len(hub.list_agents()),
                    "pending_approvals": len(hub.approvals_pending()),
                })
                return
            if path == "/api/workspace/close":
                provider.close()  # type: ignore[union-attr]
                self._json(200, {"workspace": provider.info()})  # type: ignore[union-attr]
                return
            hub = current_hub()
            if path == "/api/ask":
                goal = str(body.get("goal", ""))
                if body.get("async"):
                    job_id = jobs.submit("ask", lambda: hub.ask(
                        goal, main_agent_id=str(body.get("agent_id", "")),
                        model=str(body.get("model", ""))))
                    self._json(202, {"job_id": job_id})
                else:
                    self._json(200, hub.ask(goal,
                                            main_agent_id=str(body.get("agent_id", "")),
                                            model=str(body.get("model", ""))))
                return
            if path == "/api/solve":
                goal = str(body.get("goal", ""))
                common = {
                    "executor_agent": str(body.get("executor_agent", "")),
                    "executor_model": str(body.get("executor_model", "")),
                    "commander_agent": str(body.get("commander_agent", "")),
                    "commander_model": str(body.get("commander_model", "")),
                }
                if body.get("async"):
                    job_id = jobs.submit("solve", lambda: hub.solve(
                        goal, max_tasks=int(body.get("max_tasks", 4) or 4),
                        max_parallel=int(body.get("max_parallel", 1) or 1),
                        start=bool(body.get("start", True)),
                        background=bool(body.get("background", True)),
                        polish=bool(body.get("polish", False)),
                        approve=bool(body.get("approve", False)), **common))
                    self._json(202, {"job_id": job_id})
                else:
                    self._json(200, hub.solve(
                        goal, max_tasks=int(body.get("max_tasks", 4) or 4),
                        max_parallel=int(body.get("max_parallel", 1) or 1),
                        start=bool(body.get("start", True)),
                        background=bool(body.get("background", True)),
                        polish=bool(body.get("polish", False)),
                        approve=bool(body.get("approve", False)), **common))
                return
            if path == "/api/segment":
                self._json(200, hub.segment_goal(
                    str(body.get("goal", "")),
                    max_tasks=int(body.get("max_tasks", 4) or 4),
                    executor_agent=str(body.get("executor_agent", "")),
                    executor_model=str(body.get("executor_model", "")),
                    commander_agent=str(body.get("commander_agent", "")),
                    commander_model=str(body.get("commander_model", "")),
                ))
                return
            if path == "/api/roles/resolve":
                self._json(200, hub.resolve_role(
                    str(body.get("role", "")),
                    limit=int(body.get("limit", 6) or 6),
                    executor_agent=str(body.get("executor_agent", "")),
                    executor_model=str(body.get("executor_model", "")),
                    commander_agent=str(body.get("commander_agent", "")),
                    commander_model=str(body.get("commander_model", "")),
                ))
                return
            if path == "/api/roles/import":
                self._json(200, hub.import_role_policy(
                    str(body.get("path", "")), save=bool(body.get("save", False))))
                return
            if path == "/api/roles/save":
                self._json(200, hub.save_role_policy(str(body.get("path", ""))))
                return
            if path == "/api/aggregate":
                self._json(200, hub.aggregate_run(
                    str(body.get("run_id", "")), polish=bool(body.get("polish", False))))
                return
            if path == "/api/preflight":
                result = hub.preflight_agents(
                    list(body.get("stages") or []),
                    workspace=str(body.get("workspace", "")),
                    open_apps=bool(body.get("open_apps", False)),
                    full_control=bool(body.get("full_control", True)),
                    config=dict(body.get("config") or {}),
                )
                self._json(200, result)
                return
            if path == "/api/chain":
                result = hub.configure_chain(
                    goal=str(body.get("goal", "")),
                    stages=list(body.get("stages") or []),
                    main_agent_id=str(body.get("main_agent_id", "")),
                    start=bool(body.get("start", False)),
                    background=bool(body.get("background", True)),
                    max_parallel=int(body.get("max_parallel", 1) or 1),
                    approve=bool(body.get("approve", False)),
                    plan_id=str(body.get("plan_id", "")),
                    save_only=bool(body.get("save_only", False)),
                )
                if result.get("status") == "started" and body.get("wait"):
                    result["run"] = hub.run_status(result["run_id"])
                self._json(200, result)
                return
            if path == "/api/chain/draft":
                self._json(200, hub.save_chain_draft(dict(body.get("draft") or body)))
                return
            if len(parts) == 5 and parts[1] == "agents" and parts[3] == "interface" \
                    and parts[4] == "restart":
                self._json(200, hub.restart_agent_interface(parts[2]))
                return
            if path == "/api/bus/publish":
                self._json(200, hub.bus_publish(
                    str(body.get("kind", "")), dict(body.get("payload") or {}),
                    source=str(body.get("source", "user")),
                    correlation_id=str(body.get("correlation_id", "")),
                    topic=str(body.get("topic", ""))))
                return
            if path == "/api/bus/subscribe":
                self._json(200, hub.bus_subscribe(
                    str(body.get("subscriber", "")),
                    topics=list(body.get("topics") or []),
                    kinds=list(body.get("kinds") or [])))
                return
            if path == "/api/bus/poll":
                self._json(200, hub.bus_poll(
                    str(body.get("subscription_id", "")),
                    limit=int(body.get("limit", 100) or 100)))
                return
            if path == "/api/bus/ack":
                self._json(200, hub.bus_ack(
                    str(body.get("subscription_id", "")),
                    int(body.get("seq", 0) or 0)))
                return
            if path == "/api/bus/unsubscribe":
                self._json(200, hub.bus_unsubscribe(str(body.get("subscription_id", ""))))
                return
            if path == "/api/benchmark":
                self._json(200, hub.benchmark())
                return
            if path == "/api/sync":
                self._json(200, hub.run_sync(
                    agent_id=str(body.get("agent_id", "")),
                    limit=int(body.get("limit", 20) or 20),
                    include_unscoped=bool(body.get("include_unscoped", False))))
                return
            if path == "/api/cache/clear":
                self._json(200, hub.clear_semantic_cache())
                return
            if path == "/api/agents/discover":
                self._json(200, {"agents": [a.to_dict() for a in hub.discover(merge=True)]})
                return
            if path == "/api/agents/models/refresh":
                self._json(200, hub.refresh_agent_models(str(body.get("agent_id", ""))))
                return
            if path == "/api/agents/models/sync":
                self._json(200, hub.sync_models(str(body.get("agent_id", ""))))
                return
            if len(parts) == 4 and parts[1] == "agents" and parts[3] == "model":
                self._json(200, hub.set_agent_model(
                    parts[2], str(body.get("model", "")),
                    context_length=(int(body["context_length"])
                                    if body.get("context_length") is not None else None),
                    capability_level=(str(body.get("capability_level"))
                                      if body.get("capability_level") is not None else None),
                ))
                return
            if len(parts) == 5 and parts[1] == "agents" and parts[3] == "model" \
                    and parts[4] == "remove":
                self._json(200, hub.remove_agent_model(parts[2], str(body.get("model", ""))))
                return
            if len(parts) == 4 and parts[1] == "agents" and parts[3] == "models":
                self._json(200, hub.register_agent_models(
                    parts[2], [str(m) for m in (body.get("models") or [])],
                    note=str(body.get("note", ""))))
                return
            if len(parts) == 5 and parts[1] == "agents" and parts[3] == "mcp" \
                    and parts[4] == "call":
                self._json(200, hub.mcp_call(
                    parts[2], str(body.get("tool", "")),
                    dict(body.get("arguments") or {})))
                return
            if path == "/api/agents":
                record = hub.register_agent(body)
                self._json(200, {"agent": record.to_dict()})
                return
            if path == "/api/main/auto":
                result = hub.auto_main(
                    agent_id=str(body.get("agent_id", "")),
                    model=str(body.get("model", "")),
                )
                self._json(200, result)
                return
            if path == "/api/settings/full_control":
                self._json(200, hub.set_full_control(bool(body.get("enabled", True))))
                return
            if path == "/api/settings/multi_round":
                self._json(200, hub.set_multi_round(
                    bool(body.get("enabled", True)),
                    max_rounds=body.get("max_rounds"),
                ))
                return
            if path == "/api/budget":
                self._json(200, hub.set_project_budget(int(body.get("tokens", 0) or 0)))
                return
            if path == "/api/main":
                result = hub.set_main(
                    str(body.get("agent_id", "")), str(body.get("model", "")),
                    budget_tokens=int(body.get("budget_tokens", 0) or 0),
                    note=str(body.get("note", "")),
                    context_length=(int(body["context_length"])
                                    if body.get("context_length") is not None else None),
                    capability_level=(str(body.get("capability_level"))
                                      if body.get("capability_level") is not None else None))
                self._json(200, {"main": result})
                return
            if path == "/api/plans":
                goal = str(body.get("goal", ""))
                agent_id = str(body.get("agent_id", ""))
                model = str(body.get("model", ""))
                if body.get("async"):
                    job_id = jobs.submit("plan", lambda: hub.request_plan(goal, main_agent_id=agent_id, model=model))
                    self._json(202, {"job_id": job_id})
                else:
                    self._json(200, hub.request_plan(goal, main_agent_id=agent_id, model=model))
                return
            if path == "/api/plans/import":
                self._json(200, hub.import_plan(body.get("plan") or body.get("path"),
                                                main_agent_id=str(body.get("agent_id", "")),
                                                model=str(body.get("model", ""))))
                return
            if len(parts) == 4 and parts[1] == "plans" and parts[3] == "approve":
                approval_id = str(body.get("approval_id", ""))
                note = str(body.get("note", ""))
                if body.get("approve", True):
                    outcome = hub.approve_plan(parts[2], approval_id=approval_id, note=note)
                else:
                    outcome = hub.reject_plan(parts[2], approval_id=approval_id, note=note)
                self._json(200, outcome)
                return
            if len(parts) == 4 and parts[1] == "runs" and parts[3] == "pause":
                self._json(200, hub.pause_run(parts[2],
                                              str(body.get("reason", "用户暂停"))))
                return
            if len(parts) == 4 and parts[1] == "runs" and parts[3] == "cancel":
                self._json(200, hub.cancel_run(parts[2],
                                               str(body.get("reason", "用户停止运行"))))
                return
            if len(parts) == 4 and parts[1] == "runs" and parts[3] in ("start", "resume"):
                parallel = body.get("max_parallel")
                fn = hub.start_run if parts[3] == "start" else hub.resume_run
                self._json(202, fn(parts[2], max_parallel=int(parallel) if parallel else None,
                                   background=bool(body.get("background", True))))
                return
            if len(parts) == 4 and parts[1] == "runs" and parts[3] == "release":
                self._json(200, hub.request_release(parts[2]))
                return
            if len(parts) == 4 and parts[1] == "runs" and parts[3] == "summary":
                payload = body.get("summary")
                path = body.get("path") or body.get("summary_path")
                if payload is None and path:
                    # 2026-11-07 defect #9: the close-out channel only accepted
                    # inline JSON; a path is resolved inside the project.
                    candidate = Path(str(path))
                    if not candidate.is_absolute():
                        candidate = Path(hub.project.root) / candidate
                    resolved = candidate.resolve()
                    if not resolved.is_relative_to(Path(hub.project.root).resolve()):
                        raise ValidationError("汇总文件只能位于项目目录内")
                    if not resolved.is_file():
                        raise NotFoundError(f"汇总文件不存在: {resolved}")
                    payload = resolved
                self._json(200, hub.submit_summary(
                    parts[2], payload if payload is not None else body))
                return
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] in ("result", "submit-result"):
                self._json(200, hub.submit_result(
                    parts[2], body.get("payload") or body.get("path") or body,
                    attempt_id=str(body.get("attempt_id", "")),
                    request_id=str(body.get("request_id", ""))))
                return
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "retry":
                self._json(200, hub.retry_task(parts[2]))
                return
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "resume-interrupted":
                self._json(200, hub.resume_interrupted(parts[2]))
                return
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "repair":
                self._json(200, hub.repair_task(parts[2]))
                return
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "reconcile":
                self._json(200, hub.reconcile_task(parts[2]))
                return
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "capture-artifacts":
                self._json(200, hub.capture_artifacts(parts[2]))
                return
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "adopt-attempt":
                self._json(200, hub.runner.adopt_attempt_result(
                    parts[2], str(body.get("attempt_id", ""))))
                return
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "reverify":
                self._json(200, hub.reverify_task(parts[2]))
                return
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "binding":
                self._json(200, hub.update_task_binding(
                    parts[2],
                    agent_id=str(body.get("agent_id", "")),
                    model=str(body.get("model", "")),
                    context_length=(int(body["context_length"])
                                    if body.get("context_length") is not None else None),
                    capability_level=(str(body.get("capability_level"))
                                      if body.get("capability_level") is not None else None),
                    requeue=bool(body.get("requeue", False)),
                    stop_active=bool(body.get("stop_active", False)),
                ))
                return
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "cancel":
                self._json(200, hub.cancel_task(parts[2]))
                return
            if len(parts) == 3 and parts[1] == "approvals":
                self._json(200, hub.decide_approval(parts[2],
                                                    approve=bool(body.get("approve", True)),
                                                    note=str(body.get("note", ""))))
                return
            if path == "/api/merge":
                run_id = str(body.get("run_id", ""))
                approval_id = str(body.get("approval_id", ""))
                outcome = hub.merge(run_id, approval_id=approval_id,
                                    commit=bool(body.get("commit", False)))\
                    if approval_id else hub.merge(run_id, commit=bool(body.get("commit", False)))
                self._json(200, outcome)
                return
            if path == "/api/merge/recheck":
                self._json(200, hub.recheck_merge(
                    str(body.get("run_id", "")),
                    commit=bool(body.get("commit", True))))
                return
            if path.startswith("/api/rollback/"):
                kind = path.split("/")[-1]
                task_uid = str(body.get("task_uid") or body.get("uid") or "")
                if kind == "task":
                    outcome = hub.rollback_task(task_uid)
                elif kind == "file":
                    outcome = hub.rollback_file(task_uid, str(body.get("path", "")))
                elif kind == "run":
                    outcome = hub.rollback_run(str(body.get("run_id") or task_uid))
                else:
                    self._json(404, {"error": {"code": "not_found", "message": "回滚接口不存在"}})
                    return
                self._json(200, outcome)
                return
            if path == "/api/memory":
                self._json(200, hub.memory_add(
                    type=str(body.get("type", "project_fact")),
                    title=str(body.get("title", "")),
                    body=str(body.get("body", "")),
                    source=str(body.get("source", "user")),
                    confidence=float(body.get("confidence", 0.8)),
                    scope=str(body.get("scope", "")),
                    injectable=bool(body.get("injectable", True)),
                ))
                return
            if len(parts) == 4 and parts[1] == "memory" and parts[3] == "resolve":
                self._json(200, hub.memory_resolve(parts[2], keep=bool(body.get("keep", True))))
                return
            if path == "/api/sessions/import":
                self._json(200, hub.import_sessions(str(body.get("agent_id", "")),
                                                    limit=int(body.get("limit", 20)),
                                                    include_unscoped=bool(body.get("include_unscoped", False))))
                return
            if path == "/api/handoff":
                self._json(200, hub.handoff(to_agent_id=str(body.get("agent_id", "")),
                                            model=str(body.get("model", "")),
                                            note=str(body.get("note", ""))))
                return
            if path == "/api/recover":
                self._json(200, hub.recover(run_id=str(body.get("run_id", "")),
                                            auto_resume=bool(body.get("auto_resume", False))))
                return
            self._json(404, {"error": {"code": "not_found", "message": f"接口不存在: {path}"}})

    return Handler
