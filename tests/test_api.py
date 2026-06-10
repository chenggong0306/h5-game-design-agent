"""FastAPI 路由层测试：用 TestClient 打真实路由，agent 用 mock 顶替（不调真模型）。

把 router 挂到一个裸 app 上（不走 src.main 的 lifespan/CORS/鉴权），聚焦路由本身。
"""

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api.routes as routes


class ApiRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_dir = routes.CHAT_HISTORY_DIR
        routes.CHAT_HISTORY_DIR = Path(self.tmp)               # 历史落到临时目录
        self._orig_chat = routes.agent.chat
        routes.agent.chat = AsyncMock(return_value={"reply": "你好，这是一个游戏", "code": "", "action": "chat"})
        routes._chat_request_times.clear()                    # 清限流计数，避免跨用例串扰
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)

    def tearDown(self):
        routes.CHAT_HISTORY_DIR = self._orig_dir
        routes.agent.chat = self._orig_chat
        routes._chat_request_times.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_chat_ok(self):
        r = self.client.post("/api/chat", json={"session_id": "sess-ok", "message": "做个贪吃蛇"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["reply"], "你好，这是一个游戏")

    def test_invalid_session_id_rejected(self):
        r = self.client.post("/api/chat", json={"session_id": "../evil", "message": "hi"})
        self.assertEqual(r.status_code, 400)

    def test_too_many_images_413(self):
        imgs = [{"data_url": "data:image/png;base64,AAAA"} for _ in range(5)]
        r = self.client.post("/api/chat", json={"session_id": "sess-img", "message": "hi", "images": imgs})
        self.assertEqual(r.status_code, 413)

    def test_history_roundtrip(self):
        self.client.post("/api/chat", json={"session_id": "hist-1", "message": "做个游戏"})
        r = self.client.get("/api/chat/hist-1/history")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(any(m["role"] == "user" and m["content"] == "做个游戏" for m in body["messages"]))
        self.assertTrue(any(m["role"] == "ai" for m in body["messages"]))

    def test_list_history_returns_list(self):
        self.client.post("/api/chat", json={"session_id": "hist-2", "message": "x"})
        r = self.client.get("/api/chat/history")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any(s["session_id"] == "hist-2" for s in r.json()))

    def test_corrupt_history_backed_up_not_silently_overwritten(self):
        """历史 JSON 损坏：坏文件改名 .corrupt.bak 留痕，而不是被静默清零覆盖。"""
        sid = "corrupt-1"
        path = Path(self.tmp) / f"{sid}.json"
        path.write_text("{ 这不是合法JSON", encoding="utf-8")
        routes._append_chat_history(sid, "user", "新消息")
        backup = path.with_name(path.name + ".corrupt.bak")
        self.assertTrue(backup.exists(), "损坏文件应被备份为 .corrupt.bak")
        self.assertIn("这不是合法JSON", backup.read_text(encoding="utf-8"))
        history = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual([m["content"] for m in history], ["新消息"])

    def test_concurrent_appends_do_not_lose_messages(self):
        """同会话并发 append（多标签页/重复点击）：per-session 锁保证不丢消息。"""
        sid = "concurrent-1"
        threads = [
            threading.Thread(target=routes._append_chat_history, args=(sid, "user", f"m{i}"))
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        history = routes._load_chat_history(sid)
        self.assertEqual(len(history), 8)
        self.assertEqual({m["content"] for m in history}, {f"m{i}" for i in range(8)})


if __name__ == "__main__":
    unittest.main()
