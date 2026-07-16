"""FastAPI 后端路由 - 游戏设计 API"""

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import shutil
import socket
import tempfile
import threading
import uuid
import zipfile
import zlib

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from starlette.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.knowledge.knowledge_base import KnowledgeBase
from src.knowledge.source_asset_metadata import (
    SOURCE_ASSET_METADATA_KEYS,
    SOURCE_ASSET_METADATA_VERSION,
    enrich_source_asset_metadata,
)
from src.agent.game_agent import (
    GameDesignAgent,
    SKILLS,
    _save_custom_skills,
    build_source_web_manifest,
    restore_session_code,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# 全局实例
kb = KnowledgeBase()
agent = GameDesignAgent(kb)


# ============ HTTP 层限流（取代旧的按工具调用限流，避免卡断分段写入） ============
import time as _time

_CHAT_RATE_WINDOW = 60   # 秒
_CHAT_RATE_MAX = 30      # 每会话每窗口最多对话请求数
_chat_request_times: dict[str, list[float]] = {}
_rate_table_last_sweep = 0.0


def _check_chat_rate_limit(session_id: str) -> bool:
    """按会话滑动窗口限流。未超限返回 True。

    只在事件循环线程调用（两个聊天端点），无需加锁。每个窗口期顺带做一次全表清扫，
    删掉时间戳全部过期的会话条目——否则海量唯一 session_id 会让该表无限增长（内存 DoS）。
    """
    global _rate_table_last_sweep
    if not session_id:
        return True
    now = _time.time()
    if now - _rate_table_last_sweep >= _CHAT_RATE_WINDOW:
        _rate_table_last_sweep = now
        stale = [sid for sid, ts in _chat_request_times.items()
                 if not ts or now - ts[-1] >= _CHAT_RATE_WINDOW]
        for sid in stale:
            _chat_request_times.pop(sid, None)
    calls = [t for t in _chat_request_times.get(session_id, []) if now - t < _CHAT_RATE_WINDOW]
    if len(calls) >= _CHAT_RATE_MAX:
        _chat_request_times[session_id] = calls
        return False
    calls.append(now)
    _chat_request_times[session_id] = calls
    return True


# ============ 安全：路径 / 输入校验 ============
import re as _re
from src.config import settings as _settings

# 素材类型白名单（防止 asset_type 任意建目录/写文件）
_ALLOWED_ASSET_TYPES = {"image", "audio", "spritesheet", "tilemap", "font"}
# 扩展名按素材类型白名单（取代旧黑名单）：黑名单挡不住 .xhtml/.xht/.svgz 这类
# mimetypes 会推断成可渲染类型的漏网项——浏览器同源内联渲染并执行其中脚本即存储型 XSS。
# 不在白名单一律拒绝；上传入口与 serve 出口都按此校验（磁盘历史遗留文件也不回源）。
# 白名单覆盖所有无浏览器脚本执行风险的常见格式（位图/无损音频/Tiled 地图等都安全；
# 危险的是 .svg/.svgz/.html/.xhtml 这类可同源渲染并执行脚本的——保持拒绝）。
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".avif", ".tif", ".tiff"}
_ALLOWED_ASSET_EXTS: dict[str, set[str]] = {
    "image": _IMAGE_EXTS,
    "audio": {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus"},
    "spritesheet": _IMAGE_EXTS,
    "tilemap": {".json", ".tmx"},  # .tmx 是 Tiled 的 XML 地图：回源带 nosniff，不会被当文档渲染
    "font": {".ttf", ".otf", ".woff", ".woff2"},
}
# 文件名：无前导点、无 ".."、仅限安全字符（防穿越）
_SAFE_FILENAME = _re.compile(r"^(?!\.)(?!.*\.\.)[A-Za-z0-9_.-]+$")
# 会话 id：来源统一在 src/config.py → settings.safe_session_id_pattern（一处修改、全局生效）
_SAFE_SESSION_ID = _re.compile(_settings.safe_session_id_pattern)


def _require_safe_session_id(session_id: str) -> str:
    if not session_id or not _SAFE_SESSION_ID.match(session_id):
        raise HTTPException(status_code=400, detail="无效的 session_id")
    return session_id


def _resolve_asset_path(asset_type: str, filename: str) -> str:
    """校验并解析素材真实路径，确保落在 assets 目录内（防路径穿越读取任意文件）。

    扩展名同样按白名单校验：即便磁盘上有历史遗留的危险类型文件也拒绝回源（防存储型 XSS）。
    """
    if asset_type not in _ALLOWED_ASSET_TYPES or not _SAFE_FILENAME.match(filename):
        raise HTTPException(status_code=404, detail="文件不存在")
    if os.path.splitext(filename)[1].lower() not in _ALLOWED_ASSET_EXTS.get(asset_type, set()):
        raise HTTPException(status_code=404, detail="文件不存在")
    base = Path(_settings.assets_dir).resolve()
    target = (base / asset_type / filename).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return str(target)


# ============ 请求/响应模型 ============

CHAT_HISTORY_DIR = Path(__file__).resolve().parents[2] / "data" / "chat_history"
CHAT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def _history_path(session_id: str) -> Path:
    _require_safe_session_id(session_id)
    return CHAT_HISTORY_DIR / f"{session_id}.json"


# 聊天历史的线程锁：读写都经 asyncio.to_thread 在线程池里跑，同会话并发请求
# （多标签页共享 localStorage 里的 session_id / 重复点击）的 load→append→save
# 读改写会互相覆盖丢消息；Windows 上 os.replace 还会与并发裸读互斥失败（共享冲突）。
# 用固定数量的哈希分桶锁而非 per-session 字典：海量唯一 session_id 不会让锁表
# 无限增长（与上面限流表同型的内存 DoS），碰撞只造成无害的跨会话串行化。
_HISTORY_LOCKS = [threading.Lock() for _ in range(64)]


def _history_lock(session_id: str) -> threading.Lock:
    digest = zlib.crc32(session_id.encode("utf-8", "surrogatepass"))
    return _HISTORY_LOCKS[digest & 63]


def _read_text_retry(path: Path, attempts: int = 4, delay: float = 0.015) -> str:
    """读文件，对 Windows 共享冲突（os.replace 进行中 / 杀软短暂独占）做有界重试。

    进程内的读写已由 _history_lock 互斥，这里只兜外部进程（Defender、编辑器）的短暂占用。
    """
    while True:
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            attempts -= 1
            if attempts <= 0:
                raise
            _time.sleep(delay)


def _load_chat_history(session_id: str) -> list[dict]:
    path = _history_path(session_id)
    if not path.exists():
        return []
    try:
        raw = _read_text_retry(path)
    except OSError as e:
        # 瞬态读失败（共享冲突/磁盘问题）≠ 文件损坏：不能走改名分支把健康历史
        # 错当损坏备份掉。向上抛出，让 append 路径跳过本条、读取路径返回空。
        logger.error("聊天历史读取失败 (session=%s): %s", session_id, e)
        raise
    try:
        return json.loads(raw)
    except ValueError as e:
        # 真正的内容损坏才走这里。不能静默清零：下一次 append 会用空列表整体覆盖、
        # 全部历史无声丢失。把坏文件改名备份留痕（.corrupt.bak 不再匹配 *.json glob）。
        logger.error("聊天历史损坏，已备份为 .corrupt.bak 后从空开始 (session=%s): %s", session_id, e)
        try:
            os.replace(path, path.with_name(path.name + ".corrupt.bak"))
        except OSError as be:
            logger.error("聊天历史损坏文件备份失败 (session=%s): %s", session_id, be)
        return []


def _load_chat_history_or_empty(session_id: str) -> list[dict]:
    """只读场景（历史接口/列表）：持锁读，瞬态失败降级为空列表而非 500。"""
    with _history_lock(session_id):
        try:
            return _load_chat_history(session_id)
        except OSError:
            return []


def _save_chat_history(session_id: str, history: list[dict]) -> None:
    path = _history_path(session_id)
    # 临时文件 + os.replace 原子写（同 persistence.py 模式）：进程崩溃/磁盘满不会留下
    # 半截 JSON——半截文件下次加载会触发上面的损坏分支。.tmp 后缀不会被 *.json glob 误匹配。
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=f"{session_id}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(history, ensure_ascii=False, indent=2))
        replace_attempts = 4
        while True:
            try:
                os.replace(tmp_path, path)
                break
            except PermissionError:
                # Windows：目标文件被外部进程短暂打开时 MoveFileEx 报共享冲突，重试几次
                replace_attempts -= 1
                if replace_attempts <= 0:
                    raise
                _time.sleep(0.015)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _append_chat_history(session_id: str, role: str, content: str, extra: dict | None = None) -> None:
    with _history_lock(session_id):  # 读改写全程持锁，防同会话并发 append 互相覆盖
        try:
            history = _load_chat_history(session_id)
        except OSError:
            # 读不出来时宁可丢这一条消息也不能用空列表覆盖整个历史文件
            logger.error("聊天历史暂不可读，跳过本条消息持久化 (session=%s, role=%s)", session_id, role)
            return
        item = {
            "role": role,
            "content": content,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            item.update(extra)
        history.append(item)
        _save_chat_history(session_id, history)


def _get_latest_code_from_history(history: list[dict]) -> str:
    for item in reversed(history):
        code = item.get("code")
        if isinstance(code, str) and code:
            return code
    return ""


def _build_session_summary(session_id: str) -> dict:
    history = _load_chat_history_or_empty(session_id)
    title = "新对话"
    for item in history:
        if item.get("role") == "user" and item.get("content"):
            title = item["content"].strip().replace("\n", " ")[:32]
            break
    return {
        "session_id": session_id,
        "title": title,
        "message_count": len(history),
        "updated_at": history[-1]["ts"] if history else None,
    }

MAX_CHAT_IMAGE_BYTES = 10 * 1024 * 1024


class ChatImage(BaseModel):
    name: str = "image.png"
    type: str = "image/png"
    size: int = 0
    data_url: str


def _validate_chat_images(images: list[ChatImage]) -> list[ChatImage]:
    if len(images) > 4:
        raise HTTPException(status_code=413, detail="一次最多发送 4 张图片")
    for image in images:
        if image.size > MAX_CHAT_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail=f"图片 {image.name} 超过 10MB 限制")
        if not image.type.startswith("image/"):
            raise HTTPException(status_code=400, detail=f"文件 {image.name} 不是图片")
        prefix = f"data:{image.type};base64,"
        if not image.data_url.startswith(prefix):
            raise HTTPException(status_code=400, detail=f"图片 {image.name} data URL 格式无效")
        try:
            raw = base64.b64decode(image.data_url[len(prefix):], validate=True)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"图片 {image.name} base64 无效") from e
        if len(raw) > MAX_CHAT_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail=f"图片 {image.name} 超过 10MB 限制")
    return images


