# -*- coding: utf-8 -*-
"""内置 Phaser 运行时引擎：钉版文件、技能内容、自检识别、预览白名单、探针等价物。"""
import json
import re
import unittest
from pathlib import Path

from src.agent import game_agent, verifier
from src.config import settings


class PhaserVendorTests(unittest.TestCase):
    def test_pinned_phaser_file_exists(self):
        p = Path("src/static/vendor/phaser.min.js")
        self.assertTrue(p.exists())
        raw = p.read_text(encoding="utf-8", errors="replace")
        self.assertGreater(len(raw), 1_000_000)  # 完整引擎约 1.2MB，防下到错误页
        self.assertIn("Phaser", raw[:500])

    def test_engine_skill_content(self):
        data = json.loads(Path("src/knowledge/builtin_skills.json").read_text(encoding="utf-8"))
        skills = data if isinstance(data, list) else data["skills"]
        sk = next(s for s in skills if s["name"] == "engine_phaser")
        self.assertEqual(sk["tier"], "technique")
        for marker in ("/static/vendor/phaser.min.js", "禁止换 CDN", "generateTexture",
                       "3.60+", "createEmitter", "Scale.RESIZE", "refreshBody"):
            self.assertIn(marker, sk["content"], marker)

    def test_system_prompt_has_engine_entry(self):
        self.assertIn("engine_phaser", game_agent.SYSTEM_PROMPT)
        self.assertIn("Phaser 3.90", game_agent.SYSTEM_PROMPT)


class PhaserVerifierTests(unittest.TestCase):
    def test_looks_like_game_recognizes_phaser_without_canvas_tag(self):
        html = "<html><body><div id=game></div><script src='/static/vendor/phaser.min.js'></script><script>new Phaser.Game({});</script></body></html>"
        self.assertTrue(verifier.looks_like_game(html))
        self.assertFalse(verifier.looks_like_game("<html><p>纯文档</p></html>"))

    def test_preview_whitelist_allows_vendor_js_only(self):
        origin = f"http://127.0.0.1:{settings.port}"
        self.assertTrue(verifier._is_preview_asset_request(f"{origin}/static/vendor/phaser.min.js"))
        self.assertFalse(verifier._is_preview_asset_request(f"{origin}/static/js/app.js"))
        self.assertFalse(verifier._is_preview_asset_request(f"{origin}/static/vendor/../js/app.js"))
        self.assertFalse(verifier._is_preview_asset_request(f"{origin}/static/vendor/x.png"))
        self.assertFalse(verifier._is_preview_asset_request(f"{origin}/api/chat"))

    def test_quality_probes_recognize_phaser_equivalents(self):
        phaser_game = """<html><body><div id=game></div>
<script src="/static/vendor/phaser.min.js"></script><script>
zzfx(.3,.05,539); const emitter = this.add.particles(0,0,'spark',{emitting:false});
this.cameras.main.shake(120,0.008);
this.tweens.add({targets:t, ease:'Back.easeOut'});
function addFloatText(){} const g=this.add.graphics(); g.fillGradientStyle(1,2,3,4);
function getDifficulty(){} const hiScore=+localStorage.getItem('hi');
function drawOver(){/*GAME OVER*/}
</script></body></html>"""
        gap_ids = {g["id"] for g in verifier.quality_gaps(phaser_game)}
        for probe in ("audio", "particles", "impact", "easing", "gradient",
                      "float_text", "difficulty", "hiscore", "game_over"):
            self.assertNotIn(probe, gap_ids)


class PhaserRecallTests(unittest.TestCase):
    def test_lexical_recall_hits_engine_on_phaser_mention(self):
        cands = game_agent._recall_candidates()
        hits = game_agent._lexical_skill_hits("用 phaser 做一个物理弹球", cands)
        self.assertTrue(any(h["skill"]["name"] == "engine_phaser" for h in hits))


if __name__ == "__main__":
    unittest.main()
