"""AI 游戏设计智能体 - 主应用入口"""

import sys
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

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.middleware.cors import CORSMiddleware

from src.config import settings
from src.api.routes import router, kb, agent, cleanup_source_asset_temp_dirs
from src.agent.game_agent import SKILLS


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时恢复素材索引 + 打印技能/知识库状态（取代已弃用的 @app.on_event）。"""
    cleaned_source_temps = cleanup_source_asset_temp_dirs()
    if cleaned_source_temps:
        print(f"[Startup] Cleaned {cleaned_source_temps} incomplete source asset bundles")

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

    # 关停：显式关闭 checkpointer 的 aiosqlite 连接，避免 "Event loop is closed" 噪音 / 句柄泄漏
    conn = getattr(agent, "checkpoint_conn", None)
    if conn is not None:
        try:
            await conn.close()
        except Exception:
            pass

    # 关停自检常驻的无头浏览器（未启用/未启动过则为空操作）
    try:
        from src.agent.verifier import aclose_browser
        await aclose_browser()
    except Exception:
        pass


# 创建 FastAPI 应用
app = FastAPI(
    title=settings.project_name,
    version=settings.version,
    description="基于知识库的 AI 游戏设计智能体",
    lifespan=lifespan,
)

# CORS：本地工具只允许同源（localhost）调用；不使用凭据，去掉 "*"+credentials 的无效组合
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        f"http://localhost:{settings.port}",
        f"http://127.0.0.1:{settings.port}",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件 & 模板 - 使用绝对路径
_BASE = Path(__file__).resolve().parent


class NoCacheStaticFiles(StaticFiles):
    """静态资源加 Cache-Control: no-cache：浏览器每次回源做 ETag/Last-Modified 协商（命中返 304），
    杜绝启发式缓存拿旧文件——即使 ?v= 版本串失效也最多多一次轻量回源。"""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", NoCacheStaticFiles(directory=str(_BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(_BASE / "templates"))

# 静态资源缓存版本号：启动时取 JS/CSS 的最新 mtime，文件一改版本自动变化。
# 取代 index.html 里手工双处同步的 ?v= 常量（git 历史上曾因忘改导致浏览器拿旧 JS 的事故）。
_STATIC_VERSION = str(int(max(
    (_BASE / "static" / "js" / "app.js").stat().st_mtime,
    (_BASE / "static" / "css" / "style.css").stat().st_mtime,
)))


# 可选鉴权：设置了 API_TOKEN 时，/api/* 写操作需带 X-API-Token 头（只读素材放行，预览 iframe 才能加载）
from fastapi import Depends, Header, HTTPException


async def require_token(request: Request, x_api_token: str | None = Header(default=None)):
    if not settings.api_token:
        return  # 未配置令牌：本机自用模式，全部放行
    path = request.url.path
    # 只读媒体放行（已做路径穿越校验）：null-origin iframe 预览无法发送自定义头，
    # 必须豁免才能加载素材。风险：若 asset 文件名可猜，素材可匿名直链下载。
    # 缓解：文件名使用 UUID（不可猜）；settings.assets_dir 默认不可公开列举。
    if path.startswith("/assets/") or path.startswith("/api/assets/file/"):
        return  # 只读媒体放行（已做路径校验），供 null-origin 预览 iframe 加载
    # 真机预览放行：手机扫码是裸浏览器 GET，无法带自定义头；session_id 为不可猜 UUID
    # 且路由内已做安全正则校验（同 /assets 的免 token 理由，见 routes.play_session）
    if path.startswith("/play/"):
        return
    if x_api_token != settings.api_token:
        raise HTTPException(status_code=401, detail="缺少或无效的 API 令牌")


# 注册路由
app.include_router(router, dependencies=[Depends(require_token)])


@app.get("/")
async def index(request: Request):
    """主页（no-cache：HTML 本身不缓存，保证 ?v= 版本串每次都是最新）"""
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"v": _STATIC_VERSION},
        headers={"Cache-Control": "no-cache"},
    )


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