def _build_user_message_content(text: str, images: list[ChatImage]):
    if not images:
        return text
    content = [{"type": "text", "text": text or "请观察这张图片，并结合我的游戏需求回答。"}]
    for image in images:
        content.append({"type": "image_url", "image_url": {"url": image.data_url}})
    return content


def _image_history_meta(images: list[ChatImage]) -> list[dict]:
    return [{"name": image.name, "type": image.type, "size": image.size} for image in images]



class ChatRequest(BaseModel):
    session_id: str = ""
    message: str
    current_code: str = ""
    code_dirty: bool = False  # 前端是否在编辑器里手动改过代码（决定服务端是否采用 current_code）
    images: list[ChatImage] = Field(default_factory=list)



class ChatResponse(BaseModel):
    session_id: str
    reply: str
    code: str | None = None
    action: str = "chat"
    # 本回合参考摘要：{"skills": [{"name", "web_bundle", "source_reads", "assets"}]}；
    # 未用任何技能工具时为 null（与流式 done 事件的同名字段保持一致）
    reference_summary: dict | None = None


class ProjectSaveRequest(BaseModel):
    project_id: str = ""
    name: str
    code: str
    config: dict | None = None


class ProjectResponse(BaseModel):
    project_id: str
    name: str = ""
    code: str = ""
    config: dict | None = None


# ============ 对话 API ============

@router.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """与 AI 智能体对话"""
    session_id = req.session_id or str(uuid.uuid4())
    _require_safe_session_id(session_id)
    if not _check_chat_rate_limit(session_id):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍候再试")
    try:
        # base64 解码（最多 4×10MB）是 CPU 密集操作，放线程别卡事件循环；
        # 纯函数无共享状态，线程里抛的 HTTPException 原样传播到这里
        images = await asyncio.to_thread(_validate_chat_images, req.images)
        user_content = _build_user_message_content(req.message, images)
        result = await agent.chat(
            session_id=session_id,
            user_message=user_content,
            current_code=req.current_code,
            code_dirty=req.code_dirty,
        )
        # 历史 I/O 放到线程，别阻塞事件循环；不再把整份 HTML 塞进历史（磁盘 .html 才是权威，避免 O(N^2) 膨胀）
        await asyncio.to_thread(_append_chat_history, session_id, "user", req.message,
                                {"images": _image_history_meta(images)} if images else None)
        await asyncio.to_thread(_append_chat_history, session_id, "ai", result.get("reply", ""))
        return ChatResponse(session_id=session_id, **result)
    except HTTPException:
        raise  # 图片校验等 4xx 原样抛出，别被吞成 500
    except Exception as e:
        err_str = str(e)
        # 友好的错误提示（其余一律走通用文案 + 服务端日志，避免把原始异常/路径/配置泄露给前端）
        if "401" in err_str or "api_key" in err_str.lower() or "auth" in err_str.lower():
            detail = "API Key 无效或已过期，请在 .env 文件中检查 OPENAI_API_KEY 配置"
        elif "timeout" in err_str.lower() or "timed out" in err_str.lower():
            detail = "AI 服务响应超时，请检查网络连接或稍后重试"
        elif "connect" in err_str.lower():
            detail = "无法连接到 AI 服务，请检查网络和 OPENAI_BASE_URL 配置"
        else:
            logger.exception("chat failed (session=%s)", session_id)
            detail = "AI 对话失败，请稍后重试（详情见服务端日志）"
        raise HTTPException(status_code=500, detail=detail) from e


@router.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """流式对话 - SSE (Server-Sent Events)"""
    session_id = req.session_id or str(uuid.uuid4())
    _require_safe_session_id(session_id)
    # ⚠️ 图片校验必须在创建 StreamingResponse 之前做：否则校验失败时 HTTPException 抛在
    # SSE 生成器里（已经 200 OK），前端拿不到 4xx、流被无声截断。
    # base64 解码是 CPU 密集操作，放线程别卡事件循环（不改变上述时序）。
    images = await asyncio.to_thread(_validate_chat_images, req.images)
    user_content = _build_user_message_content(req.message, images)
    image_meta = _image_history_meta(images) if images else None

    async def event_generator():
        assistant_text = ""
        latest_code = ""
        # 先发送 session_id
        yield f"data: {json.dumps({'type': 'session', 'session_id': session_id}, ensure_ascii=False)}\n\n"
        if not _check_chat_rate_limit(session_id):
            yield f"data: {json.dumps({'type': 'error', 'content': '请求过于频繁，请稍候再试', 'error_code': 'RATE_LIMIT_EXCEEDED'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
            return
        try:
            # user 落历史必须在 try 内：append 异常（磁盘满/共享冲突重试耗尽）若逃出
            # event_generator，SSE 既无 error 事件也无 [DONE]，前端流无声中断
            await asyncio.to_thread(_append_chat_history, session_id, "user", req.message,
                                    {"images": image_meta} if image_meta else None)
            async for chunk in agent.chat_stream(
                session_id=session_id,
                user_message=user_content,
                current_code=req.current_code,
                code_dirty=req.code_dirty,
            ):
                ctype = chunk.get("type")
                if ctype == "token":
                    assistant_text += chunk.get("content", "")
                elif ctype == "code_update":
                    latest_code = chunk.get("code") or latest_code
                elif ctype == "done":
                    latest_code = chunk.get("code") or latest_code
                    # 不再把整份 HTML 写进历史（磁盘权威，避免 O(N^2) 膨胀）。
                    # 即便正文为空但确实改了代码，也落一条占位，回放时才有助手条目。
                    if assistant_text.strip():
                        await asyncio.to_thread(_append_chat_history, session_id, "ai", assistant_text)
                    elif chunk.get("action") in ("generate", "edit"):
                        await asyncio.to_thread(_append_chat_history, session_id, "ai", "（已更新右侧代码）")
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        except Exception as e:
            err_msg = str(e)
            if "401" in err_msg or "api_key" in err_msg.lower():
                err_msg = "API Key 无效或已过期，请检查 .env 配置"
            elif "timeout" in err_msg.lower():
                err_msg = "AI 服务响应超时，请稍后重试"
            yield f"data: {json.dumps({'type': 'error', 'content': err_msg}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/chat/history")
async def list_chat_history():
    """列出所有对话历史会话"""
    def _list():
        return [
            _build_session_summary(path.stem)
            for path in sorted(CHAT_HISTORY_DIR.glob("*.json"),
                               key=lambda p: p.stat().st_mtime, reverse=True)
        ]
    return await asyncio.to_thread(_list)  # glob+stat+逐文件读放线程，别阻塞事件循环


@router.get("/api/chat/{session_id}/history")
async def get_chat_history(session_id: str):
    """获取指定会话的消息历史，并返回该会话最后一次代码快照。

    代码以磁盘 .html 为权威来源（每次编辑都会落盘，含被中断回合的最新代码），
    仅当磁盘没有时才回退到聊天历史里的快照（兼容旧会话）。
    """
    from src.utils.persistence import load_session_code
    history = await asyncio.to_thread(_load_chat_history_or_empty, session_id)
    latest_code = (await asyncio.to_thread(load_session_code, session_id)) or _get_latest_code_from_history(history)
    return {
        "session_id": session_id,
        "messages": history,
        "latest_code": latest_code,
    }


@router.delete("/api/chat/{session_id}")
async def clear_chat(session_id: str):
    """清除对话历史"""
    await agent.clear_session(session_id)
    path = _history_path(session_id)

    # unlink 放线程（磁盘慢/杀软扫描时别卡事件循环）；missing_ok 顺带消除 exists/unlink 的
    # TOCTOU；持历史锁防与进行中的 append 交错（清除后又被旧回合写回半截历史）
    def _unlink_history():
        with _history_lock(session_id):
            path.unlink(missing_ok=True)

    await asyncio.to_thread(_unlink_history)
    return {"ok": True}


