"""生成后自检：静态分析（必跑、无依赖）+ 可选无头浏览器运行检查（需 playwright）。

目标：在把游戏返回给用户之前，自动发现"看不到 / 玩不了 / 一打就崩"这类问题，
交回模型自修。静态分析专门覆盖反复出现的 Canvas 样板坑；无头运行补足运行时报错/空屏。

每个 issue: {id, severity('high'|'medium'|'low'), msg, fix}
"""

import asyncio
import json
import re
import sys
import threading
from urllib.parse import urlsplit

from src.config import settings
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


def _extract_js_function_body(html: str, name: str) -> str:
    """Return a best-effort JavaScript function body for small semantic checks."""
    match = re.search(
        rf"\bfunction\s+{re.escape(name)}\s*\([^)]*\)\s*\{{",
        html,
        re.IGNORECASE,
    )
    if not match:
        return ""
    start = match.end() - 1
    depth = 0
    quote = ""
    escaped = False
    for index in range(start, len(html)):
        char = html[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return html[start + 1:index]
    return ""


def _check_unused_card_deck(html, low):
    """A declared draw/deck pile that is never read makes advertised cards unreachable."""
    names = [
        name for name in re.findall(
            r"\b(?:let|const|var)\s+([A-Za-z_$][\w$]*)\s*=",
            html,
            re.IGNORECASE,
        )
        if "deck" in name.lower() or "drawpile" in name.lower()
    ]
    unused = []
    for name in dict.fromkeys(names):
        if len(re.findall(rf"\b{re.escape(name)}\b", html)) <= 1:
            unused.append(name)
    if unused:
        shown = "、".join(f"`{name}`" for name in unused[:4])
        return {
            "id": "card_cycle_broken",
            "severity": "medium",
            "msg": f"声明了牌库 {shown}，但后续从未读取或轮换；部分卡牌实际永远无法进入手牌",
            "fix": (
                "出牌后从手牌移除该牌，从牌库补一张，并把用过的牌放回队尾；"
                "连续出牌超过初始牌组长度后仍应保持固定手牌数量"
            ),
        }
    return None


def _check_projectile_damage(html, low):
    """Catch projectile update loops that visually hit but never apply their stored damage."""
    function_names = re.findall(
        r"\bfunction\s+([A-Za-z_$][\w$]*projectile[\w$]*)\s*\(",
        html,
        re.IGNORECASE,
    )
    for name in function_names:
        body = _extract_js_function_body(html, name)
        body_low = body.lower()
        if not body or not any(marker in body_low for marker in ("hit", "命中", "hypot", "distance")):
            continue
        stores_damage = bool(
            re.search(r"\b(?:p|projectile)\s*\.\s*(?:dmg|damage)\b", body, re.I)
            or re.search(
                r"\bfunction\s+[A-Za-z_$][\w$]*projectile[\w$]*\s*\([^)]*\b(?:dmg|damage)\b[^)]*\)",
                html,
                re.IGNORECASE,
            )
        )
        applies_damage = bool(re.search(
            r"(?:\.hp\s*[-+]?=|takedamage\s*\(|applydamage\s*\(|damage\s*\()",
            body,
            re.IGNORECASE,
        ))
        if stores_damage and not applies_damage:
            return {
                "id": "projectile_no_damage",
                "severity": "high",
                "msg": f"`{name}()` 中弹道命中后只播放效果，没有把 projectile.dmg/damage 扣到目标生命值",
                "fix": "让弹道保存目标实体；命中时先校验目标仍存活，再扣除目标 HP 并触发死亡/清理逻辑",
            }
    return None


def _computed_property_key_patterns(code: str) -> list[re.Pattern]:
    """Return conservative key patterns for computed JavaScript property reads.

    Generated games commonly keep source images in a cache and select a team/action
    variant with expressions such as ``images[cardId + 'WB']``.  A literal-only static
    scan treats every ``archerWB``/``giantWB`` manifest entry as unused and can therefore
    instruct the repair model to delete assets that are required at runtime.

    Only expressions with a fixed, non-empty prefix or suffix are recognised here.  A
    generic loader access such as ``images[key]`` deliberately remains a wildcard-free
    non-use: populating the cache is not evidence that an image is ever rendered.
    """
    identifier = r"[A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*"
    suffix_expression = re.compile(
        rf"^\s*(?:{identifier})\s*\+\s*"
        r"(?P<quote>['\"])(?P<literal>(?:\\.|(?!(?P=quote)).)*)(?P=quote)\s*$",
        re.DOTALL,
    )
    prefix_expression = re.compile(
        r"^\s*(?P<quote>['\"])(?P<literal>(?:\\.|(?!(?P=quote)).)*)(?P=quote)"
        rf"\s*\+\s*(?:{identifier})\s*$",
        re.DOTALL,
    )

    patterns = []
    seen = set()
    access_pattern = re.compile(
        r"\b[A-Za-z_$][\w$]*\s*\[\s*([^\]\r\n]{1,160})\s*\]"
    )
    for expression in access_pattern.findall(code):
        match = suffix_expression.fullmatch(expression)
        if match:
            literal = re.sub(r"\\(['\"\\])", r"\1", match.group("literal"))
            if literal:
                source = rf"^.+{re.escape(literal)}$"
                if source not in seen:
                    seen.add(source)
                    patterns.append(re.compile(source))
            continue

        match = prefix_expression.fullmatch(expression)
        if match:
            literal = re.sub(r"\\(['\"\\])", r"\1", match.group("literal"))
            if literal:
                source = rf"^{re.escape(literal)}.+$"
                if source not in seen:
                    seen.add(source)
                    patterns.append(re.compile(source))
            continue

        # Template keys are the same pattern in another common generated-code form:
        # images[`${cardId}WB`] or images[`unit-${cardId}`].
        template = re.fullmatch(r"\s*`([^`]+)`\s*", expression, re.DOTALL)
        if not template or "${" not in template.group(1):
            continue
        pieces = re.split(r"\$\{[^{}]+\}", template.group(1))
        if len(pieces) < 2 or not any(pieces):
            continue
        source = "^" + ".+".join(re.escape(piece) for piece in pieces) + "$"
        if source not in seen:
            seen.add(source)
            patterns.append(re.compile(source))
    return patterns


def _check_excessive_unused_preloads(html, low):
    """Report large generated image manifests whose entries are never referenced by code."""
    object_pattern = re.compile(
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*\{(.*?)\n\s*\};",
        re.DOTALL,
    )
    for _object_name, body in object_pattern.findall(html):
        if len(re.findall(r"/assets/(?:source|image|spritesheet)/", body)) < 8:
            continue
        keys = re.findall(r"(?m)^\s*([A-Za-z_$][\w$]*)\s*:\s*['\"]/assets/", body)
        if len(keys) < 8:
            continue
        manifest_start = html.find(body)
        outside_manifest = (
            html[:manifest_start] + html[manifest_start + len(body):]
            if manifest_start >= 0 else html
        )
        computed_key_patterns = _computed_property_key_patterns(outside_manifest)
        unused = []
        for key in keys:
            # Generated manifests are normally consumed through a cache.  Count both direct
            # property access (cache.foo) and string-key mappings ('foo'); bare declarations
            # such as ``let foo = null`` do not mean that the image is ever used.
            use_pattern = rf"(?:\.\s*{re.escape(key)}\b|['\"]{re.escape(key)}['\"])"
            is_computed_use = any(pattern.fullmatch(key) for pattern in computed_key_patterns)
            if not re.search(use_pattern, outside_manifest) and not is_computed_use:
                unused.append(key)
        if len(unused) >= 8 and len(unused) / len(keys) >= 0.3:
            preview = "、".join(f"`{key}`" for key in unused[:6])
            return {
                "id": "excessive_unused_assets",
                "severity": "medium",
                "msg": (
                    f"预加载清单中有 {len(unused)}/{len(keys)} 个图片键从未被绘制或读取"
                    f"（如 {preview}）；这会拖慢启动并扩大超时竞态"
                ),
                "fix": "删除未使用素材，只保留背景、当前卡组、对应红蓝动画、塔和少量实际触发的特效",
            }
    return None


def _check_duplicate_loader_start_risk(html, low):
    """Detect a load-complete path and timeout path that can both start the main loop."""
    load_calls = list(re.finditer(r"\bloadimages\s*\(", low))
    if not load_calls:
        return None
    tail = low[load_calls[-1].start():]
    if "settimeout" not in tail or tail.count("requestanimationframe") < 2:
        return None
    has_once_guard = any(marker in tail for marker in (
        "settled", "clearTimeout".lower(), "finishonce", "startonce",
    ))
    if not has_once_guard:
        return {
            "id": "duplicate_animation_loop_risk",
            "severity": "high",
            "msg": "图片全部完成和加载超时两条路径都能启动 requestAnimationFrame，但没有共享 once/settled 守卫",
            "fix": "两条路径统一调用带 settled 守卫的 finish()，正常结束时 clearTimeout(timer)，主循环只启动一次",
        }
    return None


_STATIC_CHECKS = [
    _check_html_closed,
    _check_canvas_css_size,
    _check_mouse_input,
    _check_ctx_scale_dpr,
    _check_physical_dims_layout,
    _check_unused_card_deck,
    _check_projectile_damage,
    _check_excessive_unused_preloads,
    _check_duplicate_loader_start_risk,
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


def _source_animation_axis(spec: dict) -> str:
    """Return a canonical animation axis from the small supported schema variants."""
    animation = spec.get("animation") or {}
    if not isinstance(animation, dict):
        return ""
    for key in ("frame_axis", "axis", "layout"):
        value = animation.get(key)
        if isinstance(value, dict):
            value = value.get("axis") or value.get("frame_axis")
        text = str(value or "").strip().lower()
        if text:
            return text
    return ""


def _source_animation_catalog(spec: dict) -> dict:
    """Return a source sprite catalogue without tying the verifier to one game."""
    animation = spec.get("animation") or {}
    if not isinstance(animation, dict):
        return {}
    for key in ("fish_catalog", "sprite_catalog"):
        value = animation.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _source_cannon_catalog(spec: dict) -> dict:
    animation = spec.get("animation") or {}
    if not isinstance(animation, dict):
        return {}
    value = animation.get("cannon_catalog")
    return value if isinstance(value, dict) else {}


def _catalog_state_sequences(spec: dict) -> dict[str, set[tuple[int, ...]]]:
    """Collect the finite frame sequences declared by a source-reference profile."""
    result: dict[str, set[tuple[int, ...]]] = {}
    animation = spec.get("animation") or {}
    sources = []
    if isinstance(animation, dict):
        for key in ("state_sequences", "sequences"):
            if isinstance(animation.get(key), dict):
                sources.append(animation[key])
    for item in _source_animation_catalog(spec).values():
        if not isinstance(item, dict):
            continue
        for key in ("state_sequences", "sequences"):
            if isinstance(item.get(key), dict):
                sources.append(item[key])
        legacy = {
            "swim": item.get("swim_frames") or item.get("swimFrames"),
            "capture": item.get("capture_frames") or item.get("captureFrames"),
        }
        sources.append(legacy)
    for source in sources:
        for state, values in source.items():
            if not isinstance(values, (list, tuple)) or not values:
                continue
            try:
                sequence = tuple(int(value) for value in values)
            except (TypeError, ValueError):
                continue
            result.setdefault(str(state).strip().lower(), set()).add(sequence)
    return result


def _extract_state_frame_arrays(html: str, state: str) -> list[tuple[int, ...]]:
    """Extract explicit ``swimFrames:[...]`` style arrays from generated JavaScript."""
    pattern = re.compile(
        rf"\b{re.escape(state)}(?:[_$-]?frames?)?\s*[:=]\s*\[([^\]]*)\]",
        re.IGNORECASE,
    )
    sequences = []
    for body in pattern.findall(html or ""):
        tokens = re.findall(r"-?\d+", body)
        # Reject expressions and sparse arrays: source contracts intentionally require a
        # readable static mapping so automatic repair cannot silently guess frame ranges.
        residue = re.sub(r"(?:-?\d+|[\s,])", "", body)
        if not tokens or residue:
            continue
        sequences.append(tuple(int(token) for token in tokens))
    return sequences


def _fish_draw_scopes(html: str) -> list[str]:
    """Return ordinary function bodies that are likely to render fish sprites."""
    names = re.findall(
        r"\bfunction\s+([A-Za-z_$][\w$]*fish[A-Za-z_$\d]*)\s*\(",
        html or "",
        re.IGNORECASE,
    )
    return [body for name in dict.fromkeys(names) if (body := _extract_js_function_body(html, name))]


def _check_source_animation_semantics(html: str, spec: dict) -> list[dict]:
    """Validate strict vertical sprite semantics requested by a source profile.

    Fishjoy's source sheets are one frame wide.  A generated ``sx=frame*fw, sy=0``
    loop therefore draws frame zero and then crops outside the image, which looks like a
    blinking fish.  Keep this check profile-driven so unrelated horizontal atlases are not
    rejected.
    """
    if _source_animation_axis(spec) not in {"vertical", "y", "column"}:
        return []
    profile_id = str(spec.get("id") or "source-profile")
    scopes = _fish_draw_scopes(html)
    scope = "\n".join(scopes) if scopes else (html or "")
    frame_word = r"(?:frame|frameindex|currentframe)"
    horizontal_assignment = bool(
        re.search(
            rf"\b(?:const\s+|let\s+|var\s+)?(?:sx|srcx|sourcex)\s*=\s*"
            rf"[^;\r\n]*\b{frame_word}\b[^;\r\n]*\*[^;\r\n]+",
            scope,
            re.IGNORECASE,
        )
        and re.search(
            r"\b(?:const\s+|let\s+|var\s+)?(?:sy|srcy|sourcey)\s*=\s*0(?:\.0+)?\s*(?:;|,|\r?$)",
            scope,
            re.IGNORECASE | re.MULTILINE,
        )
    )
    horizontal_inline = bool(re.search(
        rf"\.drawImage\s*\(\s*[^,]+,\s*[^,]*\b{frame_word}\b[^,]*\*[^,]+,\s*0\s*,",
        scope,
        re.IGNORECASE,
    ))
    vertical_assignment = bool(
        re.search(
            r"\b(?:const\s+|let\s+|var\s+)?(?:sx|srcx|sourcex)\s*=\s*0(?:\.0+)?\s*(?:;|,|\r?$)",
            scope,
            re.IGNORECASE | re.MULTILINE,
        )
        and re.search(
            rf"\b(?:const\s+|let\s+|var\s+)?(?:sy|srcy|sourcey)\s*=\s*"
            rf"[^;\r\n]*\b{frame_word}\b[^;\r\n]*\*[^;\r\n]+",
            scope,
            re.IGNORECASE,
        )
    )
    vertical_inline = bool(re.search(
        rf"\.drawImage\s*\(\s*[^,]+,\s*0\s*,\s*[^,]*\b{frame_word}\b[^,]*\*[^,]+,",
        scope,
        re.IGNORECASE,
    ))
    rect_mapping = bool(re.search(
        r"\.drawImage\s*\(\s*[^,]+,\s*[^,]*(?:rect|frame)[^,]*\.x\s*,"
        r"\s*[^,]*(?:rect|frame)[^,]*\.y\s*,",
        scope,
        re.IGNORECASE,
    ))

    issues = []
    if horizontal_assignment or horizontal_inline or not (
        vertical_assignment or vertical_inline or rect_mapping
    ):
        reason = (
            "检测到 sx 随 frame 增长而 sy 固定为 0 的横向裁帧"
            if horizontal_assignment or horizontal_inline
            else "没有检测到 sx=0、sy=frame*frameHeight（或等价 rect.x/rect.y）的纵向裁帧"
        )
        issues.append({
            "id": f"source_contract_animation_axis_invalid:{profile_id}",
            "severity": "high",
            "msg": f"`{profile_id}` 要求纵向精灵表，但{reason}；后续帧会越过图片宽度并周期性消失",
            "fix": (
                "鱼类精灵使用九参数 drawImage；固定 sx=0，并令 sy=当前帧*frameHeight。"
                "若使用预声明 rect，必须读取同一帧的 rect.x/rect.y/rect.w/rect.h"
            ),
        })

    expected = _catalog_state_sequences(spec)
    required_states = [
        str(value).strip().lower()
        for value in ((spec.get("animation") or {}).get("required_states") or [])
        if str(value).strip()
    ]
    missing = []
    invalid = []
    for state in required_states:
        allowed = expected.get(state) or set()
        declared = _extract_state_frame_arrays(html, state)
        if allowed and not declared:
            missing.append(state)
            continue
        wrong = [sequence for sequence in declared if allowed and sequence not in allowed]
        if wrong:
            invalid.append((state, wrong[0]))
            continue
        # Every distinct source-derived sequence must be represented somewhere in the
        # static mapping.  This catches a single guessed 0..N loop shared by all fish.
        absent = allowed.difference(declared)
        if absent:
            missing.append(state)
    if missing or invalid:
        details = []
        if missing:
            details.append("缺少 " + ", ".join(sorted(set(missing))) + " 的源码帧序列")
        if invalid:
            details.extend(f"{state}={list(sequence)} 不属于源码序列" for state, sequence in invalid[:3])
        issues.append({
            "id": f"source_contract_animation_sequences_invalid:{profile_id}",
            "severity": "high",
            "msg": f"`{profile_id}` 的 swim/capture 配置不完整或错误：" + "；".join(details),
            "fix": (
                "把 animation.fish_catalog 中每种鱼的 swim_frames/capture_frames（或 state_sequences）"
                "原样写入静态鱼类映射；游动只循环 swim，捕获后切换 capture，不能把所有帧混为一个循环"
            ),
        })
    return issues


def _check_fishjoy_cannon_semantics(html: str, spec: dict) -> list[dict]:
    """Reject the two source-level mistakes that make Fishjoy's cannon disappear.

    Fishjoy is authored in a 980x545 logical scene.  A literal logical x coordinate is
    safe only when the renderer applies the logical scene transform; using ``x=490``
    directly in a 390px mobile canvas puts the cannon and its bullets off screen.  The
    five cannon frames are recoil/fire frames and must not be selected from the aim angle.
    """
    profile_id = str(spec.get("id") or "")
    if not profile_id.lower().startswith("fishjoy@") or not _source_cannon_catalog(spec):
        return []

    draw_scope = _extract_js_function_body(html, "drawCannon") or ""
    bullet_scope = _extract_js_function_body(html, "createBullet") or ""
    relevant = "\n".join((draw_scope, bullet_scope))
    issues: list[dict] = []

    literal_logical_x = bool(re.search(
        r"(?:\.translate\s*\(\s*(?:490|532(?:\.5)?)\b|"
        r"\bx\s*:\s*(?:490|532(?:\.5)?)\b|"
        r"\bcannon(?:Origin)?X\s*=\s*(?:490|532(?:\.5)?)\b)",
        relevant,
        re.IGNORECASE,
    ))
    viewport_x = bool(re.search(
        r"(?:\bW\s*/\s*2\b|\b(?:innerWidth|canvas\.clientWidth)\s*/\s*2\b|"
        r"\bcannon(?:Origin)?X\s*=\s*[^;\r\n]*(?:\bW\b|innerWidth|clientWidth))",
        relevant,
        re.IGNORECASE,
    ))
    logical_transform = bool(re.search(
        r"(?:\bctx\.scale\s*\(\s*(?:logicalScale|sceneScale|worldScale|gameScale|scale)\b|"
        r"\bctx\.setTransform\s*\([^;\r\n]*(?:logicalScale|sceneScale|worldScale|gameScale|\bscale\s*\*))",
        html or "",
        re.IGNORECASE,
    ))
    if literal_logical_x and not (viewport_x or logical_transform):
        issues.append({
            "id": f"source_contract_cannon_position_invalid:{profile_id}",
            "severity": "high",
            "msg": (
                f"`{profile_id}` 炮台/炮弹直接使用 980 宽逻辑场景的固定 x 坐标，"
                "在 390px 移动端画布中会整体落到屏幕右侧。"
            ),
            "fix": (
                "统一计算 cannonOrigin：采用当前可视区 W/2 与底部安全位置，或先应用"
                "完整的 980x545 逻辑场景缩放/居中变换；炮台、瞄准和炮弹出生点必须共用该 origin。"
            ),
        })

    angle_drives_frame = bool(re.search(
        r"\b(?:fi|frame(?:Idx|Index)?)\s*=\s*[^;\r\n]*\b(?:cannon)?angle\b",
        draw_scope,
        re.IGNORECASE,
    ))
    angle_frame_inline = bool(re.search(
        r"drawImage\s*\([^;\r\n]*\b(?:cannon)?angle\b[^;\r\n]*\*",
        draw_scope,
        re.IGNORECASE,
    ))
    if angle_drives_frame or angle_frame_inline:
        issues.append({
            "id": f"source_contract_cannon_animation_invalid:{profile_id}",
            "severity": "high",
            "msg": (
                f"`{profile_id}` 把炮台的 5 张开火/后坐力帧当成瞄准方向帧；"
                "转动炮口时会切换错误外观，开火动画也不会按源码播放。"
            ),
            "fix": (
                "瞄准只修改 canvas rotation；frameIndex 必须来自独立的 fireFrame/开火计时器，"
                "发射时按 cannon_catalog.fire_frames 播放 0..4，静止时使用 idle_frame。"
            ),
        })
    return issues


def _check_source_reference_contract(
    html: str,
    source_specs: list[dict] | None,
    source_assets: list[dict] | None = None,
) -> list[dict]:
    """Validate the declaration and machine-checkable parts of a source contract.

    ``source_assets`` contains only assets whose real serving URLs occur in the final HTML
    when called by ``game_agent``.  Comparing their original paths with the canonical spec
    is deterministic and avoids guessing from generated JavaScript identifiers.  This is
    deliberately stricter than the old marker-only check: a page cannot claim a profile and
    then delete its player sprites, small-tower art, or HUD during an automatic repair.
    """
    issues = []
    for spec in source_specs or []:
        profile_id = str(spec.get("id") or "").strip()
        if not profile_id:
            continue
        marker = re.search(
            r"<meta\b(?=[^>]*\bname\s*=\s*(['\"])source-reference-profile\1)"
            r"(?=[^>]*\bcontent\s*=\s*(['\"])" + re.escape(profile_id) + r"\2)[^>]*>",
            html,
            re.IGNORECASE,
        )
        if not marker:
            issues.append({
                "id": f"source_contract_missing:{profile_id}",
                "severity": "high",
                "msg": (
                    f"页面使用了 `{profile_id}` 源码项目素材，但没有声明并锁定对应源码设计契约；"
                    "重新生成时可能把纵横方向、塔、路线或卡组改成另一套游戏"
                ),
                "fix": (
                    f"按自动修复消息附带的 `{profile_id}` 契约逐项核对实现，并加入 "
                    f"<meta name=\"source-reference-profile\" content=\"{profile_id}\">；"
                    "不得只补标记"
                ),
            })

        # A caller that has no asset catalogue cannot prove path-level assertions.  The
        # production generation path always supplies a list (possibly empty); keeping None
        # as "unknown" also preserves the small marker-only API tests and third-party use.
        if source_assets is not None:
            issues.extend(
                _check_source_reference_assets(html, spec, source_assets)
            )

        required_states = [
            str(state).strip()
            for state in ((spec.get("animation") or {}).get("required_states") or [])
            if str(state).strip()
        ]
        missing_states = [
            state
            for state in required_states
            if not re.search(
                rf"\b{re.escape(state)}(?:[_$-]?(?:frames?|sequence))?\b",
                html,
                re.IGNORECASE,
            )
        ]
        if missing_states:
            shown = "、".join(f"`{state}`" for state in missing_states[:6])
            issues.append({
                "id": f"source_contract_animation_states_missing:{profile_id}",
                "severity": "high",
                "msg": (
                    f"`{profile_id}` 缺少源码方向动画状态：{shown}；"
                    "不能把所有兵种统一按 0..N 连续帧播放"
                ),
                "fix": (
                    "为每个兵种使用源码声明的帧尺寸与 up/down/side walk/attack 序列，"
                    "并根据移动或目标方向选择状态"
                ),
            })
        issues.extend(_check_source_animation_semantics(html, spec))
        issues.extend(_check_fishjoy_cannon_semantics(html, spec))
    return issues


def _normalise_contract_asset_path(value: str) -> str:
    """Normalise an uploaded project path without discarding its root prefix."""
    path = str(value or "").replace("\\", "/").strip().lower()
    while path.startswith("./"):
        path = path[2:]
    return "/".join(part for part in path.split("/") if part not in {"", "."})


def _contract_path_matches(actual: str, expected: str) -> bool:
    """Match canonical project paths while allowing an uploaded top-level folder."""
    actual_path = _normalise_contract_asset_path(actual)
    expected_path = _normalise_contract_asset_path(expected)
    return bool(
        actual_path
        and expected_path
        and (actual_path == expected_path or actual_path.endswith("/" + expected_path))
    )


def _referenced_source_assets(html: str, source_assets) -> list[dict]:
    """Keep source asset records whose exact URL is present in the final document."""
    if isinstance(source_assets, dict):
        source_assets = source_assets.get("assets") or source_assets.get("source_assets") or []
    if not isinstance(source_assets, (list, tuple)):
        return []
    result = []
    for item in source_assets:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if url and url not in html:
            continue
        result.append(item)
    return result


def _check_source_reference_assets(
    html: str,
    spec: dict,
    source_assets,
) -> list[dict]:
    """Check required/forbidden original asset paths for one canonical profile."""
    profile_id = str(spec.get("id") or "source-profile")
    asset_spec = spec.get("assets") or {}
    referenced = _referenced_source_assets(html, source_assets)
    referenced_paths = [str(item.get("path") or "") for item in referenced]
    issues: list[dict] = []

    required_paths = [
        str(path) for path in (asset_spec.get("required_paths") or []) if str(path)
    ]
    missing_required = [
        path
        for path in required_paths
        if not any(_contract_path_matches(actual, path) for actual in referenced_paths)
    ]
    if missing_required:
        shown = "、".join(f"`{path}`" for path in missing_required[:6])
        issues.append({
            "id": f"source_contract_assets_missing:{profile_id}",
            "severity": "high",
            "msg": (
                f"`{profile_id}` 缺少 {len(missing_required)}/{len(required_paths)} 个契约必需素材："
                f"{shown}；页面会退化成占位人物、错误塔或程序化 HUD"
            ),
            "fix": (
                "从 load_skill_assets 返回的真实 URL 恢复这些素材并实际绑定到背景、卡牌、"
                "小塔/弩机、王塔与法力 UI；不得用 Canvas 占位替代"
            ),
        })

    group_spec = asset_spec.get("required_team_asset_groups") or {}
    missing_groups: list[str] = []
    if isinstance(group_spec, dict):
        for troop_id, teams in group_spec.items():
            if not isinstance(teams, dict):
                continue
            for team_id, paths in teams.items():
                expected_paths = [str(path) for path in (paths or []) if str(path)]
                if expected_paths and not all(
                    any(_contract_path_matches(actual, path) for actual in referenced_paths)
                    for path in expected_paths
                ):
                    missing_groups.append(f"{troop_id}/{team_id}")
    if missing_groups:
        shown = "、".join(f"`{item}`" for item in missing_groups[:8])
        issues.append({
            "id": f"source_contract_team_assets_missing:{profile_id}",
            "severity": "high",
            "msg": (
                f"`{profile_id}` 的敌我兵种素材不完整（缺 {shown}）；"
                "每个可部署兵种必须同时保留红蓝 walk/attack 源码精灵"
            ),
            "fix": (
                "建立兵种 → 阵营 → walk/attack 的字面量静态映射，使用每张素材自己的"
                "帧尺寸和源码动画序列；禁止所有敌人共用 warrior 或蓝方回退成圆形小人"
            ),
        })

    forbidden_roles = {
        str(role).strip().lower()
        for role in (asset_spec.get("forbidden_roles") or [])
        if str(role).strip()
    }
    forbidden_paths = [
        str(path) for path in (asset_spec.get("forbidden_paths") or []) if str(path)
    ]
    forbidden = []
    for item in referenced:
        role = str(item.get("asset_role") or "").strip().lower()
        path = str(item.get("path") or "")
        if role in forbidden_roles or any(
            _contract_path_matches(path, blocked) for blocked in forbidden_paths
        ):
            forbidden.append(path or role)
    if forbidden:
        shown = "、".join(f"`{item}`" for item in forbidden[:4])
        issues.append({
            "id": f"source_contract_forbidden_assets:{profile_id}",
            "severity": "high",
            "msg": f"`{profile_id}` 引用了禁止的品牌/占位素材：{shown}",
            "fix": "移除开发者品牌与占位图，只使用契约允许的实际游戏素材",
        })
    return issues


# ---------------- 可选：无头浏览器运行检查（补足运行时报错/空屏） ----------------


def _preview_asset_origin() -> str:
    return f"http://127.0.0.1:{settings.port}"


_PREVIEW_ASSET_PATH_RE = re.compile(
    r"^/assets/(?:image|audio|spritesheet|tilemap|font)/"
    r"(?!\.)(?!.*\.\.)[A-Za-z0-9_.-]+$"
)
_PREVIEW_SOURCE_ASSET_PATH_RE = re.compile(
    r"^/assets/source/[a-f0-9]{32}/(?!\.)(?!.*\.\.)[A-Za-z0-9_.-]+$"
)


def _is_preview_asset_request(url: str) -> bool:
    """Return whether *url* is one of this service's read-only asset endpoints."""
    try:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost"}
        and port == settings.port
        and bool(
            _PREVIEW_ASSET_PATH_RE.fullmatch(parsed.path)
            or _PREVIEW_SOURCE_ASSET_PATH_RE.fullmatch(parsed.path)
        )
    )


def _is_allowed_preview_request(url: str) -> bool:
    """无头自检只放行当前服务的只读素材端点，保持其余网络/SSRF 全部阻断。"""
    try:
        scheme = urlsplit(url).scheme
    except ValueError:
        return False
    return scheme in {"data", "about", "blob"} or _is_preview_asset_request(url)


def _with_preview_asset_base(html: str) -> str:
    """让 page.set_content 的根相对 `/assets/...` 与真实预览一样指向本服务。"""
    clean = re.sub(r"(?is)<base\b[^>]*>", "", html or "")
    base = f'<base href="{_preview_asset_origin()}/">'
    head = re.search(r"(?is)<head\b[^>]*>", clean)
    if head:
        return clean[:head.end()] + base + clean[head.end():]
    html_tag = re.search(r"(?is)<html\b[^>]*>", clean)
    if html_tag:
        return clean[:html_tag.end()] + f"<head>{base}</head>" + clean[html_tag.end():]
    return f"<head>{base}</head>" + clean


_RUNTIME_PROBE_GLOBAL = "__h5gdRuntimeProbeV1"
_HEADLESS_STARTUP_WAIT_MS = 1200
_HEADLESS_POST_INPUT_WAIT_MS = 2300
_HEADLESS_START_CLICK_RATIOS = (0.50, 0.58, 0.70, 0.85)


def _with_preview_runtime_probe(html: str) -> str:
    """Mirror the real preview's storage shim and trace duplicate rAF chains.

    ``page.set_content`` runs on an origin where native Web Storage access raises a
    SecurityError.  The real sandboxed preview installs an in-memory replacement before
    generated scripts run, so the verifier must do the same to avoid environment-only
    failures.  The rAF wrapper records only the precise bad pattern we care about: the
    same callback being scheduled again while a previous request for it is still pending.
    A normal self-rescheduling loop removes its pending request before invoking the
    callback and therefore does not trigger the probe.
    """
    probe = f"""<script data-verifier-probe="runtime">
(() => {{
  const probeName = {json.dumps(_RUNTIME_PROBE_GLOBAL)};
  if (window[probeName]) return;

  function installStorageShim(name) {{
    try {{
      const storage = window[name];
      storage.getItem('__probe__');
      return;
    }} catch (_) {{}}
    let mem = Object.create(null);
    const shim = {{
      getItem(key) {{
        key = String(key);
        return Object.prototype.hasOwnProperty.call(mem, key) ? mem[key] : null;
      }},
      setItem(key, value) {{ mem[String(key)] = String(value); }},
      removeItem(key) {{ delete mem[String(key)]; }},
      clear() {{ mem = Object.create(null); }},
      key(index) {{
        const keys = Object.keys(mem);
        return index >= 0 && index < keys.length ? keys[index] : null;
      }}
    }};
    Object.defineProperty(shim, 'length', {{
      get() {{ return Object.keys(mem).length; }}
    }});
    try {{
      Object.defineProperty(window, name, {{ value: shim, configurable: true }});
    }} catch (_) {{}}
  }}
  installStorageShim('localStorage');
  installStorageShim('sessionStorage');

  const state = {{ duplicate_count: 0, examples: [] }};
  window[probeName] = state;
  if (typeof window.requestAnimationFrame !== 'function') return;

  const nativeRequest = window.requestAnimationFrame.bind(window);
  const nativeCancel = typeof window.cancelAnimationFrame === 'function'
    ? window.cancelAnimationFrame.bind(window)
    : null;
  const pendingByCallback = new Map();
  const callbackByRequest = new Map();

  window.requestAnimationFrame = function(callback) {{
    if (typeof callback !== 'function') return nativeRequest(callback);
    let pending = pendingByCallback.get(callback);
    if (!pending) {{
      pending = new Set();
      pendingByCallback.set(callback, pending);
    }}
    const pendingBefore = pending.size;
    let requestId = 0;
    const wrapped = function(timestamp) {{
      const active = pendingByCallback.get(callback);
      if (active) {{
        active.delete(requestId);
        if (active.size === 0) pendingByCallback.delete(callback);
      }}
      callbackByRequest.delete(requestId);
      return callback.call(this, timestamp);
    }};
    requestId = nativeRequest(wrapped);
    pending.add(requestId);
    callbackByRequest.set(requestId, callback);
    if (pendingBefore > 0) {{
      state.duplicate_count += 1;
      if (state.examples.length < 5) {{
        state.examples.push({{
          callback: callback.name || '(anonymous)',
          pending_before: pendingBefore,
          at_ms: Math.round(performance.now())
        }});
      }}
    }}
    return requestId;
  }};

  if (nativeCancel) {{
    window.cancelAnimationFrame = function(requestId) {{
      const callback = callbackByRequest.get(requestId);
      if (callback) {{
        const pending = pendingByCallback.get(callback);
        if (pending) {{
          pending.delete(requestId);
          if (pending.size === 0) pendingByCallback.delete(callback);
        }}
        callbackByRequest.delete(requestId);
      }}
      return nativeCancel(requestId);
    }};
  }}
}})();
</script>"""
    clean = html or ""
    head = re.search(r"(?is)<head\b[^>]*>", clean)
    if head:
        return clean[:head.end()] + probe + clean[head.end():]
    html_tag = re.search(r"(?is)<html\b[^>]*>", clean)
    if html_tag:
        return clean[:html_tag.end()] + f"<head>{probe}</head>" + clean[html_tag.end():]
    return f"<head>{probe}</head>" + clean


def _analyze_animation_loop_probe(probe_state) -> list[dict]:
    """Turn duplicate pending rAF callback evidence into one blocking issue."""
    if not isinstance(probe_state, dict):
        return []
    duplicate_count = int(_as_finite_number(probe_state.get("duplicate_count")) or 0)
    if duplicate_count <= 0:
        return []
    examples = probe_state.get("examples")
    labels = []
    if isinstance(examples, list):
        for example in examples[:3]:
            if not isinstance(example, dict):
                continue
            label = str(example.get("callback") or "(anonymous)")
            pending = int(_as_finite_number(example.get("pending_before")) or 1)
            labels.append(f"`{label}`（已有 {pending} 个待执行请求）")
    detail = "；".join(labels)
    suffix = f"：{detail}" if detail else ""
    return [{
        "id": "duplicate_animation_loop",
        "severity": "high",
        "msg": f"检测到同一动画回调被重复启动（共 {duplicate_count} 次）{suffix}",
        "fix": (
            "确保 requestAnimationFrame 主循环只启动一次；加载完成与超时保护必须共用 once/settled "
            "守卫，并在正常完成时 clearTimeout，避免两条循环链同时更新游戏"
        ),
    }]


def _analyze_asset_load_failures(failures) -> list[dict]:
    """Report failed requests only for the local asset endpoints allowed by policy."""
    if not isinstance(failures, list):
        return []
    unique = []
    seen = set()
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        url = str(failure.get("url") or "")
        if not url or url in seen or not _is_preview_asset_request(url):
            continue
        seen.add(url)
        unique.append(failure)
    if not unique:
        return []

    examples = []
    for failure in unique[:3]:
        url = str(failure.get("url") or "")
        status = failure.get("status")
        reason = str(failure.get("reason") or "")
        detail = f"HTTP {status}" if status is not None else (reason or "requestfailed")
        examples.append(f"`{url}`（{detail}）")
    return [{
        "id": "asset_load_failure",
        "severity": "medium",
        "msg": f"本地素材加载失败（共 {len(unique)} 个）：" + "；".join(examples),
        "fix": "删除臆造路径或改用素材工具返回的真实 /assets/... URL，并保留合理的加载失败降级",
    }]


def _is_browser_resource_console_error(message: str) -> bool:
    """Recognize browser diagnostics already represented by request/response evidence."""
    low = str(message or "").lower()
    return any(marker in low for marker in (
        "failed to load resource",
        "has been blocked by cors policy",
        "access to font at",
        "net::err_",
    ))


_ATLAS_USAGES = frozenset({"atlas", "sprite_sheet", "spritesheet", "sprite-sheet"})
_DRAW_IMAGE_PROBE_GLOBAL = "__h5gdDrawImageProbeV1"


def _source_assets_with_contract_metadata(source_assets, source_specs) -> list[dict]:
    """Overlay profile-derived sprite metadata onto uploaded source asset records.

    Source uploads know image dimensions but deliberately do not guess whether an image is
    a sprite sheet.  A canonical profile *does* know that.  Joining by original path lets
    the existing drawImage probe observe Fishjoy without mutating importer data.
    """
    if isinstance(source_assets, dict):
        source_assets = source_assets.get("assets") or source_assets.get("source_assets") or []
    if not isinstance(source_assets, (list, tuple)):
        return []
    result = [dict(item) for item in source_assets if isinstance(item, dict)]
    catalog_entries = []
    for spec in source_specs or []:
        profile_id = str(spec.get("id") or "")
        for role, catalog in (
            ("fish", _source_animation_catalog(spec)),
            ("cannon", _source_cannon_catalog(spec)),
        ):
            for resource_id, item in catalog.items():
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "").strip()
                if path:
                    catalog_entries.append((path, item, role, str(resource_id), profile_id))
        bottom = (spec.get("layout") or {}).get("bottom") or {}
        if isinstance(bottom, dict) and bottom.get("path") and bottom.get("source_rect"):
            rect = bottom["source_rect"]
            if isinstance(rect, (list, tuple)) and len(rect) == 4:
                catalog_entries.append((
                    str(bottom["path"]),
                    {
                        "frame_width": rect[2],
                        "frame_height": rect[3],
                        "frame_count": 1,
                        "atlas_frames": bottom.get("atlas_frames") or {},
                    },
                    "bottom",
                    str(bottom.get("resource_id") or "bottom"),
                    profile_id,
                ))
    for asset in result:
        actual_path = str(asset.get("path") or "")
        for expected_path, meta, role, resource_id, profile_id in catalog_entries:
            if not _contract_path_matches(actual_path, expected_path):
                continue
            asset["layout_type"] = "sprite_sheet"
            asset["frame_width"] = meta.get("frame_width") or meta.get("frameWidth") or 0
            asset["frame_height"] = meta.get("frame_height") or meta.get("frameHeight") or 0
            asset["frame_count"] = meta.get("frame_count") or 0
            if meta.get("atlas_frames"):
                asset["atlas_frames"] = meta["atlas_frames"]
            asset["columns"] = 1
            asset["rows"] = meta.get("frame_count") or 0
            asset["contract_role"] = role
            asset["resource_id"] = resource_id
            asset["contract_profile_id"] = profile_id
            break
    return result


