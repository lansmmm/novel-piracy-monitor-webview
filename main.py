"""
打盗全家捅 —— pywebview 版

批 1-1：搜索启动骨架（点「搜索」→ 后台线程 → 逐行推日志，暂不接引擎）
（已含第 3 步验证过的 Python→JS 推送链路 + 模拟心跳）

运行：
    python main.py
调试（开 DevTools，Ctrl+Shift+I / F12）：
    set PM_DEBUG=1 && python main.py
"""

import json
import os
import sys
import threading
import time

import webview

# ==================== 路径 ====================
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

INDEX_HTML = os.path.join(BASE_DIR, "index.html")

APP_TITLE = "打盗全家捅"
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 800

HEARTBEAT_INTERVAL = 0.5   # 心跳间隔（秒）
HEARTBEAT_TIMES = 20       # 推多少行后自然停下

SEARCH_LOG_INTERVAL = 0.5  # 模拟搜索日志每行之间的间隔（秒）

# 来源勾选框的 value（引擎组 id）→ 中文名，日志里显示用
SOURCE_LABELS = {
    "quark": "夸克",
    "baidu": "百度",
    "zhidao": "知道",
    "tieba": "贴吧",
    "bing": "必应",
    "toutiao": "头条",
    "sogou": "搜狗",
    "so360": "360",
    "sogou_weixin": "微信",
    "weibo": "微博",
}


class Api:
    """暴露给 JS 的 Python 接口。

    前端调用方式：window.pywebview.api.<方法名>(...)
    （pywebview 会自动把公开方法包成 Promise）
    """

    def __init__(self):
        self._window = None
        self._hb_thread = None
        self._hb_stop = threading.Event()
        self._search_thread = None

    # ==================== 绑定窗口 ====================
    def bind(self, window):
        """create_window 之后调用，拿到窗口句柄才能 evaluate_js"""
        self._window = window

    # ==================== 基础自检 ====================
    def ping(self):
        print("[api] ping() 被调用 → 返回 pong")
        return "pong"

    # ==================== Python → JS 推送 ====================
    def _push_js(self, code):
        """往前端丢一段 JS（后台线程里调用，evaluate_js 内部会封送到 UI 线程）"""
        window = self._window
        if window is None:
            raise RuntimeError("窗口还没绑定，无法 evaluate_js")
        window.evaluate_js(code)
        return True

    def push_log(self, text):
        """把一行文字推到前端日志区（前端有 window.appendLog）"""
        # JSON 转义，避免引号 / 换行破坏 JS 语句
        payload = json.dumps(str(text), ensure_ascii=False)
        return self._push_js("window.appendLog(" + payload + ")")

    # ==================== 搜索状态通知 ====================
    def set_searching(self, is_searching):
        """通知前端切「搜索中」状态：搜索按钮禁用 / 停止按钮启用"""
        flag = "true" if is_searching else "false"
        return self._push_js("window.setSearching(" + flag + ")")

    # ==================== 搜索启动（骨架，暂不接引擎） ====================
    def start_search(self, book, sources, pages):
        """点「搜索」调这里：起后台线程逐行推日志

        book    = 书名(str)
        sources = 勾选的来源 list（引擎组 id，如 ['baidu', 'sogou']）
        pages   = 页数(int) 或 "auto"
        """
        if self._search_thread is not None and self._search_thread.is_alive():
            print("[search] 上一次搜索还在跑，忽略本次请求")
            return "already_running"

        book = str(book or "").strip()
        sources = list(sources or [])
        self._search_thread = threading.Thread(
            target=self._search_loop,
            args=(book, sources, pages),
            name="search",
            daemon=True,
        )
        self._search_thread.start()
        print(f"[search] 已启动：book={book!r} sources={sources} pages={pages!r}")
        return "started"

    def _search_lines(self, book, sources, pages):
        """本批要推的 7 行模拟日志（以后换成真实抓取流程）"""
        src_text = " / ".join(SOURCE_LABELS.get(s, s) for s in sources) or "（无）"
        return [
            f"开始搜索：{book}",
            f"已勾选来源：{src_text}",
            f"页数：{pages}",
            "正在打开浏览器...",
            "模拟：第 1 个引擎开始",
            "模拟：第 2 个引擎开始",
            "搜索完成",
        ]

    def _search_loop(self, book, sources, pages):
        try:
            self.set_searching(True)
            for i, text in enumerate(self._search_lines(book, sources, pages)):
                if i:
                    time.sleep(SEARCH_LOG_INTERVAL)   # 第 1 行立刻出，之后每 0.5 秒一行
                self.push_log(text)
        except Exception as exc:                          # noqa: BLE001
            print(f"[search] 推送失败：{exc!r}", file=sys.stderr)
        finally:
            self.set_searching(False)
            print("[search] 结束")

    # ==================== 模拟心跳（链路验证用） ====================
    def start_heartbeat(self):
        """启动后台线程：每 0.5 秒推一行「心跳 N」，N 从 1 递增到 20"""
        if self._hb_thread is not None and self._hb_thread.is_alive():
            print("[hb] 已在运行，忽略本次 start")
            return "already_running"

        self._hb_stop.clear()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, name="heartbeat", daemon=True
        )
        self._hb_thread.start()
        print("[hb] 已启动（后台线程，非主线程）")
        return "started"

    def stop_heartbeat(self):
        """停止后台线程"""
        self._hb_stop.set()
        print("[hb] 收到停止请求")
        return "stopping"

    def _heartbeat_loop(self):
        for n in range(1, HEARTBEAT_TIMES + 1):
            if self._hb_stop.is_set():
                print(f"[hb] 被停止（第 {n} 行未推）")
                return

            try:
                self.push_log(f"心跳 {n}")
            except Exception as exc:                      # noqa: BLE001
                print(f"[hb] push_log 失败：{exc!r}", file=sys.stderr)
                raise

            # 用 wait 而不是 sleep，停止时能立刻退出
            if self._hb_stop.wait(HEARTBEAT_INTERVAL):
                print(f"[hb] 被停止（已推 {n} 行）")
                return

        print(f"[hb] 推完 {HEARTBEAT_TIMES} 行，自然结束")


def main():
    api = Api()

    window = webview.create_window(
        APP_TITLE,
        INDEX_HTML,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=(960, 640),
        js_api=api,
    )
    api.bind(window)

    webview.start(debug=os.environ.get("PM_DEBUG") == "1")


if __name__ == "__main__":
    main()