# ============ 代码版本历史 API ============

@router.get("/api/chat/{session_id}/versions")
async def list_code_versions(session_id: str):
    """列出会话代码的历史版本（新→旧；无版本返回空数组）。"""
    _require_safe_session_id(session_id)
    from src.utils.persistence import list_session_versions
    versions = await asyncio.to_thread(list_session_versions, session_id)
    return {"versions": versions}


@router.post("/api/chat/{session_id}/versions/{version_id}/restore")
async def restore_code_version(session_id: str, version_id: str):
    """恢复指定历史版本的代码。

    恢复前 save_session_code 会先把当前代码归档为新版本（恢复操作本身可回退）；
    同时经 game_agent 的会话代码写入路径更新内存，保证下一轮对话基于恢复后的代码。
    version_id 由 persistence 层白名单正则校验（防路径穿越），非法/不存在一律 404。
    """
    _require_safe_session_id(session_id)
    from src.utils.persistence import load_session_version
    # 持会话锁：与进行中的对话回合（读改写权威代码）串行，防交错覆盖
    async with agent._get_session_lock(session_id):
        code = await asyncio.to_thread(load_session_version, session_id, version_id)
        if code is None:
            raise HTTPException(status_code=404, detail="版本不存在")
        # 磁盘 I/O（归档旧版 + 写新代码）下沉线程，不卡事件循环
        await asyncio.to_thread(restore_session_code, session_id, code)
    return {"code": code}


# ============ 真机预览 API ============

@router.get("/play/{session_id}")
async def play_session(session_id: str):
    """真机预览：直接以 text/html 返回该会话的磁盘代码（data/sessions/{id}.html）。

    免 token 理由与 /assets 相同：手机扫码打开是裸浏览器 GET，无法携带 X-API-Token
    自定义头，main.py 的 require_token 对本路径放行。风险同样受控：session_id 是
    不可猜的 UUID（同素材文件名策略），且已按安全正则校验防路径穿越；无文件 404。
    """
    _require_safe_session_id(session_id)
    from src.utils.persistence import load_session_code
    code = await asyncio.to_thread(load_session_code, session_id)
    if code is None:
        raise HTTPException(status_code=404, detail="会话代码不存在")
    return HTMLResponse(code)


@router.get("/api/server-info")
async def server_info():
    """返回局域网 IP 与端口，供前端生成真机预览（手机扫码）地址。"""

    def _is_virtual_or_unusable(ip: str) -> bool:
        # 代理 TUN（Clash 等默认 198.18.0.0/15）、链路本地、回环——手机扫码连不上这些地址
        return (
            ip.startswith("198.18.") or ip.startswith("198.19.")
            or ip.startswith("169.254.") or ip.startswith("127.")
        )

    def _detect_lan_ip() -> str | None:
        # UDP connect 技巧：让内核选出通往外网的本机地址，不真正发包。
        # 开着代理 TUN 时内核会选中虚拟网卡（如 Clash 的 198.18.0.1），
        # 这种地址手机连不上，需回退到枚举本机真实私网地址。
        candidate = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                candidate = s.getsockname()[0]
            finally:
                s.close()
        except OSError:
            pass
        if candidate and not _is_virtual_or_unusable(candidate):
            return candidate
        try:
            infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
            private = [
                ip for ip in {i[4][0] for i in infos}
                if not _is_virtual_or_unusable(ip)
                and (ip.startswith("192.168.") or ip.startswith("10.")
                     or any(ip.startswith(f"172.{n}.") for n in range(16, 32)))
            ]
            if private:
                return sorted(private)[0]
        except OSError:
            pass
        return candidate

    # getsockname 本身不阻塞，但 socket 创建在个别环境（无网卡/防火墙钩子）可能变慢，放线程稳妥
    lan_ip = await asyncio.to_thread(_detect_lan_ip)
    return {"lan_ip": lan_ip, "port": _settings.port}




def _write_upload_tmp(content: bytes, suffix: str) -> str:
    """同步写上传内容到临时文件（在线程池里调用），返回路径。

    写失败（磁盘满等）时自行删除半截文件再抛出，不在系统临时目录积累孤儿文件。
    """
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        with tmp:
            tmp.write(content)
    except BaseException:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise
    return tmp.name


def _unlink_missing_ok(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


@router.post("/api/assets/upload")
async def upload_asset(
    file: UploadFile = File(...),
    asset_type: str = Form("image"),
    description: str = Form(""),
    tags: str = Form(""),
):
    """上传游戏素材"""
    # 安全校验：限定素材类型（防 asset_type 任意建目录），扩展名按类型白名单（防存储型 XSS）
    if asset_type not in _ALLOWED_ASSET_TYPES:
        raise HTTPException(status_code=400, detail=f"不支持的素材类型：{asset_type}")
    suffix = os.path.splitext(file.filename or "file")[1].lower()
    allowed_exts = _ALLOWED_ASSET_EXTS[asset_type]
    if suffix not in allowed_exts:
        raise HTTPException(
            status_code=400,
            detail=f"素材类型 {asset_type} 仅支持 {'/'.join(sorted(allowed_exts))} 文件",
        )
    content = await file.read()
    if len(content) > _settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过 {_settings.max_upload_bytes // (1024 * 1024)}MB 上限",
        )
    # 临时文件写入放线程（数十 MB 同步写盘会卡事件循环）；_write_upload_tmp 写失败时
    # 自行清理，不泄漏孤儿文件
    tmp_path = await asyncio.to_thread(_write_upload_tmp, content, suffix)
    try:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        result = await asyncio.to_thread(  # 嵌入推理 + 文件拷贝是阻塞操作，放线程
            kb.upload_asset,
            file_path=tmp_path,
            file_name=file.filename or "unknown",
            asset_type=asset_type,
            description=description,
            tags=tag_list,
        )
        # 添加可引用的 URL
        result["url"] = f"/assets/{asset_type}/{result['asset_id']}{result.get('extension', '')}"
        return result
    finally:
        await asyncio.to_thread(_unlink_missing_ok, tmp_path)  # 删除也是磁盘 I/O，放线程


@router.get("/api/assets")
async def list_assets(asset_type: str | None = None):
    """列出素材"""
    return await asyncio.to_thread(kb.list_assets, asset_type)


@router.get("/api/assets/search")
async def search_assets(q: str, asset_type: str | None = None, top_k: int = 5):
    """搜索素材"""
    return await asyncio.to_thread(kb.search_assets, q, asset_type, top_k)  # 含嵌入推理，放线程


@router.delete("/api/assets/{asset_id}")
async def delete_asset(asset_id: str):
    """删除素材"""
    ok = await asyncio.to_thread(kb.delete_asset, asset_id)
    if not ok:
        raise HTTPException(status_code=404, detail="素材不存在")
    return {"ok": True}


# ============ CSV 批量导入 ============

_CSV_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "gb2312", "gb18030", "latin-1")


def _parse_csv_metadata(content: bytes) -> tuple[dict[str, dict], list[str]]:
    """解析 CSV 标注文件，返回 {文件名: {tags, description}} 和解析错误列表。

    自动检测编码（UTF-8 BOM → UTF-8 → GBK → ...），
    列名按位置识别（标注员/文件名/文件路径/标签/描述）。
    """
    # 解码
    text = None
    for enc in _CSV_ENCODINGS:
        try:
            text = content.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        return {}, ["CSV 文件编码无法识别，尝试了 GBK/UTF-8 等均失败"]

    # 解析（csv.DictReader 需要文件头）
    import csv as _csv
    import io as _io
    reader = _csv.reader(_io.StringIO(text))
    rows = list(reader)
    if len(rows) < 2:
        return {}, ["CSV 文件为空或仅有一行表头"]

    errors: list[str] = []
    meta: dict[str, dict] = {}
    for i, row in enumerate(rows[1:], start=2):  # 从第2行开始（1-based行号）
        if not row or all(not c.strip() for c in row):
            continue  # 空行
        if len(row) < 5:
            errors.append(f"第{i}行：列数不足 ({len(row)}列，期望≥5)")
            continue
        fname = row[1].strip() if len(row) > 1 else ""
        tag = row[3].strip() if len(row) > 3 else ""
        desc = row[4].strip() if len(row) > 4 else ""
        if not fname:
            errors.append(f"第{i}行：文件名为空，跳过")
            continue
        meta[fname] = {"tags": tag, "description": desc}

    return meta, errors


