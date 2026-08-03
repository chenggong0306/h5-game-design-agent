# -*- coding: utf-8 -*-
"""虚拟模块化（体量上限方案）：模块标记解析、模块工具、上下文掩码注入。"""
import unittest

from src.agent import game_agent
from src.config import settings

_BIG_GAME = """<!DOCTYPE html>
<html><head><title>大游戏</title><style>body{margin:0}</style></head>
<body><canvas id="g"></canvas>
<script>
/* ===== module:core ===== */
const CONFIG = { speed: 5 };
function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
/* ===== module:entities ===== */
class Player { constructor() { this.hp = 100; } }
function spawnEnemy() { return { hp: 10 }; }
/* ===== module:关卡 ===== */
const LEVELS = [1, 2, 3];
</script>
</body></html>"""


class ParseTests(unittest.TestCase):
    def test_parse_three_modules_with_cjk_name(self):
        mods = game_agent._parse_modules(_BIG_GAME)
        self.assertEqual([m["name"] for m in mods], ["core", "entities", "关卡"])

    def test_last_module_ends_before_script_close(self):
        mods = game_agent._parse_modules(_BIG_GAME)
        last_body = _BIG_GAME[mods[-1]["body_start"]:mods[-1]["end"]]
        self.assertIn("LEVELS", last_body)
        self.assertNotIn("</script>", last_body)
        self.assertNotIn("</html>", last_body)

    def test_no_marks_returns_empty(self):
        self.assertEqual(game_agent._parse_modules("<html><script>1</script></html>"), [])

    def test_module_map_lists_symbols(self):
        rendered = game_agent._render_module_map(_BIG_GAME)
        self.assertIn("**core**", rendered)
        self.assertIn("clamp", rendered)
        self.assertIn("Player", rendered)
        self.assertIn("**关卡**", rendered)


class ModuleToolTests(unittest.TestCase):
    def setUp(self):
        self.session_id = "vmod-test-1"
        self._token = game_agent._current_session_id.set(self.session_id)
        game_agent._set_current_code(_BIG_GAME)

    def tearDown(self):
        game_agent._code_by_session.pop(self.session_id, None)
        game_agent._current_session_id.reset(self._token)
        from src.utils.persistence import delete_session_code
        try:
            delete_session_code(self.session_id)
        except Exception:
            pass

    def test_list_and_view(self):
        listing = game_agent.list_modules.invoke({})
        self.assertIn("core", listing)
        view = game_agent.view_module.invoke({"module_name": "entities"})
        self.assertIn("class Player", view)
        missing = game_agent.view_module.invoke({"module_name": "不存在"})
        self.assertIn("现有模块", missing)

    def test_replace_module_keeps_others_and_tail(self):
        out = game_agent.replace_module.invoke({
            "module_name": "entities",
            "new_code": "class Player { constructor() { this.hp = 200; } }\nfunction spawnBoss() { return { hp: 500 }; }",
        })
        self.assertIn("已整体替换", out)
        code = game_agent._get_current_code()
        self.assertIn("this.hp = 200", code)
        self.assertIn("spawnBoss", code)
        self.assertNotIn("spawnEnemy", code)          # 旧实现被换掉
        self.assertIn("const CONFIG", code)            # 前模块完好
        self.assertIn("const LEVELS", code)            # 后模块完好
        self.assertIn("</script>", code)               # 收尾标签完好
        self.assertIn("</html>", code)
        self.assertEqual(
            [m["name"] for m in game_agent._parse_modules(code)],
            ["core", "entities", "关卡"],
        )

    def test_replace_module_rejects_tail_tags_and_marks(self):
        bad1 = game_agent.replace_module.invoke({
            "module_name": "core", "new_code": "const A=1;\n</script>"})
        self.assertIn("收尾标签", bad1)
        bad2 = game_agent.replace_module.invoke({
            "module_name": "core",
            "new_code": "/* ===== module:sneaky ===== */\nconst B=2;"})
        self.assertIn("模块标记", bad2)
        self.assertIn("const CONFIG", game_agent._get_current_code())  # 原文未被破坏

    def test_replace_missing_module_lists_names(self):
        out = game_agent.replace_module.invoke({"module_name": "ghost", "new_code": "1"})
        self.assertIn("现有模块", out)
        self.assertIn("core", out)


