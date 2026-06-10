"""会话代码持久化管理"""

import os
import re
import tempfile
from pathlib import Path
from typing import Optional
from src.config import BASE_DIR, settings
from src.utils.logger import logger

# 持久化目录（使用项目根目录拼绝对路径，避免 CWD 依赖）
SESSIONS_DIR = BASE_DIR / "data" / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# 会话 id 安全校验（防穿越写到任意路径）
# 正则来源统一在 src/config.py → settings.safe_session_id_pattern（一处修改、全局生效）
_SAFE_SESSION_ID = re.compile(settings.safe_session_id_pattern)


def _is_safe_session_id(session_id: str) -> bool:
    return bool(session_id) and bool(_SAFE_SESSION_ID.match(session_id))


def save_session_code(session_id: str, code: str) -> bool:
    """保存会话代码到磁盘（原子写）

    先写同目录临时文件再 os.replace 到目标路径：rename 在 Windows(NTFS)/POSIX
    上对已存在目标均为原子替换，进程崩溃/磁盘满时不会留下半截 .html
    （该文件是会话恢复的磁盘权威来源，见 routes.py 历史接口）。

    Args:
        session_id: 会话 ID
        code: 游戏代码

    Returns:
        True: 保存成功
        False: 保存失败
    """
    if not _is_safe_session_id(session_id):
        logger.warning("unsafe_session_id_rejected", op="save", session_id=str(session_id)[:64])
        return False
    try:
        file_path = SESSIONS_DIR / f"{session_id}.html"
        # 临时文件用 .tmp 后缀，list_sessions 的 *.html glob 不会误匹配残留
        fd, tmp_path = tempfile.mkstemp(
            dir=SESSIONS_DIR, prefix=f"{session_id}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(code)
            os.replace(tmp_path, file_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        logger.debug("session_code_saved", session_id=session_id, size=len(code))
        return True
    except Exception as e:
        logger.error("session_code_save_failed", session_id=session_id, error=str(e))
        return False


def load_session_code(session_id: str) -> Optional[str]:
    """从磁盘加载会话代码

    Args:
        session_id: 会话 ID

    Returns:
        代码内容，如果不存在返回 None
    """
    if not _is_safe_session_id(session_id):
        return None
    try:
        file_path = SESSIONS_DIR / f"{session_id}.html"
        if not file_path.exists():
            return None

        code = file_path.read_text(encoding="utf-8")
        if not code:
            # 文件存在但内容为空：明确记日志而非静默返回，调用方按"无磁盘代码"回退
            logger.warning("session_code_empty_on_disk", session_id=session_id)
            return None
        logger.debug("session_code_loaded", session_id=session_id, size=len(code))
        return code
    except Exception as e:
        # 含解码失败等"文件存在但内容损坏"的情况：记 error 而非静默
        logger.error("session_code_load_failed", session_id=session_id, error=str(e))
        return None


def delete_session_code(session_id: str) -> bool:
    """删除会话代码文件

    Args:
        session_id: 会话 ID

    Returns:
        True: 删除成功
        False: 删除失败
    """
    if not _is_safe_session_id(session_id):
        return False
    try:
        file_path = SESSIONS_DIR / f"{session_id}.html"
        if file_path.exists():
            file_path.unlink()

        logger.debug("session_code_deleted", session_id=session_id)
        return True
    except Exception as e:
        logger.error("session_code_delete_failed", session_id=session_id, error=str(e))
        return False


def list_sessions() -> list[dict]:
    """列出所有持久化的会话

    Returns:
        会话列表，每项包含：
        - session_id: 会话 ID
        - size: 代码大小（字节）
        - modified: 最后修改时间（时间戳）
    """
    sessions = []
    try:
        for file in SESSIONS_DIR.glob("*.html"):
            session_id = file.stem
            stat = file.stat()
            sessions.append({
                "session_id": session_id,
                "size": stat.st_size,
                "modified": stat.st_mtime,
            })
    except Exception as e:
        logger.error("list_sessions_failed", error=str(e))

    return sessions
