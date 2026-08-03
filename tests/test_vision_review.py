# -*- coding: utf-8 -*-
"""视觉评审（眼睛）：配置回退、截图评审解析、缺陷→补强缺口接线。全链路 fail-open。"""
import asyncio
import io
import unittest
from types import SimpleNamespace
from unittest import mock

from src.agent import game_agent, vision_review
from src.agent import verifier
from src.config import settings


def _png(color=(30, 30, 60), size=(100, 100)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._orig = (settings.vision_model, settings.vision_api_base_url, settings.vision_api_key)

    def tearDown(self):
        (settings.vision_model, settings.vision_api_base_url, settings.vision_api_key) = self._orig

    def test_off_by_default(self):
        settings.vision_model = ""
        self.assertFalse(vision_review.is_configured())
        self.assertEqual(vision_review.review_screenshot(_png()), [])

    def test_endpoint_fallback_chain(self):
        settings.vision_api_base_url = ""
        with mock.patch.object(settings, "image_api_base_url", "https://api.siliconflow.cn/v1"):
            self.assertEqual(vision_review._endpoint(),
                             "https://api.siliconflow.cn/v1/chat/completions")
        settings.vision_api_base_url = "https://other.example.com"
        self.assertEqual(vision_review._endpoint(), "https://other.example.com/v1/chat/completions")


class ReviewParseTests(unittest.TestCase):
    def setUp(self):
        self._orig = settings.vision_model
        settings.vision_model = "Qwen/Qwen2.5-VL-32B-Instruct"

    def tearDown(self):
        settings.vision_model = self._orig

    def _resp(self, content):
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {"content": content}}]},
        )

    def test_parses_defects_and_caps(self):
        defects_json = ('分析如下 {"defects": ['
                        '{"dim": "风格统一", "issue": "怪物贴图有白边贴纸感", "fix": "抠底并加落地阴影"},'
                        '{"dim": "HUD", "issue": "血量数字顶部被裁切", "fix": "下移 8px 留安全边距"},'
                        '{"dim": "a", "issue": "1"}, {"dim": "b", "issue": "2"},'
                        '{"dim": "c", "issue": "超出上限"}]}')
        with mock.patch.object(vision_review.httpx, "post", return_value=self._resp(defects_json)):
            out = vision_review.review_screenshot(_png(), "魔塔地牢")
        self.assertEqual(len(out), 4)  # 封顶 _MAX_DEFECTS
        self.assertEqual(out[0]["dim"], "风格统一")
        self.assertIn("贴纸感", out[0]["issue"])

    def test_garbage_and_errors_fail_open(self):
        with mock.patch.object(vision_review.httpx, "post", return_value=self._resp("不是JSON")):
            self.assertEqual(vision_review.review_screenshot(_png()), [])
        with mock.patch.object(vision_review.httpx, "post", side_effect=RuntimeError("超时")):
            self.assertEqual(vision_review.review_screenshot(_png()), [])
        self.assertEqual(vision_review.review_screenshot(b""), [])

    def test_downscale_large_image(self):
        big = _png(size=(1600, 1200))
        small = vision_review._downscale(big)
        self.assertLess(len(small), len(big))
        tiny = _png(size=(200, 100))
        self.assertEqual(vision_review._downscale(tiny), tiny)


class WiringTests(unittest.TestCase):
    def test_screenshot_snapshot_roundtrip(self):
        verifier._remember_screenshot(b"PNG-BYTES")
        self.assertEqual(verifier.last_screenshot(), b"PNG-BYTES")
        verifier._remember_screenshot(b"")  # 空不覆盖
        self.assertEqual(verifier.last_screenshot(), b"PNG-BYTES")

    def test_review_visual_async_produces_core_gaps(self):
        verifier._remember_screenshot(b"SHOT")
        with mock.patch.object(vision_review, "is_configured", return_value=True), \
             mock.patch.object(vision_review, "review_screenshot", return_value=[
                 {"dim": "风格统一", "issue": "游玩区与背景割裂", "fix": "给棋盘加同风格瓷砖"}]):
            gaps = asyncio.run(game_agent._review_visual_async(
                "做个魔塔", "<html><title>魔塔地牢</title></html>"))
        self.assertEqual(len(gaps), 1)
        self.assertTrue(gaps[0]["core"])
        self.assertIn("视觉评审·风格统一", gaps[0]["label"])
        self.assertIn("瓷砖", gaps[0]["hint"])

    def test_review_visual_async_silent_when_unconfigured(self):
        with mock.patch.object(vision_review, "is_configured", return_value=False):
            self.assertEqual(
                asyncio.run(game_agent._review_visual_async("做游戏", "<html>")), [])


if __name__ == "__main__":
    unittest.main()