class MarkerLossWarningTests(unittest.TestCase):
    def test_warning_when_markers_lost(self):
        after = _BIG_GAME.replace("/* ===== module:entities ===== */", "")
        warning = game_agent._module_marker_loss_warning(_BIG_GAME, after)
        self.assertIn("移除了 1 个模块标记", warning)

    def test_silent_when_markers_kept_or_absent(self):
        self.assertEqual(game_agent._module_marker_loss_warning(_BIG_GAME, _BIG_GAME), "")
        self.assertEqual(game_agent._module_marker_loss_warning("<html>", "<html>无标记"), "")

    def test_replace_code_carries_warning(self):
        session_id = "vmod-warn-test"
        token = game_agent._current_session_id.set(session_id)
        try:
            game_agent._set_current_code(_BIG_GAME)
            out = game_agent.replace_code.invoke({
                "old_str": "/* ===== module:entities ===== */",
                "new_str": "// 标记没了",
            })
            self.assertIn("移除了 1 个模块标记", out)
        finally:
            game_agent._code_by_session.pop(session_id, None)
            game_agent._current_session_id.reset(token)
            from src.utils.persistence import delete_session_code
            try:
                delete_session_code(session_id)
            except Exception:
                pass


class CompositionRuleTests(unittest.TestCase):
    def test_visual_skill_has_composition_consistency(self):
        vis = next(s for s in game_agent.SKILLS if s["name"] == "visual")
        for marker in ("构图一致性", "游玩区 > 背景", "禁止 emoji/系统图标当游戏内实体",
                       "落地阴影", "游玩区皮肤"):
            self.assertIn(marker, vis["content"], marker)

    def test_system_prompt_budget_prioritizes_play_area(self):
        sp = game_agent.SYSTEM_PROMPT
        self.assertIn("①游玩区皮肤", sp)
        self.assertIn("禁止 emoji 当游戏内实体", sp)


class MaskedInjectionTests(unittest.TestCase):
    def setUp(self):
        self._orig_limit = settings.code_context_full_limit

    def tearDown(self):
        settings.code_context_full_limit = self._orig_limit

    def test_small_code_injected_in_full(self):
        addendum = game_agent._build_code_addendum(_BIG_GAME)
        self.assertIn("以下是当前完整代码", addendum)
        self.assertIn("const CONFIG", addendum)

    def test_big_code_switches_to_masked_module_map(self):
        settings.code_context_full_limit = 100  # 强制走掩码
        addendum = game_agent._build_code_addendum(_BIG_GAME)
        self.assertIn("掩码模式", addendum)
        self.assertIn("模块地图", addendum)
        self.assertIn("view_module", addendum)
        self.assertNotIn("const CONFIG = { speed: 5 }", addendum)  # 不再注入全文
        self.assertIn("clamp", addendum)  # 但接口摘要在

    def test_big_code_without_marks_falls_back_to_outline(self):
        settings.code_context_full_limit = 50
        plain = "<html><script>\nfunction bootGame() {}\n</script></html>"
        addendum = game_agent._build_code_addendum(plain)
        self.assertIn("掩码模式", addendum)
        self.assertIn("bootGame", addendum)
        self.assertIn("没有模块标记", addendum)


class PromptTests(unittest.TestCase):
    def test_system_prompt_has_large_game_workflow(self):
        sp = game_agent.SYSTEM_PROMPT
        self.assertIn("模块化生成工作流", sp)
        self.assertIn("replace_module", sp)
        self.assertIn("module:core", sp)


if __name__ == "__main__":
    unittest.main()
