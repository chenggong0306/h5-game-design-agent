"""自检静态分析回归测试：复现并验证最近几轮真实出现的 Canvas 样板坑被检出。"""

import asyncio
import sys
import unittest

from src.agent import verifier


def ids(issues):
    return {i["id"] for i in issues}


class StaticAnalysisTests(unittest.TestCase):
    def test_looks_like_game(self):
        self.assertTrue(verifier.looks_like_game("<canvas></canvas><script>1</script>"))
        self.assertFalse(verifier.looks_like_game("<p>hello</p>"))

    def test_missing_canvas_css_size_flagged(self):
        # "看不到人物"：设了高分缓冲 + setTransform，却没设 canvas.style 尺寸
        html = ("<canvas id='c'></canvas><script>"
                "function resize(){const dpr=window.devicePixelRatio||1;"
                "canvas.width=innerWidth*dpr;canvas.height=innerHeight*dpr;"
                "ctx.setTransform(dpr,0,0,dpr,0,0);}</script></html>")
        self.assertIn("dpr_css_size", ids(verifier.analyze_static(html)))

    def test_canvas_css_size_ok_when_style_set(self):
        html = ("<canvas id='c'></canvas><script>"
                "function resize(){const dpr=window.devicePixelRatio||1;"
                "canvas.width=innerWidth*dpr;canvas.height=innerHeight*dpr;"
                "canvas.style.width=innerWidth+'px';canvas.style.height=innerHeight+'px';"
                "ctx.setTransform(dpr,0,0,dpr,0,0);}</script></html>")
        self.assertNotIn("dpr_css_size", ids(verifier.analyze_static(html)))

    def test_canvas_css_size_ok_when_css_fullscreen(self):
        html = ("<style>canvas{width:100vw;height:100vh}</style>"
                "<canvas id='c'></canvas><script>"
                "canvas.width=innerWidth*2;ctx.setTransform(2,0,0,2,0,0);</script></html>")
        self.assertNotIn("dpr_css_size", ids(verifier.analyze_static(html)))

    def test_touch_only_input_flagged(self):
        # "玩不了"：只绑 touch、没有鼠标/指针
        html = "<canvas id='c'></canvas><script>canvas.addEventListener('touchstart', e=>{});</script></html>"
        self.assertIn("no_mouse_input", ids(verifier.analyze_static(html)))

    def test_input_ok_with_pointer(self):
        html = "<canvas id='c'></canvas><script>canvas.addEventListener('pointerdown', e=>{});canvas.addEventListener('touchstart',e=>{});</script></html>"
        self.assertNotIn("no_mouse_input", ids(verifier.analyze_static(html)))

    def test_input_ok_with_click(self):
        html = "<canvas id='c'></canvas><script>canvas.addEventListener('touchstart',e=>{});btn.onclick=()=>{};</script></html>"
        self.assertNotIn("no_mouse_input", ids(verifier.analyze_static(html)))

    def test_ctx_scale_dpr_flagged(self):
        html = "<canvas id='c'></canvas><script>const dpr=2;ctx.scale(dpr,dpr);</script></html>"
        self.assertIn("ctx_scale_dpr", ids(verifier.analyze_static(html)))

    def test_missing_html_close_flagged(self):
        html = "<canvas id='c'></canvas><script>let x=1;</script>"
        self.assertIn("no_html_close", ids(verifier.analyze_static(html)))

    def test_clean_game_has_no_blocking_issues(self):
        html = ("<!DOCTYPE html><html><head><style>canvas{width:100vw;height:100vh;display:block}</style></head><body>"
                "<canvas id='c'></canvas><script>"
                "const canvas=document.getElementById('c');const ctx=canvas.getContext('2d');"
                "function resize(){const dpr=window.devicePixelRatio||1;"
                "canvas.width=innerWidth*dpr;canvas.height=innerHeight*dpr;"
                "canvas.style.width=innerWidth+'px';canvas.style.height=innerHeight+'px';"
                "ctx.setTransform(dpr,0,0,dpr,0,0);}resize();"
                "canvas.addEventListener('pointerdown', e=>{});"
                "</script></body></html>")
        res = asyncio.run(verifier.verify_game(html, use_headless=False))
        self.assertTrue(res["ok"], res["issues"])
        self.assertEqual(res["blocking"], [])

    def test_verify_game_reports_blocking(self):
        html = "<canvas id='c'></canvas><script>canvas.addEventListener('touchstart',e=>{});canvas.width=innerWidth*2;ctx.setTransform(2,0,0,2,0,0);</script></html>"
        res = asyncio.run(verifier.verify_game(html, use_headless=False))
        self.assertFalse(res["ok"])
        got = ids(res["blocking"])
        self.assertIn("no_mouse_input", got)
        self.assertIn("dpr_css_size", got)


