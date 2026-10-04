"""
打盗全家捅 —— pywebview 版

批 1-2-B：真接 Playwright + 百度引擎（点「搜索」→ 真开 Edge → 真抓百度 → 结果逐行推表格）
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
import traceback
import webbrowser

import webview
from playwright.sync_api import sync_playwright

from config import SOURCE_GROUPS
from engines import ENGINE_MAP

import single_instance

# ==================== 路径 ====================
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

INDEX_HTML = os.path.join(BASE_DIR, "index.html")

# Edge 持久化用户目录（保留登录态 / cookies，跟 flet 版同名）
USER_DATA_DIR = os.path.join(BASE_DIR, "baidu_engine_data")

APP_TITLE = "打盗全家捅"
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 800

HEARTBEAT_INTERVAL = 0.5   # 心跳间隔（秒）
HEARTBEAT_TIMES = 20       # 推多少行后自然停下

SEARCH_LOG_INTERVAL = 0.5  # 模拟搜索日志每行之间的间隔（秒）

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0"
)

# 停止标志：搜索线程在每个循环点检查它；「停止」按钮只 set，不强关浏览器
_stop_event = threading.Event()

# 前台 / 深度两个开关：前端 switch 调 set_frontend_mode / set_deep_mode 写这里
#   前台模式 True = 显示 Edge 窗口 + 撞验证码时弹窗等用户处理
#   深度模式 True = 一个来源组里的 PC + 移动端引擎全都跑
_frontend_mode = {"v": False}
_deep_mode = {"v": False}

# 当前这轮搜索的 adapter（captcha_continue / stop_search 靠它唤醒正卡在
# pause_for_user 里等用户的搜索线程）
_current_adapter = {"v": None}

# 前台模式撞验证码后最长等用户多久（秒）：超时就本轮跳过该引擎
CAPTCHA_WAIT_SECONDS = 600

# 同一页验证码处理完最多重试几次：防止用户没真过验证时无限弹窗
MAX_CAPTCHA_RETRY = 3

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


class WebViewAppAdapter:
    """把 pywebview 的 Api 包装成引擎能认的「app」对象。

    engines/base.py 里引擎只依赖这几样：
        app.log(msg) / app.pause_for_user(reason)
        app._last_fetch_raw_count（引擎自己写）
        app.whitelist_common（in 判断）
    """

    def __init__(self, book_name, api):
        self.book_name = book_name
        self.api = api
        self._last_fetch_raw_count = 0
        self.whitelist_common = set()
        # 验证码相关：引擎调 pause_for_user 时置 captcha_hit，
        # 搜索线程看到它就知道「这次 fetch 是撞验证码了，结果不可信」
        self.captcha_hit = False
        self.wait_continue_event = threading.Event()

    def log(self, msg):
        return self.push_log(msg)

    def push_log(self, msg):
        return self.api.push_log(str(msg))

    def pause_for_user(self, reason):
        """引擎撞验证码时调这里。

        ★ 引擎在 pause_for_user 之后一律直接 return（当前页解析中止），
          所以「用户过完验证」的重试动作由搜索线程负责（重试当前页）。
        """
        self.captcha_hit = True
        self.push_log(f"[暂停] {reason}")
        # 只有前台模式才弹窗等用户；后台模式直接返回，由搜索线程跳过该引擎
        if _frontend_mode["v"]:
            self.wait_continue_event.clear()
            # 通知前端弹窗（reason 走 json.dumps，避免引号 / 换行破坏 JS 语句）
            self.api._push_js(
                "window.showCaptchaDialog("
                + json.dumps(str(reason), ensure_ascii=False)
                + ")"
            )
            # 阻塞等待用户点「我已过验证，继续」（点「停止」也会立刻唤醒）
            self.wait_continue_event.wait(timeout=CAPTCHA_WAIT_SECONDS)
            if _stop_event.is_set():
                self.push_log("[暂停] 已收到停止指令，跳过该引擎")
            elif self.wait_continue_event.is_set():
                self.push_log("用户已确认，继续...")
            else:
                self.push_log(f"[暂停] 等待超时（{CAPTCHA_WAIT_SECONDS} 秒），跳过该引擎")


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

    def push_row(self, row_dict):
        """把一条结果推到前端表格（前端有 window.appendRow）

        ★ 双层 json.dumps：前端 appendRow 里要做 JSON.parse，
          所以传过去的必须是一段「JSON 字符串」（带引号），
          而不是 JS 对象字面量。单层 dumps 出来的 {"a":1} 是对象，
          JSON.parse 会直接抛错。
        """
        inner = json.dumps(row_dict, ensure_ascii=False)
        payload = json.dumps(inner, ensure_ascii=False)
        return self._push_js("window.appendRow(" + payload + ")")

    # ==================== 搜索状态通知 ====================
    def set_searching(self, is_searching):
        """通知前端切「搜索中」状态：搜索按钮禁用 / 停止按钮启用"""
        flag = "true" if is_searching else "false"
        return self._push_js("window.setSearching(" + flag + ")")

    # ==================== 停止搜索 ====================
    def stop_search(self):
        """点「停止」调这里：置事件位，由后台线程自己在循环里退出

        ★ 前台模式下搜索线程可能正卡在 pause_for_user 里等用户点「继续」，
          这里顺手把 wait_continue_event 也 set 掉，否则「停止本轮」要等到
          那个等待超时（最长 10 分钟）才会生效。
        """
        _stop_event.set()
        adapter = _current_adapter["v"]
        if adapter is not None:
            try:
                adapter.wait_continue_event.set()
            except Exception:                                     # noqa: BLE001
                pass
        print("[search] 收到停止指令")
        return "ok"

    # ==================== 验证码：我已过验证，继续 ====================
    def captcha_continue(self):
        """前端点「我已过验证，继续」调这里：唤醒暂停中的搜索线程"""
        adapter = _current_adapter["v"]
        if adapter is None:
            return "no_adapter"
        adapter.wait_continue_event.set()
        print("[search] 收到「我已过验证，继续」")
        return "ok"

    # ==================== 前台 / 深度 两个开关 ====================
    def set_frontend_mode(self, on):
        """前台模式：True = 显示 Edge 窗口 + 撞验证码弹窗等用户处理"""
        _frontend_mode["v"] = bool(on)
        self.push_log("前台模式：" + ("开（显示浏览器 + 验证码弹窗）" if _frontend_mode["v"] else "关（后台无头，不弹窗）"))
        print(f"[mode] 前台模式 = {_frontend_mode['v']}")
        return "ok"

    def set_deep_mode(self, on):
        """深度模式：True = 一个来源组里的 PC + 移动端引擎全都跑"""
        _deep_mode["v"] = bool(on)
        self.push_log("深度模式：" + ("开（PC + 移动端一起搜）" if _deep_mode["v"] else "关（每个来源只搜主渠道）"))
        print(f"[mode] 深度模式 = {_deep_mode['v']}")
        return "ok"

    # ==================== 用系统浏览器打开链接 ====================
    def open_url(self, url):
        """表格里点 url 调这里，走 Python 打开，避免 WebView 内部跳转"""
        url = str(url or "").strip()
        if not url:
            return "empty"
        try:
            webbrowser.open(url)
        except Exception as exc:                                  # noqa: BLE001
            self.push_log(f"[提示] 打开链接失败：{exc}")
            return "error"
        return "ok"

    # ==================== 搜索（真跑 Playwright + 多引擎 + 多页） ====================
    def start_search(self, book, sources, pages):
        """点「搜索」调这里

        book    = 书名(str)
        sources = 勾选的来源 list（引擎组 id，如 ['baidu', 'sogou']）
        pages   = 页数(int) 或 "auto"
        """
        book = str(book or "").strip()
        sources = list(sources or [])

        if not book:
            self.push_log("[提示] 请先输入书名")
            return "invalid"
        if not sources:
            self.push_log("[提示] 请至少勾选一个来源")
            return "invalid"

        if self._search_thread is not None and self._search_thread.is_alive():
            print("[search] 上一次搜索还在跑，忽略本次请求")
            return "already_running"

        _stop_event.clear()                                       # 搜索开始前清零
        self._search_thread = threading.Thread(
            target=self._search_loop_real,
            args=(book, sources, pages),
            name="search",
            daemon=True,
        )
        self._search_thread.start()
        print(f"[search] 已启动：book={book!r} sources={sources} pages={pages!r}")
        return "started"

    def _search_loop_real(self, book, sources, pages):
        """真抓取：开 Edge → 逐个来源跑引擎 → 每个引擎翻 N 页 → 日志 / 表格逐行推

        前台/深度两个开关在这里生效：
          - 前台模式 → 显示 Edge 窗口 + 撞验证码弹窗等用户点「我已过验证，继续」
          - 深度模式 → 一个来源组里的 PC + 移动端引擎全都跑
        """
        self.set_searching(True)
        try:
            src_text = " / ".join(SOURCE_LABELS.get(s, s) for s in sources) or "（无）"
            self.push_log(f"已勾选来源：{src_text}")
            self.push_log(f"页数：{pages}")
            self.push_log(
                "模式：前台 %s ｜ 深度 %s"
                % (
                    "开" if _frontend_mode["v"] else "关",
                    "开" if _deep_mode["v"] else "关",
                )
            )
            self.push_log("正在打开浏览器...")

            os.makedirs(USER_DATA_DIR, exist_ok=True)

            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=USER_DATA_DIR,
                    headless=not _frontend_mode["v"],   # 前台模式才显示 Edge 窗口
                    channel="msedge",
                    args=["--disable-blink-features=AutomationControlled"],
                    user_agent=BROWSER_UA,
                    viewport={"width": 1920, "height": 1080},
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                )
                adapter = WebViewAppAdapter(book, self)
                _current_adapter["v"] = adapter
                _skip_engines = set()   # 后台模式撞验证后，该引擎本轮全跳过

                # 展开来源 → 引擎列表（深度模式全展开，常规模式只取第一个）
                engine_tasks = []
                for src_id in sources:
                    versions = SOURCE_GROUPS.get(src_id, [src_id])
                    if _deep_mode["v"]:
                        for v in versions:
                            engine_tasks.append((v, src_id))
                    else:
                        engine_tasks.append((versions[0], src_id))

                total = len(engine_tasks)
                for i, (engine_id, src_id) in enumerate(engine_tasks, 1):
                    if _stop_event.is_set():
                        break

                    label = SOURCE_LABELS.get(src_id, src_id)

                    if engine_id in _skip_engines:
                        self.push_log(f"跳过 {label}（本轮撞过验证码）")
                        continue

                    engine_cls = ENGINE_MAP.get(engine_id)
                    if engine_cls is None:
                        self.push_log(f"跳过未知来源：{src_id}")
                        continue

                    self.push_log(f"第 {i}/{total} 个引擎：{label}")

                    # 页数：夸克不翻页；auto / 空 = 10 页
                    if engine_id in ("quark", "quark_cn"):
                        max_pages = 1
                    elif pages == "auto" or pages == "":
                        max_pages = 10
                    else:
                        try:
                            max_pages = max(1, int(pages))
                        except Exception:                         # noqa: BLE001
                            max_pages = 10

                    all_results = []
                    adapter.captcha_hit = False
                    try:
                        engine = engine_cls(adapter)
                        page_num = 0
                        captcha_retry = 0
                        while page_num < max_pages:
                            if _stop_event.is_set():
                                break
                            try:
                                page_results = engine.fetch(
                                    context, book, page_num=page_num, book_kw=book
                                )
                            except Exception as e:                # noqa: BLE001
                                self.push_log(f"{label} 第 {page_num + 1} 页出错：{e}")
                                break

                            # ★ 引擎撞验证码时调过 pause_for_user，然后 return 了空结果，
                            #   这次 fetch 的结果不可信，按模式分别处理
                            if adapter.captcha_hit:
                                adapter.captcha_hit = False
                                if _stop_event.is_set():
                                    break
                                if _frontend_mode["v"] and adapter.wait_continue_event.is_set():
                                    # 前台：用户已过验证 → 重试当前页（不 page_num++）
                                    captcha_retry += 1
                                    if captcha_retry > MAX_CAPTCHA_RETRY:
                                        _skip_engines.add(engine_id)
                                        self.push_log(
                                            f"{label} 连续 {MAX_CAPTCHA_RETRY} 次验证未通过，"
                                            "本轮跳过该引擎"
                                        )
                                        break
                                    self.push_log(f"{label} 验证码已处理，重试当前页")
                                    continue
                                else:
                                    # 后台模式：直接跳过该引擎；
                                    # 前台模式但没等到确认（超时/已停止）：同样跳过
                                    _skip_engines.add(engine_id)
                                    if _frontend_mode["v"]:
                                        self.push_log(f"{label} 验证未完成（超时/已停止），本轮跳过该引擎")
                                    else:
                                        self.push_log(f"{label} 后台模式撞验证码，本轮跳过该引擎")
                                    break

                            if not page_results:
                                break
                            self.push_log(
                                f"【{label} 第 {page_num + 1} 页】命中 {len(page_results)} 条"
                            )
                            for r in page_results:
                                self.push_row({
                                    "source": label,
                                    "title": r.get("title", ""),
                                    "date": r.get("date", "未知"),
                                    "summary": r.get("summary", ""),
                                    "url": r.get("url", ""),
                                })
                            all_results.extend(page_results)
                            page_num += 1
                            captcha_retry = 0
                            if _stop_event.is_set():
                                break
                    except Exception as e:                        # noqa: BLE001
                        self.push_log(f"{label} 出错：{e}")

                    self.push_log(f"{label} 共 {len(all_results)} 条")
                    if _stop_event.is_set():
                        break

                try:
                    context.close()
                except Exception as exc:                          # noqa: BLE001
                    print(f"[search] context.close() 失败：{exc!r}", file=sys.stderr)

            # ★ 停下来了就报「已停止」，别再报「搜索完成」，否则日志自相矛盾
            if _stop_event.is_set():
                self.push_log("已停止")
            else:
                self.push_log("搜索完成")

        except Exception:                                         # noqa: BLE001
            tb = traceback.format_exc()
            print(tb, file=sys.stderr)
            for line in tb.rstrip().splitlines():
                self.push_log(line)
        finally:
            self.set_searching(False)
            _stop_event.clear()
            _current_adapter["v"] = None
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


def _bring_to_front(window):
    """把窗口显示到最前面（重复启动时被第二个实例叫醒后调用）"""
    try:
        window.show()
        # 先置顶再取消，才能在已最小化 / 被遮挡时真正跳到最前
        window.on_top = True
        window.on_top = False
    except Exception as exc:                                  # noqa: BLE001
        print(f"[single] 唤醒窗口失败：{exc!r}", file=sys.stderr)


def main():
    # ---- 单开保护：已经在跑就唤醒老窗口并退出 ----
    guard = single_instance.SingleInstance(APP_TITLE)
    if not guard.acquire():
        guard.wake_existing()
        print("程序已在运行")
        sys.exit(0)

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

    # 注册唤醒回调：第二个实例启动时，把本窗口显示到最前
    guard.on_wake(lambda: _bring_to_front(window))

    webview.start(debug=os.environ.get("PM_DEBUG") == "1")


if __name__ == "__main__":
    main()
