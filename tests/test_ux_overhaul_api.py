"""UX 大修后端 API 簇测试：C1 技能包导入防呆 / C2 源码导入跳过明细 /
C3 代码版本历史 / C4 回合参考收集器 / C5 真机预览。

沿用 test_api.py / test_sse_and_skills.py 惯例：router 挂裸 app + TestClient，
SESSIONS_DIR / CHAT_HISTORY_DIR / skills_dir 全部重定向到临时目录。
"""

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

import src.agent.game_agent as game_agent
import src.api.routes as routes
import src.utils.persistence as persistence
from src.agent.game_agent import SKILLS as _orig_skills


class SkillsImportGuardTests(unittest.TestCase):
    """C1：POST /api/skills/import 的纯源码项目防呆与混合包警告。"""

    @classmethod
    def setUpClass(cls):
        cls._orig_skills = list(_orig_skills)

    @classmethod
    def tearDownClass(cls):
        _orig_skills.clear()
        _orig_skills.extend(cls._orig_skills)

    def setUp(self):
        _orig_skills.clear()
        _orig_skills.extend(SkillsImportGuardTests._orig_skills)
        self.tmp = tempfile.mkdtemp()
        self._orig_skills_dir = routes._settings.skills_dir
        routes._settings.skills_dir = str(Path(self.tmp) / "skills")
        self._orig_custom_skills_file = game_agent._CUSTOM_SKILLS_FILE
        game_agent._CUSTOM_SKILLS_FILE = Path(routes._settings.skills_dir) / "custom_skills.json"
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)

    def tearDown(self):
        routes._settings.skills_dir = self._orig_skills_dir
        game_agent._CUSTOM_SKILLS_FILE = self._orig_custom_skills_file
        _orig_skills.clear()
        _orig_skills.extend(SkillsImportGuardTests._orig_skills)
        game_agent._rebuild_skills_prompt()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _zip(entries: dict[str, bytes]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        return buf.getvalue()

    def test_pure_source_zip_rejected_422(self):
        content = self._zip({
            "game/index.html": b"<!doctype html><canvas></canvas>",
            "game/js/main.js": b"function start(){}",
            "game/css/style.css": b"canvas{display:block}",
        })
        r = self.client.post(
            "/api/skills/import",
            files={"file": ("game.zip", content, "application/zip")},
        )
        self.assertEqual(r.status_code, 422, r.text)
        detail = r.json()["detail"]
        self.assertEqual(detail["code"], "SOURCE_PROJECT_DETECTED")
        self.assertTrue(detail["message"])  # 中文提示非空
        self.assertEqual(detail["stats"], {"html": 1, "js": 1, "css": 1})
        # 防呆生效：没有任何技能被导入
        self.assertEqual(len(routes.SKILLS), len(SkillsImportGuardTests._orig_skills))

    def test_mixed_zip_imports_docs_and_warns(self):
        content = self._zip({
            "pack/skill_doc.md": "# 混合包技能\n正文".encode("utf-8"),
            "pack/index.html": b"<!doctype html>",
            "pack/main.js": b"var x=1;",
        })
        with mock.patch.object(routes, "_save_custom_skills"):
            r = self.client.post(
                "/api/skills/import",
                files={"file": ("mixed.zip", content, "application/zip")},
            )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["added"], 1)
        self.assertIn("warnings", body)
        self.assertTrue(any("源码 ZIP" in w for w in body["warnings"]))
        self.assertIn("skill_doc", [s["name"] for s in routes.SKILLS])

    def test_doc_only_zip_has_no_warnings(self):
        content = self._zip({"only.md": "# 纯文档\n正文".encode("utf-8")})
        with mock.patch.object(routes, "_save_custom_skills"):
            r = self.client.post(
                "/api/skills/import",
                files={"file": ("docs.zip", content, "application/zip")},
            )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("warnings", r.json())


