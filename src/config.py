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

    # DeepSeek 配置（启用方式：LLM_PROVIDER=deepseek）
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

    # Qwen/vLLM/OpenAI-compatible 配置（启用方式：LLM_PROVIDER=qwen）
    qwen_api_key: str = ""
    qwen_base_url: str = ""
    qwen_model: str = ""


    # 服务
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = True

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
