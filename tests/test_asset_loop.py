"""素材发现闭环测试：图片自动描述 / PATCH 补标注 / describe 端点 /
搜索分数值域 / 项目列表元数据 / 上传后台描述线程调度。

- describe_image 用 monkeypatch 模拟 httpx，覆盖成功 / HTTP 失败 / 超时三态；
- 路由层沿用 test_api.py 惯例：router 挂裸 app + TestClient，kb 方法用 mock 顶替；
- KnowledgeBase 项目元数据用临时 Chroma 目录做真实往返（同 test_knowledge_base.py）。
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api.routes as routes
import src.knowledge.asset_describer as describer
import src.knowledge.knowledge_base as kb_module
from src.config import settings
from src.knowledge.knowledge_base import KnowledgeBase


# ============ describe_image：成功 / 失败 / 超时（monkeypatch httpx） ============

class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


@pytest.fixture
def png_file(tmp_path):
    p = tmp_path / "ball.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\nfakepng")
    return str(p)


def test_describe_image_success(monkeypatch, png_file):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        captured["timeout"] = timeout
        return _FakeResponse(200, {
            "choices": [{"message": {"content": "一个红色小球，适合弹球游戏。标签：小球、红色、弹球"}}]
        })

    monkeypatch.setattr(describer.httpx, "post", fake_post)
    result = describer.describe_image(png_file)
    assert result == "一个红色小球，适合弹球游戏。标签：小球、红色、弹球"
    # 请求形状：image_url content part 带 base64 data URI
    parts = captured["payload"]["messages"][0]["content"]
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert captured["url"].endswith("/chat/completions")
    assert captured["timeout"] == describer.DESCRIBE_TIMEOUT_SECONDS


def test_describe_image_vision_400_falls_back_to_text_model(monkeypatch, png_file):
    """实测契约：deepseek-v4-flash 不支持 image_url，视觉请求 400 →
    降级到「PIL 特征 + 文本模型」，而不是直接放弃（否则中文搜索永远命不中图片）。"""
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        parts = json["messages"][0]["content"]
        is_vision = isinstance(parts, list)
        calls.append("vision" if is_vision else "text")
        if is_vision:
            return _FakeResponse(400, text="unknown variant `image_url`")
        return _FakeResponse(200, {
            "choices": [{"message": {"content": "一个橙色圆形小球，适合弹球游戏。标签：小球、橙色、弹球"}}]
        })

    monkeypatch.setattr(describer.httpx, "post", fake_post)
    result = describer.describe_image(png_file)
    assert calls == ["vision", "text"], "视觉失败后必须尝试文本降级"
    assert result is not None and "小球" in result


def test_describe_image_all_models_down_still_returns_local_keywords(monkeypatch, png_file):
    """两级模型都不可用（断网）→ 仍用文件名关键词兜底，保证中文可检索。"""
    def fake_post(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(describer.httpx, "post", fake_post)
    result = describer.describe_image(png_file)
    assert result is not None
    assert "小球" in result  # ball.png → 球 小球 弹球


def test_keywords_from_filename_maps_common_game_assets():
    assert "小球" in describer._keywords_from_filename("ball.png")
    assert "球拍" in describer._keywords_from_filename("paddle_blue.png")
    assert "敌人" in describer._keywords_from_filename("enemy_boss.png")
    assert describer._keywords_from_filename("xyz123.png") == ""


def test_describe_image_timeout_falls_back_without_raising(monkeypatch, png_file):
    """超时不抛出；两级模型都超时后仍走本地兜底。"""
    def fake_post(*a, **kw):
        raise httpx.TimeoutException("read timeout")

    monkeypatch.setattr(describer.httpx, "post", fake_post)
    result = describer.describe_image(png_file)
    assert result is None or "小球" in result


def test_describe_image_missing_file_returns_none(tmp_path):
    assert describer.describe_image(str(tmp_path / "nope.png")) is None


def test_describe_image_unrecognizable_name_and_dead_models_returns_none(monkeypatch, tmp_path):
    """文件名无可用关键词 + 模型全挂 + PIL 读不出特征 → None（宁可没有也不编造）。"""
    p = tmp_path / "xyz123.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\nnotarealpng")  # PIL 打不开 → 无特征

    def fake_post(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(describer.httpx, "post", fake_post)
    assert describer.describe_image(str(p)) is None


# ============ 路由层：PATCH / describe 端点 / 上传后台线程调度（mock kb） ============

class AssetRouteTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)

    # ---- PATCH /api/assets/{id} ----

    def test_patch_asset_200(self):
        updated = {
            "id": "aid-1", "asset_id": "aid-1", "file_name": "ball.png",
            "asset_type": "image", "file_path": "x", "extension": ".png",
            "tags": json.dumps(["小球"], ensure_ascii=False),
            "description": "红色小球",
            "document": "[image] ball.png - 红色小球 | 标签: 小球",
        }
        with mock.patch.object(routes.kb, "update_asset_annotation", return_value=dict(updated)) as m:
            r = self.client.patch(
                "/api/assets/aid-1",
                json={"description": "红色小球", "tags": ["小球"]},
            )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["description"], "红色小球")
        self.assertEqual(body["url"], "/assets/image/aid-1.png")
        m.assert_called_once_with("aid-1", "红色小球", ["小球"])

    def test_patch_asset_404(self):
        with mock.patch.object(routes.kb, "update_asset_annotation", return_value=None):
            r = self.client.patch("/api/assets/nope", json={"description": "x"})
        self.assertEqual(r.status_code, 404)

    # ---- POST /api/assets/{id}/describe ----

    def test_describe_endpoint_200(self):
        asset = {"id": "aid-2", "asset_id": "aid-2", "file_path": "C:/fake/ball.png",
                 "asset_type": "image", "file_name": "ball.png"}
        with mock.patch.object(routes.kb, "get_asset", return_value=asset), \
             mock.patch.object(routes.kb, "update_asset_description", return_value=True) as upd, \
             mock.patch.object(routes, "describe_image", return_value="蓝色方块。标签：方块") as di:
            r = self.client.post("/api/assets/aid-2/describe")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"description": "蓝色方块。标签：方块"})
        di.assert_called_once_with("C:/fake/ball.png")
        upd.assert_called_once_with("aid-2", "蓝色方块。标签：方块")

    def test_describe_endpoint_502_when_model_fails(self):
        asset = {"id": "aid-3", "asset_id": "aid-3", "file_path": "C:/fake/x.png"}
        with mock.patch.object(routes.kb, "get_asset", return_value=asset), \
             mock.patch.object(routes, "describe_image", return_value=None):
            r = self.client.post("/api/assets/aid-3/describe")
        self.assertEqual(r.status_code, 502, r.text)
        detail = r.json()["detail"]
        self.assertEqual(detail["code"], "DESCRIBE_FAILED")
        self.assertTrue(detail["message"])

    def test_describe_endpoint_404_when_asset_missing(self):
        with mock.patch.object(routes.kb, "get_asset", return_value=None):
            r = self.client.post("/api/assets/nope/describe")
        self.assertEqual(r.status_code, 404)

    # ---- 上传后自动调度后台描述线程 ----

    def test_upload_image_schedules_auto_describe(self):
        fake_result = {
            "asset_id": "new-1", "file_path": "C:/fake/new-1.png",
            "file_name": "ball.png", "asset_type": "image", "extension": ".png",
            "tags": "[]", "description": "",
        }
        with mock.patch.object(routes.kb, "upload_asset", return_value=dict(fake_result)), \
             mock.patch.object(routes, "_schedule_auto_describe") as sched:
            r = self.client.post(
                "/api/assets/upload",
                files={"file": ("ball.png", b"\x89PNGdata", "image/png")},
                data={"asset_type": "image"},
            )
        self.assertEqual(r.status_code, 200, r.text)
        # 响应形状不变：仍是素材对象 + url
        self.assertEqual(r.json()["url"], "/assets/image/new-1.png")
        sched.assert_called_once_with("new-1", "C:/fake/new-1.png")

    def test_upload_audio_does_not_schedule_describe(self):
        fake_result = {
            "asset_id": "new-2", "file_path": "C:/fake/new-2.mp3",
            "file_name": "bgm.mp3", "asset_type": "audio", "extension": ".mp3",
            "tags": "[]", "description": "",
        }
        with mock.patch.object(routes.kb, "upload_asset", return_value=dict(fake_result)), \
             mock.patch.object(routes, "_schedule_auto_describe") as sched:
            r = self.client.post(
                "/api/assets/upload",
                files={"file": ("bgm.mp3", b"ID3data", "audio/mpeg")},
                data={"asset_type": "audio"},
            )
        self.assertEqual(r.status_code, 200, r.text)
        sched.assert_not_called()

    def test_upload_with_manual_description_skips_auto_describe(self):
        fake_result = {
            "asset_id": "new-3", "file_path": "C:/fake/new-3.png",
            "file_name": "hero.png", "asset_type": "image", "extension": ".png",
            "tags": "[]", "description": "手填的主角描述",
        }
        with mock.patch.object(routes.kb, "upload_asset", return_value=dict(fake_result)), \
             mock.patch.object(routes, "_schedule_auto_describe") as sched:
            r = self.client.post(
                "/api/assets/upload",
                files={"file": ("hero.png", b"\x89PNGdata", "image/png")},
                data={"asset_type": "image", "description": "手填的主角描述"},
            )
        self.assertEqual(r.status_code, 200, r.text)
        sched.assert_not_called()

    def test_auto_describe_worker_updates_kb(self):
        """后台线程体：describe 成功 → 回填 ChromaDB；失败 → 静默。"""
        with mock.patch.object(routes, "describe_image", return_value="绿色树木。标签：树"), \
             mock.patch.object(routes.kb, "update_asset_description", return_value=True) as upd:
            routes._auto_describe_asset("aid-9", "C:/fake/tree.png")
        upd.assert_called_once_with("aid-9", "绿色树木。标签：树")

        with mock.patch.object(routes, "describe_image", return_value=None), \
             mock.patch.object(routes.kb, "update_asset_description") as upd2:
            routes._auto_describe_asset("aid-9", "C:/fake/tree.png")
        upd2.assert_not_called()


# ============ 搜索分数值域：score = 1/(1+L2距离) ∈ (0,1] ============

def test_format_results_score_range():
    fake = {
        "ids": [["a", "b", "c"]],
        "documents": [["[image] a.png", "[image] b.png", "[image] c.png"]],
        "metadatas": [[{"asset_id": "a"}, {"asset_id": "b"}, {"asset_id": "c"}]],
        "distances": [[0.0, 1.0, 3.0]],
    }
    items = KnowledgeBase._format_results(fake)
    scores = [it["score"] for it in items]
    assert scores == [1.0, 0.5, 0.25]
    for s in scores:
        assert 0 < s <= 1
    # 大 L2 距离也绝不出负分（旧算法 1-distance 的回归点）
    far = {
        "ids": [["x"]], "documents": [["d"]], "metadatas": [[{}]],
        "distances": [[57.3]],
    }
    assert 0 < KnowledgeBase._format_results(far)[0]["score"] <= 1


# ============ 项目元数据：created_at / line_count 真实往返 ============

class ProjectMetadataTests(unittest.TestCase):
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
        pids = self.kb.projects_collection.get()["ids"]
        if pids:
            self.kb.projects_collection.delete(ids=pids)

    def _get_item(self, pid):
        items = self.kb.list_projects()
        return next(p for p in items if p["project_id"] == pid)

    def test_new_project_has_created_at_and_line_count(self):
        code = "<!DOCTYPE html>\n<html>\n<body></body>\n</html>"
        self.kb.save_project("meta-1", "元数据项目", code)
        item = self._get_item("meta-1")
        self.assertEqual(item["line_count"], 4)
        self.assertIsInstance(item["line_count"], int)
        # created_at 是可解析的 ISO8601（UTC）
        parsed = datetime.fromisoformat(item["created_at"])
        self.assertIsNotNone(parsed.tzinfo)

    def test_update_preserves_created_at_and_refreshes_line_count(self):
        self.kb.save_project("meta-2", "P", "line1\nline2")
        first = self._get_item("meta-2")["created_at"]
        self.kb.save_project("meta-2", "P", "line1\nline2\nline3\nline4\nline5")
        item = self._get_item("meta-2")
        self.assertEqual(item["created_at"], first, "created_at 不应随保存改变")
        self.assertEqual(item["line_count"], 5)

    def test_legacy_project_returns_nulls(self):
        """旧数据（无 created_at/line_count 字段）→ 两个键都存在且为 None。"""
        self.kb.projects_collection.add(
            ids=["legacy-1"], documents=["old code"],
            metadatas=[{"name": "旧项目", "config": "{}"}],
        )
        item = self._get_item("legacy-1")
        self.assertIn("created_at", item)
        self.assertIn("line_count", item)
        self.assertIsNone(item["created_at"])
        self.assertIsNone(item["line_count"])

    def test_asset_annotation_roundtrip_updates_document_and_search(self):
        """update_asset_annotation：document 与 metadata 同步更新，语义搜索能命中新描述。"""
        img = Path(settings.assets_dir) / "ann.png"
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b"png")
        asset = self.kb.upload_asset(
            file_path=str(img), file_name="ball.png", asset_type="image",
        )
        aid = asset["asset_id"]
        updated = self.kb.update_asset_annotation(aid, "一个红色小球素材", ["小球", "红色"])
        self.assertIsNotNone(updated)
        self.assertEqual(updated["description"], "一个红色小球素材")
        self.assertIn("一个红色小球素材", updated["document"])
        self.assertIn("小球", updated["document"])
        # 语义搜索命中新描述，且 score 落在 (0,1]
        results = self.kb.search_assets("红色小球", top_k=3)
        self.assertTrue(any(r.get("asset_id") == aid for r in results))
        for r in results:
            if "score" in r:
                self.assertGreater(r["score"], 0)
                self.assertLessEqual(r["score"], 1)
        # 只改 tags 时描述保留
        updated2 = self.kb.update_asset_annotation(aid, None, ["新标签"])
        self.assertEqual(updated2["description"], "一个红色小球素材")
        self.assertEqual(json.loads(updated2["tags"]), ["新标签"])

    def test_update_asset_annotation_missing_returns_none(self):
        self.assertIsNone(self.kb.update_asset_annotation("no-such-id", "x", None))


if __name__ == "__main__":
    unittest.main()