def _normalise_declared_atlas_frames(value) -> dict[str, dict[str, float]]:
    """Keep a bounded set of finite positive frame rectangles for probe transport."""
    if not isinstance(value, dict):
        return {}
    frames: dict[str, dict[str, float]] = {}
    for name, rect in list(value.items())[:512]:
        if not isinstance(rect, dict):
            continue
        numbers = {}
        valid = True
        for key in ("x", "y", "w", "h"):
            try:
                number = float(rect.get(key))
            except (TypeError, ValueError):
                valid = False
                break
            if number != number or number in (float("inf"), float("-inf")):
                valid = False
                break
            numbers[key] = number
        if not valid or numbers["x"] < 0 or numbers["y"] < 0:
            continue
        if numbers["w"] <= 0 or numbers["h"] <= 0:
            continue
        frames[str(name)[:120]] = numbers
    return frames


def _normalise_source_atlases(source_assets) -> list[dict]:
    """Keep explicit atlas metadata and forbidden placeholder images for the probe.

    The verifier deliberately does not guess from image dimensions or filenames: a large
    background may be scaled down legitimately.  Only assets explicitly identified by the
    source importer as an atlas/sprite sheet participate in this blocking check.
    """
    if isinstance(source_assets, dict):
        source_assets = source_assets.get("assets") or source_assets.get("source_assets") or []
    if not isinstance(source_assets, (list, tuple)):
        return []

    result: list[dict] = []
    for item in source_assets:
        if not isinstance(item, dict):
            continue
        usage = str(
            item.get("usage")
            or item.get("render_kind")
            or item.get("layout_type")
            or ""
        ).strip().lower()
        asset_role = str(item.get("asset_role") or "").strip().lower()
        if usage not in _ATLAS_USAGES and asset_role != "placeholder":
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        result.append({
            "url": url,
            "path": str(item.get("path") or url),
            "usage": usage,
            "width": item.get("width") or item.get("natural_width") or 0,
            "height": item.get("height") or item.get("natural_height") or 0,
            "frame_width": item.get("frame_width") or item.get("frameWidth") or 0,
            "frame_height": item.get("frame_height") or item.get("frameHeight") or 0,
            "columns": item.get("columns") or 0,
            "rows": item.get("rows") or 0,
            "frame_count": item.get("frame_count") or 0,
            "atlas_frames": _normalise_declared_atlas_frames(item.get("atlas_frames")),
            "asset_role": asset_role,
            "contract_role": str(item.get("contract_role") or ""),
            "resource_id": str(item.get("resource_id") or ""),
            "contract_profile_id": str(item.get("contract_profile_id") or ""),
        })
    return result


