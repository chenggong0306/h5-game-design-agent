"""AI 游戏设计智能体 - 启动入口"""

import sys
import os
from pathlib import Path

# 修复 Windows 控制台编码
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 最先加载 .env 到系统环境变量
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

import uvicorn
from src.config import settings


def main():
    print("[Game Design Agent] AI 游戏设计工坊启动中...")
    print(f"[Server] http://localhost:{settings.port}")
    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    main()