class BlankDetectionTests(unittest.TestCase):
    """无头检查的空屏判定：纯色截图=空屏，高方差画面=非空。"""

    def _png(self, painter):
        from io import BytesIO
        from PIL import Image
        img = Image.new("RGB", (64, 64))
        px = img.load()
        for x in range(64):
            for y in range(64):
                px[x, y] = painter(x, y)
        buf = BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def test_solid_color_is_blank(self):
        self.assertTrue(verifier._is_blank_png(self._png(lambda x, y: (20, 30, 40))))

    def test_high_variance_is_not_blank(self):
        png = self._png(lambda x, y: ((x * 4) % 256, (y * 4) % 256, ((x + y) * 4) % 256))
        self.assertFalse(verifier._is_blank_png(png))


class _FakeMouse:
    async def click(self, x, y):
        pass


class _FakeKeyboard:
    async def press(self, key):
        pass


class _FakePage:
    def __init__(self, shot, errors):
        self._shot = shot
        self._errors = errors
        self._handlers = {}
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard()

    async def route(self, pattern, handler):
        pass

    def on(self, event, cb):
        self._handlers.setdefault(event, []).append(cb)

    async def set_content(self, html, wait_until=None):
        for e in self._errors:
            for cb in self._handlers.get("pageerror", []):
                cb(e)

    async def wait_for_timeout(self, ms):
        pass

    async def screenshot(self):
        return self._shot

    async def close(self):
        pass


class _FakeBrowser:
    def __init__(self, page):
        self._page = page
        self._closed = False

    def is_connected(self):
        return not self._closed

    async def new_page(self, **kw):
        return self._page

    async def close(self):
        self._closed = True


class _FakeChromium:
    def __init__(self, owner):
        self._owner = owner

    async def launch(self, **kw):
        self._owner.launch_count += 1
        return _FakeBrowser(self._owner._page)


class _FakePW:
    def __init__(self, owner):
        self.chromium = _FakeChromium(owner)

    async def stop(self):
        pass


class _FakePlaywright:
    """模拟 async_playwright().start()/stop() 生命周期（verifier 常驻复用浏览器实例）。"""

    def __init__(self, page):
        self._page = page
        self.launch_count = 0

    def __call__(self):
        return self

    async def start(self):
        return _FakePW(self)


