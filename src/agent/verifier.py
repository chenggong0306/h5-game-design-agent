"""生成后自检：静态分析（必跑、无依赖）+ 可选无头浏览器运行检查（需 playwright）。

目标：在把游戏返回给用户之前，自动发现"看不到 / 玩不了 / 一打就崩"这类问题，
交回模型自修。静态分析专门覆盖反复出现的 Canvas 样板坑；无头运行补足运行时报错/空屏。

每个 issue: {id, severity('high'|'medium'|'low'), msg, fix}
"""

import asyncio
import re

from src.utils.logger import logger


def looks_like_game(html: str) -> bool:
    low = (html or "").lower()
    return ("<canvas" in low or "getcontext" in low) and "<script" in low


# ---------------- 静态检查（高精度、低误报） ----------------

def _check_html_closed(html, low):
    if "</html>" not in low:
        return {"id": "no_html_close", "severity": "high",
                "msg": "HTML 缺少 </html> 结束标签（文档可能被截断）",
                "fix": "补全文档到 </body></html> 闭合"}
    return None


def _check_canvas_css_size(html, low):
    """设了高分缓冲(canvas.width=W*dpr)+DPR 适配，却没设 CSS 显示尺寸 → 高分屏放大、人物/地面跑屏外。"""
    sets_buffer = re.search(r"canvas\.width\s*=", html) is not None
    uses_dpr = ("devicepixelratio" in low) or ("settransform" in low)
    sets_style = "canvas.style.width" in low or "canvas.style.height" in low
    css_full = re.search(r"canvas\s*\{[^}]*(100vw|100vh|width\s*:\s*100%|height\s*:\s*100%)", low) is not None
    if sets_buffer and uses_dpr and not sets_style and not css_full:
        return {"id": "dpr_css_size", "severity": "high",
                "msg": "resize 设置了高分缓冲(canvas.width=W*dpr)却没设 CSS 显示尺寸，高分屏会把画面放大约2倍、地面和人物跑到屏幕外看不到",
                "fix": "在 resize 里加 canvas.style.width=W+'px'; canvas.style.height=H+'px'；或 CSS 给 canvas 加 width:100vw;height:100vh"}
    return None


def _check_mouse_input(html, low):
    """绑了 touch 却完全没有鼠标/指针/点击 → 桌面预览点不动、连开始都进不去。"""
    has_touch = "touchstart" in low
    has_mouse = any(k in low for k in (
        "pointerdown", "mousedown",
        "addeventlistener('click'", 'addeventlistener("click"',
        ".onclick", "onclick=",
    ))
    if has_touch and not has_mouse:
        return {"id": "no_mouse_input", "severity": "high",
                "msg": "只绑定了触摸事件、没有鼠标/指针输入(pointerdown/mousedown/click)，桌面预览用鼠标点击无反应、可能连开始都进不去",
                "fix": "改用 Pointer Events(pointerdown/move/up)统一覆盖鼠标+触摸，或额外补 mousedown/click；开始界面也要能点击进入"}
    return None


def _check_ctx_scale_dpr(html, low):
    if re.search(r"ctx\.scale\s*\(\s*(dpr|window\.devicepixelratio)", low):
        return {"id": "ctx_scale_dpr", "severity": "medium",
                "msg": "用 ctx.scale(dpr) 做 DPR 适配会在每次 resize 叠加缩放、越缩越小",
                "fix": "改用 ctx.setTransform(dpr,0,0,dpr,0,0)"}
    return None


def _check_physical_dims_layout(html, low):
    """启发式：setTransform(dpr) 后还用 canvas.height 参与定位/运算（low，仅提示）。"""
    if "settransform" in low and re.search(r"canvas\.height\s*[-*]", low):
        return {"id": "physical_dims_layout", "severity": "low",
                "msg": "疑似用 canvas.height（物理像素）做布局/定位；setTransform(dpr) 后应改用逻辑尺寸 H",
                "fix": "用 H=window.innerHeight 等逻辑尺寸定位，canvas.width/height 仅用于缓冲"}
    return None