def _do_import_batch(
    files: list[tuple[str, bytes]],  # [(filename, content), ...]
    csv_content: bytes,
) -> dict:
    """批量导入的核心逻辑（纯数据，不依赖 Request/UploadFile）。

    files 在线程池里从 UploadFile 读取后传入；kb I/O 也在线程池。
    """
    csv_meta, csv_errors = _parse_csv_metadata(csv_content)

    success = 0
    failed: list[str] = []
    matched_csv_names: set[str] = set()

    for fname, fcontent in files:
        meta = csv_meta.get(fname)
        if meta:
            matched_csv_names.add(fname)
            tags_str = meta["tags"]
            description = meta["description"]
        else:
            tags_str = ""
            description = ""

        tag_list = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else []

        # 写临时文件 → upload_asset（复用已有逻辑，包括扩展名白名单校验）
        suffix = os.path.splitext(fname)[1].lower()
        tmp_path = _write_upload_tmp(fcontent, suffix)
        try:
            kb.upload_asset(
                file_path=tmp_path,
                file_name=fname,
                asset_type="audio",
                description=description,
                tags=tag_list,
            )
            success += 1
        except Exception as e:
            failed.append(f"{fname}: {e}")
        finally:
            _unlink_missing_ok(tmp_path)

    # CSV 里有标注但没传文件的
    not_found = [n for n in csv_meta if n not in matched_csv_names]

    result: dict = {"ok": True, "success": success, "failed": failed, "total": len(files)}
    if csv_errors:
        result["csv_errors"] = csv_errors
    if not_found:
        result["not_found"] = not_found[:50]  # 最多显示前50个
        result["not_found_count"] = len(not_found)
    return result


@router.post("/api/assets/import/batch")
async def import_assets_batch(
    csv_file: UploadFile = File(...),
    files: list[UploadFile] = File(...),
):
    """CSV 标注 + 音频文件批量导入。

    上传一份 CSV 标注文件 + 多份音频文件，按文件名自动匹配标注（标签+描述）并入库。
    CSV 格式：标注员,文件名,文件路径,标签,描述（UTF-8 或 GBK 编码均可）
    """
    if len(files) > 500:
        raise HTTPException(status_code=400, detail="单次最多导入 500 个文件")
    if not csv_file.filename or not csv_file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="CSV 文件须为 .csv 格式")

    csv_content = await csv_file.read()
    if len(csv_content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="CSV 文件超过 5MB 限制")

    # 读所有上传文件内容到内存（音频通常几MB以内，最多500个）
    total_bytes = 0
    file_data: list[tuple[str, bytes]] = []
    for f in files:
        content = await f.read()
        total_bytes += len(content)
        if total_bytes > _settings.max_upload_bytes * 50:  # 总额 1GB
            raise HTTPException(status_code=413, detail="文件总量超过 1GB 上限")
        # 扩展名白名单校验（复用已有规则）
        suffix = os.path.splitext(f.filename or "")[1].lower()
        if suffix not in _ALLOWED_ASSET_EXTS.get("audio", set()):
            raise HTTPException(status_code=400,
                detail=f"不支持的文件格式: {suffix}（仅支持 mp3/wav/ogg/m4a/flac/aac/opus）")
        file_data.append((f.filename or "unknown", content))

    # kb.upload_asset 内部做 shutil.copy2 + ChromaDB 写入，全是阻塞 I/O → 放线程池
    result = await asyncio.to_thread(_do_import_batch, file_data, csv_content)
    return result


def _annotate_existing(csv_content: bytes) -> dict:
    """事后补标注：拿 CSV 匹配已有素材，填写标签和描述。

    按文件名匹配已有素材，收集所有更新后一次性提交给 ChromaDB（避免逐条 I/O 超时）。
    """
    csv_meta, csv_errors = _parse_csv_metadata(csv_content)

    # 获取所有已有素材，建文件名→资产列表索引
    all_assets = kb.list_assets()
    by_fname: dict[str, list[dict]] = {}
    for a in all_assets:
        fn = a.get("file_name", "")
        by_fname.setdefault(fn, []).append(a)

    # 收集批量更新数据（三个并行列表）
    update_ids: list[str] = []
    update_metas: list[dict] = []
    update_docs: list[str] = []
    not_found: list[str] = []

    for fname, meta in csv_meta.items():
        matches = by_fname.get(fname, [])
        if not matches:
            not_found.append(fname)
            continue
        for asset in matches:
            aid = asset.get("id") or asset.get("asset_id", "")
            if not aid:
                continue
            tag_list = [t.strip() for t in meta["tags"].split(",") if t.strip()] if meta["tags"] else []
            doc = f"[{asset.get('asset_type', 'audio')}] {asset.get('file_name', fname)}"
            if meta["description"]:
                doc += f" - {meta['description']}"
            if tag_list:
                doc += f" | 标签: {', '.join(tag_list)}"
            update_ids.append(aid)
            update_metas.append({
                "asset_id": aid,
                "file_name": asset.get("file_name", fname),
                "asset_type": asset.get("asset_type", "audio"),
                "file_path": asset.get("file_path", ""),
                "extension": asset.get("extension", ""),
                "tags": json.dumps(tag_list, ensure_ascii=False),
            })
            update_docs.append(doc)

    # 一次性批量提交（避免 181 次逐条 ChromaDB I/O）
    if update_ids:
        try:
            kb.assets_collection.update(
                ids=update_ids,
                metadatas=update_metas,
                documents=update_docs,
            )
        except Exception as e:
            logger.error("annotate_batch_update_failed", error=str(e)[:200])
            return {"ok": False, "error": f"批量更新失败: {e}"}

    result: dict = {"ok": True, "updated": len(update_ids), "csv_entries": len(csv_meta)}
    if not_found:
        result["not_found"] = not_found[:50]
        result["not_found_count"] = len(not_found)
    if csv_errors:
        result["csv_errors"] = csv_errors
    return result


@router.post("/api/assets/annotate/batch")
async def annotate_assets_batch(csv_file: UploadFile = File(...)):
    """CSV 事后补标注：用 CSV 匹配已有素材，自动填写标签和描述。

    只传 CSV 文件，不传素材文件。按文件名匹配已有素材并更新其元数据。
    适用于先上传了素材但没带 CSV，或分批导入后有遗漏标注的情况。
    """
    if not csv_file.filename or not csv_file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="CSV 文件须为 .csv 格式")
    csv_content = await csv_file.read()
    if len(csv_content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="CSV 文件超过 5MB 限制")
    result = await asyncio.to_thread(_annotate_existing, csv_content)
    return result


_ASSET_HEADERS = {"X-Content-Type-Options": "nosniff"}
_SOURCE_ASSET_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "public, max-age=31536000, immutable",
    "Access-Control-Allow-Origin": "*",
    "Cross-Origin-Resource-Policy": "cross-origin",
}
_SOURCE_ASSET_MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".ico": "image/x-icon", ".avif": "image/avif", ".tif": "image/tiff",
    ".tiff": "image/tiff", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".flac": "audio/flac",
    ".aac": "audio/aac", ".opus": "audio/ogg", ".ttf": "font/ttf",
    ".otf": "font/otf", ".woff": "font/woff", ".woff2": "font/woff2",
    ".json": "application/json", ".tmx": "application/xml",
}
_SOURCE_ASSET_MANIFEST = "manifest.json"


def _resolve_source_asset_path(bundle_id: str, filename: str) -> str:
    """按技能清单校验源码素材，不允许通过 bundle URL 枚举或读取未登记文件。"""
    if not _SOURCE_ASSET_BUNDLE_RE.fullmatch(bundle_id) or not _SAFE_FILENAME.fullmatch(filename):
        raise HTTPException(status_code=404, detail="文件不存在")
    suffix = Path(filename).suffix.lower()
    if suffix not in _SOURCE_USABLE_ASSET_EXTS:
        raise HTTPException(status_code=404, detail="文件不存在")
    root = _source_asset_root().resolve()
    bundle_dir = (root / bundle_id).resolve()
    target = (bundle_dir / filename).resolve()
    if bundle_dir.parent != root or target.parent != bundle_dir or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    try:
        manifest = json.loads((bundle_dir / _SOURCE_ASSET_MANIFEST).read_text(encoding="utf-8"))
        registered = filename in manifest.get("files", [])
    except (OSError, ValueError, TypeError):
        registered = False
    if not registered:
        raise HTTPException(status_code=404, detail="文件不存在")
    return str(target)


@router.get("/api/assets/file/{asset_type}/{filename}")
async def serve_asset(asset_type: str, filename: str):
    """提供素材文件访问（API路径）。路径已做白名单+穿越校验。"""
    return FileResponse(_resolve_asset_path(asset_type, filename), headers=_ASSET_HEADERS)


@router.get("/assets/source/{bundle_id}/{filename}")
async def serve_source_asset(bundle_id: str, filename: str):
    """提供源码技能内已保存的图片、音频、字体和地图资源。"""
    return FileResponse(
        _resolve_source_asset_path(bundle_id, filename),
        media_type=_SOURCE_ASSET_MEDIA_TYPES[Path(filename).suffix.lower()],
        headers=_SOURCE_ASSET_HEADERS,
    )


@router.get("/assets/{asset_type}/{filename}")
async def serve_asset_short(asset_type: str, filename: str):
    """提供素材文件访问（短路径，供游戏代码引用）。路径已做白名单+穿越校验。"""
    return FileResponse(_resolve_asset_path(asset_type, filename), headers=_ASSET_HEADERS)


# ============ 项目 API ============

@router.post("/api/projects", response_model=ProjectResponse)
async def save_project(req: ProjectSaveRequest):
    """保存项目"""
    project_id = await asyncio.to_thread(
        kb.save_project,
        project_id=req.project_id,
        name=req.name,
        code=req.code,
        config=req.config,
    )
    return ProjectResponse(project_id=project_id, name=req.name, code=req.code, config=req.config)