def _with_draw_image_probe(html: str, source_assets=None) -> str:
    """Insert a small, non-visual probe before generated scripts execute.

    It records Canvas drawImage overload/source-rectangle data for explicit atlases and
    importer-identified placeholder art. Analysis remains deterministic in Python.
    """
    atlases = _normalise_source_atlases(source_assets)
    if not atlases:
        return html or ""
    payload = json.dumps(atlases, ensure_ascii=False, separators=(",", ":"))
    # Source metadata is user-derived.  Prevent a path containing "</script>" from ending
    # the verifier-owned script element early.
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    probe = f"""<script data-verifier-probe="draw-image">
(() => {{
  const probeName = {json.dumps(_DRAW_IMAGE_PROBE_GLOBAL)};
  if (window[probeName]) return;
  const assets = {payload};
  const byKey = Object.create(null);
  const normalise = value => {{
    const raw = String(value || '');
    if (!raw) return [];
    const keys = [raw];
    try {{
      const parsed = new URL(raw, 'http://127.0.0.1/');
      keys.push(parsed.href, parsed.pathname);
    }} catch (_) {{}}
    return keys;
  }};
  for (const meta of assets) {{
    for (const key of normalise(meta.url)) byKey[key] = meta;
  }}
  const records = [];
  const recordByKey = Object.create(null);
  let drawIndex = 0;
  window[probeName] = {{ records }};
  const proto = window.CanvasRenderingContext2D && CanvasRenderingContext2D.prototype;
  if (!proto || typeof proto.drawImage !== 'function') return;
  const original = proto.drawImage;
  proto.drawImage = function(...args) {{
    try {{
      const image = args[0];
      const src = image && (image.currentSrc || image.src || '');
      let meta = null;
      for (const key of normalise(src)) {{
        if (byKey[key]) {{ meta = byKey[key]; break; }}
      }}
      if (meta) {{
        const currentDrawIndex = ++drawIndex;
        const argc = args.length;
        const naturalWidth = Number(
          (image && (image.naturalWidth || image.videoWidth || image.width)) || meta.width || 0
        );
        const naturalHeight = Number(
          (image && (image.naturalHeight || image.videoHeight || image.height)) || meta.height || 0
        );
        let sx = 0, sy = 0, sw = naturalWidth, sh = naturalHeight;
        if (argc === 9) {{
          sx = Number(args[1]); sy = Number(args[2]);
          sw = Number(args[3]); sh = Number(args[4]);
        }}
        let dx = 0, dy = 0, dw = naturalWidth, dh = naturalHeight;
        if (argc === 3) {{
          dx = Number(args[1]); dy = Number(args[2]);
        }} else if (argc === 5) {{
          dx = Number(args[1]); dy = Number(args[2]);
          dw = Number(args[3]); dh = Number(args[4]);
        }} else if (argc === 9) {{
          dx = Number(args[5]); dy = Number(args[6]);
          dw = Number(args[7]); dh = Number(args[8]);
        }}
        const matrix = this.getTransform();
        const corners = [
          [dx, dy], [dx + dw, dy], [dx, dy + dh], [dx + dw, dy + dh]
        ].map(([x, y]) => [
          matrix.a * x + matrix.c * y + matrix.e,
          matrix.b * x + matrix.d * y + matrix.f
        ]);
        const xs = corners.map(point => point[0]);
        const ys = corners.map(point => point[1]);
        const destLeft = Math.min(...xs), destRight = Math.max(...xs);
        const destTop = Math.min(...ys), destBottom = Math.max(...ys);
        const canvasWidth = Number(this.canvas && this.canvas.width || 0);
        const canvasHeight = Number(this.canvas && this.canvas.height || 0);
        const visible = Number.isFinite(destLeft) && Number.isFinite(destTop)
          && destRight > 0 && destBottom > 0
          && destLeft < canvasWidth && destTop < canvasHeight;
        const signature = [meta.url, argc, sx, sy, sw, sh].join('|');
        const prior = recordByKey[signature];
        if (prior) {{
          prior.count += 1;
          prior.last_draw_index = currentDrawIndex;
          if (visible) prior.visible_count += 1;
          else prior.offscreen_count += 1;
        }} else if (records.length < 512) {{
          const record = {{
            url: meta.url, path: meta.path, usage: meta.usage,
             width: Number(meta.width || 0), height: Number(meta.height || 0),
             frame_width: Number(meta.frame_width || 0),
             frame_height: Number(meta.frame_height || 0),
             columns: Number(meta.columns || 0), rows: Number(meta.rows || 0),
             frame_count: Number(meta.frame_count || 0),
             atlas_frames: meta.atlas_frames || {{}},
             contract_role: meta.contract_role || '',
             resource_id: meta.resource_id || '',
             contract_profile_id: meta.contract_profile_id || '',
             natural_width: naturalWidth, natural_height: naturalHeight,
            argc, sx, sy, sw, sh, dx, dy, dw, dh,
            dest_left: destLeft, dest_top: destTop,
            dest_right: destRight, dest_bottom: destBottom,
            canvas_width: canvasWidth, canvas_height: canvasHeight,
            visible_count: visible ? 1 : 0,
            offscreen_count: visible ? 0 : 1,
            first_draw_index: currentDrawIndex,
            last_draw_index: currentDrawIndex,
            count: 1
          }};
          recordByKey[signature] = record;
          records.push(record);
        }}
      }}
    }} catch (_) {{}}
    return original.apply(this, args);
  }};
}})();
</script>"""
    clean = html or ""
    head = re.search(r"(?is)<head\b[^>]*>", clean)
    if head:
        return clean[:head.end()] + probe + clean[head.end():]
    html_tag = re.search(r"(?is)<html\b[^>]*>", clean)
    if html_tag:
        return clean[:html_tag.end()] + f"<head>{probe}</head>" + clean[html_tag.end():]
    return f"<head>{probe}</head>" + clean


