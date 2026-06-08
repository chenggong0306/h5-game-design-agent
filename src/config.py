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
    embedding_model: str = "text-embedding-3-small"

    # 多模型服务配置：LLM_PROVIDER 可选 openai / deepseek / qwen
    llm_provider: str = "openai"

    # 生成质量调参
    # temperature：写代码宜偏低（0.2~0.4 更稳、bug 更少）；想要更多玩法/视觉变化可调高
    temperature: float = 0.3
    # 单次输出上限。务必 ≤ 你所用模型的真实最大输出（如 deepseek-chat≈8192、gpt-4o≈16384）。
    # 太大可能被 provider 拒绝/截断；配合"分段写入"使用，单段建议 ≤300 行。
    max_output_tokens: int = 8192

    # DeepSeek 配置（启用方式：LLM_PROVIDER=deepseek）
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

    # Qwen/vLLM/OpenAI-compatible 配置（启用方式：LLM_PROVIDER=qwen）
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
    templates_dir: str = str(BASE_DIR / "data" / "templates")

    # 项目
    project_name: str = "AI Game Design Agent"
    version: str = "0.1.0"

    model_config = {
        "env_file": str(BASE_DIR / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
