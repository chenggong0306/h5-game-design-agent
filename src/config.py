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
    turn_deadline_seconds: float = 600.0
    # 流式时是否请求 usage_metadata（stream_options.include_usage）。默认开（供压缩计数校正）；
    # 个别严格的 OpenAI 兼容网关不认这个字段时可设 false 关闭。
    stream_usage: bool = True

    # 生成后自检 + 自修闭环
    self_check_enabled: bool = True      # 生成游戏后自动质检，发现问题让模型自修后再返回
    self_check_max_rounds: int = 2       # 最多自修轮数（每轮一次模型修复 + 重新质检；第2轮只在仍失败时才跑）
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

    # 项目
    project_name: str = "AI Game Design Agent"
    version: str = "0.1.0"

    model_config = {
        "env_file": str(BASE_DIR / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
