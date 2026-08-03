# -*- coding: utf-8 -*-
"""全量升级批次：云生图管线、自检帧 diff/评分、意图评审、知识库相似匹配、
自动描述、会话回流模板、渐进披露。全部要求 fail-open——任何新增环节故障不阻断主流程。"""
import asyncio
import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.agent import game_agent, repair_kb, verifier
from src.api import routes
from src.config import settings
from src.knowledge import image_gen


def _png(color, size=(20, 20)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class ImageGenTests(unittest.TestCase):
    def setUp(self):
        self._orig = (settings.image_model, settings.image_api_base_url, settings.image_api_key)

    def tearDown(self):
        settings.image_model, settings.image_api_base_url, settings.image_api_key = self._orig

    def test_unconfigured_by_default_and_raises(self):
        settings.image_model = ""
        self.assertFalse(image_gen.is_configured())
        with self.assertRaises(RuntimeError):
            image_gen.generate_image("x")

    def test_endpoint_appends_v1(self):
        settings.image_api_base_url = "https://api.example.com"
        self.assertEqual(image_gen._endpoint(), "https://api.example.com/v1/images/generations")
        settings.image_api_base_url = "https://api.example.com/v1"
        self.assertEqual(image_gen._endpoint(), "https://api.example.com/v1/images/generations")

    def test_generate_image_b64_path(self):
        import base64
        settings.image_model = "test-model"
        settings.image_api_base_url = "https://api.example.com/v1"
        settings.image_api_key = "sk-test"
        fake = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": [{"b64_json": base64.b64encode(b"PNGBYTES").decode()}]},
        )
        with mock.patch.object(image_gen.httpx, "post", return_value=fake) as post:
            out = image_gen.generate_image("一只猫")
        self.assertEqual(out, b"PNGBYTES")
        self.assertIn("images/generations", post.call_args[0][0])

    def test_strip_flat_background_keys_out_uniform_bg(self):
        from PIL import Image
        img = Image.new("RGB", (20, 20), (255, 255, 255))
        for x in range(8, 12):
            for y in range(8, 12):
                img.putpixel((x, y), (200, 30, 30))
        buf = io.BytesIO(); img.save(buf, format="PNG")
        out = Image.open(io.BytesIO(image_gen.strip_flat_background(buf.getvalue())))
        self.assertEqual(out.getpixel((1, 1))[3], 0)      # 角落白底透明
        self.assertEqual(out.getpixel((10, 10))[3], 255)  # 主体保留

    def test_strip_keeps_non_uniform_background(self):
        from PIL import Image
        img = Image.new("RGB", (20, 20), (255, 255, 255))
        img.putpixel((2, 2), (0, 0, 0))  # 采样角是黑的 → 判定不是平色底，原样返回
        buf = io.BytesIO(); img.save(buf, format="PNG")
        self.assertEqual(image_gen.strip_flat_background(buf.getvalue()), buf.getvalue())


