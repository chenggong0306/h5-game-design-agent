"""会话代码持久化的存取往返测试（save→load 内容、删除、列举、元数据、安全 id）。"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import src.utils.persistence as p


class PersistenceRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._dir, self._meta = p.SESSIONS_DIR, p.SESSIONS_META_FILE
        p.SESSIONS_DIR = self.tmp                      # 隔离到临时目录，别碰真实 data/sessions
        p.SESSIONS_META_FILE = self.tmp / "_sessions.json"

    def tearDown(self):
        p.SESSIONS_DIR, p.SESSIONS_META_FILE = self._dir, self._meta
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_load_roundtrip_unicode(self):
        code = "<!DOCTYPE html><html><body>贪吃蛇🐍 — fullwidth：（）</body></html>"
        self.assertTrue(p.save_session_code("sess-1", code))
        self.assertEqual(p.load_session_code("sess-1"), code)

    def test_load_missing_returns_none(self):
        self.assertIsNone(p.load_session_code("does-not-exist"))

    def test_delete_removes_file_and_meta(self):
        p.save_session_code("sess-2", "<html></html>")
        self.assertTrue(p.delete_session_code("sess-2"))
        self.assertIsNone(p.load_session_code("sess-2"))
        meta = json.loads(p.SESSIONS_META_FILE.read_text(encoding="utf-8"))
        self.assertNotIn("sess-2", meta)

    def test_list_sessions(self):
        p.save_session_code("a", "1")
        p.save_session_code("b", "22")
        ids = {s["session_id"] for s in p.list_sessions()}
        self.assertEqual(ids, {"a", "b"})

    def test_meta_records_size(self):
        p.save_session_code("m1", "abc")
        meta = json.loads(p.SESSIONS_META_FILE.read_text(encoding="utf-8"))
        self.assertEqual(meta["m1"]["size"], 3)

    def test_unsafe_id_not_written(self):
        self.assertFalse(p.save_session_code("../evil", "x"))
        self.assertEqual(list(self.tmp.glob("*.html")), [])


if __name__ == "__main__":
    unittest.main()
