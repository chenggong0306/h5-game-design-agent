# -*- coding: utf-8 -*-
"""项目工作区（阶段一后端）：安全路径、读写清单、入口双写同步、树服务/play/导出、自检白名单。"""
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.agent import game_agent, project_store, verifier
from src.api import routes
from src.config import settings


class ProjectBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = settings.chroma_persist_dir
        settings.chroma_persist_dir = str(Path(self._tmp.name) / "chroma")

    def tearDown(self):
        settings.chroma_persist_dir = self._orig_dir
        self._tmp.cleanup()


class SafePathTests(unittest.TestCase):
    def test_valid_paths(self):
        self.assertEqual(project_store.safe_relpath("index.html"), "index.html")
        self.assertEqual(project_store.safe_relpath("js/main.js"), "js/main.js")
        self.assertEqual(project_store.safe_relpath("levels\\1.json"), "levels/1.json")
        self.assertEqual(project_store.safe_relpath("素材/背景.png"), "素材/背景.png")

    def test_rejects_traversal_hidden_and_unknown_ext(self):
        for bad in ("../etc/passwd.txt", "js/../../x.js", "/abs/x.js", ".env",
                    "a/.git/config", "run.exe", "x.php", "", "a" * 300 + ".js"):
            self.assertIsNone(project_store.safe_relpath(bad), bad)


class StoreTests(ProjectBase):
    def test_init_write_read_list_roundtrip(self):
        sid = "proj-store-test"
        self.assertFalse(project_store.is_project(sid))
        entry = project_store.init(sid, "index.html")
        self.assertTrue(project_store.is_project(sid))
        self.assertEqual(project_store.init(sid, "other.html"), entry)  # 幂等保持原入口
        project_store.write_file(sid, "index.html", "<html>入口</html>")
        project_store.write_file(sid, "js/main.js", "console.log(1)")
        self.assertEqual(project_store.read_file(sid, "js/main.js"), "console.log(1)")
        paths = [f["path"] for f in project_store.list_files(sid)]
        self.assertEqual(paths, ["index.html", "js/main.js"])
        with self.assertRaises(ValueError):
            project_store.write_file(sid, "../escape.js", "x")

    def test_file_count_cap(self):
        sid = "proj-cap-test"
        project_store.init(sid)
        with mock.patch.object(project_store, "_MAX_FILES", 2):
            project_store.write_file(sid, "a.js", "1")
            project_store.write_file(sid, "b.js", "2")
            with self.assertRaises(ValueError):
                project_store.write_file(sid, "c.js", "3")
            project_store.write_file(sid, "a.js", "覆盖已有不占新额度")


class ToolTests(ProjectBase):
    def setUp(self):
        super().setUp()
        self.sid = "proj-tool-test"
        self._token = game_agent._current_session_id.set(self.sid)

    def tearDown(self):
        game_agent._code_by_session.pop(self.sid, None)
        game_agent._current_session_id.reset(self._token)
        from src.utils.persistence import delete_session_code
        try:
            delete_session_code(self.sid)
        except Exception:
            pass
        super().tearDown()

    def test_init_migrates_existing_blob_and_dual_write(self):
        game_agent._set_current_code("<html><head></head><body>旧单文件</body></html>")
        out = game_agent.init_project.invoke({})
        self.assertIn("项目模式已开启", out)
        self.assertIn("旧单文件", project_store.read_file(self.sid, "index.html"))
        # 单文件通道写入 → 自动同步到入口文件
        game_agent._set_current_code("<html><head></head><body>v2</body></html>")
        self.assertIn("v2", project_store.read_file(self.sid, "index.html"))
        # write_file 写入口 → 同步回单文件通道
        game_agent.write_file.invoke({"path": "index.html", "content": "<html>v3</html>"})
        self.assertIn("v3", game_agent._get_current_code())

    def test_non_entry_files_and_listing(self):
        game_agent.init_project.invoke({})
        out = game_agent.write_file.invoke({"path": "js/game.js", "content": "let x = 1;"})
        self.assertIn("js/game.js", out)
        listing = game_agent.list_files.invoke({})
        self.assertIn("js/game.js", listing)
        read = game_agent.read_file.invoke({"path": "js/game.js"})
        self.assertIn("let x = 1;", read)
        rep = game_agent.replace_in_file.invoke(
            {"path": "js/game.js", "old_str": "x = 1", "new_str": "x = 2"})
        self.assertIn("js/game.js", rep)
        self.assertIn("x = 2", project_store.read_file(self.sid, "js/game.js"))

    def test_tools_guide_when_not_project(self):
        self.assertIn("不是项目模式", game_agent.list_files.invoke({}))
        self.assertIn("write_game", game_agent.write_file.invoke({"path": "a.js", "content": "1"}))


