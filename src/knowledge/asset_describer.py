"""图片素材自动描述 - 调用 OpenAI 兼容视觉模型生成中文描述

设计约束：
- 复用 src/config.py settings 的 provider 解析逻辑（与 GameDesignAgent 同源），
  deepseek / vllm / openai 三分支各取各的 key/base_url/model。
- 模型不支持视觉（deepseek-v4-flash 是否支持未知）、超时、网络错误等任何异常
  都必须优雅降级：返回 None + logger.warning，绝不向调用方抛出。
"""

import base64
import colorsys
import re
from pathlib import Path

import httpx

from src.config import settings
from src.utils.logger import logger

# 视觉调用超时（秒）：上传后台补描述与手动 describe 端点共用
DESCRIBE_TIMEOUT_SECONDS = 15
# 图片体积上限：base64 后约 1.37 倍，太大的图直接放弃（多数网关也会拒绝）
MAX_IMAGE_BYTES = 10 * 1024 * 1024

DESCRIBE_PROMPT = (
    "用中文一句话描述这张游戏素材的内容、颜色和适用场景，"
    "并给出3-6个中文标签词（标签用顿号分隔）。"
)

_MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".ico": "image/x-icon", ".avif": "image/avif",
    ".tif": "image/tiff", ".tiff": "image/tiff",
}


def _resolve_vision_config() -> tuple[str, str, str]:
    """按 llm_provider 解析 (api_key, base_url, model)。

    与 GameDesignAgent.__init__ 的 provider 嗅探保持一致（含 base_url/model 提示词
    自动纠偏），避免"对话走 deepseek、描述走 openai 空 key"这类配置分叉。
    """
    provider = (settings.llm_provider or "openai").lower().strip()
    base_url_hint = (settings.openai_base_url or "").lower()
    model_hint = (settings.openai_model or "").lower()
    compatible = {"qwen", "vllm", "gemma", "compatible"}
    if provider == "openai" and ("api.deepseek.com" in base_url_hint or model_hint.startswith("deepseek")):
        provider = "deepseek"
    elif provider == "openai" and (
        "qwen" in model_hint or "gemma" in model_hint
        or "autodl" in base_url_hint or ":6006" in base_url_hint
    ):
        provider = "vllm"
    elif provider in compatible:
        provider = "vllm"

    if provider == "deepseek":
        return (
            settings.deepseek_api_key or settings.openai_api_key,
            settings.deepseek_base_url or settings.openai_base_url,
            settings.deepseek_model or settings.openai_model,
        )
    if provider == "vllm":
        return (
            settings.qwen_api_key or settings.openai_api_key or "EMPTY",
            settings.qwen_base_url or settings.openai_base_url,
            settings.qwen_model or settings.openai_model,
        )
    return settings.openai_api_key, settings.openai_base_url, settings.openai_model


_COLOR_NAMES = [
    (0, "红色"), (20, "橙色"), (45, "黄色"), (70, "黄绿色"), (100, "绿色"),
    (160, "青色"), (200, "蓝色"), (250, "紫色"), (290, "品红"), (330, "红色"),
]

_FILENAME_HINTS = {
    "ball": "球 小球 弹球", "paddle": "球拍 挡板 板子", "brick": "砖块 方块",
    "player": "玩家 角色 主角", "enemy": "敌人 怪物", "bullet": "子弹 弹药",
    "coin": "金币 硬币 钱币", "star": "星星 星形", "heart": "心 生命 红心",
    "bg": "背景 背景图", "background": "背景 背景图", "btn": "按钮", "button": "按钮",
    "icon": "图标", "tile": "瓦片 地块", "bomb": "炸弹", "gem": "宝石 钻石",
    "fish": "鱼", "bird": "鸟", "car": "汽车 车辆", "ship": "飞船 船",
    "boss": "首领 boss", "food": "食物", "apple": "苹果", "cloud": "云 云朵",
    "tree": "树 树木", "rock": "石头 岩石", "explosion": "爆炸 爆炸特效",
}


def _hue_to_name(h_deg: float) -> str:
    name = "红色"
    for start, label in _COLOR_NAMES:
        if h_deg >= start:
            name = label
    return name


