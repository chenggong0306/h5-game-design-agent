"""会话代码持久化管理"""

import itertools
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
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

# ---- 版本历史 ----
# 每会话最多保留的历史版本数（超出裁剪最旧）
MAX_VERSIONS_PER_SESSION = 10
# 版本 id 白名单正则（严格匹配生成格式，防 version_id 拼路径穿越）
_VERSION_ID = re.compile(r"^v-\d{8}T\d{6}-\d{6}$")
# 进程内单调序号：同一秒内多次保存也不会撞文件名（重启后从 1 重来，
# 但时间戳在前，排序仍以时间为主，序号只做同秒消歧）
_version_seq = itertools.count(1)


def _is_safe_session_id(session_id: str) -> bool:
    return bool(session_id) and bool(_SAFE_SESSION_ID.match(session_id))


def _versions_dir(session_id: str) -> Path:
    # 运行时取 SESSIONS_DIR（测试会临时重定向该模块变量）
    return SESSIONS_DIR / "versions" / session_id


def _trim_versions(vdir: Path) -> None:
    """裁剪最旧版本，保证每会话最多 MAX_VERSIONS_PER_SESSION 个。

    文件名 v-<UTC时间戳>-<序号>.html 按字典序即时间序（序号零填充仅同秒消歧）。
    """
    versions = sorted(vdir.glob("v-*.html"), key=lambda p: p.name)
    excess = len(versions) - MAX_VERSIONS_PER_SESSION
    for stale in versions[:excess] if excess > 0 else []:
        try:
            stale.unlink()
        except OSError as e:
            logger.warning("session_version_trim_failed", path=str(stale), error=str(e))


def _archive_current_version(session_id: str, file_path: Path, new_code: str) -> None:
    """覆盖前把当前磁盘代码挪进版本目录（内容相同则不产生新版本）。

    用 os.replace 同卷原子挪动（沿用本模块原子写惯例），不复制内容、不留半截文件。
    """
    try:
        if file_path.read_text(encoding="utf-8") == new_code:
            return
    except (OSError, ValueError):
        # 旧文件读不出（编码损坏/共享冲突）：无法证明内容相同，照样归档留痕
        pass
    vdir = _versions_dir(session_id)
    vdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    version_path = vdir / f"v-{ts}-{next(_version_seq):06d}.html"
    while version_path.exists():  # 进程重启后序号归零的同秒撞名兜底
        version_path = vdir / f"v-{ts}-{next(_version_seq):06d}.html"
    os.replace(file_path, version_path)
    _trim_versions(vdir)


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
            # 覆盖前归档旧版本（内容相同不归档）。归档失败只告警不阻断保存：
            # 版本历史是增值能力，不能因为它让权威代码写不进磁盘。
            if file_path.exists():
                try:
                    _archive_current_version(session_id, file_path, code)
                except Exception as e:
                    logger.warning("session_version_archive_failed",
                                   session_id=session_id, error=str(e))
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


def list_session_versions(session_id: str) -> list[dict]:
    """列出会话代码的历史版本，新→旧排序。

    Returns:
        每项 {"id": 版本 id, "time": ISO8601 UTC 时间, "size": 字节数, "lines": 行数}；
        无版本或 session_id 非法返回空列表。
    """
    if not _is_safe_session_id(session_id):
        return []
    vdir = _versions_dir(session_id)
    if not vdir.is_dir():
        return []
    versions: list[dict] = []
    for path in sorted(vdir.glob("v-*.html"), key=lambda p: p.name, reverse=True):
        if not _VERSION_ID.match(path.stem):
            continue  # 非本模块生成的文件（手工放置/残留）不当版本上报
        try:
            text = path.read_text(encoding="utf-8")
            size = path.stat().st_size
        except (OSError, ValueError) as e:
            # 单个版本损坏不拖垮整个列表，但要留痕可排查
            logger.error("session_version_read_failed",
                         session_id=session_id, version=path.stem, error=str(e))
            continue
        # 时间取自文件名里的 UTC 时间戳（生成时写入，不受复制/杀软触碰 mtime 影响）
        stamp = path.stem[2:17]  # v-YYYYMMDDTHHMMSS-NNNNNN → YYYYMMDDTHHMMSS
        try:
            iso_time = datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(
                tzinfo=timezone.utc).isoformat()
        except ValueError:
            iso_time = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc).isoformat()
        versions.append({
            "id": path.stem,
            "time": iso_time,
            "size": size,
            "lines": text.count("\n") + 1,
        })
    return versions


def load_session_version(session_id: str, version_id: str) -> Optional[str]:
    """读取指定历史版本的完整代码；版本不存在或 id 非法返回 None。

    version_id 必须严格匹配白名单正则（防拼接路径穿越）。
    """
    if not _is_safe_session_id(session_id):
        return None
    if not version_id or not _VERSION_ID.match(version_id):
        logger.warning("unsafe_version_id_rejected",
                       session_id=session_id, version_id=str(version_id)[:64])
        return None
    path = _versions_dir(session_id) / f"{version_id}.html"
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError) as e:
        logger.error("session_version_load_failed",
                     session_id=session_id, version=version_id, error=str(e))
        return None


def delete_session_code(session_id: str) -> bool:
    """删除会话代码文件（连同版本历史目录一起删）

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
        vdir = _versions_dir(session_id)
        if vdir.is_dir():
            shutil.rmtree(vdir)  # 会话删了版本也没有意义，一并回收防目录膨胀

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