class SourceImportSkipDetailsTests(unittest.TestCase):
    """C2：源码导入响应的 skipped_details（path/reason/hint）。"""

    @classmethod
    def setUpClass(cls):
        cls._orig_skills = list(_orig_skills)

    @classmethod
    def tearDownClass(cls):
        _orig_skills.clear()
        _orig_skills.extend(cls._orig_skills)

    def setUp(self):
        _orig_skills.clear()
        _orig_skills.extend(SourceImportSkipDetailsTests._orig_skills)
        self.tmp = tempfile.mkdtemp()
        self._orig_skills_dir = routes._settings.skills_dir
        routes._settings.skills_dir = str(Path(self.tmp) / "skills")
        self._orig_custom_skills_file = game_agent._CUSTOM_SKILLS_FILE
        game_agent._CUSTOM_SKILLS_FILE = Path(routes._settings.skills_dir) / "custom_skills.json"
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)

    def tearDown(self):
        routes._settings.skills_dir = self._orig_skills_dir
        game_agent._CUSTOM_SKILLS_FILE = self._orig_custom_skills_file
        _orig_skills.clear()
        _orig_skills.extend(SourceImportSkipDetailsTests._orig_skills)
        game_agent._rebuild_skills_prompt()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_minified_runtime_lib_is_retained_but_unsupported_file_is_skipped(self):
        files = [
            ("files", ("demo/index.html", b"<!doctype html><script src='js/game.js'></script>", "text/html")),
            ("files", ("demo/js/game.js", b"function start(){return 1;}", "text/javascript")),
            ("files", ("demo/js/three.min.js", b"/*! three r160 */", "text/javascript")),
            ("files", ("demo/notes.exe", b"MZbinary", "application/octet-stream")),
        ]
        with mock.patch.object(routes, "_save_custom_skills"):
            r = self.client.post(
                "/api/skills/source",
                data={"name": "skip_details_demo", "description": "跳过明细测试", "content": ""},
                files=files,
            )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("skipped_details", body)
        self.assertIn("skipped", body)  # 现有字段保留
        details = {item["path"]: item for item in body["skipped_details"]}
        # 运行时库既不丢弃、也不当可读源码：改为「可服务但不可读」的库资产。
        self.assertNotIn("demo/js/three.min.js", details)
        # 只有 index.html 和 game.js 是可读源码；three.min.js 不再计入源码文件数。
        self.assertEqual(body["source_file_count"], 2)
        saved = next(s for s in _orig_skills if s["name"] == "skip_details_demo")
        self.assertNotIn("demo/js/three.min.js",
                         {f["path"] for f in saved["source_files"]})
        # 库以 kind=library 的源码素材形式保存，带可回源 URL 供 <script src> 引入。
        three = next(a for a in saved["source_assets"]
                     if a["path"] == "demo/js/three.min.js")
        self.assertEqual(three["kind"], "library")
        self.assertTrue(str(three["url"]).startswith("/assets/source/"))
        # 不支持类型也有结构化明细
        self.assertIn("demo/notes.exe", details)
        self.assertEqual(details["demo/notes.exe"]["reason"], "unsupported_type")
        for item in body["skipped_details"]:
            self.assertIn(item["reason"],
                          {"third_party_lib", "unsupported_type", "too_large", "unsafe"})
            self.assertTrue(item["hint"])


