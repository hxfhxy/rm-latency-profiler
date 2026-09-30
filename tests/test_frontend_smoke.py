"""前端端到端冒烟：真实浏览器加载示例 → 断言图表/表格/视频渲染无 JS 异常。

需要 playwright + chromium：pip install -e ".[e2e]" && playwright install chromium。
未安装时整组跳过；CI 会安装并在每次 push 上执行。
这条测试存在的意义：前端键名错位、事件绑定失效这类 bug（v0.4.1 之前真实发生过）
只有真实浏览器执行 JS 才能暴露。
"""

import socket
import threading
import time
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api", reason="需要 pip install -e '.[e2e]'")

from rm_latency.webapp import create_app  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "docs" / "example.mcap"


@pytest.fixture(scope="module")
def base_url():
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = uvicorn.Server(
        uvicorn.Config(create_app(EXAMPLE), host="127.0.0.1", port=port,
                       log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.1).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


def test_frontend_smoke(base_url):
    with pw.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        js_errors: list[str] = []
        page.on("pageerror", lambda e: js_errors.append(str(e)))

        page.goto(base_url)
        page.get_by_role("button", name="加载示例数据").click()
        page.locator(".card").first.wait_for(state="visible")

        # 三张 plotly 图全部渲染出 SVG（键名错位 bug 的直接回归网）
        page.wait_for_function(
            "document.querySelectorAll('.js-plotly-plot svg').length >= 3"
        )
        # 视野表 = 表头 + 三段
        assert page.locator("#viewTable tr").count() >= 4
        # 视频面板拿到了帧接口的数据
        assert "/api/frame/" in (page.locator("#videoImg").get_attribute("src") or "")
        # 整个加载流程不允许出现未捕获 JS 异常
        assert js_errors == []
        browser.close()
