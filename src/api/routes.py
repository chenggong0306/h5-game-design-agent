"""FastAPI 后端路由 - 游戏设计 API"""

import base64
import json
import os
import tempfile
import uuid

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from starlette.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.knowledge.knowledge_base import KnowledgeBase
from src.agent.game_agent import GameDesignAgent, SKILLS, _save_custom_skills

router = APIRouter()

# 全局实例
kb = KnowledgeBase()
agent = GameDesignAgent(kb)


# ============ 请求/响应模型 ============

CHAT_HISTORY_DIR = Path(__file__).resolve().parents[2] / "data" / "chat_history"
CHAT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def _history_path(session_id: str) -> Path:
    return CHAT_HISTORY_DIR / f"{session_id}.json"


def _load_chat_history(session_id: str) -> list[dict]:
    path = _history_path(session_id)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_chat_history(session_id: str, history: list[dict]) -> None:
    path = _history_path(session_id)
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_chat_history(session_id: str, role: str, content: str, extra: dict | None = None) -> None:
    history = _load_chat_history(session_id)
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
    history = _load_chat_history(session_id)
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
        except Exception:
            raise HTTPException(status_code=400, detail=f"图片 {image.name} base64 无效")
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
    images: list[ChatImage] = Field(default_factory=list)



class ChatResponse(BaseModel):
    session_id: str
    reply: str
    code: str | None = None
    action: str = "chat"


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
    try:
        images = _validate_chat_images(req.images)
        user_content = _build_user_message_content(req.message, images)
        result = await agent.chat(
            session_id=session_id,
            user_message=user_content,
            current_code=req.current_code,
        )
        _append_chat_history(session_id, "user", req.message, {"images": _image_history_meta(images)} if images else None)
        _append_chat_history(session_id, "ai", result.get("reply", ""), {"code": result.get("code") or ""})
        return ChatResponse(session_id=session_id, **result)
    except Exception as e:
        err_str = str(e)
        # 友好的错误提示
        if "401" in err_str or "api_key" in err_str.lower() or "auth" in err_str.lower():
            detail = "API Key 无效或已过期，请在 .env 文件中检查 OPENAI_API_KEY 配置"
        elif "timeout" in err_str.lower() or "timed out" in err_str.lower():
            detail = "AI 服务响应超时，请检查网络连接或稍后重试"
        elif "connect" in err_str.lower():
            detail = "无法连接到 AI 服务，请检查网络和 OPENAI_BASE_URL 配置"
        else:
            detail = f"AI 对话失败: {err_str}"
        raise HTTPException(status_code=500, detail=detail)