_STATIC_CHECKS = [
    _check_html_closed,
    _check_canvas_css_size,
    _check_mouse_input,
    _check_ctx_scale_dpr,
    _check_physical_dims_layout,
]


def analyze_static(html: str) -> list[dict]:
    low = (html or "").lower()
    issues = []
    for chk in _STATIC_CHECKS:
        try:
            r = chk(html, low)
            if r:
                issues.append(r)
        except Exception as e:
            logger.warning("static_check_failed", check=chk.__name__, error=str(e))
    return issues


# ---------------- 可选：无头浏览器运行检查（补足运行时报错/空屏） ----------------

def _is_blank_png(png_bytes: bytes) -> bool:
    """整屏近乎纯色 → 判为空白（初始化失败/全屏没画东西）。需要 PIL（chromadb 已带 pillow）。"""
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB").resize((48, 48))
        raw = img.tobytes()  # RGBRGB...（避开已弃用的 Image.getdata）
        n = len(raw) // 3
        if n == 0:
            return False
        def var(off):
            a = raw[off::3]            # 该通道的全部字节（R=0/G=1/B=2）
            m = sum(a) / n
            return sum((x - m) ** 2 for x in a) / n
        return (var(0) + var(1) + var(2)) < 60
    except Exception:
        return False


def _is_visually_torn(png_bytes: bytes) -> bool:
    """检测 3D 渲染画面是否出现严重的面片撕裂/碎片化。

    将截图缩到 40×30，计算相邻像素颜色差：正常 3D 场景有少数清晰边缘（低噪声），
    撕裂画面有大量混乱颜色跳跃（高噪声）。返回 True 表示高度疑似画面破碎。
    """
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB").resize((40, 30))
        raw = img.tobytes()  # RGBRGB...（避开已弃用的 Image.getdata）
        pixels = [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 3)]
        if not pixels:
            return False
        w, h = 40, 30
        diffs = []
        for y in range(h):
            for x in range(w):
                idx = y * w + x
                r, g, b = pixels[idx]
                if x < w - 1:
                    r2, g2, b2 = pixels[idx + 1]
                    diffs.append(abs(r - r2) + abs(g - g2) + abs(b - b2))
                if y < h - 1:
                    r2, g2, b2 = pixels[idx + w]
                    diffs.append(abs(r - r2) + abs(g - g2) + abs(b - b2))
        if not diffs:
            return False
        avg = sum(diffs) / len(diffs)
        # 正常 3D 场景 avg ≈ 20-50，撕裂场景 avg ≈ 80+
        high = sum(1 for d in diffs if d > 40) / len(diffs)
        return avg > 70 and high > 0.35
    except Exception:
        return False


# ---- 浏览器实例复用：self_check 默认开启时每回合最多 2-3 次检查，逐次冷启动 Chromium
# 各付 0.5-1.5 秒；改为懒启动 + 常驻复用（每次检查仍开独立 page）。
# 生命周期规则（并发安全的关键，全部在事件循环线程内、持 _browser_lock 时变更）：
#   · 检查开始：登记使用计数 + 取消空闲关停定时器（防定时器到点关掉正在用的实例）
#   · 异常/超时报废：换代——把全局引用清掉让下次冷启动；若还有并发检查在用旧实例，
#     只标记 doomed，由最后一个使用者真正 close（不能从别人脚下关浏览器）
#   · 检查结束：注销计数；自己是 doomed 实例的最后使用者就顺手关掉它；
#     全局实例空闲（无任何使用者）时才排空闲关停定时器
# 应用 shutdown 时由 aclose_browser() 兜底。
_pw = None
_browser = None
_browser_lock: asyncio.Lock | None = None
_idle_close_task: asyncio.Task | None = None
_browser_users: dict[int, int] = {}   # id(browser) -> 进行中的检查数
_doomed_browsers: dict[int, object] = {}  # 已报废但仍有人在用的实例，最后使用者负责 close
_BROWSER_IDLE_CLOSE_S = 300.0


def _get_browser_lock() -> asyncio.Lock:
    global _browser_lock
    if _browser_lock is None:
        _browser_lock = asyncio.Lock()
    return _browser_lock


