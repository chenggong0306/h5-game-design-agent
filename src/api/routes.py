"""FastAPI 后端路由 - 游戏设计 API"""

import os
import json
import uuid
import tempfile

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from starlette.responses import StreamingResponse
from pydantic import BaseModel

from src.knowledge.knowledge_base import KnowledgeBase
from src.agent.game_agent import GameDesignAgent

router = APIRouter()

# 全局实例
kb = KnowledgeBase()
agent = GameDesignAgent(kb)


# ============ 请求/响应模型 ============

class ChatRequest(BaseModel):
    session_id: str = ""
    message: str
    current_code: str = ""


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
        result = await agent.chat(
            session_id=session_id,
            user_message=req.message,
            current_code=req.current_code,
        )
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
        # 先发送 session_id
        yield f"data: {json.dumps({'type': 'session', 'session_id': session_id}, ensure_ascii=False)}\n\n"
        try:
            async for chunk in agent.chat_stream(
                session_id=session_id,
                user_message=req.message,
                current_code=req.current_code,
            ):
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


@router.delete("/api/chat/{session_id}")
async def clear_chat(session_id: str):
    """清除对话历史"""
    agent.clear_session(session_id)
    return {"ok": True}


# ============ 素材 API ============

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


# ============ 知识库 API ============

@router.get("/api/knowledge/stats")
async def knowledge_stats():
    """知识库统计"""
    return kb.get_stats()


@router.get("/api/knowledge/search")
async def search_knowledge(q: str, category: str | None = None, top_k: int = 5):
    """搜索知识库"""
    return kb.search_skills(q, category, top_k)
