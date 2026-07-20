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
    from src.agent.game_agent import _BUILTIN_SKILL_NAMES
    builtin = sum(1 for s in SKILLS if s["name"] in _BUILTIN_SKILL_NAMES)
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


# 安全默认值鉴权：局域网访问管理接口默认拒绝（403），本机 UI 与公开预览路径不受影响。
# 背景：HOST=0.0.0.0 + 扫码真机预览会把服务暴露到局域网，旧逻辑在 API_TOKEN 为空时
# 全放行 → 同 WiFi 任何人可调 /api/chat 消耗模型余额。
from fastapi import Depends, Header, HTTPException

# 回环客户端地址：本机 UI 零配置全功能（与是否配置 API_TOKEN 无关）
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


async def require_token(request: Request, x_api_token: str | None = Header(default=None)):
    """鉴权策略（按顺序判定）：
    1. 公开路径永不鉴权：/play/（手机扫码是裸浏览器 GET，无法携带自定义头；session_id
       为不可猜 UUID 且路由内已做安全正则校验）、/assets/ 与 /api/assets/file/（null-origin
       预览 iframe 无法发送自定义头；文件名 UUID 不可猜、已做白名单+路径穿越校验）。
    2. 回环客户端放行：本机自用零配置不变。
    3. Starlette TestClient 哨兵 "testclient" 放行：真实部署中 client 地址由服务器从
       TCP 套接字对端地址填入 scope["client"]，无法被任何请求头伪造；"testclient" 这个
       哨兵值只会在进程内测试（TestClient/ASGITransport 默认值）出现，不构成放行漏洞。
       不放行它，全部现存 API 测试会因非回环地址被 403 打爆。
    4. 其余（真实局域网客户端）：仅当 .env 配置了 API_TOKEN 且请求头 X-API-Token 精确
       匹配才放行；否则 403 TOKEN_REQUIRED。
    """
    path = request.url.path
    if (path.startswith("/play/")
            or path.startswith("/assets/")
            or path.startswith("/api/assets/file/")):
        return
    client_host = request.client.host if request.client else ""
    if client_host in _LOOPBACK_HOSTS:
        return
    if client_host == "testclient":
        return
    if settings.api_token and x_api_token == settings.api_token:
        return
    raise HTTPException(
        status_code=403,
        detail={
            "code": "TOKEN_REQUIRED",
            "message": (
                "局域网访问管理接口需在 .env 设置 API_TOKEN 并在请求头携带 "
                "X-API-Token；手机预览请使用扫码链接"
            ),
        },
    )


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
