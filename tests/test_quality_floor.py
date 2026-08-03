# -*- coding: utf-8 -*-
"""生成质量下限：质量探针 + 底座必载 + 音频引擎 + 补强轮素材。

覆盖"生成的游戏简陋潦草"主诉的修复面：
- verifier 质量探针能区分裸游戏和精美游戏；触发阈值合理
- load_skill 支持逗号多载（底座四件套一次拿全）；含逗号的技能名整名优先
- polish 技能带 WebAudio 音频引擎（此前平台音频指导为零）
- SYSTEM_PROMPT 把底座加载从"建议"改为硬性要求；质量底线含音频/缓动
- 新游戏时召回块附带必载清单
"""
import json
import unittest
from pathlib import Path

from src.agent import game_agent
from src.agent.verifier import quality_gaps, should_polish

_CRUDE_GAME = """<!DOCTYPE html><html><body><canvas id=g></canvas>
<script>const ctx=document.getElementById('g').getContext('2d');
let x=0;function loop(){x+=1;ctx.fillRect(x,10,5,5);requestAnimationFrame(loop)}loop();
</script></body></html>"""

_POLISHED_GAME = """<!DOCTYPE html><html><body><canvas id=g></canvas>
<script>
const AC = new (window.AudioContext||window.webkitAudioContext)();
function spawnParticles(){} let particles=[]; particles.push({});
const ease={outQuad:t=>1-(1-t)*(1-t)}; function screenShake(){}
function addFloatText(){} const bg=ctx.createLinearGradient(0,0,0,1);
function getDifficulty(){return 1} const hiScore=+localStorage.getItem('hi');
function drawOver(){ctx.fillText('GAME OVER',0,0)}
</script></body></html>"""


class QualityProbeTests(unittest.TestCase):
    def test_crude_game_has_core_gaps(self):
        gaps = quality_gaps(_CRUDE_GAME)
        ids = {g["id"] for g in gaps}
        self.assertIn("audio", ids)
        self.assertIn("particles", ids)
        self.assertIn("easing", ids)
        self.assertTrue(should_polish(gaps))

    def test_polished_game_has_no_gaps(self):
        gaps = quality_gaps(_POLISHED_GAME)
        self.assertEqual(gaps, [], [g["id"] for g in gaps])
        self.assertFalse(should_polish(gaps))

    def test_should_polish_thresholds(self):
        core = [{"id": "audio", "core": True}]
        self.assertTrue(should_polish(core))
        two_minor = [{"id": "a", "core": False}, {"id": "b", "core": False}]
        self.assertFalse(should_polish(two_minor))
        three_minor = two_minor + [{"id": "c", "core": False}]
        self.assertTrue(should_polish(three_minor))
        self.assertFalse(should_polish([]))

    def test_quality_message_lists_gaps_and_forbids_rewrite(self):
        gaps = quality_gaps(_CRUDE_GAME)
        msg = game_agent.GameDesignAgent._build_quality_message(gaps)
        self.assertIn("音频", msg)
        self.assertIn("不改玩法逻辑", msg)
        self.assertIn('load_skill("polish,visual,gamedesign")', msg)
        self.assertIn("ensureAudio", msg)


class MultiLoadSkillTests(unittest.TestCase):
    def setUp(self):
        self._orig_skills = list(game_agent.SKILLS)

    def tearDown(self):
        game_agent.SKILLS[:] = self._orig_skills
        game_agent._rebuild_skills_prompt()

    def test_comma_loads_multiple_skills(self):
        result = game_agent.load_skill.invoke({"skill_name": "polish,gamedesign"})
        self.assertIn("视觉与手感打磨", result)
        self.assertIn("游戏设计规范", result)
        self.assertIn("\n\n---\n\n", result)

    def test_single_name_unchanged(self):
        result = game_agent.load_skill.invoke({"skill_name": "polish"})
        self.assertIn("视觉与手感打磨", result)
        self.assertNotIn("\n\n---\n\n", result)

    def test_exact_name_with_comma_takes_precedence(self):
        game_agent.SKILLS.append({
            "name": "旧作,合集", "description": "名字带逗号的技能", "content": "整名内容",
        })
        result = game_agent.load_skill.invoke({"skill_name": "旧作,合集"})
        self.assertIn("整名内容", result)
        self.assertNotIn("不存在", result)

    def test_unknown_in_multi_reports_suggestion(self):
        result = game_agent.load_skill.invoke({"skill_name": "polish,不存在的技能名"})
        self.assertIn("视觉与手感打磨", result)
        self.assertIn("不存在", result)


