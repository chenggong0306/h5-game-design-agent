"""项目工作区：会话可选"项目模式"——游戏落成真实多文件目录（data/projects/{session_id}/）。

设计：
- 入口文件（默认 index.html）与现有单文件通道**双写同步**：编辑器、导出、自检、
  write_game 等存量子系统照常面对"一份 HTML"；非入口文件只住磁盘，由
  /project/{session_id}/{path} 树服务在运行时提供（相对路径 + <base href> 解析）。
- 所有路径经 safe_relpath 白名单化：拒绝绝对路径/盘符/.././隐藏段，防穿越。
- fail-open 不适用于写入：写文件失败必须报错给模型（静默丢文件比报错更糟）。
"""

import json
import os
import re
import threading
import time
from pathlib import Path

from src.config import settings

_MARKER = ".project.json"
_MAX_FILES = 400
_MAX_FILE_BYTES = 5 * 1024 * 1024
_MAX_TOTAL_BYTES = 100 * 1024 * 1024
# 可写扩展名白名单：网页项目常规文本/数据/媒体；可执行与未知一律拒绝
_ALLOWED_EXTS = {
    ".html", ".htm", ".js", ".mjs", ".css", ".json", ".txt", ".md", ".csv", ".xml",
    ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    ".mp3", ".ogg", ".wav", ".ttf", ".woff", ".woff2",
}
_SAFE_SEG_RE = re.compile(r"^[A-Za-z0-9_\-.一-鿿]{1,64}$")
_lock = threading.Lock()


def projects_root() -> Path:
    return Path(settings.chroma_persist_dir).parent / "projects"


def project_dir(session_id: str) -> Path:
    return projects_root() / session_id


def safe_relpath(path: str) -> str | None:
    """归一化并校验项目内相对路径；非法返回 None（绝对路径显式拒绝，不做静默降级）。"""
    raw = str(path or "").replace("\\", "/").strip()
    if raw.startswith("/"):
        return None
    if not raw or len(raw) > 240:
        return None
    segments = []
    for seg in raw.split("/"):
        if not seg or seg in {".", ".."} or seg.startswith("."):
            return None
        if not _SAFE_SEG_RE.match(seg):
            return None
        segments.append(seg)
    rel = "/".join(segments)
    if Path(rel).suffix.lower() not in _ALLOWED_EXTS:
        return None
    return rel


def is_project(session_id: str) -> bool:
    return (project_dir(session_id) / _MARKER).exists()


def entry_file(session_id: str) -> str:
    try:
        meta = json.loads((project_dir(session_id) / _MARKER).read_text(encoding="utf-8"))
        return str(meta.get("entry") or "index.html")
    except Exception:
        return "index.html"


def init(session_id: str, entry: str = "index.html") -> str:
    """把会话初始化为项目模式。返回入口相对路径。已是项目则幂等返回现有入口。"""
    if is_project(session_id):
        return entry_file(session_id)
    rel = safe_relpath(entry) or "index.html"
    if not rel.lower().endswith((".html", ".htm")):
        rel = "index.html"
    root = project_dir(session_id)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / _MARKER
    tmp = marker.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(
        {"entry": rel, "created": time.strftime("%Y-%m-%d %H:%M:%S")},
        ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, marker)
    return rel


def _abs(session_id: str, rel: str) -> Path:
    return project_dir(session_id) / rel


def write_file(session_id: str, path: str, content: bytes | str) -> str:
    """写入项目文件（原子替换）。返回归一化相对路径；违规抛 ValueError。"""
    rel = safe_relpath(path)
    if rel is None:
        raise ValueError(f"非法路径 '{path}'：只允许项目内相对路径、白名单扩展名、无隐藏段")
    data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    if len(data) > _MAX_FILE_BYTES:
        raise ValueError(f"文件超过 {_MAX_FILE_BYTES // (1024*1024)}MB 上限")
    with _lock:
        files = list_files(session_id)
        existing = {f["path"] for f in files}
        if rel not in existing and len(existing) >= _MAX_FILES:
            raise ValueError(f"项目文件数已达 {_MAX_FILES} 上限")
        total = sum(f["size"] for f in files if f["path"] != rel)
        if total + len(data) > _MAX_TOTAL_BYTES:
            raise ValueError("项目总大小超过上限")
        target = _abs(session_id, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    return rel


def read_file(session_id: str, path: str) -> str | None:
    rel = safe_relpath(path)
    if rel is None:
        return None
    target = _abs(session_id, rel)
    if not target.is_file():
        return None
    return target.read_text(encoding="utf-8", errors="replace")


def read_file_bytes(session_id: str, path: str) -> bytes | None:
    rel = safe_relpath(path)
    if rel is None:
        return None
    target = _abs(session_id, rel)
    return target.read_bytes() if target.is_file() else None


def list_files(session_id: str) -> list[dict]:
    root = project_dir(session_id)
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name == _MARKER or p.name.endswith(".tmp"):
            continue
        rel = p.relative_to(root).as_posix()
        out.append({"path": rel, "size": p.stat().st_size})
        if len(out) >= _MAX_FILES:
            break
    return out