class GenerateAssetToolTests(unittest.TestCase):
    def setUp(self):
        self._orig_model = settings.image_model
        self._orig_kb = game_agent._kb
        self._token = game_agent._current_session_id.set("asset-tool-test")
        game_agent._image_gen_turn_count.clear()

    def tearDown(self):
        settings.image_model = self._orig_model
        game_agent._kb = self._orig_kb
        game_agent._current_session_id.reset(self._token)
        game_agent._image_gen_turn_count.clear()

    def test_unconfigured_returns_fallback_hint(self):
        settings.image_model = ""
        out = game_agent.generate_asset.invoke({"description": "忍者"})
        self.assertIn("未配置", out)
        self.assertIn("程序化绘制", out)

    def test_generates_uploads_and_budgets(self):
        settings.image_model = "m"
        game_agent._kb = mock.Mock(upload_asset=mock.Mock(return_value={
            "asset_id": "abc123", "extension": ".png"}))
        with mock.patch.object(image_gen, "is_configured", return_value=True), \
             mock.patch.object(image_gen, "generate_image", return_value=_png((255, 255, 255))):
            out = game_agent.generate_asset.invoke({"description": "红色飞船", "asset_kind": "sprite"})
            self.assertIn("/assets/image/abc123.png", out)
            for _ in range(3):
                game_agent.generate_asset.invoke({"description": "更多素材"})
            out5 = game_agent.generate_asset.invoke({"description": "第五张"})
        self.assertIn("额度", out5)
        self.assertIn("用完", out5)

    def test_concurrent_calls_respect_budget(self):
        """模型会并行发多个 generate_asset：6 路并发只能放行 4 张（读-判-改持锁）。"""
        import threading, time as _t
        settings.image_model = "m"
        game_agent._kb = mock.Mock(upload_asset=mock.Mock(return_value={
            "asset_id": "cc", "extension": ".png"}))

        def _slow_gen(prompt):
            _t.sleep(0.05)  # 拉开竞态窗口
            return _png((1, 2, 3))

        results = []
        with mock.patch.object(image_gen, "is_configured", return_value=True), \
             mock.patch.object(image_gen, "generate_image", side_effect=_slow_gen):
            def _call():
                out = game_agent.generate_asset.invoke({"description": "并发素材"})
                results.append(out)
            threads = [threading.Thread(target=_call) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        ok = sum(1 for r in results if "/assets/image/" in r)
        refused = sum(1 for r in results if "已用完" in r)
        self.assertEqual(ok, 4)
        self.assertEqual(refused, 2)

    def test_failure_refunds_budget(self):
        settings.image_model = "m"
        with mock.patch.object(image_gen, "is_configured", return_value=True), \
             mock.patch.object(image_gen, "generate_image", side_effect=RuntimeError("超时")):
            game_agent.generate_asset.invoke({"description": "会失败"})
        self.assertEqual(game_agent._image_gen_turn_count.get("asset-tool-test", 0), 0)

    def test_generation_failure_falls_back(self):
        settings.image_model = "m"
        with mock.patch.object(image_gen, "is_configured", return_value=True), \
             mock.patch.object(image_gen, "generate_image", side_effect=RuntimeError("接口 500")):
            out = game_agent.generate_asset.invoke({"description": "x"})
        self.assertIn("生图失败", out)
        self.assertIn("程序化绘制", out)


class VerifierEvolutionTests(unittest.TestCase):
    def test_frames_identical(self):
        a, b = _png((10, 20, 30)), _png((10, 20, 30))
        c = _png((200, 20, 30))
        self.assertTrue(verifier._frames_identical(a, b))
        self.assertFalse(verifier._frames_identical(a, c))
        self.assertFalse(verifier._frames_identical(b"", a))

    def test_axis_scores(self):
        perfect = verifier._axis_scores([], [])
        self.assertEqual(perfect, {"build_health": 100, "playability": 100})
        issues = [
            {"id": "runtime_error", "severity": "high"},
            {"id": "frozen_frames", "severity": "medium"},
        ]
        s = verifier._axis_scores(issues, [issues[0]])
        self.assertEqual(s["build_health"], 55)
        self.assertEqual(s["playability"], 65)

    def test_verify_result_carries_scores(self):
        result = asyncio.run(verifier.verify_game("<html><canvas></canvas><script>const W=innerWidth;</script></html>"))
        self.assertIn("scores", result)
        self.assertIn("build_health", result["scores"])


class IntentReviewTests(unittest.TestCase):
    def setUp(self):
        self._orig_model = game_agent._recall_model
        self._orig_flag = settings.intent_review_enabled

    def tearDown(self):
        game_agent._recall_model = self._orig_model
        settings.intent_review_enabled = self._orig_flag

    def test_missing_extracted(self):
        fake = SimpleNamespace(content='{"missing": ["双人对战模式", "排行榜"]}')
        game_agent._recall_model = mock.Mock(ainvoke=mock.AsyncMock(return_value=fake))
        out = asyncio.run(game_agent._review_intent_async("做一个双人对战贪吃蛇带排行榜", "<html><title>贪吃蛇</title></html>"))
        self.assertEqual(out, ["双人对战模式", "排行榜"])

    def test_disabled_or_failure_is_silent(self):
        settings.intent_review_enabled = False
        game_agent._recall_model = mock.Mock()
        self.assertEqual(asyncio.run(game_agent._review_intent_async("做游戏", "<html>")), [])
        settings.intent_review_enabled = True
        game_agent._recall_model = mock.Mock(ainvoke=mock.AsyncMock(side_effect=RuntimeError("挂了")))
        self.assertEqual(asyncio.run(game_agent._review_intent_async("做一个塔防游戏", "<html>")), [])

    def test_code_evidence_compact(self):
        ev = game_agent._code_evidence("<html><title>测试游戏</title><script>function startGame(){}</script></html>")
        self.assertIn("测试游戏", ev)
        self.assertIn("startGame", ev)


class RepairKbSimilarityTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = settings.chroma_persist_dir
        settings.chroma_persist_dir = str(__import__("pathlib").Path(self._tmp.name) / "chroma")
        repair_kb.reset_cache_for_tests()

    def tearDown(self):
        settings.chroma_persist_dir = self._orig_dir
        repair_kb.reset_cache_for_tests()
        self._tmp.cleanup()

    def test_similar_message_hits(self):
        repair_kb.record_success(
            [{"id": "runtime_error", "msg": "Audio key \"bgm\" not found in cache"}],
            "改用 ZzFX 合成，去掉 load.audio",
        )
        hint = repair_kb.hints_for(
            [{"id": "runtime_error", "msg": "Audio key \"explosion\" not found in cache"}]
        )
        self.assertIn("相似问题曾修复成功", hint)
        self.assertIn("ZzFX", hint)

    def test_unrelated_message_does_not_hit(self):
        repair_kb.record_success(
            [{"id": "runtime_error", "msg": "Audio key \"bgm\" not found in cache"}],
            "改用 ZzFX",
        )
        self.assertEqual(
            repair_kb.hints_for([{"id": "runtime_error", "msg": "Cannot read properties of undefined"}]),
            "",
        )


class AutoDescriptionTests(unittest.TestCase):
    def setUp(self):
        self._orig_model = game_agent._recall_model

    def tearDown(self):
        game_agent._recall_model = self._orig_model

    def test_no_model_returns_empty(self):
        game_agent._recall_model = None
        self.assertEqual(game_agent.generate_source_description("游戏6", None, []), "")

    def test_generates_from_title_evidence(self):
        fake = SimpleNamespace(content="青蛙过马路躲车流的休闲敏捷小游戏")
        game_agent._recall_model = mock.Mock(invoke=mock.Mock(return_value=fake))
        out = game_agent.generate_source_description(
            "游戏6", "frog/index.html",
            [{"path": "frog/index.html", "content": "<title>青蛙过河</title>"}],
        )
        self.assertIn("青蛙", out)
        prompt_sent = game_agent._recall_model.invoke.call_args[0][0]
        self.assertIn("青蛙过河", prompt_sent)  # 入口标题进了证据


class ProgressiveDisclosureTests(unittest.TestCase):
    def test_visual_split_and_ref_loading(self):
        vis = next(s for s in game_agent.SKILLS if s["name"] == "visual")
        self.assertIn("layered-characters", vis.get("references") or {})
        main = game_agent._load_single_skill("visual")
        self.assertIn("参考子文档", main)
        self.assertIn('load_skill_ref("visual", "layered-characters")', main)
        ref = game_agent.load_skill_ref.invoke(
            {"skill_name": "visual", "ref_name": "layered-characters"})
        self.assertIn("分层", ref)
        missing = game_agent.load_skill_ref.invoke(
            {"skill_name": "visual", "ref_name": "不存在"})
        self.assertIn("可用参考", missing)


class FromSessionTemplateTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)
        from src.utils.persistence import save_session_code
        self.session_id = "tpl-batch-test-1"
        save_session_code(self.session_id, (
            "<!DOCTYPE html><html><head><title>回流测试游戏</title></head>"
            "<body><canvas id='g'></canvas><script>function startGame(){}</script></body></html>"
        ))
        self._added = []

    def tearDown(self):
        from src.utils.persistence import delete_session_code
        try:
            delete_session_code(self.session_id)
        except Exception:
            pass
        game_agent.SKILLS[:] = [s for s in game_agent.SKILLS if s["name"] != "回流模板A"]
        game_agent._rebuild_skills_prompt()

    def test_session_code_becomes_inspired_template(self):
        with mock.patch.object(routes, "_save_custom_skills"), \
             mock.patch.object(routes, "generate_source_description", return_value="像素跑酷测试模板"):
            r = self.client.post("/api/skills/from-session",
                                 json={"session_id": self.session_id, "name": "回流模板A"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["name"], "回流模板A")
        self.assertEqual(body["source_mode"], "inspired")
        self.assertEqual(body["description"], "像素跑酷测试模板")
        self.assertTrue(any(s["name"] == "回流模板A" for s in game_agent.SKILLS))

    def test_missing_session_404(self):
        r = self.client.post("/api/skills/from-session",
                             json={"session_id": "no-such-session-xyz"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