def _as_finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _source_rect_out_of_bounds_reason(record: dict) -> str | None:
    """Return why a nine-argument drawImage source crop is outside its image."""
    if not isinstance(record, dict):
        return None
    try:
        argc = int(record.get("argc"))
    except (TypeError, ValueError):
        return None
    if argc != 9:
        return None
    width = _as_finite_number(record.get("natural_width"))
    height = _as_finite_number(record.get("natural_height"))
    if not width or width <= 0:
        width = _as_finite_number(record.get("width"))
    if not height or height <= 0:
        height = _as_finite_number(record.get("height"))
    sx = _as_finite_number(record.get("sx"))
    sy = _as_finite_number(record.get("sy"))
    sw = _as_finite_number(record.get("sw"))
    sh = _as_finite_number(record.get("sh"))
    if None in (sx, sy, sw, sh):
        return "源矩形包含非有限数值"
    if sw <= 0 or sh <= 0:
        return "源矩形宽高不是正数"
    if sx < 0 or sy < 0:
        return "源矩形起点为负数"
    tolerance = 0.5
    if width and sx + sw > width + tolerance:
        return f"sx+sw={sx + sw:g} 超过图片宽度 {width:g}"
    if height and sy + sh > height + tolerance:
        return f"sy+sh={sy + sh:g} 超过图片高度 {height:g}"
    return None


