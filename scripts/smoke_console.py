"""控制台端到端冒烟：起真服务 → 走 API → 发起评测 → 拉结果。

不写进 tests/ 是因为它要占端口、要等任务跑完，属于"跑一次看看"的脚本。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from floweval.server import ConsoleHandler, ConsoleState  # noqa: E402

PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"


def req(path, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="floweval-console-"))
    state = ConsoleState(str(tmp / "t.db"), tmp / "datasets")
    handler = type("H", (ConsoleHandler,), {"state": state})
    httpd = HTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print("server up")

    print("health:", req("/api/health"))
    targets = req("/api/targets")
    print("targets:", [t["name"] for t in targets])
    print("cs fields:", [f["key"] for f in targets[0]["fields"]])

    ds = req("/api/datasets")["datasets"]
    print("datasets:", [(d["name"], d["cases"]) for d in ds])

    # 上传一个临时数据集，验证格式校验
    # 注意：知识库原文是「7 天」（带空格），断言写「7天」会误判，这里用「退款」
    content = "\n".join([
        json.dumps({"id": f"u{i}", "input": q, "must_contain": [k]}, ensure_ascii=False)
        for i, (q, k) in enumerate([("怎么退款", "退款"), ("发票怎么开", "发票"), ("多久到货", "天")])
    ])
    saved = req("/api/datasets", "POST", {"filename": "uploaded.jsonl", "content": content})
    print("uploaded:", saved)

    # 1) 基线：v1 完整知识库
    job = req("/api/runs", "POST", {
        "target": "customer_service",
        "config": {"version": "v1"},
        "dataset": saved["path"],
        "workers": 3,
    })
    print("job:", job)
    run_id = wait(job["job_id"])
    base_data = req(f"/api/runs/{run_id}")
    print("v1 pass_rate:", base_data["summary"]["pass_rate"])

    # 2) 事故版：v2 误删发票/保修 —— 应当退化，并且归因到 retrieve
    job2 = req("/api/runs", "POST", {
        "target": "customer_service",
        "config": {"version": "v2"},
        "dataset": saved["path"],
        "workers": 3,
        "baseline": run_id,
        "gate": {"min_pass_rate": 0.9, "min_mean_score": 0.85},
    })
    run2 = wait(job2["job_id"])
    data2 = req(f"/api/runs/{run2}")
    print("v2 pass_rate:", data2["summary"]["pass_rate"])
    print("v2 gate ok:", data2["gate"]["ok"],
          "| 未过规则:", [(r["name"], r["message"]) for r in data2["gate"]["rules"] if not r["passed"]])
    reg = data2["regression"]
    degrades = [d for d in reg["deltas"] if d["status"] == "regression"]
    print("v2 退化:", reg["summary"]["regressions"], [(d["case_id"], d["current_blame"]) for d in degrades])
    print("v2 回归 blame:", reg["blame_distribution"])
    print("v2 失败归因:", data2["blame_distribution"])

    # 3) 错误路径：http 缺 url 要在发起时就报 400
    try:
        req("/api/runs", "POST", {"target": "http", "config": {}, "dataset": saved["path"]})
        print("!! 缺 url 竟然没报错")
        return 1
    except urllib.error.HTTPError as e:
        print("expected 400:", e.code, e.read().decode("utf-8")[:120])

    # 4) 坏数据集要报 400
    try:
        req("/api/datasets", "POST", {"filename": "bad.jsonl", "content": "not json at all"})
        print("!! 坏数据集竟然没报错")
        return 1
    except urllib.error.HTTPError as e:
        print("expected 400 bad dataset:", e.code, e.read().decode("utf-8")[:120])

    runs = req("/api/runs")["runs"]
    print("history count:", len(runs))

    latest = ROOT / "web" / "data" / "latest.json"
    print("latest.json written:", latest.exists())

    httpd.shutdown()
    print("OK")
    return 0


def wait(job_id: str, timeout: float = 60.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = req(f"/api/runs/{job_id}/status")
        if s["status"] == "done":
            return s["run_id"]
        if s["status"] == "error":
            raise RuntimeError(s["error"])
        time.sleep(0.2)
    raise TimeoutError("任务超时")


if __name__ == "__main__":
    raise SystemExit(main())