class VersionPersistenceTests(unittest.TestCase):
    """C3（存储层）：版本归档、轮转裁剪、白名单校验、级联删除。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._dir = persistence.SESSIONS_DIR
        persistence.SESSIONS_DIR = self.tmp

    def tearDown(self):
        persistence.SESSIONS_DIR = self._dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rotation_keeps_only_ten_newest(self):
        sid = "rot-1"
        for i in range(12):
            self.assertTrue(persistence.save_session_code(sid, f"<html>v{i}</html>"))
        versions = persistence.list_session_versions(sid)
        # 12 次写入 → 11 次归档 → 裁剪到 10
        self.assertEqual(len(versions), 10)
        ids = [v["id"] for v in versions]
        self.assertEqual(ids, sorted(ids, reverse=True), "必须新→旧排序")
        for v in versions:
            self.assertRegex(v["id"], r"^v-\d{8}T\d{6}-\d{6}$")
            self.assertIn("time", v)
            self.assertGreater(v["size"], 0)
            self.assertGreaterEqual(v["lines"], 1)
        # 最旧的 v0 已被裁掉：所有版本内容都不是 v0
        contents = {persistence.load_session_version(sid, vid) for vid in ids}
        self.assertNotIn("<html>v0</html>", contents)
        # 最新归档的是 v10（当前权威文件是 v11）
        self.assertIn("<html>v10</html>", contents)
        self.assertEqual(persistence.load_session_code(sid), "<html>v11</html>")

    def test_same_content_does_not_create_version(self):
        sid = "same-1"
        persistence.save_session_code(sid, "<html>same</html>")
        persistence.save_session_code(sid, "<html>same</html>")
        self.assertEqual(persistence.list_session_versions(sid), [])

    def test_no_versions_returns_empty_list(self):
        self.assertEqual(persistence.list_session_versions("nothing-here"), [])

    def test_version_id_traversal_rejected(self):
        sid = "trav-1"
        persistence.save_session_code(sid, "v1")
        persistence.save_session_code(sid, "v2")
        for bad in ("../../secret", "..\\evil", "v-123.html", "", "v-20260101T000000-1;rm"):
            self.assertIsNone(persistence.load_session_version(sid, bad))
        self.assertIsNone(persistence.load_session_version("../evil", "v-20260101T000000-000001"))

    def test_delete_session_code_removes_versions_dir(self):
        sid = "del-1"
        persistence.save_session_code(sid, "v1")
        persistence.save_session_code(sid, "v2")
        self.assertTrue((self.tmp / "versions" / sid).is_dir())
        self.assertTrue(persistence.delete_session_code(sid))
        self.assertFalse((self.tmp / "versions" / sid).exists())
        self.assertEqual(persistence.list_session_versions(sid), [])


class VersionApiTests(unittest.TestCase):
    """C3（HTTP 层）：GET versions / POST restore 契约与内存同步。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._sessions_dir = persistence.SESSIONS_DIR
        persistence.SESSIONS_DIR = self.tmp / "sessions"
        persistence.SESSIONS_DIR.mkdir(parents=True)
        self._orig_history_dir = routes.CHAT_HISTORY_DIR
        routes.CHAT_HISTORY_DIR = self.tmp / "history"
        routes.CHAT_HISTORY_DIR.mkdir(parents=True)
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)
        self.sid = "ver-api-1"

    def tearDown(self):
        persistence.SESSIONS_DIR = self._sessions_dir
        routes.CHAT_HISTORY_DIR = self._orig_history_dir
        game_agent._code_by_session.pop(self.sid, None)
        game_agent._code_session_last_access.pop(self.sid, None)
        game_agent._staging_by_session.pop(self.sid, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_versions_empty_for_new_session(self):
        r = self.client.get(f"/api/chat/{self.sid}/versions")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"versions": []})

    def test_restore_roundtrip_updates_disk_memory_and_history(self):
        v1, v2 = "<html>v1 初版</html>", "<html>v2 改版</html>"
        persistence.save_session_code(self.sid, v1)
        persistence.save_session_code(self.sid, v2)

        r = self.client.get(f"/api/chat/{self.sid}/versions")
        self.assertEqual(r.status_code, 200)
        versions = r.json()["versions"]
        self.assertEqual(len(versions), 1)
        vid = versions[0]["id"]

        r2 = self.client.post(f"/api/chat/{self.sid}/versions/{vid}/restore")
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["code"], v1)

        # 磁盘权威代码 = 恢复版；恢复前的当前代码 v2 已归档为新版本（恢复操作可回退）
        self.assertEqual(persistence.load_session_code(self.sid), v1)
        contents = {
            persistence.load_session_version(self.sid, v["id"])
            for v in persistence.list_session_versions(self.sid)
        }
        self.assertIn(v2, contents)

        # game_agent 会话内存同步为恢复版（下一轮对话基于它）
        self.assertEqual(game_agent._code_by_session.get(self.sid), v1)

        # GET history 的 latest_code 也是恢复版
        r3 = self.client.get(f"/api/chat/{self.sid}/history")
        self.assertEqual(r3.status_code, 200)
        self.assertEqual(r3.json()["latest_code"], v1)

    def test_restore_missing_version_404(self):
        persistence.save_session_code(self.sid, "v1")
        r = self.client.post(
            f"/api/chat/{self.sid}/versions/v-20200101T000000-000001/restore")
        self.assertEqual(r.status_code, 404)

    def test_restore_traversal_version_id_rejected(self):
        persistence.save_session_code(self.sid, "v1")
        persistence.save_session_code(self.sid, "v2")
        for bad in ("evil", "v-123", "..%2F..%2Fsecret"):
            r = self.client.post(f"/api/chat/{self.sid}/versions/{bad}/restore")
            self.assertIn(r.status_code, (404, 422), f"{bad} → {r.status_code}")
        # 磁盘上没有被写坏
        self.assertEqual(persistence.load_session_code(self.sid), "v2")

    def test_versions_invalid_session_id_400(self):
        r = self.client.get("/api/chat/bad.sid/versions")
        self.assertEqual(r.status_code, 400)