class QualityContentTests(unittest.TestCase):
    def test_polish_skill_carries_audio_engine(self):
        data = json.loads(
            Path("src/knowledge/builtin_skills.json").read_text(encoding="utf-8")
        )
        skills = data if isinstance(data, list) else data["skills"]
        polish = next(s for s in skills if s["name"] == "polish")
        self.assertIn("AudioContext", polish["content"])
        self.assertIn("ensureAudio", polish["content"])
        self.assertIn("startBGM", polish["content"])
        self.assertIn("sfx", polish["content"])

    def test_gamedesign_carries_fun_patterns(self):
        data = json.loads(
            Path("src/knowledge/builtin_skills.json").read_text(encoding="utf-8")
        )
        skills = data if isinstance(data, list) else data["skills"]
        gd = next(s for s in skills if s["name"] == "gamedesign")
        for marker in ("好玩的骨架", "onScoreEvent", "updatePowerups", "FORGIVE",
                       "行为差异，不是换色", "差距钩子"):
            self.assertIn(marker, gd["content"], marker)
        vis = next(s for s in skills if s["name"] == "visual")
        for marker in ("drawShadow", "环境活化", "HUD 排版纪律"):
            self.assertIn(marker, vis["content"], marker)

    def test_system_prompt_mandates_fun_essentials(self):
        sp = game_agent.SYSTEM_PROMPT
        self.assertIn("好玩三要素", sp)
        self.assertIn("连击/倍率", sp)
        self.assertIn("换色不算变体", sp)
        self.assertIn("距最高分差距", sp)

    def test_system_prompt_mandates_base_skills_and_audio(self):
        sp = game_agent.SYSTEM_PROMPT
        self.assertIn("硬性要求", sp)
        self.assertIn("gameloop,visual,polish,gamedesign", sp)
        self.assertNotIn("简单游戏可不加载", sp)
        self.assertIn("无声=简陋的第一信号", sp)
        self.assertIn("过渡与缓动", sp)


class GenerationMandateTests(unittest.TestCase):
    def test_mandate_present_for_new_game_even_with_zero_recall_hits(self):
        """mandate 独立于召回命中：冷门题材零命中也必须载底座（曾挂在召回块里被 hits 为空吞掉）。"""
        mandate = game_agent._format_generation_mandate("做一个冷门小游戏")
        self.assertIn('load_skill("gameloop,visual,polish,gamedesign")', mandate)
        self.assertNotIn("强制模块化", mandate)  # 小游戏不逼模块化

    def test_big_game_request_forces_modular_workflow(self):
        for text in ("做一个大型的魔塔式地牢探索游戏，带商店系统",
                     "来个多关卡的RPG，内容丰富不要缩水"):
            mandate = game_agent._format_generation_mandate(text)
            self.assertIn("强制模块化", mandate, text)
            self.assertIn("module:core", mandate, text)
            self.assertIn("replace_module", mandate, text)

    def test_mandate_absent_when_session_has_code(self):
        token = game_agent._current_session_id.set("quality-floor-test-session")
        game_agent._code_by_session["quality-floor-test-session"] = "<html>已有游戏</html>"
        try:
            self.assertEqual(game_agent._format_generation_mandate("做一个大型RPG"), "")
        finally:
            game_agent._current_session_id.reset(token)
            game_agent._code_by_session.pop("quality-floor-test-session", None)

    def test_big_game_detector(self):
        self.assertTrue(game_agent._looks_like_big_game_request("做个开放世界经营游戏"))
        self.assertFalse(game_agent._looks_like_big_game_request("做个打砖块"))


if __name__ == "__main__":
    unittest.main()
