"""视觉评审（管线的"眼睛"）：视觉模型审自检截图，按固定维度挑毛病。

补齐闭环里唯一缺的一环——此前所有质检都是代码级/文字级（探针查"有没有粒子代码"，
查不出"怪物像贴纸"），风格割裂、HUD 裁切、空场景只有"看一眼"才能发现。
接口走 OpenAI 兼容 chat completions + image_url（硅基流动 Qwen2.5-VL 同 key 可用）。
fail-open：未配置/超时/解析失败一律返回空清单，绝不阻断生成。
"""

import base64
import io
import json
import re

import httpx

from src.config import settings

_TIMEOUT_S = 60
_MAX_DEFECTS = 4
_MAX_IMAGE_DIM = 800  # 降采样上限：省 token，缺陷判定不需要原图分辨率

_REVIEW_PROMPT = """你是严格的游戏画面评审。审阅这张 H5 游戏运行截图，按以下维度找**确定可见**的缺陷：
1. 风格统一：游玩区和背景像不像同一个游戏？实体是否有"贴纸感"（方形边界/白边/悬浮不落地）
2. 主角存在感：主角是否可见、比例是否合理、是否被 UI 遮挡
3. HUD 完整性：文字是否裁切/重叠/超出屏幕；是否有与美术风格违和的 emoji 系统图标当游戏元素
4. 空间利用：画面是否大而空，玩法内容占比是否过低
5. 整体：这像一个正经游戏，还是像原型/demo？

游戏背景信息：{context}

宁缺毋滥：只报确定看得见的问题，拿不准不报；无缺陷就给空数组。最多 {max_defects} 条。
只输出严格 JSON：{{"defects": [{{"dim": "维度词", "issue": "缺陷描述≤30字", "fix": "修法建议≤40字"}}]}}"""


def is_configured() -> bool:
    return bool(
        settings.vision_model
        and (settings.vision_api_base_url or settings.image_api_base_url or settings.openai_base_url)
        and (settings.vision_api_key or settings.image_api_key or settings.openai_api_key)
    )


def _endpoint() -> str:
    base = (settings.vision_api_base_url or settings.image_api_base_url
            or settings.openai_base_url or "").rstrip("/")
    if not re.search(r"/v\d+$", base):
        base += "/v1"
    return base + "/chat/completions"


def _api_key() -> str:
    return settings.vision_api_key or settings.image_api_key or settings.openai_api_key or ""


def _downscale(png_bytes: bytes) -> bytes:
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes))
        if max(img.size) <= _MAX_IMAGE_DIM:
            return png_bytes
        img.thumbnail((_MAX_IMAGE_DIM, _MAX_IMAGE_DIM))
        out = io.BytesIO()
        img.convert("RGB").save(out, format="JPEG", quality=85)
        return out.getvalue()
    except Exception:
        return png_bytes


def review_screenshot(png_bytes: bytes, context: str = "") -> list[dict]:
    """返回缺陷清单 [{dim, issue, fix}]；未配置/任何失败 → []。"""
    if not png_bytes or not is_configured():
        return []
    try:
        image = _downscale(png_bytes)
        mime = "image/jpeg" if image[:3] == b"\xff\xd8\xff" else "image/png"
        data_url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        payload = {
            "model": settings.vision_model,
            "temperature": 0,
            "max_tokens": 500,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": _REVIEW_PROMPT.format(
                        context=(context or "未提供")[:300], max_defects=_MAX_DEFECTS)},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
        }
        resp = httpx.post(
            _endpoint(), json=payload,
            headers={"Authorization": f"Bearer {_api_key()}"},
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        text = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
        m = re.search(r"\{.*\}", text or "", re.S)
        if not m:
            return []
        data = json.loads(m.group(0))
        items = data.get("defects") if isinstance(data, dict) else None
        out = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            issue = str(item.get("issue") or "").strip()
            if not issue:
                continue
            out.append({
                "dim": str(item.get("dim") or "画面")[:16],
                "issue": issue[:60],
                "fix": str(item.get("fix") or "").strip()[:80],
            })
            if len(out) >= _MAX_DEFECTS:
                break
        return out
    except Exception:
        return []  # 眼睛失明不能阻断生产线