@router.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """流式对话 - SSE (Server-Sent Events)"""
    session_id = req.session_id or str(uuid.uuid4())

    async def event_generator():
        assistant_text = ""
        latest_code = ""
        images = _validate_chat_images(req.images)
        user_content = _build_user_message_content(req.message, images)
        _append_chat_history(session_id, "user", req.message, {"images": _image_history_meta(images)} if images else None)
        # 先发送 session_id
        yield f"data: {json.dumps({'type': 'session', 'session_id': session_id}, ensure_ascii=False)}\n\n"
        try:
            async for chunk in agent.chat_stream(
                session_id=session_id,
                user_message=user_content,
                current_code=req.current_code,
            ):
                if chunk.get("type") == "token":
                    assistant_text += chunk.get("content", "")
                elif chunk.get("type") == "code_update":
                    latest_code = chunk.get("code") or latest_code
                elif chunk.get("type") == "done":
                    latest_code = chunk.get("code") or latest_code
                    if assistant_text.strip():
                        _append_chat_history(session_id, "ai", assistant_text, {"code": latest_code})
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
    sessions = []
    for path in sorted(CHAT_HISTORY_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        session_id = path.stem
        sessions.append(_build_session_summary(session_id))
    return sessions


@router.get("/api/chat/{session_id}/history")
async def get_chat_history(session_id: str):
    """获取指定会话的消息历史，并返回该会话最后一次代码快照。"""
    history = _load_chat_history(session_id)
    return {
        "session_id": session_id,
        "messages": history,
        "latest_code": _get_latest_code_from_history(history),
    }


@router.delete("/api/chat/{session_id}")
async def clear_chat(session_id: str):
    """清除对话历史"""
    await agent.clear_session(session_id)
    path = _history_path(session_id)
    if path.exists():
        path.unlink()
    return {"ok": True}




@router.post("/api/assets/upload")
async def upload_asset(
    file: UploadFile = File(...),
    asset_type: str = Form("image"),
    description: str = Form(""),
    tags: str = Form(""),
):
    """上传游戏素材"""
    # 保存临时文件
    suffix = os.path.splitext(file.filename or "file")[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        result = kb.upload_asset(
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
        os.unlink(tmp_path)


@router.get("/api/assets")
async def list_assets(asset_type: str | None = None):
    """列出素材"""
    return kb.list_assets(asset_type)


@router.get("/api/assets/search")
async def search_assets(q: str, asset_type: str | None = None, top_k: int = 5):
    """搜索素材"""
    return kb.search_assets(q, asset_type, top_k)


@router.delete("/api/assets/{asset_id}")
async def delete_asset(asset_id: str):
    """删除素材"""
    ok = kb.delete_asset(asset_id)
    if not ok:
        raise HTTPException(status_code=404, detail="素材不存在")
    return {"ok": True}


@router.get("/api/assets/file/{asset_type}/{filename}")
async def serve_asset(asset_type: str, filename: str):
    """提供素材文件访问（API路径）"""
    from src.config import settings
    file_path = os.path.join(settings.assets_dir, asset_type, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(file_path)


@router.get("/assets/{asset_type}/{filename}")
async def serve_asset_short(asset_type: str, filename: str):
    """提供素材文件访问（短路径，供游戏代码引用）"""
    from src.config import settings
    file_path = os.path.join(settings.assets_dir, asset_type, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(file_path)


# ============ 项目 API ============

@router.post("/api/projects", response_model=ProjectResponse)
async def save_project(req: ProjectSaveRequest):
    """保存项目"""
    project_id = kb.save_project(
        project_id=req.project_id,
        name=req.name,
        code=req.code,
        config=req.config,
    )
    return ProjectResponse(project_id=project_id, name=req.name, code=req.code, config=req.config)


@router.get("/api/projects")
async def list_projects():
    """列出项目"""
    return kb.list_projects()


@router.get("/api/projects/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: str):
    """获取项目"""
    project = kb.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return ProjectResponse(**project)


@router.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    """删除项目"""
    ok = kb.delete_project(project_id)
    if not ok:
        raise HTTPException(status_code=404, detail="项目不存在")
    return {"ok": True}


# ============ Skills API ============

def _sync_skills():
    """更新 SkillMiddleware 的 prompt 缓存 + 持久化到磁盘"""
    import src.agent.game_agent as ga
    ga._skills_prompt = "\n".join(f"- **{s['name']}**: {s['description']}" for s in SKILLS)
    _save_custom_skills()

class SkillCreateRequest(BaseModel):
    name: str
    description: str
    content: str

@router.get("/api/skills")
async def list_skills():
    """列出所有技能"""
    return [{"name": s["name"], "description": s["description"]} for s in SKILLS]

@router.post("/api/skills")
async def add_skill(req: SkillCreateRequest):
    """添加自定义技能"""
    for s in SKILLS:
        if s["name"] == req.name:
            raise HTTPException(400, f"技能 '{req.name}' 已存在")
    SKILLS.append({"name": req.name, "description": req.description, "content": req.content})
    _sync_skills()
    return {"ok": True, "name": req.name}

@router.delete("/api/skills/{skill_name}")
async def delete_skill(skill_name: str):
    """删除技能"""
    for i, s in enumerate(SKILLS):
        if s["name"] == skill_name:
            SKILLS.pop(i)
            _sync_skills()
            return {"ok": True}
    raise HTTPException(404, f"技能 '{skill_name}' 不存在")

@router.get("/api/skills/{skill_name}")
async def get_skill(skill_name: str):
    """获取技能完整内容"""
    for s in SKILLS:
        if s["name"] == skill_name:
            return s
    raise HTTPException(404, f"技能 '{skill_name}' 不存在")


@router.post("/api/skills/import")
async def import_skills_zip(file: UploadFile = File(...)):
    """从 ZIP 文件批量导入技能（每个 .md 文件 = 一个技能，每个 .json 文件 = 一个或多个技能）"""
    import zipfile, io, json as json_mod
    if not file.filename or not file.filename.endswith('.zip'):
        raise HTTPException(400, "请上传 .zip 文件")
    content = await file.read()
    added = 0
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            for name in zf.namelist():
                # 跳过目录和隐藏文件
                if name.endswith('/') or '/__MACOSX' in name or name.startswith('.'):
                    continue
                ext = name.rsplit('.', 1)[-1].lower()
                raw = zf.read(name).decode('utf-8', errors='ignore')

                if ext == 'md':
                    skill_name = name.rsplit('/', 1)[-1].replace('.md', '').replace(' ', '_')
                    lines = raw.split('\n')
                    description = skill_name
                    skill_content = raw
                    if lines[0].startswith('#'):
                        description = lines[0].lstrip('#').strip()
                        skill_content = '\n'.join(lines[1:]).strip()
                    if any(s["name"] == skill_name for s in SKILLS):
                        continue
                    SKILLS.append({"name": skill_name, "description": description, "content": skill_content})
                    added += 1

                elif ext == 'json':
                    data = json_mod.loads(raw)
                    items = data if isinstance(data, list) else [data]
                    for s in items:
                        if not s.get("name") or not s.get("content"):
                            continue
                        if any(existing["name"] == s["name"] for existing in SKILLS):
                            continue
                        SKILLS.append({
                            "name": s["name"],
                            "description": s.get("description", s["name"]),
                            "content": s["content"],
                        })
                        added += 1
    except zipfile.BadZipFile:
        raise HTTPException(400, "无效的 ZIP 文件")

    _sync_skills()
    return {"ok": True, "added": added}


class SkillScanRequest(BaseModel):
    path: str  # 本地文件夹路径

@router.post("/api/skills/scan")
async def scan_skills_folder(req: SkillScanRequest):
    """扫描本地文件夹，找到所有 SKILL.md 文件并导入"""
    import os, re as re_mod
    folder = req.path.strip()
    if not os.path.isdir(folder):
        raise HTTPException(400, f"路径不存在: {folder}")

    found = []
    for root, dirs, files in os.walk(folder):
        for fname in files:
            if fname.upper() == 'SKILL.MD':
                filepath = os.path.join(root, fname)
                try:
                    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                        raw = f.read()
                    # 解析 YAML frontmatter
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
                    found.append({
                        "name": skill_name,
                        "description": description,
                        "content": content,
                        "source": filepath,
                    })
                except Exception:
                    continue

    if not found:
        raise HTTPException(404, f"在 {folder} 中未找到 SKILL.md 文件")

    # 导入（跳过重名）
    added = 0
    skipped = []
    for s in found:
        if any(existing["name"] == s["name"] for existing in SKILLS):
            skipped.append(s["name"])
            continue
        SKILLS.append({"name": s["name"], "description": s["description"], "content": s["content"]})
        added += 1

    _sync_skills()
    return {"ok": True, "added": added, "skipped": skipped, "total_found": len(found)}