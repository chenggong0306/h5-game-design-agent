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

# 可选：使用系统证书存储，改善 Windows / 企业代理环境下的 LangSmith HTTPS 证书问题
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass


from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.middleware.cors import CORSMiddleware

from src.config import settings
from src.api.routes import router, kb
from src.agent.game_agent import SKILLS


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时恢复素材索引 + 打印技能/知识库状态（取代已弃用的 @app.on_event）。"""
    # 1. 自动恢复：扫描 data/assets/ 把已有文件重建索引（ChromaDB 被清空时救命用）
    rebuilt = kb.rebuild_assets_index()
    if rebuilt > 0:
        print(f"[Startup] Rebuilt {rebuilt} asset records from disk")

    # 2. 打印技能状态（内置 + 自定义已在 game_agent 模块加载时恢复）
    from src.knowledge.phaser_skills import H5_GAME_SKILLS
    builtin_names = {s["category"] for s in H5_GAME_SKILLS}
    builtin = sum(1 for s in SKILLS if s["name"] in builtin_names)
    custom = len(SKILLS) - builtin
    print(f"[Startup] Skills: {builtin} builtin + {custom} custom = {len(SKILLS)} total")
    print(f"[Startup] KB stats: {kb.get_stats()}")

    yield


# 创建 FastAPI 应用
app = FastAPI(
    title=settings.project_name,
    version=settings.version,
    description="基于知识库的 AI 游戏设计智能体",
    lifespan=lifespan,
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


@app.get("/")
async def index(request: Request):
    """主页"""
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/preview")
async def preview_page(request: Request):
    """游戏预览页面"""
    return templates.TemplateResponse(request=request, name="preview.html")



@app.get("/favicon.ico")
async def favicon():
    """屏蔽浏览器默认 favicon 请求"""
    return Response(status_code=204)


@app.get("/.well-known/appspecific/com.chrome.devtools.json")
async def chrome_devtools_config():
    """屏蔽 Chrome DevTools 自动探测请求"""
    return Response(status_code=204)


if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