def _analyze_image_features(path: Path) -> str | None:
    """无视觉模型时的降级：用 PIL 提取可言说的视觉特征（尺寸/主色/透明/形状倾向）。

    纯文本模型也能据此产出可搜索的中文描述——比只有文件名强得多。
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as im:
            im = im.convert("RGBA")
            w, h = im.size
            small = im.resize((min(w, 48), min(h, 48)))
            pixels = [p for p in small.getdata() if p[3] > 32]
            if not pixels:
                return None
            n = len(pixels)
            avg = tuple(sum(p[i] for p in pixels) / n for i in range(3))
            hh, ss, vv = colorsys.rgb_to_hsv(*(c / 255 for c in avg))
            total_px = small.size[0] * small.size[1]
            opaque_ratio = n / total_px if total_px else 1.0

            parts = [f"{w}×{h} 像素"]
            if ss < 0.12:
                parts.append("灰白色" if vv > 0.6 else "深灰黑色")
            else:
                shade = "亮" if vv > 0.65 else ("暗" if vv < 0.35 else "")
                parts.append(shade + _hue_to_name(hh * 360))
            if opaque_ratio < 0.75:
                parts.append("透明背景")
                parts.append("圆形或不规则轮廓" if opaque_ratio < 0.55 else "带透明边缘")
            else:
                parts.append("矩形填充")
            if w >= 640 and h >= 480:
                parts.append("尺寸较大，可能是背景图")
            elif w > h * 2.2:
                parts.append("横向长条，可能是横条/挡板/进度条")
            elif h > w * 2.2:
                parts.append("纵向长条")
            elif abs(w - h) <= max(w, h) * 0.12 and w <= 256:
                parts.append("方形小图，可能是角色/道具/图标")
            return "，".join(parts)
    except Exception as e:  # noqa: BLE001
        logger.warning("analyze_image_features_failed", error=str(e)[:120])
        return None


def _keywords_from_filename(name: str) -> str:
    """文件名 → 中英文关键词（ball.png → 球 小球 弹球）。纯本地，零成本。"""
    stem = re.sub(r"\.[a-z0-9]+$", "", name, flags=re.IGNORECASE)
    words = [w.lower() for w in re.split(r"[^A-Za-z一-鿿]+", stem) if w]
    hits = []
    for w in words:
        for key, zh in _FILENAME_HINTS.items():
            if key in w and zh not in hits:
                hits.append(zh)
    cjk = [w for w in words if re.search(r"[一-鿿]", w)]
    return " ".join(hits + cjk)


def _describe_via_text_model(file_name: str, features: str | None) -> str | None:
    """把文件名+视觉特征交给文本模型润色成中文描述（视觉模型不可用时的降级）。"""
    api_key, base_url, model = _resolve_vision_config()
    if not base_url or not model:
        return None
    hint = _keywords_from_filename(file_name)
    prompt = (
        "你在为 H5 游戏素材库写检索用的中文描述。已知信息：\n"
        f"- 文件名：{file_name}\n"
        + (f"- 图像特征：{features}\n" if features else "")
        + (f"- 文件名可能含义：{hint}\n" if hint else "")
        + "请用中文一句话推测并描述这个素材可能是什么、什么颜色、用在什么场景，"
        "然后给出3-6个中文标签词（顿号分隔）。不要说'可能'之外的不确定措辞，不要解释推理过程。"
    )
    try:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        resp = httpx.post(
            base_url.rstrip("/") + "/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200,
                "temperature": 0.3,
            },
            headers=headers,
            timeout=DESCRIBE_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            logger.warning("describe_text_fallback_http_error", status=resp.status_code)
            return None
        choices = resp.json().get("choices") or []
        content = (choices[0].get("message") or {}).get("content") if choices else None
        return content.strip() if isinstance(content, str) and content.strip() else None
    except Exception as e:  # noqa: BLE001
        logger.warning("describe_text_fallback_failed", error=str(e)[:150])
        return None


def describe_image(file_path: str) -> str | None:
    """给图片素材生成中文描述（描述 + 标签拼成一段文本）。

    三级降级链（实测 deepseek-v4-flash 不支持 image_url，会直接 400 → 走第2级）：
    1. 视觉模型看图（最准，需模型支持 image_url）
    2. PIL 提取视觉特征（尺寸/主色/透明/形状）+ 文本模型润色成中文描述
    3. 纯本地：文件名关键词映射 + 视觉特征拼串（无网络也能让中文搜索命中）

    任何失败都返回 None 并记 warning，绝不抛出——调用方据 None 降级。
    """
    vision = _describe_via_vision(file_path)
    if vision:
        return vision
    path = Path(file_path)
    if not path.is_file():
        return None
    features = _analyze_image_features(path)
    text_desc = _describe_via_text_model(path.name, features)
    if text_desc:
        return text_desc
    # 第3级：零依赖兜底，保证中文可检索
    hint = _keywords_from_filename(path.name)
    if not hint and not features:
        return None
    bits = [b for b in (hint, features) if b]
    return "游戏素材：" + "，".join(bits)


def _describe_via_vision(file_path: str) -> str | None:
    """第1级：视觉模型看图。模型不支持视觉时返回 None（不抛出）。"""
    try:
        path = Path(file_path)
        if not path.is_file():
            logger.warning("describe_image_file_missing", file_path=str(file_path))
            return None
        raw = path.read_bytes()
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            logger.warning(
                "describe_image_size_rejected",
                file_path=str(file_path), size=len(raw), limit=MAX_IMAGE_BYTES,
            )
            return None

        api_key, base_url, model = _resolve_vision_config()
        if not base_url or not model:
            logger.warning("describe_image_no_model_configured")
            return None

        mime = _MIME_BY_EXT.get(path.suffix.lower(), "image/png")
        data_uri = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
        payload = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": DESCRIBE_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }],
            "max_tokens": 300,
            "temperature": 0.2,
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        url = base_url.rstrip("/") + "/chat/completions"
        resp = httpx.post(url, json=payload, headers=headers, timeout=DESCRIBE_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            # 模型不支持视觉输入时多为 400（invalid content type）——统一降级
            logger.warning(
                "describe_image_http_error",
                status=resp.status_code, body=(resp.text or "")[:200],
            )
            return None

        data = resp.json()
        choices = data.get("choices") or []
        content = (choices[0].get("message") or {}).get("content") if choices else None
        # 部分网关会把 content 拆成 content parts 列表
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        text = content.strip() if isinstance(content, str) else ""
        if not text:
            logger.warning("describe_image_empty_response", file_path=str(file_path))
            return None
        return text
    except Exception as e:  # noqa: BLE001 —— 兜底所有异常（超时/连接/JSON/编码），只降级不抛出
        logger.warning(
            "describe_image_failed",
            file_path=str(file_path), error=str(e)[:200], error_type=type(e).__name__,
        )
        return None
