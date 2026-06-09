"""知识库测试：素材 CRUD、项目保存、索引重建。"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import src.knowledge.knowledge_base as kb_module
from src.config import settings


class KnowledgeBaseTests(unittest.TestCase):
    """使用临时目录隔离 ChromaDB + assets，不影响真实数据。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls._orig_chroma = settings.chroma_persist_dir
        cls._orig_assets = settings.assets_dir
        settings.chroma_persist_dir = os.path.join(cls.tmp, "chroma_test")
        settings.assets_dir = os.path.join(cls.tmp, "assets_test")
        cls.kb = kb_module.KnowledgeBase()

    @classmethod
    def tearDownClass(cls):
        settings.chroma_persist_dir = cls._orig_chroma
        settings.assets_dir = cls._orig_assets
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        try:
            ids = self.kb.assets_collection.get()["ids"]
            if ids:
                self.kb.assets_collection.delete(ids=ids)
            pids = self.kb.projects_collection.get()["ids"]
            if pids:
                self.kb.projects_collection.delete(ids=pids)
        except Exception:
            pass

    def _create_test_file(self, rel_path: str, content: bytes = b"test") -> Path:
        p = Path(settings.assets_dir) / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    # ---- 素材 CRUD ----

    def test_upload_asset_creates_file_and_db_record(self):
        fp = self._create_test_file("upload_test.png", b"pngdata")
        asset = self.kb.upload_asset(
            file_path=str(fp),
            file_name="upload_test.png",
            asset_type="image",
            description="测试上传",
        )
        self.assertEqual(asset["file_name"], "upload_test.png")
        self.assertEqual(asset["asset_type"], "image")

        # 验证 ChromaDB 记录
        results = self.kb.assets_collection.get(ids=[asset["asset_id"]])
        self.assertEqual(len(results["ids"]), 1)

    def test_search_assets_finds_by_description(self):
        self.kb.upload_asset(
            file_path=str(self._create_test_file("search_test.png", b"png")),
            file_name="search_test.png",
            asset_type="image",
            description="一个红色的飞船精灵",
        )
        results = self.kb.search_assets("飞船", top_k=5)
        self.assertGreaterEqual(len(results), 1)
        # description 被合并到 ChromaDB document/search_text 里，不在 metadata 直接字段中
        self.assertTrue(any("飞船" in r.get("document", "") for r in results))

    def test_delete_asset_removes_record(self):
        asset = self.kb.upload_asset(
            file_path=str(self._create_test_file("del_test.png", b"png")),
            file_name="del_test.png",
            asset_type="image",
            description="待删除",
        )
        self.kb.delete_asset(asset["asset_id"])
        results = self.kb.assets_collection.get(ids=[asset["asset_id"]])
        self.assertEqual(len(results["ids"]), 0)

    def test_list_assets_returns_all(self):
        self.kb.upload_asset(
            file_path=str(self._create_test_file("list_a.png", b"a")),
            file_name="list_a.png",
            asset_type="image",
        )
        self.kb.upload_asset(
            file_path=str(self._create_test_file("list_b.png", b"b")),
            file_name="list_b.png",
            asset_type="image",
        )
        assets = self.kb.list_assets()
        self.assertGreaterEqual(len(assets), 2)

    # ---- 项目保存 ----

    def test_save_and_get_project(self):
        self.kb.save_project("proj-1", "测试项目", "<!DOCTYPE html><html></html>")
        p = self.kb.get_project("proj-1")
        self.assertIsNotNone(p)
        self.assertEqual(p["name"], "测试项目")
        self.assertIn("<html", p["code"])

    def test_list_projects(self):
        self.kb.save_project("proj-a", "A", "codeA")
        self.kb.save_project("proj-b", "B", "codeB")
        projects = self.kb.list_projects()
        self.assertTrue(any(p["project_id"] == "proj-a" for p in projects))
        self.assertTrue(any(p["project_id"] == "proj-b" for p in projects))

    def test_delete_project(self):
        self.kb.save_project("proj-del", "待删", "code")
        self.kb.delete_project("proj-del")
        self.assertIsNone(self.kb.get_project("proj-del"))

    # ---- 索引重建 ----

    def test_rebuild_assets_index_discovers_existing_files(self):
        f = Path(settings.assets_dir) / "image" / "rebuild_test.png"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"rebuild")

        rebuilt = self.kb.rebuild_assets_index()
        self.assertGreaterEqual(rebuilt, 1)

        results = self.kb.assets_collection.get()["ids"]
        self.assertGreaterEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
