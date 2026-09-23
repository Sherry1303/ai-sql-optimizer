#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""重抓 README 里的界面截图（可选开发工具，不是应用运行时依赖）。

用法：
    python -m pip install playwright      # 只需一次；无需 playwright install，
                                          # 脚本直接调用系统已安装的 Edge
    python -X utf8 tools/capture_screenshots.py

脚本会依次完成：
    1. 以子进程启动 `streamlit run app.py`（默认 8520 端口、headless）；
    2. 用 Playwright 驱动本机 Edge 打开页面，真实走完「执行 → DeepSeek 分析 →
       一键创建 AI 建议的索引 → 对比耗时」全流程；
    3. 把 9 张视口截图写入 docs/images/。

注意：
    - 第 2 步会真实调用 DeepSeek（读环境变量 DEEPSEEK_API_KEY 或 .env），
      会产生极少量 token 费用；
    - 流程会在 data/demo.db 上真的建出 AI 建议的索引，跑完后建议执行
      `python db_setup.py --force` 把演示库还原到初始（无业务索引）状态。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PORT = 8520
URL = f"http://127.0.0.1:{PORT}/"
SHOTS = ROOT / "docs" / "images"
SHOTS.mkdir(parents=True, exist_ok=True)


def log(*args) -> None:
    print("[shots]", *args, flush=True)


def wait_server(timeout: float = 120) -> bool:
    """轮询到 Streamlit 端口可访问为止。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(URL, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(1.5)
    return False


def shot(page, name: str, locator=None, settle: int = 900) -> None:
    """先把目标元素滚进视口再截当前视口（Streamlit 的内部滚动容器不适合整页截图）。"""
    if locator is not None:
        try:
            locator.scroll_into_view_if_needed(timeout=8000)
        except Exception as exc:  # 滚动失败也照样截图，便于排查
            log("scroll warn:", type(exc).__name__)
        page.wait_for_timeout(settle)
    path = SHOTS / f"{name}.png"
    page.screenshot(path=str(path))
    log("saved", path.name, f"{path.stat().st_size // 1024} KB")


def scroll_top(page) -> None:
    """滚回主区域顶部（Streamlit 的滚动发生在 stMain 容器上，不在 window 上）。"""
    page.evaluate(
        """() => {
            const el = document.querySelector('[data-testid="stMain"]') || document.scrollingElement;
            if (el) el.scrollTop = 0;
            window.scrollTo(0, 0);
        }"""
    )
    page.wait_for_timeout(400)


def click_tab(page, index: int) -> None:
    """Streamlit 1.6x 的页签是 div[data-testid="stTab"][role="tab"]，不是 button。"""
    page.locator('[data-testid="stTab"]').nth(index).click()
    page.wait_for_timeout(1500)


def in_panel(page, text: str):
    """只在「当前可见的页签面板」里按文本定位，避免命中隐藏面板（如历史记录）里的同名字符串。"""
    return page.locator('[data-testid="stTabPanel"]:visible').get_by_text(text, exact=False).first


def capture(page) -> None:
    """按演示动线依次截图。"""
    page.goto(URL, wait_until="load", timeout=60000)
    page.wait_for_selector("text=AI SQL 优化助手", timeout=60000)
    page.wait_for_timeout(3500)
    log("页面已渲染")

    # 1) 首屏：示例选择 + SQL 编辑器 + 操作按钮
    scroll_top(page)
    shot(page, "01-query-editor")

    # 2) 执行：实测耗时 / 返回行数 / 数据预览
    page.get_by_role("button", name="执行").first.click()
    page.wait_for_selector("text=执行结果", timeout=60000)
    page.wait_for_timeout(1500)
    shot(page, "02-result-table", in_panel(page, "📊 执行结果"))

    # 3) 执行计划 + 自动识别出的性能信号
    shot(page, "03-query-plan", in_panel(page, "EXPLAIN QUERY PLAN"))

    # 4) DeepSeek 诊断（真实 API 调用）
    page.get_by_role("button", name="DeepSeek 分析").first.click()
    log("已发起 DeepSeek 分析，等待返回…")
    page.wait_for_selector("text=问题清单", timeout=300000)
    page.wait_for_timeout(2500)
    shot(page, "04-diagnosis", in_panel(page, "🩺 DeepSeek 诊断"))
    shot(page, "05-index-suggestion", in_panel(page, "索引建议（已做安全校验"))

    # 5) 一键落地索引 -> 对比耗时
    create = page.get_by_role("button", name="创建该索引")
    if create.count() > 0:
        create.first.click()
        log("已点击「创建该索引」")
        page.wait_for_timeout(4000)
        page.get_by_role("button", name="对比耗时").first.click()
        page.wait_for_selector("text=优化前后对比", timeout=120000)
        page.wait_for_timeout(1500)
        shot(page, "06-before-after", in_panel(page, "⚡ 优化前后对比"))
    else:
        log("WARN: 没有可执行的索引建议，跳过对比截图")

    # 6) 其余页签（切页签后回到顶部再截视口）
    click_tab(page, 1)
    scroll_top(page)
    shot(page, "07-schema-tab", settle=500)

    click_tab(page, 2)
    scroll_top(page)
    shot(page, "08-index-tab", settle=500)

    click_tab(page, 3)
    scroll_top(page)
    shot(page, "09-history-tab", settle=500)


def main() -> int:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    server = subprocess.Popen(
        [
            sys.executable, "-m", "streamlit", "run", "app.py",
            "--server.headless=true", f"--server.port={PORT}",
            "--browser.gatherUsageStats=false",
        ],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )
    log("streamlit pid", server.pid)
    try:
        if not wait_server():
            log("ERROR: streamlit 未能在超时内启动")
            return 1
        log("streamlit 已就绪")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1500, "height": 950}, device_scale_factor=1.5)
            capture(page)
            browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
        log("streamlit 已停止")
    log("DONE ->", SHOTS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
