"""云生图素材管线：OpenAI images 兼容端点 → PNG 字节 →（可选）扣平色底 → 知识库素材。

设计要点：
- 未配置 IMAGE_MODEL 即整体关闭，调用方拿到明确提示后退回程序化绘制（fail-open）
- 端点形状按 OpenAI /v1/images/generations（b64_json 优先，url 兜底再取一次）
- 精灵类素材要透明底，但多数生图 API 只出实底图——用"纯色背景提示词 + 角部取样
  全局色键抠底"的组合近似（对卡通/扁平风足够；写实风建议生成背景类素材）
"""

import base64
import io
import re

import httpx

from src.config import settings

_TIMEOUT_S = 120


def is_configured() -> bool:
    return bool(
        settings.image_model
        and (settings.image_api_base_url or settings.openai_base_url)
        and (settings.image_api_key or settings.openai_api_key)
    )


def _endpoint() -> str:
    base = (settings.image_api_base_url or settings.openai_base_url or "").rstrip("/")
    if not re.search(r"/v\d+$", base):
        base += "/v1"
    return base + "/images/generations"


def _api_key() -> str:
    return settings.image_api_key or settings.openai_api_key or ""


def generate_image(prompt: str, size: str = "") -> bytes:
    """调云端生图，返回 PNG/JPEG 字节。配置缺失或任何失败抛 RuntimeError（带可读原因）。"""
    if not is_configured():
        raise RuntimeError("未配置 IMAGE_MODEL（.env 里设 IMAGE_MODEL/IMAGE_API_BASE_URL/IMAGE_API_KEY）")
    payload = {
        "model": settings.image_model,
        "prompt": prompt,
        "n": 1,
        "size": size or settings.image_size,
        "response_format": "b64_json",
    }
    headers = {"Authorization": f"Bearer {_api_key()}"}
    try:
        resp = httpx.post(_endpoint(), json=payload, headers=headers, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"生图接口 HTTP {e.response.status_code}: {e.response.text[:200]}") from e
    except httpx.HTTPError as e:
        raise RuntimeError(f"生图接口网络错误: {e}") from e
    except ValueError as e:
        raise RuntimeError("生图接口返回非 JSON") from e

    items = data.get("data") or []
    if not items:
        raise RuntimeError(f"生图接口返回空 data: {str(data)[:200]}")
    item = items[0] or {}
    b64 = item.get("b64_json")
    if b64:
        try:
            return base64.b64decode(b64)
        except Exception as e:
            raise RuntimeError("b64_json 解码失败") from e
    url = item.get("url")
    if url:
        try:
            img = httpx.get(url, timeout=_TIMEOUT_S)
            img.raise_for_status()
            return img.content
        except httpx.HTTPError as e:
            raise RuntimeError(f"下载生图结果失败: {e}") from e
    raise RuntimeError("生图接口既无 b64_json 也无 url")


def strip_flat_background(image_bytes: bytes, tolerance: int = 34) -> bytes:
    """把四角取样的近似纯色背景抠成透明（PNG RGBA）。

    仅当四角颜色彼此接近（确为平色底）才动手；否则原样返回，绝不毁图。
    """
    try:
        from PIL import Image
    except ImportError:
        return image_bytes
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        w, h = img.size
        corners = [img.getpixel(p)[:3] for p in ((2, 2), (w - 3, 2), (2, h - 3), (w - 3, h - 3))]
        base = tuple(sum(c[i] for c in corners) // 4 for i in range(3))
        if any(sum(abs(c[i] - base[i]) for i in range(3)) > tolerance for c in corners):
            return image_bytes  # 四角不一致：不是平色底，别抠
        px = img.load()
        for y in range(h):
            for x in range(w):
                r, g, b, a = px[x, y]
                if abs(r - base[0]) + abs(g - base[1]) + abs(b - base[2]) <= tolerance:
                    px[x, y] = (r, g, b, 0)
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return image_bytes  # 抠底失败原图照用


def build_prompt(description: str, asset_kind: str, art_style: str) -> str:
    """按素材类型和美术方向组装生图提示词。"""
    style = art_style.strip() or "flat cartoon game art, clean shapes, vivid colors"
    if asset_kind == "background":
        return (
            f"{description}. Game background art, {style}, "
            "full scene, no characters, no text, no watermark, no UI"
        )
    if asset_kind == "icon":
        return (
            f"{description}. Game icon, {style}, centered single object, "
            "solid pure white background, no text, no watermark"
        )
    return (
        f"{description}. Game sprite, {style}, single character/object centered, "
        "full body, solid pure white background, no shadow on ground, no text, no watermark"
    )
