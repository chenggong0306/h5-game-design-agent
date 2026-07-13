"""SSE 流式端点 + Skills API 测试（mock agent 避免调真模型）。"""

import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api.routes as routes
from src.agent.game_agent import (
    SKILLS as _orig_skills,
    load_skill,
    load_skill_source,
)


class SseStreamTests(unittest.TestCase):
    """SSE /api/chat/stream 端点测试（mock agent LLM 层）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_dir = routes.CHAT_HISTORY_DIR
        routes.CHAT_HISTORY_DIR = Path(self.tmp)
        routes._chat_request_times.clear()

        # Mock agent.chat_stream 返回可控 SSE 事件序列
        async def _mock_stream(session_id, user_message, current_code="", code_dirty=False):
            yield {"type": "session", "session_id": session_id}
            yield {"type": "token", "content": "你好"}
            yield {"type": "token", "content": "！"}
            yield {"type": "done", "code": "<html></html>", "action": "generate"}

        self._orig_stream = routes.agent.chat_stream
        routes.agent.chat_stream = _mock_stream

        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)

    def tearDown(self):
        routes.agent.chat_stream = self._orig_stream
        routes.CHAT_HISTORY_DIR = self._orig_dir
        routes._chat_request_times.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sse_stream_returns_events(self):
        with self.client.stream("POST", "/api/chat/stream", json={
            "session_id": "sse-test", "message": "做个游戏",
        }) as r:
            self.assertEqual(r.status_code, 200)
            body = "".join(r.iter_text())
            self.assertIn("session", body)
            self.assertIn("done", body)

    def test_sse_stream_rejects_invalid_session(self):
        r = self.client.post(
            "/api/chat/stream",
            json={"session_id": "../evil", "message": "x"},
        )
        self.assertIn(r.status_code, (400, 422))

    def test_sse_stream_with_code(self):
        with self.client.stream("POST", "/api/chat/stream", json={
            "session_id": "sse-code",
            "message": "加个功能",
            "current_code": "<html></html>",
            "code_dirty": False,
        }) as r:
            self.assertEqual(r.status_code, 200)


class SkillsApiTests(unittest.TestCase):
    """Skills CRUD API 测试。"""

    @classmethod
    def setUpClass(cls):
        cls._orig_skills = list(_orig_skills)

    @classmethod
    def tearDownClass(cls):
        _orig_skills.clear()
        _orig_skills.extend(cls._orig_skills)

    def setUp(self):
        _orig_skills.clear()
        _orig_skills.extend(SkillsApiTests._orig_skills)
        self.tmp = tempfile.mkdtemp()
        self._orig_dir = routes.CHAT_HISTORY_DIR
        routes.CHAT_HISTORY_DIR = Path(self.tmp)
        routes._chat_request_times.clear()

        async def _mock_chat(session_id, msg, current_code="", code_dirty=False):
            return {"reply": "ok", "code": "", "action": "chat"}

        self._orig_chat = routes.agent.chat
        routes.agent.chat = _mock_chat

        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)

    def tearDown(self):
        routes.agent.chat = self._orig_chat
        routes.CHAT_HISTORY_DIR = self._orig_dir
        routes._chat_request_times.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_skills(self):
        r = self.client.get("/api/skills")
        self.assertEqual(r.status_code, 200)
        skills = r.json()
        self.assertIsInstance(skills, list)

    def test_add_and_delete_custom_skill(self):
        r = self.client.post("/api/skills", json={
            "name": "test_skill",
            "description": "一个测试技能",
            "content": "# 测试\n这是测试内容。",
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])

        r2 = self.client.get("/api/skills")
        names = [s["name"] for s in r2.json()]
        self.assertIn("test_skill", names)

        r3 = self.client.delete("/api/skills/test_skill")
        self.assertEqual(r3.status_code, 200, r3.text)

        r4 = self.client.get("/api/skills")
        names4 = [s["name"] for s in r4.json()]
        self.assertNotIn("test_skill", names4)

    def test_scan_rejects_system_directories(self):
        r = self.client.post("/api/skills/scan", json={"path": "C:\\Windows"})
        self.assertIn(r.status_code, (400, 403, 404))

    def test_import_zip_partial_success(self):
        """zip 里单个坏 JSON 只跳过该文件并记入 errors，其余照常导入并持久化。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("good.json", json.dumps(
                {"name": "zip_good_skill", "description": "ok", "content": "x"}))
            zf.writestr("bad.json", "{ 不是合法JSON")
            zf.writestr("doc.md", "# md 技能说明\n正文内容")
        with mock.patch.object(routes, "_save_custom_skills") as save_mock:
            r = self.client.post(
                "/api/skills/import",
                files={"file": ("skills.zip", buf.getvalue(), "application/zip")},
            )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["added"], 2)
        self.assertEqual(len(body["errors"]), 1)
        self.assertIn("bad.json", body["errors"][0])
        names = [s["name"] for s in routes.SKILLS]
        self.assertIn("zip_good_skill", names)
        self.assertIn("doc", names)
        save_mock.assert_called_once()  # 部分导入成功也必须持久化（_sync_skills 被调用）

    def test_import_zip_invalid_zip_400(self):
        r = self.client.post(
            "/api/skills/import",
            files={"file": ("skills.zip", b"not a zip at all", "application/zip")},
        )
        self.assertEqual(r.status_code, 400)

    def test_import_source_folder_as_one_skill(self):
        files = [
            ("files", ("demo/index.html", b"<canvas id='game'></canvas>", "text/html")),
            ("files", ("demo/js/game.js", b"function startGame() { return true; }", "text/javascript")),
            ("files", ("demo/js/jquery.min.js", b"/*! vendor */", "text/javascript")),
            ("files", ("demo/images/player.png", b"\x89PNG\r\n", "image/png")),
        ]
        with mock.patch.object(routes, "_save_custom_skills"):
            r = self.client.post(
                "/api/skills/source",
                data={
                    "name": "platform_demo",
                    "description": "平台跳跃游戏参考",
                    "content": "重点参考关卡和跳跃手感。",
                },
                files=files,
            )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["source_file_count"], 2)
        self.assertEqual(body["asset_file_count"], 1)
        self.assertEqual(body["entrypoint"], "demo/index.html")
        self.assertGreaterEqual(body["skipped_count"], 1)

        detail = self.client.get("/api/skills/platform_demo").json()
        self.assertEqual(detail["source_file_count"], 2)
        self.assertEqual(
            [item["path"] for item in detail["source_files"]],
            ["demo/index.html", "demo/js/game.js"],
        )
        self.assertNotIn("content", detail["source_files"][0])

        overview = load_skill.invoke({"skill_name": "platform_demo"})
        self.assertIn("demo/index.html", overview)
        self.assertIn("load_skill_source", overview)
        source = load_skill_source.invoke({
            "skill_name": "platform_demo",
            "file_path": "demo/js/game.js",
            "start_line": 1,
            "line_count": 20,
        })
        self.assertIn("function startGame", source)

    def test_update_source_skill_preserves_files(self):
        with mock.patch.object(routes, "_save_custom_skills"):
            created = self.client.post(
                "/api/skills/source",
                data={"name": "source_edit", "description": "旧描述", "content": "旧说明"},
                files={"files": ("game/index.html", b"<h1>game</h1>", "text/html")},
            )
            self.assertEqual(created.status_code, 200, created.text)
            updated = self.client.put(
                "/api/skills/source_edit",
                json={"description": "新描述", "content": "新说明"},
            )
        self.assertEqual(updated.status_code, 200, updated.text)
        skill = next(s for s in routes.SKILLS if s["name"] == "source_edit")
        self.assertEqual(skill["description"], "新描述")
        self.assertEqual(skill["source_files"][0]["path"], "game/index.html")

    def test_source_zip_rejects_traversal_and_keeps_safe_source(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../outside.js", "alert('bad')")
            zf.writestr("project/index.html", "<canvas></canvas>")
        with mock.patch.object(routes, "_save_custom_skills"):
            r = self.client.post(
                "/api/skills/source",
                data={"name": "safe_zip", "description": "安全 ZIP", "content": ""},
                files={"files": ("safe.zip", buf.getvalue(), "application/zip")},
            )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["source_file_count"], 1)
        self.assertTrue(any("不安全路径" in item for item in body["skipped"]))
        detail = self.client.get("/api/skills/safe_zip").json()
        self.assertEqual(detail["source_files"][0]["path"], "project/index.html")


if __name__ == "__main__":
    unittest.main()
