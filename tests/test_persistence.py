"""会话代码持久化的存取往返测试（save→load 内容、原子写、删除、列举、安全 id）。"""

import shutil
import tempfile
import unittest
from pathlib import Path

import src.utils.persistence as p


class PersistenceRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._dir = p.SESSIONS_DIR
        p.SESSIONS_DIR = self.tmp                      # 隔离到临时目录，别碰真实 data/sessions

    def tearDown(self):
        p.SESSIONS_DIR = self._dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_load_roundtrip_unicode(self):
        code = "<!DOCTYPE html><html><body>贪吃蛇🐍 — fullwidth：（）</body></html>"
        self.assertTrue(p.save_session_code("sess-1", code))
        self.assertEqual(p.load_session_code("sess-1"), code)

    def test_load_missing_returns_none(self):
        self.assertIsNone(p.load_session_code("does-not-exist"))

    def test_delete_removes_file(self):
        p.save_session_code("sess-2", "<html></html>")
        self.assertTrue(p.delete_session_code("sess-2"))
        self.assertIsNone(p.load_session_code("sess-2"))
        self.assertEqual(list(self.tmp.glob("sess-2*")), [])

    def test_list_sessions(self):
        p.save_session_code("a", "1")
        p.save_session_code("b", "22")
        ids = {s["session_id"] for s in p.list_sessions()}
        self.assertEqual(ids, {"a", "b"})

    def test_save_overwrites_existing_atomically(self):
        # os.replace 对已存在目标也要成功覆盖（Windows 上 rename 不允许覆盖，replace 才行）
        p.save_session_code("ow", "v1")
        self.assertTrue(p.save_session_code("ow", "v2-新版本"))
        self.assertEqual(p.load_session_code("ow"), "v2-新版本")

    def test_save_leaves_no_tmp_residue_and_no_meta_file(self):
        p.save_session_code("clean", "<html>ok</html>")
        # 原子写的临时文件必须被 rename 走，不留 .tmp 残留
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])
        # 元数据子系统已移除：不得再生成 _sessions.json
        self.assertFalse((self.tmp / "_sessions.json").exists())
        # list_sessions 的 *.html glob 不会把非 .html 文件算进去
        ids = {s["session_id"] for s in p.list_sessions()}
        self.assertEqual(ids, {"clean"})

    def test_load_empty_file_returns_none(self):
        # 文件存在但内容为空（如崩溃/磁盘满遗留）：按"无磁盘代码"处理，返回 None 而非空串
        (self.tmp / "empty-1.html").write_text("", encoding="utf-8")
        self.assertIsNone(p.load_session_code("empty-1"))

    def test_unsafe_id_not_written(self):
        self.assertFalse(p.save_session_code("../evil", "x"))
        self.assertEqual(list(self.tmp.glob("*.html")), [])


if __name__ == "__main__":
    unittest.main()
