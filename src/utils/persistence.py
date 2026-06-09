"""会话代码持久化管理"""

import json
import re
import time
from pathlib import Path
from typing import Optional
from src.config import BASE_DIR, settings
from src.utils.logger import logger

# 持久化目录（使用项目根目录拼绝对路径，避免 CWD 依赖）
SESSIONS_DIR = BASE_DIR / "data" / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# 会话元数据文件
SESSIONS_META_FILE = SESSIONS_DIR / "_sessions.json"

# 会话 id 安全校验（防穿越写到任意路径）
# 正则来源统一在 src/config.py → settings.safe_session_id_pattern（一处修改、全局生效）
_SAFE_SESSION_ID = re.compile(settings.safe_session_id_pattern)


def _is_safe_session_id(session_id: str) -> bool:
    return bool(session_id) and bool(_SAFE_SESSION_ID.match(session_id))


def save_session_code(session_id: str, code: str) -> bool:
    """保存会话代码到磁盘
    
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
        file_path.write_text(code, encoding="utf-8")

        # 更新元数据
        _update_session_meta(session_id, len(code))
        
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
        logger.debug("session_code_loaded", session_id=session_id, size=len(code))
        return code
    except Exception as e:
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
        
        # 从元数据中移除
        _remove_session_meta(session_id)
        
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


def _update_session_meta(session_id: str, code_size: int):
    """更新会话元数据"""
    try:
        meta = {}
        if SESSIONS_META_FILE.exists():
            meta = json.loads(SESSIONS_META_FILE.read_text(encoding="utf-8"))
        
        meta[session_id] = {
            "size": code_size,
            "updated_at": time.time(),
        }
        
        SESSIONS_META_FILE.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        logger.debug("update_session_meta_failed", session_id=session_id, error=str(e))  # 元数据失败不影响主流程


def _remove_session_meta(session_id: str):
    """从元数据中移除会话"""
    try:
        if SESSIONS_META_FILE.exists():
            meta = json.loads(SESSIONS_META_FILE.read_text(encoding="utf-8"))
            meta.pop(session_id, None)
            SESSIONS_META_FILE.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
    except Exception as e:
        logger.debug("remove_session_meta_failed", session_id=session_id, error=str(e))


def cleanup_old_files(max_age_days: int = 7):
    """清理超过指定天数的会话文件
    
    Args:
        max_age_days: 最大保留天数
    """
    import time
    cutoff = time.time() - (max_age_days * 86400)
    
    cleaned = 0
    for file in SESSIONS_DIR.glob("*.html"):
        if file.stat().st_mtime < cutoff:
            try:
                file.unlink()
                cleaned += 1
            except Exception as e:
                logger.debug("cleanup_file_failed", file=str(file), error=str(e))
    
    if cleaned > 0:
        logger.info("cleanup_old_sessions", cleaned=cleaned, max_age_days=max_age_days)
