"""AI 游戏设计智能体 - 主应用入口"""

import sys
import os
from pathlib import Path

# 修复 Windows 控制台编码
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 最先加载 .env 到系统环境变量（在所有 import 之前）
from dotenv import load_dotenv
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH, override=True)

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.middleware.cors import CORSMiddleware

from src.config import settings
from src.api.routes import router, kb
from src.knowledge.phaser_skills import load_default_skills

# 创建 FastAPI 应用
app = FastAPI(
    title=settings.project_name,
    version=settings.version,
    description="基于知识库的 AI 游戏设计智能体",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件 & 模板 - 使用绝对路径
from pathlib import Path
_BASE = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(_BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(_BASE / "templates"))

# 注册路由
app.include_router(router)


@app.on_event("startup")
async def startup():
    """启动时加载默认知识 + 自动恢复素材索引"""
    # 1. 加载/更新技能文档
    count = load_default_skills(kb)
    print(f"[Startup] Loaded {count} skill docs")

    # 2. 自动恢复：扫描 data/assets/ 把已有文件重建索引（ChromaDB 被清空时救命用）
    rebuilt = kb.rebuild_assets_index()
    if rebuilt > 0:
        print(f"[Startup] Rebuilt {rebuilt} asset records from disk")

    stats = kb.get_stats()
    print(f"[Startup] KB stats: {stats}")


@app.get("/")
async def index(request: Request):
    """主页"""
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/preview")
async def preview_page(request: Request):
    """游戏预览页面"""
    return templates.TemplateResponse(request=request, name="preview.html")


if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