def _analyze_source_rect_bounds(records) -> list[dict]:
    """Convert source crop overflow records into a dedicated actionable issue."""
    if not isinstance(records, list):
        return []
    invalid = []
    for record in records:
        reason = _source_rect_out_of_bounds_reason(record)
        if reason:
            invalid.append((record, reason))
    if not invalid:
        return []
    total = 0
    examples = []
    seen = set()
    for record, reason in invalid:
        total += max(1, int(_as_finite_number(record.get("count")) or 1))
        label = str(record.get("path") or record.get("url") or "未知图片")
        rect = tuple(record.get(key) for key in ("sx", "sy", "sw", "sh"))
        signature = (label, rect, reason)
        if signature in seen or len(examples) >= 3:
            continue
        seen.add(signature)
        examples.append(f"`{label}` 源矩形 {rect}（{reason}）")
    return [{
        "id": "source_rect_out_of_bounds",
        "severity": "high",
        "msg": f"检测到 drawImage 源矩形越过原图边界（共 {total} 次）：" + "；".join(examples),
        "fix": (
            "按图片真实宽高检查 sx/sy/sw/sh。纵向精灵表固定 sx=0，令 sy=frame*frameHeight；"
            "横向精灵表才可以让 sx 随 frame 增长"
        ),
    }]