@router.get("/api/projects")
async def list_projects():
    """列出项目"""
    return await asyncio.to_thread(kb.list_projects)


@router.get("/api/projects/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: str):
    """获取项目"""
    project = await asyncio.to_thread(kb.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return ProjectResponse(**project)


@router.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    """删除项目"""
    ok = await asyncio.to_thread(kb.delete_project, project_id)
    if not ok:
        raise HTTPException(status_code=404, detail="项目不存在")
    return {"ok": True}


# ============ Skills API ============

# _sync_skills 的磁盘全量重写会卡事件循环，调用方一律 `await asyncio.to_thread(_sync_skills)`；
# 进线程后失去事件循环的天然串行化，用线程锁防止并发技能变更交错重写同一文件
_skills_sync_guard = threading.Lock()
_skills_mutation_lock = asyncio.Lock()


def _sync_skills():
    """更新 SkillMiddleware 的 prompt 缓存 + 持久化到磁盘（同步函数，放线程调用）"""
    with _skills_sync_guard:
        import src.agent.game_agent as ga
        # 旧版源码技能没有图片尺寸/精灵帧元数据。同步前补齐一次，这样任何后续技能
        # 变更都会把兼容性回填一并持久化，而不是只在当前进程内临时可见。
        backfill_source_asset_metadata()
        # 先原子持久化，再发布新的 prompt 缓存；磁盘失败时调用方可安全回滚 SKILLS，
        # 不会留下“内存已回滚但模型仍看到新技能”的三态不一致。
        _save_custom_skills()
        ga._rebuild_skills_prompt()  # 只列常用技能 + 检索提示（不再注入全部上百条）
        ga._invalidate_system_overhead()  # 技能变了，固定开销需重新计算（影响上下文圆环）

class SkillCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=100000)


class SkillUpdateRequest(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=100000)


_SOURCE_TEXT_EXTS = {
    ".html", ".htm", ".css", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".json", ".md", ".txt", ".xml", ".yaml", ".yml", ".py", ".lua", ".glsl",
    ".vert", ".frag", ".shader", ".csv", ".ini", ".toml",
}
_SOURCE_ASSET_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico",
    ".avif", ".tif", ".tiff",
    ".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus", ".swf",
    ".ttf", ".otf", ".woff", ".woff2", ".map", ".tmx",
}
_SOURCE_IGNORED_DIRS = {
    ".git", ".svn", "__macosx", "node_modules", "bower_components", "vendor",
    "dist", "build", ".idea", ".vscode", "coverage", "__pycache__",
}
_SOURCE_MAX_FILES = 500
_SOURCE_MAX_ARCHIVE_ENTRIES = 5000
_SOURCE_MAX_UPLOAD_PARTS = 5000
_SOURCE_MAX_FILE_BYTES = 2 * 1024 * 1024
_SOURCE_MAX_TEXT_BYTES = 2 * 1024 * 1024
_SOURCE_MAX_ASSET_PATHS = 2000
_SOURCE_USABLE_ASSET_EXTS = set().union(*_ALLOWED_ASSET_EXTS.values())
_SOURCE_ASSET_BUNDLE_RE = _re.compile(r"^[a-f0-9]{32}$")
_SOURCE_ASSET_TEMP_RE = _re.compile(r"^\.tmp-[a-f0-9]{32}$")


class _SourceImportLimitError(ValueError):
    pass


def _source_asset_kind(suffix: str) -> str | None:
    """把安全扩展名映射为浏览器可用的源码素材类型。"""
    if suffix in _ALLOWED_ASSET_EXTS["image"]:
        return "image"
    if suffix in _ALLOWED_ASSET_EXTS["audio"]:
        return "audio"
    if suffix in _ALLOWED_ASSET_EXTS["font"]:
        return "font"
    if suffix in _ALLOWED_ASSET_EXTS["tilemap"]:
        return "tilemap"
    return None


def _source_asset_root() -> Path:
    return Path(_settings.skills_dir) / "source_assets"


def cleanup_source_asset_temp_dirs() -> int:
    """启动时清理上次进程中断遗留的未发布临时素材包。"""
    root = _source_asset_root()
    if not root.is_dir():
        return 0
    cleaned = 0
    for child in root.iterdir():
        if not child.is_dir() or not _SOURCE_ASSET_TEMP_RE.fullmatch(child.name):
            continue
        try:
            shutil.rmtree(child)
            cleaned += 1
        except OSError as exc:
            logger.warning(
                "source_asset_temp_cleanup_failed",
                extra={"path": str(child), "error": str(exc)[:200]},
            )
    return cleaned


def _delete_source_asset_bundle(bundle_id: str | None) -> None:
    """只删除 skills/source_assets 下符合 UUID 规则的单个素材包。"""
    if not bundle_id or not _SOURCE_ASSET_BUNDLE_RE.fullmatch(bundle_id):
        return
    root = _source_asset_root().resolve()
    target = (root / bundle_id).resolve()
    if target.parent == root:
        try:
            shutil.rmtree(target)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(
                "source_asset_bundle_delete_failed",
                extra={"path": str(target), "error": str(exc)[:200]},
            )


def _delete_unreferenced_source_asset_bundle(bundle_id: str | None) -> None:
    """仅当当前没有技能引用素材包时删除，避免导入记录伪造 bundle id 后误删。"""
    if not bundle_id:
        return
    if any(item.get("source_asset_bundle_id") == bundle_id for item in SKILLS):
        return
    _delete_source_asset_bundle(bundle_id)