class RunHeadlessTests(unittest.TestCase):
    """run_headless 后处理路径回归：截图非空白时结果绝不能被静默丢弃。

    背景：曾因 _run() 只返回 (errs, blank) 而外层引用局部变量 shot，导致
    非空白截图必抛 NameError → except 吞掉 → return None → 运行时报错全部丢失。
    """

    def _png(self, size, painter):
        from io import BytesIO
        from PIL import Image
        img = Image.new("RGB", size)
        px = img.load()
        for x in range(size[0]):
            for y in range(size[1]):
                px[x, y] = painter(x, y)
        buf = BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def _gradient_png(self):  # 非空白（高方差）、非撕裂（相邻平滑）
        return self._png((64, 64), lambda x, y: ((x * 4) % 256, (y * 4) % 256, ((x + y) * 2) % 256))

    def _solid_png(self):  # 空白（纯色）
        return self._png((64, 64), lambda x, y: (20, 30, 40))

    def _noise_png(self):  # 撕裂（相邻像素剧烈跳变）；40×30 与检测分辨率一致，避免缩放平滑
        import random
        rng = random.Random(42)
        return self._png((40, 30), lambda x, y: (rng.randrange(256), rng.randrange(256), rng.randrange(256)))

    def _install_fakes(self, fake):
        import sys
        import types
        mod = types.ModuleType("playwright.async_api")
        mod.async_playwright = fake
        pkg = types.ModuleType("playwright")
        pkg.async_api = mod
        saved = {k: sys.modules.get(k) for k in ("playwright", "playwright.async_api")}
        sys.modules["playwright"] = pkg
        sys.modules["playwright.async_api"] = mod
        return saved

    def _restore_modules(self, saved):
        import sys
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    _HTML = "<canvas></canvas><script>1</script></html>"

    def _run_with_fake(self, shot, errors):
        saved = self._install_fakes(_FakePlaywright(_FakePage(shot, errors)))

        async def _go():
            try:
                return await verifier.run_headless(self._HTML)
            finally:
                await verifier.aclose_browser()  # 复位常驻实例，避免测试间串台

        try:
            return asyncio.run(_go())
        finally:
            self._restore_modules(saved)

    def test_idle_close_timer_paused_while_check_active(self):
        """空闲关停定时器在检查进行中必须取消：否则上一轮排的定时器到点，
        会从正在进行的检查脚下把浏览器关掉（检查失败、自检静默退化）。"""
        fake = _FakePlaywright(_FakePage(self._gradient_png(), []))
        saved = self._install_fakes(fake)

        async def _go():
            try:
                b = await verifier._acquire_browser()
                during = verifier._idle_close_task
                await verifier._release_browser(b)
                after = verifier._idle_close_task
                return during, after
            finally:
                await verifier.aclose_browser()

        try:
            during, after = asyncio.run(_go())
        finally:
            self._restore_modules(saved)
        self.assertIsNone(during, "检查进行中不应存在空闲关停定时器")
        self.assertIsNotNone(after, "全部检查结束后应重新排定空闲关停")

    def test_retire_does_not_close_browser_under_concurrent_check(self):
        """异常报废 = 换代语义：并发检查还在用旧实例时不得 close（否则一个会话的
        超时会把另一个会话的自检一起打挂），由最后一个使用者真正关闭。"""
        fake = _FakePlaywright(_FakePage(self._gradient_png(), []))
        saved = self._install_fakes(fake)

        async def _go():
            try:
                a = await verifier._acquire_browser()
                b = await verifier._acquire_browser()
                assert a is b, "并发检查应共享同一常驻实例"
                await verifier._retire_browser(a)    # A 的检查异常/超时 → 报废
                await verifier._release_browser(a)   # A 结束：B 还在用，不能关
                still_open = a.is_connected()
                c = await verifier._acquire_browser()  # 换代：新检查拿到新实例
                fresh = c is not a
                await verifier._release_browser(b)   # 旧实例最后一个使用者退出 → 真正 close
                closed = not a.is_connected()
                await verifier._release_browser(c)
                return still_open, fresh, closed
            finally:
                await verifier.aclose_browser()

        try:
            still_open, fresh, closed = asyncio.run(_go())
        finally:
            self._restore_modules(saved)
        self.assertTrue(still_open, "报废时并发检查仍在用，不能立即 close")
        self.assertTrue(fresh, "报废后新检查应拿到换代的新实例")
        self.assertTrue(closed, "最后一个使用者退出后报废实例才被关闭")
        self.assertEqual(fake.launch_count, 2)

    def test_browser_instance_reused_across_checks(self):
        # 同一常驻实例下，连续两次检查只 launch 一次 Chromium
        fake = _FakePlaywright(_FakePage(self._gradient_png(), []))
        saved = self._install_fakes(fake)

        async def _go():
            try:
                return await verifier.run_headless(self._HTML), await verifier.run_headless(self._HTML)
            finally:
                await verifier.aclose_browser()

        try:
            r1, r2 = asyncio.run(_go())
        finally:
            self._restore_modules(saved)
        self.assertEqual(r1, [])
        self.assertEqual(r2, [])
        self.assertEqual(fake.launch_count, 1)

    def test_runtime_error_reported_on_nonblank_screen(self):
        issues = self._run_with_fake(self._gradient_png(), ["ReferenceError: h is not defined"])
        self.assertIsNotNone(issues, "非空白截图时结果被整体丢弃（shot 回归复现）")
        got = ids(issues)
        self.assertIn("runtime_error", got)
        self.assertTrue(any("h is not defined" in i["msg"] for i in issues))

    def test_clean_nonblank_screen_returns_empty_list(self):
        issues = self._run_with_fake(self._gradient_png(), [])
        self.assertEqual(issues, [])

    def test_blank_screen_reported(self):
        issues = self._run_with_fake(self._solid_png(), [])
        self.assertIsNotNone(issues)
        self.assertIn("blank_screen", ids(issues))

    def test_visual_tearing_reported(self):
        issues = self._run_with_fake(self._noise_png(), [])
        self.assertIsNotNone(issues)
        self.assertIn("visual_tearing", ids(issues))


class HeadlessSelectorLoopTests(unittest.TestCase):
    """回归：Windows 下 uvicorn 跑的是 Selector 事件循环（不支持子进程），playwright
    曾因此 NotImplementedError、无头检查静默降级成"从不报问题"。现在 playwright 被
    调度到专属 Proactor 循环线程，Selector 主循环下也必须能跑（真实浏览器冒烟测试；
    未装 playwright/浏览器的环境自动跳过，CI 上不跑）。"""

    HTML = (
        "<!DOCTYPE html><html><head><style>canvas{width:100vw;height:100vh;display:block}"
        "</style></head><body><canvas id='c'></canvas><script>"
        "const canvas=document.getElementById('c');const ctx=canvas.getContext('2d');"
        "canvas.addEventListener('pointerdown',e=>{});"
        "function loop(){ drawCharacter(); requestAnimationFrame(loop); }"  # 故意未定义
        "loop();</script></body></html>"
    )

    def tearDown(self):
        # 复位 verifier 的循环绑定全局，别让真实浏览器测试影响后面的 fake 生命周期测试
        verifier._browser_lock = None
        verifier._idle_close_task = None

    def test_headless_catches_runtime_error_under_selector_loop(self):
        try:
            import playwright  # noqa: F401
        except Exception:
            self.skipTest("playwright 未安装")
        old_policy = asyncio.get_event_loop_policy()
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # 复现 uvicorn 环境
        try:
            async def scenario():
                res = await verifier.run_headless(self.HTML, timeout_s=30)
                await verifier.aclose_browser()
                return res
            res = asyncio.run(scenario())
        finally:
            asyncio.set_event_loop_policy(old_policy)
        if res is None:
            self.skipTest("chromium 浏览器不可用，跳过")
        self.assertIn("runtime_error", ids(res))
        self.assertTrue(any("drawCharacter" in i["msg"] for i in res))


if __name__ == "__main__":
    unittest.main()
