"""全局配置管理"""

from pathlib import Path
from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """应用配置 - 自动从 .env 文件和环境变量读取"""

    # LLM
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o"

    # 多模型服务配置：LLM_PROVIDER 可选 openai / deepseek / vllm
    #（qwen / gemma / compatible 为同义别名，均走本地 vLLM/OpenAI 兼容分支）
    llm_provider: str = "openai"

    # 生成质量调参
    # 默认使用确定性更高的生成参数：游戏代码对重现性和正确性的要求
    # 高于文案创意性。如果明确需要“换一种方案”，可通过环境变量调高。
    temperature: float = 0.0
    top_p: float = 1.0
    # 单次输出上限。务必 ≤ 你所用模型的真实最大输出（如 deepseek-chat≈8192、gpt-4o≈16384）。
    # 太大可能被 provider 拒绝/截断；配合"分段写入"使用，单段建议 ≤300 行。
    max_output_tokens: int = 8192
    # 单回合墙钟上限（秒）：防止个别回合（工具循环/自修）跑太久、占资源。流式与非流式都生效。
    turn_deadline_seconds: float = 900.0  # 大型游戏一轮要写十几段，600s 会掐断主回合
    # Unity 线专属回合上限：编辑器施工+PlayMode 自测+WebGL 构建（3-6 分钟）天然比 H5 长，
    # 900s 会把构建掐在半路（实测 8 分钟才走完装配）。仅用户点名 unity 的回合生效
    unity_turn_deadline_seconds: float = 2400.0
    # 大游戏上下文掩码阈值（字符）：代码超过后，每轮注入"模块地图+接口摘要"而非全文，
    # 模型用 view_module 按需查看、replace_module 定点改写——体量上限的解法（调研 P11）
    code_context_full_limit: int = 45000
    # 流式时是否请求 usage_metadata（stream_options.include_usage）。默认开（供压缩计数校正）；
    # 个别严格的 OpenAI 兼容网关不认这个字段时可设 false 关闭。
    stream_usage: bool = True

    # 生成后自检 + 自修闭环
    self_check_enabled: bool = True      # 生成游戏后自动质检，发现问题让模型自修后再返回
    self_check_max_rounds: int = 2       # 最多自修轮数（每轮一次模型修复 + 重新质检；第2轮只在仍失败时才跑）
    # 质量补强轮：功能自检通过后，静态探针查"精美要素"（音频/粒子/缓动/难度曲线等），
    # 有缺口且是全新生成（非忠实移植）时追加一轮自动补强。QUALITY_PASS_ENABLED=0 可关
    quality_pass_enabled: bool = True
    # 意图对齐评审：全新生成通过功能自检后，用主模型对照用户原始请求判一次"哪些明确要求没实现"，
    # 缺口并入补强轮。INTENT_REVIEW_ENABLED=0 可关
    intent_review_enabled: bool = True
    # 生成前绘制清单（实验开关，默认关）：新游戏先输出玩法规格+逐元素绘制清单再写码，
    # 用于 A/B 验证一次通过率。DESIGN_MANIFEST_ENABLED=1 开启
    design_manifest_enabled: bool = False

    # 视觉评审（"眼睛"）：用视觉模型审自检截图挑毛病（贴纸感/HUD 裁切/风格割裂/空场景），
    # 缺陷并入补强轮。VISION_MODEL 留空=关闭；base/key 缺省回退 IMAGE_API_* 再回退 OPENAI_*
    # （硅基流动同一个 key 就有 Qwen2.5-VL）。示例：VISION_MODEL=Qwen/Qwen2.5-VL-32B-Instruct
    vision_model: str = ""
    vision_api_base_url: str = ""
    vision_api_key: str = ""

    # 联网搜索工具：查玩法规则/技术资料。TAVILY_API_KEY 配了走 Tavily（agent 专用搜索，
    # 结果干净），没配自动走 DuckDuckGo 免 key 兜底。WEB_SEARCH_ENABLED=0 整体关闭
    web_search_enabled: bool = True
    tavily_api_key: str = ""

    # Unity 3D 生成线：平台经 unity-mcp 桥驱动本机 Unity 编辑器（画布工程需在编辑器中打开）。
    # 桥不在线时 Unity 线工具自动降级提示，H5 主线不受影响
    unity_project_path: str = r"C:\xiangmu\unity_games\demo3d"
    unity_bridge_url: str = "http://localhost:27099"
    unity_editor_exe: str = r"C:\Program Files\Unity\Hub\Editor\6000.5.6f1\Editor\Unity.exe"
    # 素材工厂：独立无头工程批量渲染 3D 模型为 2D 序列帧图集（与编辑器画布工程互不干扰）
    unity_factory_path: str = r"C:\xiangmu\unity_games\asset_factory"

    # 云生图素材管线（OpenAI images 兼容端点 /v1/images/generations）。
    # IMAGE_MODEL 留空 = 功能关闭（生成走程序化绘制）。base/key 缺省回退 OPENAI_*
    # （聚合代理通常同时提供图像端点）。示例：IMAGE_MODEL=Kwai-Kolors/Kolors
    image_api_base_url: str = ""
    image_api_key: str = ""
    image_model: str = ""
    image_size: str = "1024x1024"
    self_check_headless: bool = True      # 启用无头浏览器运行检查（需 playwright + chromium）：
                                           # 未安装时自动降级到纯静态分析

    # DeepSeek 配置（启用方式：LLM_PROVIDER=deepseek）
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

    # Qwen/vLLM/OpenAI-compatible 配置（启用方式：LLM_PROVIDER=vllm，qwen/gemma 同义）
    qwen_api_key: str = ""
    qwen_base_url: str = ""
    qwen_model: str = ""


    # 服务
    # 默认只绑本机回环，避免把无鉴权的接口暴露到局域网；确需联网时再用 HOST=0.0.0.0 覆盖
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = True

    # 可选 API 令牌：留空=本机自用、接口开放；设置后所有 /api/* 写操作需带 X-API-Token 头
    # （配合 HOST=0.0.0.0 对外暴露时使用）
    api_token: str = ""

    # 路径
    chroma_persist_dir: str = str(BASE_DIR / "data" / "chroma_db")
    assets_dir: str = str(BASE_DIR / "data" / "assets")
    skills_dir: str = str(BASE_DIR / "data" / "skills")

    # 安全
    # 会话 ID 安全校验正则（字母数字/下划线/连字符 1-128 位）
    # 供 HTTP 层和持久化层共用，防路径穿越（一处修改、全局生效）
    safe_session_id_pattern: str = r"^[A-Za-z0-9_-]{1,128}$"
    # 上传大小上限（字节，默认 20MB）：素材上传与技能 ZIP 导入共用。
    # 这两个端点都把整个文件读进内存，uvicorn/Starlette 默认不限请求体大小，必须设限防 OOM
    max_upload_bytes: int = 20 * 1024 * 1024
    # 源码项目导入上限（字节，默认 200MB）：整游戏项目自带图片/音频/大库，20MB 装不下真实项目。
    # 与 max_upload_bytes 分开——素材/技能 ZIP 端点保持紧上限防误传超大文件，源码导入走大上限
    # （本机工具，瞬时内存占用可接受；ZIP 解压炸弹守卫仍按本值 ×3 派生）。.env 里 SOURCE_MAX_UPLOAD_BYTES 可覆盖
    source_max_upload_bytes: int = 200 * 1024 * 1024

    # 技能语义路由开关：词法召回空手时，用主模型跑一次轻量 JSON 调用做语义匹配
    # （"做个塔防"也能命中"植物大战僵尸"源码参考）。SKILL_RECALL_ROUTER=0 可关
    skill_recall_router: bool = True

    # 项目
    project_name: str = "AI Game Design Agent"
    version: str = "0.1.0"

    model_config = {
        "env_file": str(BASE_DIR / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
