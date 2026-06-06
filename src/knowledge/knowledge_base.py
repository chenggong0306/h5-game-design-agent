"""知识库管理 - ChromaDB 向量存储 + 素材管理"""

import os
import uuid
import json
import shutil
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
        }

        # 构建搜索文本
        search_text = f"[{asset_type}] {file_name}"
        if description:
            search_text += f" - {description}"
        if tags:
            search_text += f" | 标签: {', '.join(tags)}"

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
        results = self.assets_collection.get(where=where_filter)
        items = []
        if results and results["ids"]:
            for i, aid in enumerate(results["ids"]):
                meta = results["metadatas"][i] if results["metadatas"] else {}
                items.append({"id": aid, **meta})
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

    # ============ 项目管理 ============

    def save_project(self, project_id: str, name: str, code: str, config: dict | None = None) -> str:
        """保存用户项目"""
        if not project_id:
            project_id = str(uuid.uuid4())
        metadata = {
            "name": name,
            "config": json.dumps(config or {}, ensure_ascii=False),
        }
        # upsert: 先检查是否存在
        existing = self.projects_collection.get(ids=[project_id])
        if existing and existing["ids"]:
            self.projects_collection.update(ids=[project_id], documents=[code], metadatas=[metadata])
        else:
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
        """列出所有项目"""
        results = self.projects_collection.get()
        items = []
        if results and results["ids"]:
            for i, pid in enumerate(results["ids"]):
                meta = results["metadatas"][i] if results["metadatas"] else {}
                items.append({"project_id": pid, **meta})
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
                item["score"] = 1 - results["distances"][0][i]
            items.append(item)
        return items

    def get_stats(self) -> dict:
        """获取知识库统计信息"""
        return {
            "assets_count": self.assets_collection.count(),
            "projects_count": self.projects_collection.count(),
        }