def _analyze_fishjoy_cannon_draw_records(records) -> list[dict]:
    """Require the active Fishjoy cannon to be drawn visibly above the bottom bar."""
    if not isinstance(records, list):
        return []
    fishjoy = [
        record for record in records
        if isinstance(record, dict)
        and str(record.get("contract_profile_id") or "").lower().startswith("fishjoy@")
    ]
    if not fishjoy:
        return []
    profile_id = str(fishjoy[0].get("contract_profile_id") or "fishjoy@1")
    fish_records = [record for record in fishjoy if record.get("contract_role") == "fish"]
    cannon_records = [record for record in fishjoy if record.get("contract_role") == "cannon"]
    bottom_records = [record for record in fishjoy if record.get("contract_role") == "bottom"]
    issues: list[dict] = []

    # Fish drawing proves the gameplay scene has started.  Avoid blaming a missing cannon
    # when a non-standard title screen simply was not activated by the generic input probe.
    if fish_records and not cannon_records:
        issues.append({
            "id": f"source_contract_cannon_not_drawn:{profile_id}",
            "severity": "high",
            "msg": f"`{profile_id}` 已进入捕鱼场景并绘制鱼类，但没有绘制任何源码炮台素材。",
            "fix": "每帧在底栏之后用当前等级 cannon1..cannon7 的九参数 drawImage 绘制炮台。",
        })
        return issues
    if not cannon_records:
        return issues

    visible = sum(
        max(0, int(_as_finite_number(record.get("visible_count")) or 0))
        for record in cannon_records
    )
    if visible == 0:
        issues.append({
            "id": f"source_contract_cannon_offscreen:{profile_id}",
            "severity": "high",
            "msg": (
                f"`{profile_id}` 炮台素材有 drawImage 调用，但目标矩形全部在移动端画布外；"
                "玩家看不到炮台，炮弹出生点通常也会同步偏移。"
            ),
            "fix": (
                "用可视区 W/H 计算共享 cannonOrigin，或应用完整逻辑场景变换；"
                "验证旋转后的炮台目标包围盒与 canvas 相交。"
            ),
        })

    cannon_last = max(
        int(_as_finite_number(record.get("last_draw_index")) or 0)
        for record in cannon_records
    )
    bottom_last = max(
        (int(_as_finite_number(record.get("last_draw_index")) or 0) for record in bottom_records),
        default=0,
    )
    if cannon_last and bottom_last > cannon_last:
        issues.append({
            "id": f"source_contract_cannon_draw_order_invalid:{profile_id}",
            "severity": "high",
            "msg": (
                f"`{profile_id}` 底栏在炮台之后绘制，可能覆盖炮座与大部分炮身；"
                "源码层级要求炮台位于底栏上方。"
            ),
            "fix": "每帧先绘制 bottom 的 [0,0,765,72] 区域，再绘制炮台、按钮和数字 HUD。",
        })
    return issues


def _is_uncropped_atlas_draw(record: dict) -> bool:
    """Return True only for a known atlas drawn with its complete source rectangle."""
    if not isinstance(record, dict):
        return False
    usage = str(record.get("usage") or "").strip().lower()
    if usage not in _ATLAS_USAGES:
        return False
    try:
        argc = int(record.get("argc"))
    except (TypeError, ValueError):
        return False
    if argc in (3, 5):
        return True
    if argc != 9:
        return False

    width = _as_finite_number(record.get("natural_width"))
    height = _as_finite_number(record.get("natural_height"))
    if not width or width <= 0:
        width = _as_finite_number(record.get("width"))
    if not height or height <= 0:
        height = _as_finite_number(record.get("height"))
    sx = _as_finite_number(record.get("sx"))
    sy = _as_finite_number(record.get("sy"))
    sw = _as_finite_number(record.get("sw"))
    sh = _as_finite_number(record.get("sh"))
    if not width or not height or None in (sx, sy, sw, sh):
        return False
    # Canvas image dimensions and source rectangles are exact pixel values in normal use.
    # Keep the tolerance below one pixel so a 1px frame in a tiny atlas is still a crop.
    tolerance_w = 0.5
    tolerance_h = 0.5
    return (
        abs(sx) <= 0.5
        and abs(sy) <= 0.5
        and abs(sw - width) <= tolerance_w
        and abs(sh - height) <= tolerance_h
    )