async def _get_browser():
    """懒启动并复用 Chromium 实例；断连自动重启。调用方需持 _browser_lock。"""
    global _pw, _browser
    if _browser is not None and _browser.is_connected():
        return _browser
    from playwright.async_api import async_playwright
    if _pw is None:
        _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
    return _browser


async def _close_quiet(b) -> None:
    try:
        await b.close()
    except Exception:
        pass


def _cancel_idle_close() -> None:
    global _idle_close_task
    task, _idle_close_task = _idle_close_task, None
    if task is not None and task is not asyncio.current_task() and not task.done():
        task.cancel()


async def _acquire_browser():
    """取常驻实例并登记使用。返回 browser；启动失败返回 None（调用方降级）。"""
    async with _get_browser_lock():
        try:
            browser = await _get_browser()
        except Exception as e:
            logger.warning("headless_browser_launch_failed", error=str(e))
            return None
        _browser_users[id(browser)] = _browser_users.get(id(browser), 0) + 1
        _cancel_idle_close()  # 使用中不许空闲关停
        return browser


async def _release_browser(browser) -> None:
    """注销使用计数；关掉自己是最后使用者的报废实例；空闲时排关停定时器。"""
    async with _get_browser_lock():
        key = id(browser)
        n = _browser_users.get(key, 1) - 1
        if n > 0:
            _browser_users[key] = n
        else:
            _browser_users.pop(key, None)
            doomed = _doomed_browsers.pop(key, None)
            if doomed is not None:
                await _close_quiet(doomed)
        # 只有在【完全】无人使用时才排空闲关停：aclose_browser 会连 playwright driver
        # 一起停掉，报废实例上若还有进行中的检查，停 driver 会从它们脚下断连
        if not _browser_users and (_browser is not None or _pw is not None):
            _schedule_idle_close()


async def _retire_browser(browser) -> None:
    """异常/超时后报废实例：不复用状态不明的进程，但也不从并发检查脚下关掉它。

    调用方自己仍在使用计数里（_release_browser 还没跑），所以这里只做标记与换代，
    真正的 close 在最后一个使用者 _release_browser 时发生。"""
    global _browser
    async with _get_browser_lock():
        if _browser is browser:
            _browser = None  # 换代：下次检查冷启动新实例
        _doomed_browsers[id(browser)] = browser


async def aclose_browser():
    """关停常驻浏览器与 playwright driver（应用 shutdown / 空闲超时调用）。

    连同报废待关的实例与使用计数一起清掉：shutdown 时进行中的检查随事件循环
    一起终止，不会再来 _release_browser。"""
    global _pw, _browser
    _cancel_idle_close()
    async with _get_browser_lock():
        doomed = list(_doomed_browsers.values())
        _doomed_browsers.clear()
        _browser_users.clear()
        b, _browser = _browser, None
        pw, _pw = _pw, None
        for d in doomed:
            await _close_quiet(d)
        if b is not None:
            await _close_quiet(b)
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass


def _schedule_idle_close():
    global _idle_close_task
    old = _idle_close_task
    if old is not None and not old.done():
        old.cancel()

    async def _idle():
        try:
            await asyncio.sleep(_BROWSER_IDLE_CLOSE_S)
        except asyncio.CancelledError:
            return
        await aclose_browser()

    _idle_close_task = asyncio.create_task(_idle())


