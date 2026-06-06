"""会话代码持久化管理"""

import json
from pathlib import Path
from typing import Optional
from src.utils.logger import logger

# 持久化目录
SESSIONS_DIR = Path("data/sessions")
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# 会话元数据文件
SESSIONS_META_FILE = SESSIONS_DIR / "_sessions.json"


def save_session_code(session_id: str, code: str) -> bool:
    """保存会话代码到磁盘
    
    Args:
        session_id: 会话 ID
        code: 游戏代码
        
    Returns:
        True: 保存成功
        False: 保存失败
    """
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
            "updated_at": __import__("time").time(),
        }
        
        SESSIONS_META_FILE.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception:
        pass  # 元数据失败不影响主流程


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
    except Exception:
        pass


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
            except Exception:
                pass
    
    if cleaned > 0:
        logger.info("cleanup_old_sessions", cleaned=cleaned, max_age_days=max_age_days)