def _atlas_frame_validation_reason(record: dict) -> str | None:
    """Return why a nine-argument atlas crop is not one of its declared frames."""
    if not isinstance(record, dict):
        return None
    usage = str(record.get("usage") or "").strip().lower()
    if usage not in _ATLAS_USAGES:
        return None
    try:
        argc = int(record.get("argc"))
    except (TypeError, ValueError):
        return None
    if argc != 9 or _is_uncropped_atlas_draw(record):
        return None

    sx = _as_finite_number(record.get("sx"))
    sy = _as_finite_number(record.get("sy"))
    sw = _as_finite_number(record.get("sw"))
    sh = _as_finite_number(record.get("sh"))
    if None in (sx, sy, sw, sh) or sw <= 0 or sh <= 0 or sx < 0 or sy < 0:
        return "源矩形包含无效或越界数值"

    tolerance = 0.5
    declared = _normalise_declared_atlas_frames(record.get("atlas_frames"))
    if declared:
        for rect in declared.values():
            exact_frame = all(
                abs(actual - rect[key]) <= tolerance
                for actual, key in ((sx, "x"), (sy, "y"), (sw, "w"), (sh, "h"))
            )
            # A colour/mana reveal may draw a strict sub-rectangle of one declared
            # card frame.  It is still tied to the correct atlas as long as the full
            # source crop stays inside that frame; rejecting it encourages callers to
            # mutate atlas metadata or abandon useful source art.
            contained_in_frame = (
                sx >= rect["x"] - tolerance
                and sy >= rect["y"] - tolerance
                and sx + sw <= rect["x"] + rect["w"] + tolerance
                and sy + sh <= rect["y"] + rect["h"] + tolerance
            )
            if exact_frame or contained_in_frame:
                return None
        return "源矩形不属于、也不完全包含在该图片 atlas_frames 声明的任何帧内"

    frame_width = _as_finite_number(record.get("frame_width"))
    frame_height = _as_finite_number(record.get("frame_height"))
    if not frame_width or not frame_height or frame_width <= 0 or frame_height <= 0:
        return None
    if abs(sw - frame_width) > tolerance or abs(sh - frame_height) > tolerance:
        return "源矩形尺寸与规则网格帧尺寸不一致"

    col = round(sx / frame_width)
    row = round(sy / frame_height)
    if abs(sx - col * frame_width) > tolerance or abs(sy - row * frame_height) > tolerance:
        return "源矩形起点没有对齐规则网格"

    width = _as_finite_number(record.get("natural_width"))
    height = _as_finite_number(record.get("natural_height"))
    if not width or width <= 0:
        width = _as_finite_number(record.get("width"))
    if not height or height <= 0:
        height = _as_finite_number(record.get("height"))
    columns = _as_finite_number(record.get("columns"))
    rows = _as_finite_number(record.get("rows"))
    if columns and col >= int(columns):
        return "源矩形列索引超出规则网格"
    if rows and row >= int(rows):
        return "源矩形行索引超出规则网格"
    if width and sx + sw > width + tolerance:
        return "源矩形超出图片宽度"
    if height and sy + sh > height + tolerance:
        return "源矩形超出图片高度"
    return None


def _analyze_atlas_frame_records(records) -> list[dict]:
    """Report crops whose coordinates do not belong to that same image's metadata."""
    if not isinstance(records, list):
        return []
    invalid = []
    for record in records:
        reason = _atlas_frame_validation_reason(record)
        if reason:
            invalid.append((record, reason))
    if not invalid:
        return []

    total = 0
    examples = []
    seen = set()
    for record, reason in invalid:
        total += max(1, int(_as_finite_number(record.get("count")) or 1))
        label = str(record.get("path") or record.get("url") or "未知图集")
        rect = tuple(record.get(key) for key in ("sx", "sy", "sw", "sh"))
        signature = (label, rect, reason)
        if signature in seen or len(examples) >= 3:
            continue
        seen.add(signature)
        examples.append(f"`{label}` 源矩形 {rect}（{reason}）")
    return [{
        "id": "invalid_atlas_frame",
        "severity": "high",
        "msg": (
            f"检测到图集裁剪坐标与该图片声明的合法帧不匹配（共 {total} 次）："
            + "；".join(examples)
        ),
        "fix": (
            "从当前图片自己的 atlas_frames 或 frame_width/frame_height 网格选择 sx/sy/sw/sh；"
            "不要把 card.png 的小图坐标用于 card-big.png，图片 URL 与帧坐标必须来自同一元数据"
        ),
    }]


def _analyze_atlas_draw_records(records) -> list[dict]:
    """Convert drawImage trace records into one actionable, de-duplicated issue."""
    if not isinstance(records, list):
        return []
    bad = [record for record in records if _is_uncropped_atlas_draw(record)]
    if not bad:
        return []

    examples = []
    total = 0
    seen = set()
    for record in bad:
        count = max(1, int(_as_finite_number(record.get("count")) or 1))
        total += count
        label = str(record.get("path") or record.get("url") or "未知图集")
        if label in seen or len(examples) >= 3:
            continue
        seen.add(label)
        width = int(_as_finite_number(record.get("natural_width")) or _as_finite_number(record.get("width")) or 0)
        height = int(_as_finite_number(record.get("natural_height")) or _as_finite_number(record.get("height")) or 0)
        fw = int(_as_finite_number(record.get("frame_width")) or 0)
        fh = int(_as_finite_number(record.get("frame_height")) or 0)
        size = f" {width}x{height}" if width and height else ""
        frame = f"，声明帧 {fw}x{fh}" if fw and fh else ""
        examples.append(f"`{label}`{size}{frame}")
    detail = "；".join(examples)
    return [{
        "id": "uncropped_sprite_atlas",
        "severity": "high",
        "msg": f"检测到已知精灵图集被整张绘制（共 {total} 次）：{detail}",
        "fix": (
            "不要用 3/5 参数 drawImage 缩放整张图集；按源码声明的帧尺寸计算 sx/sy/sw/sh，"
            "使用 drawImage(img,sx,sy,sw,sh,dx,dy,dw,dh) 九参数形式，每个单位只绘制一个帧"
        ),
    }]


def _analyze_placeholder_draw_records(records) -> list[dict]:
    """Reject importer-identified mock/placeholder art that reached the canvas."""
    if not isinstance(records, list):
        return []
    drawn = [
        record for record in records
        if str(record.get("asset_role") or "").lower() == "placeholder"
    ]
    if not drawn:
        return []
    labels = []
    seen = set()
    total = 0
    for record in drawn:
        total += max(1, int(_as_finite_number(record.get("count")) or 1))
        label = str(record.get("path") or record.get("url") or "placeholder")
        if label not in seen and len(labels) < 3:
            labels.append(f"`{label}`")
            seen.add(label)
    return [{
        "id": "placeholder_asset_drawn",
        "severity": "high",
        "msg": f"检测到占位示意图被绘制进成品（共 {total} 次）：" + "、".join(labels),
        "fix": "改用源码真正的游戏场景素材；asset_role=placeholder 的网页外壳/尺寸示意图不得预加载或绘制",
    }]

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


# ---- Windows 专用：Playwright 专属事件循环线程 ----
# uvicorn 在 Windows 上跑的是 Selector 事件循环，它不支持 create_subprocess_exec
# （NotImplementedError），Playwright 连启动 node driver 都做不到 → 无头检查静默降级。
# 解决：起一个常驻后台线程、自带 Proactor 循环，所有 playwright 工作（含浏览器复用的
# 锁/定时器/任务）都固定在这个循环上；主循环通过 run_coroutine_threadsafe 调度并 await 结果。
_pw_loop: asyncio.AbstractEventLoop | None = None
_pw_loop_guard = threading.Lock()


def _get_pw_loop() -> asyncio.AbstractEventLoop:
    global _pw_loop
    with _pw_loop_guard:
        if _pw_loop is not None and not _pw_loop.is_closed():
            return _pw_loop
        loop = asyncio.ProactorEventLoop()  # 显式 Proactor：别跟随 uvicorn 可能改过的全局策略

        def _spin():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        threading.Thread(target=_spin, name="pw-proactor-loop", daemon=True).start()
        _pw_loop = loop
        return loop


async def _on_pw_loop(coro):
    """在 playwright 专属循环上执行协程并等待结果（仅 Windows 路径使用）。"""
    fut = asyncio.run_coroutine_threadsafe(coro, _get_pw_loop())
    return await asyncio.wrap_future(fut)


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


def _cancel_task_safely(task) -> None:
    """取消任务；若任务属于别的事件循环（Windows 上主循环/专属循环并存），
    必须经它自己的循环 call_soon_threadsafe 取消——跨线程直接 cancel 不是线程安全的。"""
    if task is None or task.done() or task is asyncio.current_task():
        return
    loop = task.get_loop()
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if loop is running:
        task.cancel()
    else:
        loop.call_soon_threadsafe(task.cancel)


def _cancel_idle_close() -> None:
    global _idle_close_task
    task, _idle_close_task = _idle_close_task, None
    _cancel_task_safely(task)


