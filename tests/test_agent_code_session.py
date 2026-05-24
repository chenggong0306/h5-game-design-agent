import unittest

import src.agent.game_agent as game_agent


class AgentCodeSessionTests(unittest.TestCase):
    def test_code_tools_are_isolated_by_context_session(self):
        token_a = game_agent._begin_code_session("session-a", "let speed = 100;\ndrawA();")
        try:
            message_a = game_agent.str_replace_code.invoke({
                "old_str": "let speed = 100;",
                "new_str": "let speed = 200;",
            })
            self.assertIn("已替换", message_a)
            self.assertEqual(game_agent._get_current_code(), "let speed = 200;\ndrawA();")

            token_b = game_agent._begin_code_session("session-b", "let speed = 50;\ndrawB();")
            try:
                message_b = game_agent.replace_code_lines.invoke({
                    "start_line": 2,
                    "end_line": 2,
                    "new_str": "drawB2();",
                })
                self.assertIn("已替换", message_b)
                self.assertEqual(game_agent._get_current_code(), "let speed = 50;\ndrawB2();")
            finally:
                game_agent._end_code_session(token_b)

            self.assertEqual(game_agent._get_current_code(), "let speed = 200;\ndrawA();")
        finally:
            game_agent._end_code_session(token_a)

    def test_view_code_reads_current_session_code(self):
        token = game_agent._begin_code_session("view-session", "a\nb\nc")
        try:
            content = game_agent.view_code.invoke({"start_line": 2, "end_line": 2})

            self.assertIn("共 3 行", content)
            self.assertIn("   2 | b", content)
            self.assertNotIn("   1 | a", content)
        finally:
            game_agent._end_code_session(token)

    def test_write_game_code_updates_current_session_code(self):
        token = game_agent._begin_code_session("write-session", "old code")
        try:
            content = "<!DOCTYPE html>\n<html><body><canvas></canvas></body></html>"
            message = game_agent.write_game_code.invoke({
                "code": content,
                "reason": "创建测试游戏",
            })

            self.assertIn("已写入右侧编辑器 game.html", message)
            self.assertIn("共 2 行", message)
            self.assertEqual(game_agent._get_current_code(), content)
        finally:
            game_agent._end_code_session(token)

    def test_write_game_code_handles_missing_code_argument(self):
        token = game_agent._begin_code_session("write-missing-code-session", "old code")
        try:
            message = game_agent.write_game_code.invoke({})

            self.assertIn("写入失败：缺少 code 参数", message)
            self.assertEqual(game_agent._get_current_code(), "old code")
        finally:
            game_agent._end_code_session(token)


    def test_write_game_code_rejects_startup_order_black_screen_risk(self):
        token = game_agent._begin_code_session("startup-order-session", "old code")
        try:
            bad_code = """<!DOCTYPE html>
<html><body><canvas id=\"c\"></canvas><script>
const canvas=document.getElementById('c');
const ctx=canvas.getContext('2d');
function resize(){ if(player){ player.x=1; } ctx.scale(2,2); }
resize();
let player=null;
requestAnimationFrame(()=>{});
</script></body></html>"""
            message = game_agent.write_game_code.invoke({"code": bad_code})

            self.assertIn("写入失败：生成的 HTML 可能黑屏", message)
            self.assertIn("resize(); 出现在 player 声明之前", message)
            self.assertIn("ctx.scale()", message)
            self.assertEqual(game_agent._get_current_code(), "old code")
        finally:
            game_agent._end_code_session(token)



if __name__ == "__main__":
    unittest.main()