async def run_headless(html: str, timeout_s: float = 15.0) -> list[dict] | None:
    """用 playwright chromium 跑一遍游戏，捕获运行时报错 + 空屏。
    未安装 playwright/chromium 或出错/超时 → 返回 None（视为不可用，降级到纯静态）。
    整个过程有 timeout_s 墙钟上限，卡住的页面不会拖死自检。"""
    try:
        from playwright.async_api import async_playwright  # noqa: F401 仅探测依赖可用性
    except Exception:
        return None

    browser = await _acquire_browser()
    if browser is None:
        return None

    async def _run() -> tuple[list[str], bool, bytes]:
        # --no-sandbox：root 容器/AutoDL 等环境下 Chromium 启动的必要条件。
        # 去掉沙箱降低进程隔离，威胁模型里的缓解措施：
        #   1. 仅跑不可信 HTML 且拦截外部网络请求（下面 page.route 只放行 data:/about:/blob:）
        #   2. 整段有 wall-clock timeout（asyncio.wait_for），卡住不会拖死自检
        #   3. 每次检查用独立 page、用完即关（finally 保证）；浏览器实例复用但异常即报废换代、空闲超时自动关停
        page = await browser.new_page(viewport={"width": 390, "height": 740}, device_scale_factor=2)
        try:
            # 安全：被检代码是模型生成的不可信 HTML。拦截一切外部请求，只放行内联资源
            # （data:/about:/blob:），防止 SSRF 打云元数据(169.254.169.254)/内网服务、或读本地文件。
            async def _block_external(route):
                scheme = route.request.url.split(":", 1)[0].lower()
                if scheme in ("data", "about", "blob"):
                    await route.continue_()
                else:
                    await route.abort()
            await page.route("**/*", _block_external)

            errs: list[str] = []
            page.on("pageerror", lambda e: errs.append(str(e)))
            page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
            await page.set_content(html, wait_until="load")
            await page.wait_for_timeout(1200)            # 让 requestAnimationFrame 跑起来
            try:                                          # 模拟"点击开始"+按键，触发进入游戏
                await page.mouse.click(195, 370)
                await page.keyboard.press("Space")
            except Exception:
                pass
            await page.wait_for_timeout(800)
            shot = await page.screenshot()
            return errs, _is_blank_png(shot), shot
        finally:
            await page.close()  # 即便超时取消，也保证关掉页面；浏览器实例留给下次复用

    issues = []
    try:
        try:
            errors, blank, shot = await asyncio.wait_for(_run(), timeout=timeout_s)

            seen = set()
            for e in errors:
                key = e[:80]
                if key in seen:
                    continue
                seen.add(key)
                issues.append({"id": "runtime_error", "severity": "high",
                               "msg": f"运行时报错：{e[:180]}",
                               "fix": "修复该 JS 运行时错误（如未定义变量、空引用）"})
                if len(seen) >= 5:
                    break
            if blank:
                issues.append({"id": "blank_screen", "severity": "high",
                               "msg": "运行后画面几乎空白（元素可能在屏幕外/未绘制/初始化失败）",
                               "fix": "检查 DPR 与坐标：canvas.style 尺寸、用逻辑 W/H 定位、实体 y 在可视区内"})
            elif _is_visually_torn(shot):
                issues.append({"id": "visual_tearing", "severity": "high",
                               "msg": "画面出现严重的面片撕裂/碎片化（3D 渲染的顶点计算或投影/背面剔除逻辑有误）",
                               "fix": "检查 V3 类方法（sub/mul/cross/norm）是否返回正确类型、project() 返回值是否被当成 V3 调用方法、背面剔除是否在视角空间判 vn.z、旋转后 orig 是否 Math.round 量化"})
        except (NameError, AttributeError, TypeError, UnboundLocalError) as e:
            # 自身编程错误（而非环境性失败）：必须高调记录，否则无头检测会静默退化成"从不报告问题"
            logger.error("headless_verify_bug", error=repr(e))
            await _retire_browser(browser)  # 状态不明的实例不复用，报废换代
            return None
        except Exception as e:
            logger.warning("headless_verify_failed", error=str(e))
            await _retire_browser(browser)
            return None
    finally:
        await _release_browser(browser)
    return issues


async def verify_game(html: str, use_headless: bool = False) -> dict:
    """返回 {ok, issues}。ok = 没有 high/medium 级问题（low 仅提示，不触发自修）。"""
    issues = analyze_static(html)
    if use_headless:
        h = await run_headless(html)
        if h:
            issues.extend(h)
    # 按 id 去重
    seen, uniq = set(), []
    for i in issues:
        if i["id"] not in seen:
            seen.add(i["id"])
            uniq.append(i)
    blocking = [i for i in uniq if i["severity"] in ("high", "medium")]
    return {"ok": len(blocking) == 0, "issues": uniq, "blocking": blocking}