class RouteTests(ProjectBase):
    def setUp(self):
        super().setUp()
        self.sid = "proj-route-test"
        project_store.init(self.sid)
        project_store.write_file(self.sid, "js/main.js", "console.log('hi')")
        from src.utils.persistence import save_session_code
        save_session_code(self.sid, "<html><head><title>T</title></head><body></body></html>")
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)

    def tearDown(self):
        from src.utils.persistence import delete_session_code
        try:
            delete_session_code(self.sid)
        except Exception:
            pass
        super().tearDown()

    def test_serve_project_file_and_media_type(self):
        r = self.client.get(f"/project/{self.sid}/js/main.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn("hi", r.text)
        self.assertIn("javascript", r.headers["content-type"])
        # ACAO:* 必须在——无头自检 set_content 是不透明 origin，缺了它 fetch JSON 全拦、
        # 游戏被误判空白（实测踩过：levels/1.json CORS 拦截 → blank_screen 误报）
        self.assertEqual(r.headers.get("access-control-allow-origin"), "*")
        self.assertEqual(self.client.get(f"/project/{self.sid}/no/such.js").status_code, 404)
        self.assertEqual(self.client.get(f"/project/{self.sid}/..%2fsecret.js").status_code, 404)

    def test_play_injects_project_base(self):
        r = self.client.get(f"/play/{self.sid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f'<base href="/project/{self.sid}/">', r.text)

    def test_workspace_files_and_read(self):
        r = self.client.get(f"/api/workspace/{self.sid}/files")
        body = r.json()
        self.assertTrue(body["project"])
        self.assertEqual(body["entry"], "index.html")
        self.assertIn("js/main.js", [f["path"] for f in body["files"]])
        r2 = self.client.get(f"/api/workspace/{self.sid}/file", params={"path": "js/main.js"})
        self.assertEqual(r2.json()["content"], "console.log('hi')")
        self.assertEqual(
            self.client.get(f"/api/workspace/{self.sid}/file", params={"path": "x.png"}).status_code,
            415)

    def test_workspace_files_non_project_session(self):
        r = self.client.get("/api/workspace/no-such-proj/files")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["project"])

    def test_workspace_write_non_entry_and_entry_sync(self):
        r = self.client.put(f"/api/workspace/{self.sid}/file",
                            json={"path": "js/extra.js", "content": "var y = 9;"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(project_store.read_file(self.sid, "js/extra.js"), "var y = 9;")
        # 入口写入走双写助手：单文件通道（磁盘会话代码）同步更新
        new_entry = "<html><head><title>T2</title></head><body>synced</body></html>"
        r2 = self.client.put(f"/api/workspace/{self.sid}/file",
                             json={"path": "index.html", "content": new_entry})
        self.assertEqual(r2.status_code, 200, r2.text)
        from src.utils.persistence import load_session_code
        self.assertIn("synced", load_session_code(self.sid))
        self.assertIn("synced", project_store.read_file(self.sid, "index.html"))

    def test_export_zip(self):
        r = self.client.get(f"/api/project/{self.sid}/export.zip")
        self.assertEqual(r.status_code, 200)
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        self.assertIn("js/main.js", zf.namelist())


class VerifierIntegrationTests(ProjectBase):
    def test_preview_whitelist_allows_project_paths(self):
        origin = f"http://127.0.0.1:{settings.port}"
        self.assertTrue(verifier._is_preview_asset_request(f"{origin}/project/abc-123/js/main.js"))
        self.assertTrue(verifier._is_preview_asset_request(f"{origin}/project/abc/levels/1.json"))
        self.assertFalse(verifier._is_preview_asset_request(f"{origin}/project/abc/../x.js"))
        self.assertFalse(verifier._is_preview_asset_request(f"{origin}/project/abc/.env"))

    def test_preview_base_override(self):
        html = "<html><head></head><body><script src='js/main.js'></script></body></html>"
        out = verifier._with_preview_asset_base(html, base_path="/project/abc-123/")
        self.assertIn('/project/abc-123/"', out)
        # 非法 base_path 回退默认根
        out2 = verifier._with_preview_asset_base(html, base_path="//evil.com/")
        self.assertNotIn("evil.com", out2)


if __name__ == "__main__":
    unittest.main()
