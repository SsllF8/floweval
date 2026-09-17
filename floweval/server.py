"""Web 控制台服务。

用标准库实现，不引入 FastAPI/Flask —— 保持"clone 下来就能跑"这个前提。

API 约定：
    GET    /api/health
    GET    /api/targets              被测对象类型 + 各自的配置字段（前端靠它动态渲染表单）
    GET    /api/scorers              可用评分器
    GET    /api/datasets             已存在的数据集
    POST   /api/datasets             上传数据集（JSON 传内容，省掉 multipart 解析）
    POST   /api/runs                 发起评测，立即返回 run_id
    GET    /api/runs                 历史列表
    GET    /api/runs/<id>            完整结果（看板 payload）
    GET    /api/runs/<id>/status     进度轮询
    DELETE /api/runs/<id>

前端上传文件走 JSON（{filename, content}）而不是 multipart，
是为了不用在标准库上手写 multipart 解析 —— 少一处出错的地方。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .dataset import DatasetError, load_cases
from .gate import GateConfig, evaluate_gate
from .regression import compare
from .report import build_dashboard_payload
from .runner import EvalRunner
from .scorers import available_scorers
from .storage import RunStore
from .targets.factory import TARGET_SCHEMAS, build_target, normalize_config

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
# 只服务这些后缀，避免把 .db / .py 之类的文件暴露出去
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class Job:
    """一次评测任务的运行状态。

    job_id 是前端拿到就能立刻轮询的句柄（发起时就有）；
    run_id 是评测跑完后才确定的落库 id。分成两个而不是共用一个，
    是因为跑之前没法预知 run_id，而前端又需要一个稳定的轮询地址。
    """

    def __init__(self, job_id: str, total: int):
        self.job_id = job_id
        self.run_id: str | None = None
        self.total = total
        self.done = 0
        self.status = "running"  # running | done | error
        self.error: str | None = None
        self.started_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "status": self.status,
            "done": self.done,
            "total": self.total,
            "error": self.error,
        }


class ConsoleState:
    """服务级共享状态：任务表 + 存储。"""

    def __init__(self, db_path: str, datasets_dir: str | Path):
        self.store = RunStore(db_path)
        self.datasets_dir = Path(datasets_dir)
        self.datasets_dir.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def new_job(self, job_id: str, total: int) -> Job:
        with self._lock:
            job = Job(job_id, total)
            self.jobs[job_id] = job
            return job

    # ---------------- 数据集

    def list_datasets(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for base, tag in ((Path("examples"), "内置"), (self.datasets_dir, "上传")):
            if not base.exists():
                continue
            for p in sorted(base.glob("*.jsonl")) + sorted(base.glob("*.json")):
                if p.name in seen:
                    continue
                seen.add(p.name)
                count = _safe_count(p)
                # 解析不了的（比如 examples/ 里的工作流样例）不列为数据集。
                # 用户上传时 save_dataset 会立刻校验并报 400，所以不会在这里静默丢失。
                if count < 0:
                    continue
                out.append(
                    {
                        "name": p.name,
                        "path": str(p).replace("\\", "/"),
                        "source": tag,
                        "cases": count,
                    }
                )
        return out

    def save_dataset(self, filename: str, content: str) -> dict[str, Any]:
        name = Path(filename).name or "dataset.jsonl"
        if Path(name).suffix.lower() not in {".jsonl", ".json", ".yaml", ".yml"}:
            raise DatasetError(f"不支持的数据集格式: {Path(name).suffix}")
        target = self.datasets_dir / name
        if target.exists():
            stem, suffix = target.stem, target.suffix
            n = 1
            while target.exists():
                target = self.datasets_dir / f"{stem}-{n}{suffix}"
                n += 1
        target.write_text(content, encoding="utf-8")
        cases = load_cases(target)  # 校验格式，坏了直接报错给用户
        return {"name": target.name, "path": str(target).replace("\\", "/"), "cases": len(cases)}

    # ---------------- 评测

    def start_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        dataset = payload.get("dataset")
        if not dataset:
            raise ValueError("缺少数据集")
        cases = load_cases(dataset)
        if not cases:
            raise ValueError("数据集里没有用例")

        kind = payload.get("target") or "customer_service"
        config = normalize_config(kind, payload.get("config") or {})
        # 构造一次，把配置错误（缺 url / 缺 model / agentflow 没装）
        # 在发起时就暴露出来，而不是让用户在进度条上干等到任务失败。
        target = build_target(kind, config)

        job = self.new_job(f"job-{int(time.time() * 1000)}", len(cases))

        thread = threading.Thread(
            target=self._run_worker,
            args=(job, target, cases, dataset, payload),
            daemon=True,
        )
        thread.start()
        return job.to_dict()

    def _run_worker(self, job, target, cases, dataset, payload) -> None:
        try:
            runner = EvalRunner(
                target,
                max_workers=max(1, int(payload.get("workers") or 4)),
                retries=max(0, int(payload.get("retries") or 0)),
            )
            run = runner.run(
                cases,
                dataset=str(dataset),
                on_progress=lambda done, total: setattr(job, "done", done),
            )
            run.meta["target_kind"] = payload.get("target")
            run.meta["console"] = True

            regression = None
            baseline_id = payload.get("baseline")
            baseline = self.store.get(baseline_id) if baseline_id else None
            if baseline is not None:
                regression = compare(baseline, run)
                run.meta["regression"] = regression.to_dict()

            gate_cfg = payload.get("gate")
            if gate_cfg:
                cfg = GateConfig(
                    min_pass_rate=float(gate_cfg.get("min_pass_rate", 0.9)),
                    min_mean_score=float(gate_cfg.get("min_mean_score", 0.85)),
                    max_p95_latency_ms=_opt_float(gate_cfg.get("max_p95_latency_ms")),
                    max_total_cost=_opt_float(gate_cfg.get("max_total_cost")),
                    # 指定了基线却没给退化上限时按零退化判定 —— 和 CLI 保持一致：
                    # 你都选基线了，目的就是防退化。
                    max_regressions=0 if baseline is not None else _opt_int(gate_cfg.get("max_regressions")),
                    require_no_regression=bool(gate_cfg.get("require_no_regression", False)),
                )
                gate = evaluate_gate(run, cfg, regression)
                run.meta["gate"] = gate.to_dict()

            self.store.save(run)
            self._write_latest(run)

            job.run_id = run.run_id
            job.status = "done"
            job.done = job.total
        except Exception as exc:  # noqa: BLE001 - 任务线程不能静默死掉
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"

    def _write_latest(self, run: Any) -> None:
        """把最新一次结果导出成静态看板能直接读的 JSON。

        这样 /index.html 打开就是刚跑完的那次，不用再手动 --html 导出一遍。
        写成文件而不是让看板走 API，是为了保留「单文件看板双击就能开」这个用法。
        """
        try:
            payload = build_dashboard_payload(
                run,
                history=self.store.history(limit=30),
            )
            payload["gate"] = run.meta.get("gate")
            payload["regression"] = run.meta.get("regression")
            target = WEB_DIR / "data" / "latest.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001 - 导出失败不该让整次评测白跑
            pass


def _opt_float(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None
    return float(value)


def _opt_int(value: Any) -> int | None:
    if value in (None, "", "null"):
        return None
    return int(value)


def _safe_count(path: Path) -> int:
    try:
        return len(load_cases(path))
    except Exception:  # noqa: BLE001 - 列表页不该因为一个坏文件挂掉
        return -1


# ------------------------------------------------------------------ Handler


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "FlowEval/0.1"

    state: ConsoleState

    # ---------------- 基础

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data: Any) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"error": message})

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"请求体不是合法 JSON: {exc}") from exc

    # ---------------- 路由

    def do_GET(self) -> None:  # noqa: N802 - 基类约定的方法名
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        try:
            if path.startswith("/api/"):
                self._api(method, path, query)
            else:
                self._static(path)
        except ValueError as exc:
            self._error(400, str(exc))
        except KeyError as exc:
            self._error(404, f"资源不存在: {exc}")
        except DatasetError as exc:
            self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._error(500, f"{type(exc).__name__}: {exc}")

    def _api(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        state = self.state

        if path == "/api/health":
            return self._json(200, {"ok": True, "jobs": len(state.jobs)})

        if path == "/api/targets":
            return self._json(
                200,
                [
                    {"name": name, **schema}
                    for name, schema in TARGET_SCHEMAS.items()
                ],
            )

        if path == "/api/scorers":
            return self._json(200, {"scorers": available_scorers()})

        if path == "/api/datasets":
            if method == "GET":
                return self._json(200, {"datasets": state.list_datasets()})
            if method == "POST":
                body = self._body()
                content = body.get("content")
                if not content:
                    raise ValueError("缺少文件内容")
                return self._json(201, state.save_dataset(body.get("filename", "dataset.jsonl"), content))
            return self._error(405, "不支持的方法")

        if path == "/api/runs":
            if method == "GET":
                runs = state.store.list_runs(limit=50)
                return self._json(200, {"runs": runs})
            if method == "POST":
                job = state.start_run(self._body())
                return self._json(202, job)
            return self._error(405, "不支持的方法")

        if path.startswith("/api/runs/"):
            parts = path.split("/")
            run_id = parts[3] if len(parts) > 3 else ""
            if not run_id:
                raise KeyError(path)

            if len(parts) > 4 and parts[4] == "status":
                job = state.jobs.get(run_id)
                if job is None:
                    # 任务已结束并从表里清掉时，把查的 id 当成 run_id 返回，
                    # 前端拿到 status=done + run_id 就能直接拉结果。
                    return self._json(
                        200,
                        {
                            "job_id": run_id,
                            "run_id": run_id,
                            "status": "done",
                            "done": 0,
                            "total": 0,
                            "error": None,
                        },
                    )
                return self._json(200, job.to_dict())

            if method == "GET":
                run = state.store.get(run_id)
                if run is None:
                    raise KeyError(run_id)
                payload = build_dashboard_payload(
                    run, history=state.store.history(limit=30)
                )
                # gate / regression 在跑的时候存进了 meta，这里回填给看板
                payload["gate"] = run.meta.get("gate")
                payload["regression"] = run.meta.get("regression")
                return self._json(200, payload)

            if method == "DELETE":
                state.store.delete(run_id)
                state.jobs.pop(run_id, None)
                return self._json(200, {"deleted": run_id})

            return self._error(405, "不支持的方法")

        raise KeyError(path)

    def _static(self, path: str) -> None:
        if path == "/":
            path = "/console.html"
        if path == "/dashboard":
            path = "/index.html"

        file_path = (WEB_DIR / path.lstrip("/")).resolve()
        # 目录穿越防护：解析后的路径必须仍在 web 目录内
        if WEB_DIR.resolve() not in file_path.parents and file_path != WEB_DIR.resolve():
            raise KeyError(path)
        if not file_path.exists() or not file_path.is_file():
            raise KeyError(path)

        ctype = STATIC_TYPES.get(file_path.suffix.lower(), "application/octet-stream")
        self._send(200, file_path.read_bytes(), ctype)


# ------------------------------------------------------------------ 启动


def start_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    db_path: str = "floweval.db",
    datasets_dir: str = "datasets",
    open_browser: bool = True,
) -> None:
    state = ConsoleState(db_path, datasets_dir)
    handler = type("BoundHandler", (ConsoleHandler,), {"state": state})

    class Server(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    httpd = Server((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"FlowEval 控制台已启动: {url}")
    print(f"数据库: {Path(db_path).resolve()}")
    print(f"数据集目录: {Path(datasets_dir).resolve()}")
    print("按 Ctrl+C 停止")

    if open_browser:
        import threading as _t
        import webbrowser

        _t.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