async def _acquire_browser():
    """取常驻实例并登记使用。返回 browser；启动失败返回 None（调用方降级）。"""
    async with _get_browser_lock():
        try:
            browser = await _get_browser()
        except Exception as e:
            # str(NotImplementedError()) 是空串，必须带上类型名才查得了问题
            logger.warning("headless_browser_launch_failed", error=str(e) or repr(e))
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

    Windows 上 playwright 全部状态都在专属循环线程上，必须调度过去执行
    （跨线程 cancel 任务/关浏览器都不是线程安全的）。"""
    if sys.platform == "win32":
        if _pw_loop is None or _pw_loop.is_closed():
            return  # 从未启动过 playwright，无可关
        try:
            await _on_pw_loop(_aclose_browser_impl())
        except Exception:
            pass
        return
    await _aclose_browser_impl()


async def _aclose_browser_impl():
    """实际关停逻辑（必须在 playwright 所在循环上执行）。

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
    _cancel_task_safely(_idle_close_task)

    async def _idle():
        try:
            await asyncio.sleep(_BROWSER_IDLE_CLOSE_S)
        except asyncio.CancelledError:
            return
        await _aclose_browser_impl()  # 本任务就跑在 playwright 所在循环上，直接关

    _idle_close_task = asyncio.create_task(_idle())


async def run_headless(
    html: str,
    timeout_s: float = 15.0,
    source_assets: list[dict] | None = None,
) -> list[dict] | None:
    """用 playwright chromium 跑一遍游戏，捕获运行时报错 + 空屏。
    未安装 playwright/chromium 或出错/超时 → 返回 None（视为不可用，降级到纯静态）。
    整个过程有 timeout_s 墙钟上限，卡住的页面不会拖死自检。"""
    try:
        from playwright.async_api import async_playwright  # noqa: F401 仅探测依赖可用性
    except Exception:
        return None
    if sys.platform == "win32":
        # uvicorn 在 Windows 的 Selector 循环不支持子进程 → 整套搬到专属 Proactor 循环执行
        try:
            return await _on_pw_loop(_run_headless_impl(html, timeout_s, source_assets))
        except Exception as e:
            logger.warning("headless_verify_failed", error=str(e) or repr(e))
            return None
    return await _run_headless_impl(html, timeout_s, source_assets)


async def _run_headless_impl(
    html: str,
    timeout_s: float,
    source_assets: list[dict] | None = None,
) -> list[dict] | None:
    browser = await _acquire_browser()
    if browser is None:
        return None
    atlas_assets = _normalise_source_atlases(source_assets)

    async def _run() -> tuple[list[str], bool, bytes, list[dict], dict, list[dict]]:
        # --no-sandbox：root 容器/AutoDL 等环境下 Chromium 启动的必要条件。
        # 去掉沙箱降低进程隔离，威胁模型里的缓解措施：
        #   1. 仅跑不可信 HTML 且拦截外部网络请求（只额外放行本服务只读 /assets/）
        #   2. 整段有 wall-clock timeout（asyncio.wait_for），卡住不会拖死自检
        #   3. 每次检查用独立 page、用完即关（finally 保证）；浏览器实例复用但异常即报废换代、空闲超时自动关停
        page = await browser.new_page(viewport={"width": 390, "height": 740}, device_scale_factor=2)
        try:
            # 安全：被检代码是模型生成的不可信 HTML。只放行内联资源与当前服务严格限定的
            # /assets/ 路径，防止 SSRF 打云元数据(169.254.169.254)/其它内网服务或读本地文件。
            async def _block_external(route):
                if _is_allowed_preview_request(route.request.url):
                    await route.continue_()
                else:
                    await route.abort()
            await page.route("**/*", _block_external)

            errs: list[str] = []
            asset_failures: list[dict] = []

            def _record_response(response):
                try:
                    url, status = response.url, int(response.status)
                except (AttributeError, TypeError, ValueError):
                    return
                if _is_preview_asset_request(url) and not 200 <= status < 300:
                    if len(asset_failures) < 64:
                        asset_failures.append({"url": url, "status": status})

            def _record_request_failure(request):
                try:
                    url = request.url
                    reason = request.failure or "requestfailed"
                except AttributeError:
                    return
                # Deliberately ignore external URLs rejected by the SSRF policy.  Only a
                # failure of an otherwise allowed local asset is actionable game evidence.
                if _is_preview_asset_request(url) and len(asset_failures) < 64:
                    asset_failures.append({"url": url, "reason": str(reason)})

            def _record_console_error(message):
                if message.type != "error":
                    return
                text = message.text
                # Browser-generated resource diagnostics are represented structurally by
                # response/requestfailed above and are not uncaught JavaScript exceptions.
                if not _is_browser_resource_console_error(text):
                    errs.append(text)

            page.on("pageerror", lambda e: errs.append(str(e)))
            page.on("console", _record_console_error)
            page.on("response", _record_response)
            page.on("requestfailed", _record_request_failure)
            prepared_html = _with_draw_image_probe(html, atlas_assets)
            prepared_html = _with_preview_runtime_probe(prepared_html)
            prepared_html = _with_preview_asset_base(prepared_html)
            await page.set_content(prepared_html, wait_until="load")
            await page.wait_for_timeout(_HEADLESS_STARTUP_WAIT_MS)
            try:
                # Start controls vary widely across generated mobile games. A single fixed click
                # used to miss centre buttons around 58% height, so only the title screen was
                # checked. Exercise the common start keys and several centre-button positions.
                # Extra clicks after a successful start also smoke-test card/deployment handlers.
                await page.keyboard.press("Enter")
                await page.keyboard.press("Space")
                for ratio in _HEADLESS_START_CLICK_RATIOS:
                    await page.mouse.click(195, round(740 * ratio))
                    await page.wait_for_timeout(100)
            except Exception:
                pass
            # 总观察时间超过 3 秒，覆盖常见的“加载完成先启动、3 秒超时又启动”缺陷。
            await page.wait_for_timeout(_HEADLESS_POST_INPUT_WAIT_MS)
            shot = await page.screenshot()
            draw_records: list[dict] = []
            if atlas_assets:
                try:
                    draw_records = await page.evaluate(
                        f"() => (window[{json.dumps(_DRAW_IMAGE_PROBE_GLOBAL)}] || {{}}).records || []"
                    )
                    if not isinstance(draw_records, list):
                        draw_records = []
                except Exception as e:
                    # Runtime and screenshot checks remain useful if optional probe collection fails.
                    logger.warning("draw_image_probe_failed", error=str(e) or repr(e))
            runtime_probe: dict = {}
            try:
                runtime_probe = await page.evaluate(
                    f"() => window[{json.dumps(_RUNTIME_PROBE_GLOBAL)}] || {{}}"
                )
                if not isinstance(runtime_probe, dict):
                    runtime_probe = {}
            except Exception as e:
                logger.warning("runtime_probe_failed", error=str(e) or repr(e))
            return (
                errs,
                _is_blank_png(shot),
                shot,
                draw_records,
                runtime_probe,
                asset_failures,
            )
        finally:
            await page.close()  # 即便超时取消，也保证关掉页面；浏览器实例留给下次复用

    issues = []
    try:
        try:
            (
                errors,
                blank,
                shot,
                draw_records,
                runtime_probe,
                asset_failures,
            ) = await asyncio.wait_for(_run(), timeout=timeout_s)

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
            # An uncaught initialization error commonly causes the blank frame.  Report the
            # actionable exception once instead of sending a second speculative root cause to
            # the repair model.
            if blank and not errors:
                issues.append({"id": "blank_screen", "severity": "high",
                               "msg": "运行后画面几乎空白（元素可能在屏幕外/未绘制/初始化失败）",
                               "fix": "检查 DPR 与坐标：canvas.style 尺寸、用逻辑 W/H 定位、实体 y 在可视区内"})
            elif _is_visually_torn(shot):
                issues.append({"id": "visual_tearing", "severity": "high",
                               "msg": "画面出现严重的面片撕裂/碎片化（3D 渲染的顶点计算或投影/背面剔除逻辑有误）",
                               "fix": "检查 V3 类方法（sub/mul/cross/norm）是否返回正确类型、project() 返回值是否被当成 V3 调用方法、背面剔除是否在视角空间判 vn.z、旋转后 orig 是否 Math.round 量化"})
            issues.extend(_analyze_source_rect_bounds(draw_records))
            issues.extend(_analyze_fishjoy_cannon_draw_records(draw_records))
            issues.extend(_analyze_atlas_draw_records(draw_records))
            issues.extend(_analyze_atlas_frame_records(draw_records))
            issues.extend(_analyze_placeholder_draw_records(draw_records))
            issues.extend(_analyze_animation_loop_probe(runtime_probe))
            issues.extend(_analyze_asset_load_failures(asset_failures))
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


async def verify_game(
    html: str,
    use_headless: bool = False,
    source_assets: list[dict] | None = None,
    source_specs: list[dict] | None = None,
) -> dict:
    """返回 {ok, issues}。ok = 没有 high/medium 级问题（low 仅提示，不触发自修）。"""
    issues = analyze_static(html)
    issues.extend(_check_source_reference_contract(html, source_specs, source_assets))
    if use_headless:
        headless_assets = _source_assets_with_contract_metadata(source_assets, source_specs)
        h = await run_headless(html, source_assets=headless_assets)
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
