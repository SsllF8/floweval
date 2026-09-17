"""控制台前端截图 + 交互冒烟（Playwright）。

产出 docs/screenshots/console-*.png，README 要用。
同时会把页面 console error 收集出来 —— 有报错就直接失败，不让"看起来能跑"过关。
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from http.server import HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from floweval.server import ConsoleHandler, ConsoleState  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

PORT = 8795
BASE = f"http://127.0.0.1:{PORT}"
OUT = ROOT / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="floweval-shot-"))
    state = ConsoleState(str(tmp / "shot.db"), tmp / "datasets")
    handler = type("H", (ConsoleHandler,), {"state": state})
    httpd = HTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_selector(".target-card")
        page.screenshot(path=str(OUT / "console-01-setup.png"), full_page=True)

        # 切到 http，验证配置表单是按 schema 动态渲染的
        page.click('.target-card[data-kind="http"]')
        page.wait_for_selector("#cfg-url")
        page.screenshot(path=str(OUT / "console-02-http-form.png"), full_page=True)

        # 回到内置客服，跑一次 v1 基线
        page.click('.target-card[data-kind="customer_service"]')
        page.wait_for_selector("#cfg-version")
        page.select_option("#dataset-select", index=0)
        page.check("#opt-gate")
        page.click("#btn-run")
        page.wait_for_selector(".result-verdict", timeout=60000)
        page.wait_for_timeout(400)
        page.screenshot(path=str(OUT / "console-03-result.png"), full_page=True)

        baseline = page.eval_on_selector(".result-head .badge.mono", "el => el.textContent").strip()
        print("baseline:", baseline)

        # 第二次：v2 + 基线，用来展示回归与判定失败
        page.select_option("#cfg-version", "v2")
        page.select_option("#opt-baseline", baseline)
        page.click("#btn-run")
        page.wait_for_function(
            "baseline => {"
            "  const el = document.querySelector('.result-head .badge.mono');"
            "  return !!el && el.textContent.trim() !== baseline;"
            "}",
            arg=baseline,
            timeout=60000,
        )
        page.wait_for_timeout(600)
        page.screenshot(path=str(OUT / "console-04-regression.png"), full_page=True)

        # 历史页
        page.click('.nav-item[data-view="history"]')
        page.wait_for_selector(".run-row")
        page.screenshot(path=str(OUT / "console-05-history.png"), full_page=True)

        browser.close()

    httpd.shutdown()
    time.sleep(0.2)

    if errors:
        print("PAGE ERRORS:")
        for e in errors:
            print(" -", e)
        return 1
    print("OK: 无 console error")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
