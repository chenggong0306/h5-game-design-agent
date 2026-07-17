"""知识库管理 - ChromaDB 向量存储 + 素材管理"""

import os
import re
import uuid
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.config import Settings as ChromaSettings

from src.config import settings


class KnowledgeBase:
    """知识库：管理游戏素材和文档的向量存储"""

    def __init__(self):
        os.makedirs(settings.chroma_persist_dir, exist_ok=True)
        os.makedirs(settings.assets_dir, exist_ok=True)

        self.client = chromadb.PersistentClient(
            path=settings.chroma_persist_dir,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        # 游戏素材集合（图片、音频等的元数据）
        self.assets_collection = self.client.get_or_create_collection(
            name="game_assets",
            metadata={"description": "游戏素材的元数据和描述"},
        )
        # 用户项目集合
        self.projects_collection = self.client.get_or_create_collection(
            name="user_projects",
            metadata={"description": "用户的游戏项目代码和配置"},
        )

    # ============ 素材管理 ============

    def rebuild_assets_index(self) -> int:
        """扫描 data/assets/ 目录，把已有文件重新注册到 ChromaDB。
        用于 ChromaDB 被清空但文件还在时的恢复。
        Returns: 重建的素材数量
        """
        assets_dir = Path(settings.assets_dir)
        if not assets_dir.exists():
            return 0

        # 先获取 ChromaDB 中已有的文件路径，避免重复注册
        existing = self.assets_collection.get()
        existing_paths = set()
        if existing and existing.get("metadatas"):
            for m in existing["metadatas"]:
                if m and m.get("file_path"):
                    existing_paths.add(m["file_path"])

        count = 0
        # 遍历所有子目录（image/audio/spritesheet 等）
        for asset_type_dir in assets_dir.iterdir():
            if not asset_type_dir.is_dir():
                continue
            asset_type = asset_type_dir.name
            for file_path in asset_type_dir.iterdir():
                if not file_path.is_file() or file_path.name == ".gitkeep":
                    continue
                if str(file_path) in existing_paths:
                    continue  # 已注册，跳过

                # 用文件名（去掉 UUID 前缀后的原始名）作为显示名
                ext = file_path.suffix
                asset_id = file_path.stem  # UUID 就是 stem
                file_name = file_path.name  # 先用完整文件名

                metadata = {
                    "asset_id": asset_id,
                    "file_name": file_name,
                    "asset_type": asset_type,
                    "file_path": str(file_path),
                    "extension": ext,
                    "tags": "[]",
                }
                search_text = f"[{asset_type}] {file_name}"
                try:
                    self.assets_collection.add(
                        ids=[asset_id],
                        documents=[search_text],
                        metadatas=[metadata],
                    )
                    count += 1
                except Exception:
                    pass  # 可能 id 已存在，忽略
        return count

    def upload_asset(
        self,
        file_path: str,
        file_name: str,
        asset_type: str,
        description: str = "",
        tags: list[str] | None = None,
    ) -> dict:
        """上传游戏素材到知识库

        Args:
            file_path: 文件的临时路径
            file_name: 原始文件名
            asset_type: 素材类型 (image/audio/spritesheet/tilemap/font)
            description: 素材描述
            tags: 标签列表
        """
        asset_id = str(uuid.uuid4())
        ext = Path(file_name).suffix
        save_dir = Path(settings.assets_dir) / asset_type
        os.makedirs(save_dir, exist_ok=True)
        save_path = save_dir / f"{asset_id}{ext}"

        # 复制文件到素材目录
        shutil.copy2(file_path, save_path)

        metadata = {
            "asset_id": asset_id,
            "file_name": file_name,
            "asset_type": asset_type,
            "file_path": str(save_path),
            "extension": ext,
            "tags": json.dumps(tags or [], ensure_ascii=False),
            "description": description or "",
        }

        # 构建搜索文本
        search_text = self._compose_asset_document(asset_type, file_name, description, tags or [])

        self.assets_collection.add(
            ids=[asset_id],
            documents=[search_text],
            metadatas=[metadata],
        )

        return {"asset_id": asset_id, "file_path": str(save_path), **metadata}

    def search_assets(
        self,
        query: str,
        asset_type: Optional[str] = None,
        top_k: int = 5,
    ) -> list[dict]:
        """搜索素材"""
        where_filter = {"asset_type": asset_type} if asset_type else None
        results = self.assets_collection.query(
            query_texts=[query],
            n_results=top_k,
            where=where_filter,
        )
        return self._format_results(results)

    def list_assets(self, asset_type: Optional[str] = None) -> list[dict]:
        """列出所有素材"""
        where_filter = {"asset_type": asset_type} if asset_type else None
        results = self.assets_collection.get(where=where_filter, include=["documents", "metadatas"])
        items = []
        if results and results["ids"]:
            for i, aid in enumerate(results["ids"]):
                meta = results["metadatas"][i] if results["metadatas"] else {}
                doc = results["documents"][i] if results.get("documents") else ""
                items.append({"id": aid, "document": doc, **meta})
        return items

    def delete_asset(self, asset_id: str) -> bool:
        """删除素材"""
        try:
            result = self.assets_collection.get(ids=[asset_id])
            if result and result["metadatas"]:
                file_path = result["metadatas"][0].get("file_path", "")
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)
            self.assets_collection.delete(ids=[asset_id])
            return True
        except Exception:
            return False

    def get_asset(self, asset_id: str) -> dict | None:
        """按 ID 获取单个素材（含 document 搜索文本与全部 metadata），不存在返回 None。"""
        try:
            result = self.assets_collection.get(ids=[asset_id])
        except Exception:
            return None
        if not result or not result["ids"]:
            return None
        meta = result["metadatas"][0] if result.get("metadatas") else {}
        doc = result["documents"][0] if result.get("documents") else ""
        return {"id": result["ids"][0], "document": doc or "", **(meta or {})}

    # document 格式: "[audio] click2.ogg - 描述文本 | 标签: a, b"（与 CSV 补标注一致）
    _DOC_DESC_RE = re.compile(r"\]\s+.+?\s+-\s+(.+?)(?:\s*\|\s*标签:.*)?$")

    @staticmethod
    def _compose_asset_document(asset_type: str, file_name: str,
                                description: str, tags: list[str]) -> str:
        """统一构建素材的 ChromaDB 搜索文本（上传/补标注/自动描述共用一种格式）。"""
        doc = f"[{asset_type}] {file_name}"
        if description:
            doc += f" - {description}"
        if tags:
            doc += f" | 标签: {', '.join(tags)}"
        return doc

    def update_asset_annotation(
        self,
        asset_id: str,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> dict | None:
        """更新素材的描述/标签（None 表示该字段不改动）。

        同时重建 document 搜索文本并更新 metadata（参考 CSV 补标注的实现），
        保证中文语义搜索能命中新描述。素材不存在返回 None。
        """
        asset = self.get_asset(asset_id)
        if asset is None:
            return None

        # 现有描述：优先 metadata.description；旧数据只有 document，从中解析
        old_desc = asset.get("description") or ""
        if not old_desc and asset.get("document"):
            m = self._DOC_DESC_RE.search(asset["document"])
            old_desc = m.group(1).strip() if m else ""
        try:
            old_tags = json.loads(asset.get("tags") or "[]")
            if not isinstance(old_tags, list):
                old_tags = []
        except (ValueError, TypeError):
            old_tags = []

        new_desc = old_desc if description is None else str(description).strip()
        new_tags = old_tags if tags is None else [str(t).strip() for t in tags if str(t).strip()]

        meta = {k: v for k, v in asset.items() if k not in ("id", "document")}
        meta["tags"] = json.dumps(new_tags, ensure_ascii=False)
        meta["description"] = new_desc
        doc = self._compose_asset_document(
            meta.get("asset_type", "image"), meta.get("file_name", ""), new_desc, new_tags,
        )
        try:
            self.assets_collection.update(ids=[asset_id], documents=[doc], metadatas=[meta])
        except Exception:
            return None
        return {"id": asset_id, "document": doc, **meta}

    def update_asset_description(self, asset_id: str, description: str) -> bool:
        """更新素材描述（供上传后台自动描述 / describe 端点回填），成功返回 True。"""
        return self.update_asset_annotation(asset_id, description=description) is not None

    # ============ 项目管理 ============

    def save_project(self, project_id: str, name: str, code: str, config: dict | None = None) -> str:
        """保存用户项目（metadata 记录 created_at UTC ISO8601 与 line_count 代码行数）"""
        if not project_id:
            project_id = str(uuid.uuid4())
        metadata = {
            "name": name,
            "config": json.dumps(config or {}, ensure_ascii=False),
            "line_count": len(code.splitlines()),
        }
        # upsert: 先检查是否存在
        existing = self.projects_collection.get(ids=[project_id])
        if existing and existing["ids"]:
            # 更新：保留原 created_at（创建时间不随保存改变）；
            # 旧项目本来没有 created_at 就保持缺失（列表返回 null），不伪造创建时间
            old_meta = existing["metadatas"][0] if existing.get("metadatas") else {}
            if old_meta and old_meta.get("created_at"):
                metadata["created_at"] = old_meta["created_at"]
            self.projects_collection.update(ids=[project_id], documents=[code], metadatas=[metadata])
        else:
            metadata["created_at"] = datetime.now(timezone.utc).isoformat()
            self.projects_collection.add(ids=[project_id], documents=[code], metadatas=[metadata])
        return project_id

    def get_project(self, project_id: str) -> dict | None:
        """获取项目"""
        try:
            result = self.projects_collection.get(ids=[project_id])
            if result and result["ids"]:
                meta = result["metadatas"][0] if result["metadatas"] else {}
                # config 存的是 JSON 字符串，需要反序列化
                config_str = meta.pop("config", "{}")
                try:
                    config = json.loads(config_str)
                except (json.JSONDecodeError, TypeError):
                    config = {}
                return {
                    "project_id": result["ids"][0],
                    "code": result["documents"][0] if result["documents"] else "",
                    "config": config,
                    **meta,
                }
        except Exception:
            pass
        return None

    def list_projects(self) -> list[dict]:
        """列出所有项目（created_at / line_count 旧数据无字段时补 null）"""
        results = self.projects_collection.get()
        items = []
        if results and results["ids"]:
            for i, pid in enumerate(results["ids"]):
                meta = dict(results["metadatas"][i] or {}) if results["metadatas"] else {}
                line_count = meta.get("line_count")
                if isinstance(line_count, float):
                    line_count = int(line_count)
                elif not isinstance(line_count, int) or isinstance(line_count, bool):
                    line_count = None
                items.append({
                    "project_id": pid,
                    **meta,
                    "created_at": meta.get("created_at") or None,
                    "line_count": line_count,
                })
        return items

    def delete_project(self, project_id: str) -> bool:
        """删除项目"""
        try:
            self.projects_collection.delete(ids=[project_id])
            return True
        except Exception:
            return False

    # ============ 工具方法 ============

    @staticmethod
    def _format_results(results: dict) -> list[dict]:
        """格式化 ChromaDB 查询结果"""
        items = []
        if not results or not results.get("ids"):
            return items
        for i, aid in enumerate(results["ids"][0]):
            item = {"id": aid}
            if results.get("documents") and results["documents"][0]:
                item["document"] = results["documents"][0][i]
            if results.get("metadatas") and results["metadatas"][0]:
                item.update(results["metadatas"][0][i])
            if results.get("distances") and results["distances"][0]:
                # 1/(1+L2距离)：值域 (0,1]，距离 0 → 1.0，越远越接近 0。
                # 旧算法 1-distance 在 L2 距离 >1 时出负分，前端/模型都难解读。
                item["score"] = 1 / (1 + results["distances"][0][i])
            items.append(item)
        return items

    def get_stats(self) -> dict:
        """获取知识库统计信息"""
        return {
            "assets_count": self.assets_collection.count(),
            "projects_count": self.projects_collection.count(),
        }