class ReferenceSummaryTests(unittest.TestCase):
    """C4：回合参考收集器的记录/清零/汇总逻辑（不调真模型）。"""

    def setUp(self):
        self.sid = "ref-sess-1"
        self.token = game_agent._current_session_id.set(self.sid)
        game_agent._reset_turn_skill_refs(self.sid)

    def tearDown(self):
        game_agent._turn_skill_refs_by_session.pop(self.sid, None)
        game_agent._current_session_id.reset(self.token)

    def test_summary_none_when_no_tools_used(self):
        self.assertIsNone(game_agent._build_reference_summary(self.sid))

    def test_records_accumulate_per_skill(self):
        game_agent._record_skill_reference("卡牌塔防", "load")
        game_agent._record_skill_reference("卡牌塔防", "web_bundle")
        game_agent._record_skill_reference("卡牌塔防", "source_read")
        game_agent._record_skill_reference("卡牌塔防", "source_read")
        game_agent._record_skill_reference("卡牌塔防", "assets")
        game_agent._record_skill_reference("gameloop", "load")
        summary = game_agent._build_reference_summary(self.sid)
        self.assertIsNotNone(summary)
        by_name = {s["name"]: s for s in summary["skills"]}
        self.assertEqual(by_name["卡牌塔防"],
                         {"name": "卡牌塔防", "web_bundle": True,
                          "source_reads": 2, "assets": 1})
        self.assertEqual(by_name["gameloop"],
                         {"name": "gameloop", "web_bundle": False,
                          "source_reads": 0, "assets": 0})

    def test_reset_clears_previous_turn(self):
        game_agent._record_skill_reference("卡牌塔防", "load")
        game_agent._reset_turn_skill_refs(self.sid)
        self.assertIsNone(game_agent._build_reference_summary(self.sid))

    def test_record_without_session_is_noop(self):
        inner = game_agent._current_session_id.set("")
        try:
            game_agent._record_skill_reference("卡牌塔防", "load")
        finally:
            game_agent._current_session_id.reset(inner)
        self.assertNotIn("", game_agent._turn_skill_refs_by_session)


class PlayAndServerInfoTests(unittest.TestCase):
    """C5：/play/{session_id} 真机预览与 /api/server-info。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._sessions_dir = persistence.SESSIONS_DIR
        persistence.SESSIONS_DIR = self.tmp
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)

    def tearDown(self):
        persistence.SESSIONS_DIR = self._sessions_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_play_returns_html(self):
        code = "<!DOCTYPE html><html><body>贪吃蛇</body></html>"
        persistence.save_session_code("play-1", code)
        r = self.client.get("/play/play-1")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/html"))
        self.assertEqual(r.text, code)

    def test_play_missing_session_404(self):
        r = self.client.get("/play/no-such-session")
        self.assertEqual(r.status_code, 404)

    def test_play_invalid_session_id_rejected(self):
        r = self.client.get("/play/bad.sid")
        self.assertEqual(r.status_code, 400)

    def test_server_info_shape(self):
        r = self.client.get("/api/server-info")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(set(body.keys()), {"lan_ip", "port"})
        self.assertEqual(body["port"], routes._settings.port)
        self.assertTrue(body["lan_ip"] is None or isinstance(body["lan_ip"], str))


if __name__ == "__main__":
    unittest.main()