def _persist_source_asset_bundle(asset_files: list[dict]) -> tuple[str | None, list[dict]]:
    """原子落盘源码素材，返回不可猜 bundle id 与不含二进制内容的公开清单。"""
    if not asset_files:
        return None, []
    root = _source_asset_root()
    root.mkdir(parents=True, exist_ok=True)
    bundle_id = uuid.uuid4().hex
    tmp_dir = root / f".tmp-{bundle_id}"
    final_dir = root / bundle_id
    tmp_dir.mkdir()
    manifest: list[dict] = []
    try:
        for item in asset_files:
            path = str(item["path"])
            suffix = PurePosixPath(path).suffix.lower()
            digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:32]
            stored_name = f"{digest}{suffix}"
            (tmp_dir / stored_name).write_bytes(item["content"])
            manifest_item = {
                "path": path,
                "file_name": PurePosixPath(path).name,
                "kind": item["kind"],
                "size": item["size"],
                "stored_name": stored_name,
                "url": f"/assets/source/{bundle_id}/{stored_name}",
            }
            for key in SOURCE_ASSET_METADATA_KEYS:
                if key in item:
                    manifest_item[key] = item[key]
            manifest.append(manifest_item)
        (tmp_dir / _SOURCE_ASSET_MANIFEST).write_text(
            json.dumps(
                {"version": 1, "files": [item["stored_name"] for item in manifest]},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        # Windows Defender/索引器可能在刚写完大量图片时短暂持有目录句柄，导致
        # 同卷原子 rename 报 WinError 5；短暂退避重试，仍失败才整体回滚。
        for attempt in range(8):
            try:
                tmp_dir.rename(final_dir)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                _time.sleep(0.05 * (attempt + 1))
        return bundle_id, manifest
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        shutil.rmtree(final_dir, ignore_errors=True)
        raise


def _safe_source_path(raw_path: str) -> str | None:
    """把浏览器/ZIP 文件名归一成安全的项目相对路径。"""
    value = (raw_path or "").replace("\\", "/").strip("/")
    if not value or len(value) > 500:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return "/".join(path.parts)


def _decode_source_text(raw: bytes) -> str | None:
    if b"\x00" in raw:
        return None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    decoded = raw.decode("utf-8", errors="replace")
    if decoded.count("\ufffd") > max(8, len(decoded) // 100):
        return None
    return decoded


def _source_language(path: str) -> str:
    return {
        ".html": "html", ".htm": "html", ".css": "css", ".js": "javascript",
        ".jsx": "jsx", ".mjs": "javascript", ".cjs": "javascript",
        ".ts": "typescript", ".tsx": "tsx", ".json": "json", ".md": "markdown",
        ".py": "python", ".lua": "lua", ".xml": "xml", ".yaml": "yaml",
        ".yml": "yaml", ".glsl": "glsl", ".vert": "glsl", ".frag": "glsl",
        ".shader": "glsl", ".toml": "toml",
    }.get(PurePosixPath(path).suffix.lower(), "text")


# 跳过项的中文后果说明（skipped_details.hint）。third_party_lib 的措辞为前后端契约，勿改。
_SKIP_HINT_THIRD_PARTY = "AI 无法直接使用该库，只能用原生 Canvas 重实现相关效果，还原度会打折"


def _skip_detail(path: str, reason: str, hint: str) -> dict:
    """skipped_details 单项：reason ∈ third_party_lib/unsupported_type/too_large/unsafe。"""
    return {"path": path, "reason": reason, "hint": hint}


def _expand_source_uploads(
    uploaded: list[tuple[str, bytes]],
) -> tuple[list[tuple[str, bytes]], list[str], list[dict]]:
    """展开普通多文件上传和单个源码 ZIP，并保留安全的相对路径。

    返回 (entries, errors, error_details)：errors 是给响应 skipped 的文案，
    error_details 是同一批跳过项的结构化清单（path/reason/hint）。
    """
    if len(uploaded) > _SOURCE_MAX_UPLOAD_PARTS:
        raise _SourceImportLimitError(f"上传文件数超过 {_SOURCE_MAX_UPLOAD_PARTS} 个")
    entries: list[tuple[str, bytes]] = []
    errors: list[str] = []
    error_details: list[dict] = []
    expanded_bytes = 0
    for filename, raw in uploaded:
        safe_name = _safe_source_path(filename)
        if not safe_name:
            errors.append(f"跳过不安全路径: {filename or '(空文件名)'}")
            error_details.append(_skip_detail(
                filename or "(空文件名)", "unsafe",
                "路径含越界或非法字符，为安全起见未导入，游戏无法引用该文件"))
            continue
        if PurePosixPath(safe_name).suffix.lower() != ".zip":
            if len(entries) >= _SOURCE_MAX_ARCHIVE_ENTRIES:
                raise _SourceImportLimitError(
                    f"源码项目文件数超过 {_SOURCE_MAX_ARCHIVE_ENTRIES} 个"
                )
            entries.append((safe_name, raw))
            expanded_bytes += len(raw)
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                infos = [info for info in zf.infolist() if not info.is_dir()]
                if len(infos) > _SOURCE_MAX_ARCHIVE_ENTRIES:
                    raise _SourceImportLimitError(
                        f"{safe_name}: ZIP 文件数超过 {_SOURCE_MAX_ARCHIVE_ENTRIES} 个"
                    )
                for info in infos:
                    inner = _safe_source_path(info.filename)
                    if not inner:
                        errors.append(f"{safe_name}: 跳过不安全路径 {info.filename}")
                        error_details.append(_skip_detail(
                            f"{safe_name}:{info.filename}", "unsafe",
                            "ZIP 内路径含越界或非法字符，为安全起见未导入，游戏无法引用该文件"))
                        continue
                    expanded_bytes += info.file_size
                    if expanded_bytes > _settings.max_upload_bytes * 3:
                        raise _SourceImportLimitError(f"{safe_name}: 解压后内容超过安全上限")
                    if len(entries) >= _SOURCE_MAX_ARCHIVE_ENTRIES:
                        raise _SourceImportLimitError(
                            f"源码项目文件数超过 {_SOURCE_MAX_ARCHIVE_ENTRIES} 个"
                        )
                    entries.append((inner, zf.read(info)))
        except zipfile.BadZipFile:
            errors.append(f"{safe_name}: 无效的 ZIP 文件")
            error_details.append(_skip_detail(
                safe_name, "unsupported_type",
                "ZIP 文件损坏无法解压，其中的内容完全没有导入"))
    if expanded_bytes > _settings.max_upload_bytes * 3:
        raise _SourceImportLimitError("源码项目展开后超过安全上限")
    return entries, errors, error_details


def _build_source_reference(
    uploaded: list[tuple[str, bytes]],
) -> tuple[list[dict], list[dict], dict, list[str], list[dict]]:
    """把源码项目整理为文本源码、可持久化二进制素材、摘要、跳过清单和跳过明细。

    skipped 是给人看的单行文案；skipped_details 是结构化契约字段
    （path/reason/hint，reason ∈ third_party_lib/unsupported_type/too_large/unsafe），
    归类沿用本函数既有的跳过判定，不新造规则。
    """
    entries, errors, skipped_details = _expand_source_uploads(uploaded)
    source_files: list[dict] = []
    asset_files: list[dict] = []
    path_statuses: dict[str, str] = {}
    skipped: list[str] = list(errors)
    total_text_bytes = 0
    seen: set[str] = set()

    for path, raw in entries:
        if path in seen:
            skipped.append(f"{path}: 重复路径")
            skipped_details.append(_skip_detail(
                path, "unsupported_type",
                "同名路径重复出现，仅保留第一份，后续副本未导入"))
            continue
        seen.add(path)
        pure = PurePosixPath(path)
        parts_lower = {part.lower() for part in pure.parts[:-1]}
        suffix = pure.suffix.lower()
        name_lower = pure.name.lower()
        if parts_lower & _SOURCE_IGNORED_DIRS:
            path_statuses[path] = "skipped_vendor"
            skipped.append(f"{path}: 第三方或构建目录")
            skipped_details.append(_skip_detail(path, "third_party_lib", _SKIP_HINT_THIRD_PARTY))
            continue
        if suffix in _SOURCE_ASSET_EXTS:
            kind = _source_asset_kind(suffix)
            if not kind:
                path_statuses[path] = "unsupported"
                skipped.append(f"{path}: 该资源类型不允许浏览器回源")
                skipped_details.append(_skip_detail(
                    path, "unsupported_type",
                    "该资源类型浏览器无法安全加载，最终游戏不能使用它"))
                continue
            if len(asset_files) >= _SOURCE_MAX_ASSET_PATHS:
                path_statuses[path] = "skipped"
                skipped.append(f"{path}: 资源文件数超过 {_SOURCE_MAX_ASSET_PATHS} 个")
                skipped_details.append(_skip_detail(
                    path, "too_large",
                    "资源数量超出上限未保存，游戏中会缺少这部分素材"))
                continue
            path_statuses[path] = "asset"
            asset_files.append({
                "path": path,
                "kind": kind,
                "size": len(raw),
                "content": raw,
            })
            continue
        if suffix not in _SOURCE_TEXT_EXTS:
            path_statuses[path] = "unsupported"
            skipped.append(f"{path}: 不支持的文件类型")
            skipped_details.append(_skip_detail(
                path, "unsupported_type",
                "不支持的文件类型，AI 不会读取该文件内容"))
            continue
        if name_lower.endswith((".min.js", ".min.css")) or name_lower in {
            "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock",
        }:
            path_statuses[path] = "skipped_vendor"
            skipped.append(f"{path}: 压缩库或锁文件")
            skipped_details.append(_skip_detail(path, "third_party_lib", _SKIP_HINT_THIRD_PARTY))
            continue
        if len(source_files) >= _SOURCE_MAX_FILES:
            path_statuses[path] = "skipped"
            skipped.append(f"{path}: 源码文件数超过 {_SOURCE_MAX_FILES} 个")
            skipped_details.append(_skip_detail(
                path, "too_large",
                "源码文件数超出上限，该文件未导入，AI 无法参考其内容"))
            continue
        if len(raw) > _SOURCE_MAX_FILE_BYTES:
            path_statuses[path] = "skipped"
            skipped.append(f"{path}: 单文件超过 {_SOURCE_MAX_FILE_BYTES // (1024 * 1024)}MB")
            skipped_details.append(_skip_detail(
                path, "too_large",
                "单文件超过大小上限未导入，AI 无法参考其内容"))
            continue
        if total_text_bytes + len(raw) > _SOURCE_MAX_TEXT_BYTES:
            path_statuses[path] = "skipped"
            skipped.append(f"{path}: 源码总量超过 {_SOURCE_MAX_TEXT_BYTES // (1024 * 1024)}MB")
            skipped_details.append(_skip_detail(
                path, "too_large",
                "源码总量超出上限，该文件未导入，AI 无法参考其内容"))
            continue
        text = _decode_source_text(raw)
        if text is None:
            path_statuses[path] = "skipped"
            skipped.append(f"{path}: 不是可读文本")
            skipped_details.append(_skip_detail(
                path, "unsupported_type",
                "文件不是可读文本，AI 无法参考其内容"))
            continue
        source_files.append({
            "path": path,
            "language": _source_language(path),
            "size": len(raw),
            "lines": text.count("\n") + 1,
            "content": text,
        })
        path_statuses[path] = "readable"
        total_text_bytes += len(raw)

    source_files.sort(key=lambda item: item["path"].lower())
    asset_files.sort(key=lambda item: item["path"].lower())
    enrich_source_asset_metadata(source_files, asset_files)
    html_entries = [
        item["path"] for item in source_files
        if PurePosixPath(item["path"]).name.lower() in {"index.html", "index.htm"}
    ]
    entrypoint = (
        min(html_entries, key=lambda path: (len(PurePosixPath(path).parts), path.lower()))
        if html_entries else None
    )
    web_bundle = build_source_web_manifest(source_files, entrypoint, path_statuses)
    web_dependencies = web_bundle.get("dependencies") or []
    summary = {
        "asset_metadata_version": SOURCE_ASSET_METADATA_VERSION,
        "source_file_count": len(source_files),
        "asset_file_count": len(asset_files),
        "usable_asset_count": len(asset_files),
        "asset_bytes": sum(item["size"] for item in asset_files),
        "source_bytes": total_text_bytes,
        "skipped_count": len(skipped),
        "entrypoint": entrypoint,
        "web_dependency_count": len(web_dependencies),
        "web_readable_dependency_count": sum(
            1 for item in web_dependencies if item.get("status") == "readable"
        ),
        "web_bundle": web_bundle,
    }
    return source_files, asset_files, summary, skipped, skipped_details


async def _read_source_uploads(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    if len(files) > _SOURCE_MAX_UPLOAD_PARTS:
        raise HTTPException(413, f"源码上传文件数超过 {_SOURCE_MAX_UPLOAD_PARTS} 个上限")
    uploaded: list[tuple[str, bytes]] = []
    total = 0
    for file in files:
        raw = await file.read()
        total += len(raw)
        if total > _settings.max_upload_bytes:
            raise HTTPException(
                413,
                f"源码上传总量超过 {_settings.max_upload_bytes // (1024 * 1024)}MB 上限",
            )
        uploaded.append((file.filename or "", raw))
    return uploaded


def _source_skill_record(
    name: str,
    description: str,
    content: str,
    source_files: list[dict],
    source_assets: list[dict],
    source_asset_bundle_id: str | None,
    summary: dict,
) -> dict:
    notes = content.strip() or (
        "这是一个完整游戏项目的源码参考。先查看源码清单，按需读取入口文件和核心逻辑；"
        "复用玩法、架构和交互模式；项目内已保存的图片、音频和字体可通过资源清单按需使用。"
    )
    return {
        "name": name.strip(),
        "description": description.strip(),
        "content": notes,
        "source_files": source_files,
        "asset_paths": [item["path"] for item in source_assets],
        "source_assets": source_assets,
        "source_asset_bundle_id": source_asset_bundle_id,
        "source_summary": summary,
    }


def _ensure_source_skill_asset_metadata(skill: dict) -> bool:
    """Best-effort metadata backfill for source skills saved before this metadata layer."""
    assets = skill.get("source_assets") or []
    if not assets:
        return False
    source_summary = skill.setdefault("source_summary", {})
    if source_summary.get("asset_metadata_version") == SOURCE_ASSET_METADATA_VERSION:
        return False
    bundle_id = str(skill.get("source_asset_bundle_id") or "")
    if not _SOURCE_ASSET_BUNDLE_RE.fullmatch(bundle_id):
        return False
    bundle_dir = _source_asset_root() / bundle_id

    def load_content(asset: dict) -> bytes | None:
        stored_name = str(asset.get("stored_name") or "")
        if not stored_name:
            url_name = PurePosixPath(str(asset.get("url") or "")).name
            stored_name = url_name
        if not _SAFE_FILENAME.fullmatch(stored_name):
            return None
        path = bundle_dir / stored_name
        if not path.is_file():
            return None
        return path.read_bytes()

    enrich_source_asset_metadata(
        skill.get("source_files") or [],
        assets,
        content_loader=load_content,
    )
    source_summary["asset_metadata_version"] = SOURCE_ASSET_METADATA_VERSION
    return True


def backfill_source_asset_metadata() -> int:
    """Enrich all legacy source skills in memory; return changed skill count."""
    changed = 0
    for skill in SKILLS:
        if _ensure_source_skill_asset_metadata(skill):
            changed += 1
    return changed


def _public_skill(skill: dict, *, detail: bool = False) -> dict:
    _ensure_source_skill_asset_metadata(skill)
    source_summary = skill.get("source_summary") or {}
    payload = {
        "name": skill["name"],
        "description": skill.get("description", ""),
        "source_file_count": len(skill.get("source_files") or []),
        "asset_file_count": len(skill.get("asset_paths") or []),
        "usable_asset_count": len(skill.get("source_assets") or []),
        "web_dependency_count": source_summary.get("web_dependency_count", 0),
    }
    if detail:
        payload["content"] = skill.get("content", "")
        payload["source_files"] = [
            {key: item.get(key) for key in ("path", "language", "size", "lines")}
            for item in skill.get("source_files") or []
        ]
        payload["asset_paths"] = skill.get("asset_paths") or []
        payload["source_assets"] = [
            {
                key: item.get(key)
                for key in (
                    "path", "file_name", "kind", "size", "url",
                    *SOURCE_ASSET_METADATA_KEYS,
                )
                if key in item or key in {"path", "file_name", "kind", "size", "url"}
            }
            for item in skill.get("source_assets") or []
        ]
        payload["source_summary"] = source_summary
    return payload


# Source skills are loaded by game_agent before this route module.  Backfill once at
# import so tools sharing the same SKILLS objects can immediately see the metadata;
# _sync_skills persists it on the next skill mutation.
backfill_source_asset_metadata()

@router.get("/api/skills")
async def list_skills():
    """列出所有技能"""
    return [_public_skill(s) for s in SKILLS]

@router.post("/api/skills")
async def add_skill(req: SkillCreateRequest):
    """添加自定义技能"""
    async with _skills_mutation_lock:
        if any(s["name"] == req.name for s in SKILLS):
            raise HTTPException(400, f"技能 '{req.name}' 已存在")
        record = {"name": req.name, "description": req.description, "content": req.content}
        SKILLS.append(record)
        try:
            await asyncio.to_thread(_sync_skills)  # 磁盘写入放线程
        except Exception:
            SKILLS.remove(record)
            raise
    return {"ok": True, "name": req.name}


@router.put("/api/skills/{skill_name}")
async def update_skill(skill_name: str, req: SkillUpdateRequest):
    """更新技能说明，同时保留已上传的源码参考。"""
    async with _skills_mutation_lock:
        for s in SKILLS:
            if s["name"] == skill_name:
                old_description = s.get("description", "")
                old_content = s.get("content", "")
                s["description"] = req.description
                s["content"] = req.content
                try:
                    await asyncio.to_thread(_sync_skills)
                except Exception:
                    s["description"] = old_description
                    s["content"] = old_content
                    raise
                return {"ok": True, "name": skill_name}
    raise HTTPException(404, f"技能 '{skill_name}' 不存在")


async def _save_source_skill(
    *,
    name: str,
    description: str,
    content: str,
    files: list[UploadFile],
    replace: bool,
) -> dict:
    clean_name = name.strip()
    clean_description = description.strip()
    if not clean_name or not clean_description:
        raise HTTPException(400, "技能名称和描述不能为空")
    if len(clean_name) > 100 or len(clean_description) > 500:
        raise HTTPException(400, "技能名称最多 100 个字符，描述最多 500 个字符")
    if len(content) > 100000:
        raise HTTPException(400, "技能补充说明最多 100000 个字符")
    if not files:
        raise HTTPException(400, "请选择源码文件夹或源码 ZIP")

    uploaded = await _read_source_uploads(files)
    try:
        source_files, asset_files, summary, skipped, skipped_details = await asyncio.to_thread(
            _build_source_reference, uploaded
        )
    except _SourceImportLimitError as exc:
        raise HTTPException(413, str(exc)) from exc
    if not source_files:
        detail = skipped[0] if skipped else "没有找到可读的源码文件"
        raise HTTPException(400, f"没有导入源码：{detail}")
    bundle_id, source_assets = await asyncio.to_thread(
        _persist_source_asset_bundle, asset_files
    )
    record = _source_skill_record(
        clean_name,
        clean_description,
        content,
        source_files,
        source_assets,
        bundle_id,
        summary,
    )
    committed = False
    try:
        async with _skills_mutation_lock:
            existing_index = next(
                (i for i, s in enumerate(SKILLS) if s["name"] == clean_name),
                None,
            )
            if replace and existing_index is None:
                raise HTTPException(404, f"技能 '{clean_name}' 不存在")
            if not replace and existing_index is not None:
                raise HTTPException(400, f"技能 '{clean_name}' 已存在")
            old_record = SKILLS[existing_index] if replace else None
            try:
                if replace:
                    SKILLS[existing_index] = record
                else:
                    SKILLS.append(record)
                await asyncio.to_thread(_sync_skills)
            except Exception:
                if replace:
                    SKILLS[existing_index] = old_record
                elif record in SKILLS:
                    SKILLS.remove(record)
                raise
            committed = True
    except Exception:
        if not committed:
            await asyncio.to_thread(_delete_unreferenced_source_asset_bundle, bundle_id)
        raise
    # 已生成/保存的游戏会直接引用 bundle URL。替换技能后保留旧包，避免历史项目丢图；
    # 只有提交持久化失败、URL 从未对调用方生效时才回滚删除新包。
    return {
        "ok": True,
        "name": clean_name,
        **summary,
        "skipped": skipped[:50],
        # 结构化跳过明细（与 skipped 同源同序，截断上限保持一致）
        "skipped_details": skipped_details[:50],
    }


@router.post("/api/skills/source")
async def add_source_skill(
    name: str = Form(...),
    description: str = Form(...),
    content: str = Form(""),
    files: list[UploadFile] = File(...),
):
    """把一个源码文件夹或 ZIP 作为一个游戏参考技能导入。"""
    return await _save_source_skill(
        name=name, description=description, content=content, files=files, replace=False
    )


@router.put("/api/skills/{skill_name}/source")
async def replace_source_skill(
    skill_name: str,
    description: str = Form(...),
    content: str = Form(""),
    files: list[UploadFile] = File(...),
):
    """替换现有技能的源码参考。"""
    return await _save_source_skill(
        name=skill_name,
        description=description,
        content=content,
        files=files,
        replace=True,
    )

@router.delete("/api/skills/{skill_name}")
async def delete_skill(skill_name: str):
    """删除技能"""
    async with _skills_mutation_lock:
        for i, s in enumerate(SKILLS):
            if s["name"] == skill_name:
                removed = SKILLS.pop(i)
                try:
                    await asyncio.to_thread(_sync_skills)  # 磁盘写入放线程
                except Exception:
                    SKILLS.insert(i, removed)
                    raise
                # 素材 URL 可能已写入其它已保存项目；删除技能仅移除参考记录，不破坏历史游戏。
                return {"ok": True}
    raise HTTPException(404, f"技能 '{skill_name}' 不存在")

@router.get("/api/skills/{skill_name}")
async def get_skill(skill_name: str):
    """获取技能完整内容"""
    for s in SKILLS:
        if s["name"] == skill_name:
            return _public_skill(s, detail=True)
    raise HTTPException(404, f"技能 '{skill_name}' 不存在")


def _parse_skills_zip(content: bytes) -> tuple[list[dict], list[str], dict[str, int]]:
    """解压 + 解码 + 解析 ZIP 里的技能（CPU/内存密集，在线程池里调用）。

    返回 (parsed, errors, stats)：单个坏 JSON 不让整个导入失败——逐文件捕获、收集错误信息。
    stats 按扩展名统计 md/json/html/js/css 文件数，供导入端点识别"误传源码项目"场景。
    整体损坏时抛 zipfile.BadZipFile，由调用方转成 400。
    """
    parsed: list[dict] = []
    errors: list[str] = []
    stats = {"md": 0, "json": 0, "html": 0, "js": 0, "css": 0}
    # .htm 与 .html 同义；.mjs/.cjs 也是脚本文件，一并计入 js（识别更稳）
    _stat_key = {"md": "md", "json": "json", "html": "html", "htm": "html",
                 "js": "js", "mjs": "js", "cjs": "js", "css": "css"}
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for name in zf.namelist():
            # 跳过目录和隐藏文件
            if name.endswith('/') or '/__MACOSX' in name or name.startswith('.'):
                continue
            ext = name.rsplit('.', 1)[-1].lower()
            if ext in _stat_key:
                stats[_stat_key[ext]] += 1
            if ext not in ('md', 'json'):
                continue  # 只有技能文档才需要解压解析，源码文件仅计数
            raw = zf.read(name).decode('utf-8', errors='ignore')

            if ext == 'md':
                skill_name = name.rsplit('/', 1)[-1].replace('.md', '').replace(' ', '_')
                lines = raw.split('\n')
                description = skill_name
                skill_content = raw
                if lines[0].startswith('#'):
                    description = lines[0].lstrip('#').strip()
                    skill_content = '\n'.join(lines[1:]).strip()
                parsed.append({"name": skill_name, "description": description, "content": skill_content})

            elif ext == 'json':
                try:
                    data = json.loads(raw)
                except ValueError as e:
                    errors.append(f"{name}: JSON 解析失败（{e}）")
                    continue
                items = data if isinstance(data, list) else [data]
                for s in items:
                    if not isinstance(s, dict):
                        errors.append(f"{name}: 跳过非对象条目")
                        continue
                    if not s.get("name") or not s.get("content"):
                        continue
                    parsed.append({
                        "name": s["name"],
                        "description": s.get("description", s["name"]),
                        "content": s["content"],
                    })
    return parsed, errors, stats


@router.post("/api/skills/import")
async def import_skills_zip(file: UploadFile = File(...)):
    """从 ZIP 文件批量导入技能（每个 .md 文件 = 一个技能，每个 .json 文件 = 一个或多个技能）。

    单个损坏的 JSON 只跳过该文件并记入 errors，不影响其余导入；
    只要请求走到导入阶段就调用 _sync_skills 持久化，避免内存/磁盘/prompt 三态不一致。
    """
    if not file.filename or not file.filename.endswith('.zip'):
        raise HTTPException(400, "请上传 .zip 文件")
    content = await file.read()
    if len(content) > _settings.max_upload_bytes:
        raise HTTPException(413, f"文件超过 {_settings.max_upload_bytes // (1024 * 1024)}MB 上限")
    try:
        # 解压/解码/JSON 解析是 CPU 密集操作，放线程别卡事件循环
        parsed, errors, stats = await asyncio.to_thread(_parse_skills_zip, content)
    except zipfile.BadZipFile as e:
        raise HTTPException(400, "无效的 ZIP 文件") from e

    # 防呆：纯源码项目特征（有 HTML + JS/CSS、没有任何可导入技能文档）→ 明确拒绝，
    # 引导走「源码 ZIP」入口，而不是导入 0 条技能让用户误以为成功
    has_source_files = stats["html"] >= 1 and (stats["js"] + stats["css"]) >= 1
    if has_source_files and stats["md"] + stats["json"] == 0:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SOURCE_PROJECT_DETECTED",
                "message": "检测到这是一个源码项目 ZIP（含 HTML/JS/CSS，无 .md/.json 技能文档）。"
                           "技能包入口只导入技能文档；要把游戏源码作为参考，请使用「源码 ZIP」导入入口。",
                "stats": {"html": stats["html"], "js": stats["js"], "css": stats["css"]},
            },
        )

    async with _skills_mutation_lock:
        existing_names = {s["name"] for s in SKILLS}
        added_records = []
        for s in parsed:
            if s["name"] in existing_names:
                continue
            SKILLS.append(s)
            added_records.append(s)
            existing_names.add(s["name"])
        try:
            await asyncio.to_thread(_sync_skills)  # 部分导入成功也要持久化；磁盘写入放线程
        except Exception:
            for record in added_records:
                if record in SKILLS:
                    SKILLS.remove(record)
            raise
        added = len(added_records)
    result: dict = {"ok": True, "added": added, "errors": errors}
    # 混合包（技能文档 + 源码文件同在）：照旧导入文档，但把"源码被忽略"讲明白
    if (stats["md"] + stats["json"]) and (stats["html"] + stats["js"] + stats["css"]):
        result["warnings"] = ["检测到源码文件已忽略，如需源码参考请用 源码 ZIP 入口"]
    return result


class SkillScanRequest(BaseModel):
    path: str  # 本地文件夹路径

@router.post("/api/skills/scan")
async def scan_skills_folder(req: SkillScanRequest):
    """扫描本地文件夹，找到所有 SKILL.md 文件并导入（本地管理操作）。

    安全限制：仅允许扫描项目 skills_dir 或用户家目录下的路径；
    拒绝系统目录（/etc, /tmp, C:\\Windows 等）以防止本地文件读取。

    注意：这是面向本机管理员的便捷功能（默认 HOST=127.0.0.1）。若把服务对外暴露，
    务必同时设置 API_TOKEN —— 否则任意客户端都能让服务端遍历目录、读取 SKILL.md。
    """
    import os
    import re as re_mod
    folder_raw = req.path.strip()
    folder = os.path.realpath(folder_raw)

    if not os.path.isdir(folder):
        raise HTTPException(400, f"路径不存在: {folder}")

    # 安全：仅允许扫描项目 skills_dir 或用户家目录；拒绝系统目录
    allowed_roots = [
        os.path.realpath(_settings.skills_dir),
        os.path.realpath(os.path.expanduser("~")),
    ]
    if not any(os.path.commonpath([folder, root]) == root for root in allowed_roots):
        raise HTTPException(
            403,
            "出于安全考虑，仅允许扫描项目技能目录和用户家目录。提供的路径不在允许范围内。",
        )

    _MAX_DIRS, _MAX_FOUND = 5000, 500  # 遍历上限：防超大目录树拖垮（DoS）

    def _scan():
        found, dirs_seen = [], 0
        for root, _dirs, files in os.walk(folder):
            dirs_seen += 1
            if dirs_seen > _MAX_DIRS or len(found) >= _MAX_FOUND:
                break
            for fname in files:
                if fname.upper() != 'SKILL.MD':
                    continue
                filepath = os.path.join(root, fname)
                try:
                    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                        raw = f.read()
                    skill_name = os.path.basename(root).replace(' ', '_')
                    description = skill_name
                    content = raw
                    fm_match = re_mod.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', raw, re_mod.DOTALL)
                    if fm_match:
                        fm_text = fm_match.group(1)
                        content = fm_match.group(2).strip()
                        for line in fm_text.split('\n'):
                            if line.startswith('name:'):
                                skill_name = line.split(':', 1)[1].strip().strip('"').strip("'")
                            elif line.startswith('description:'):
                                description = line.split(':', 1)[1].strip().strip('"').strip("'")
                    found.append({"name": skill_name, "description": description,
                                  "content": content, "source": filepath})
                    if len(found) >= _MAX_FOUND:
                        break
                except Exception:
                    continue
        return found

    found = await asyncio.to_thread(_scan)  # 遍历/读盘放线程，别阻塞事件循环

    if not found:
        raise HTTPException(404, f"在 {folder} 中未找到 SKILL.md 文件")

    # 导入（跳过重名）；提交阶段串行化，避免扫描期间其它请求新增同名技能。
    async with _skills_mutation_lock:
        existing_names = {item["name"] for item in SKILLS}
        skipped = []
        added_records = []
        for s in found:
            if s["name"] in existing_names:
                skipped.append(s["name"])
                continue
            record = {
                "name": s["name"],
                "description": s["description"],
                "content": s["content"],
            }
            SKILLS.append(record)
            added_records.append(record)
            existing_names.add(s["name"])
        try:
            await asyncio.to_thread(_sync_skills)  # 磁盘写入放线程
        except Exception:
            for record in added_records:
                if record in SKILLS:
                    SKILLS.remove(record)
            raise
        added = len(added_records)
    return {"ok": True, "added": added, "skipped": skipped, "total_found": len(found)}
