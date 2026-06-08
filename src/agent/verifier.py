"""生成后自检：静态分析（必跑、无依赖）+ 可选无头浏览器运行检查（需 playwright）。

目标：在把游戏返回给用户之前，自动发现"看不到 / 玩不了 / 一打就崩"这类问题，
交回模型自修。静态分析专门覆盖反复出现的 Canvas 样板坑；无头运行补足运行时报错/空屏。

每个 issue: {id, severity('high'|'medium'|'low'), msg, fix}
"""

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
        except Exception:
            pass
    return issues


# ---------------- 可选：无头浏览器运行检查（补足运行时报错/空屏） ----------------

def _is_blank_png(png_bytes: bytes) -> bool:
    """整屏近乎纯色 → 判为空白（初始化失败/全屏没画东西）。需要 PIL（chromadb 已带 pillow）。"""
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB").resize((48, 48))
        px = list(img.getdata())
        def var(idx):
            a = [p[idx] for p in px]
            m = sum(a) / len(a)
            return sum((x - m) ** 2 for x in a) / len(a)
        return (var(0) + var(1) + var(2)) < 60
    except Exception:
        return False


async def run_headless(html: str, timeout_s: float = 6.0) -> list[dict] | None:
    """用 playwright chromium 跑一遍游戏，捕获运行时报错 + 空屏。
    未安装 playwright/chromium 或出错 → 返回 None（视为不可用，降级到纯静态）。"""
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return None

    issues = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
            page = await browser.new_page(viewport={"width": 390, "height": 740}, device_scale_factor=2)
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            await page.set_content(html, wait_until="load")
            await page.wait_for_timeout(1200)            # 让 requestAnimationFrame 跑起来
            try:                                          # 模拟"点击开始"+按键，触发进入游戏
                await page.mouse.click(195, 370)
                await page.keyboard.press("Space")
            except Exception:
                pass
            await page.wait_for_timeout(800)
            shot = await page.screenshot()
            blank = _is_blank_png(shot)
            await browser.close()

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
    except Exception as e:
        logger.warning("headless_verify_failed", error=str(e))
        return None
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
