"""上下文清理编辑链测试：旧 write/append 入参【恒清】（消重复代码副本），
其余工具只受通用阈值清理；进行中的分段续写受 keep 保护。"""

import unittest

from langchain_core.messages import AIMessage, ToolMessage

import src.agent.game_agent as ga

WRITE_PLACEHOLDER = "[已清理：该段代码已生效，完整代码见系统注入]"


def _calls(start: int, n: int, name: str, payload: str):
    """构造 n 对 (AIMessage 带 tool_call, ToolMessage 结果)。"""
    msgs = []
    for i in range(start, start + n):
        args = {"html": payload} if name in ("write_game", "append_game") else {"q": payload}
        msgs.append(AIMessage(content="", tool_calls=[{"id": f"t{i}", "name": name, "args": args}]))
        msgs.append(ToolMessage(content=f"结果{i}", tool_call_id=f"t{i}", name=name))
    return msgs


def _edit(msgs):
    ce = ga.CJKContextEditingMiddleware(
        edits=ga._build_context_edits(ga.CLEAR_TOOL_TRIGGER_TOKENS_DEFAULT))
    return ce._edit(type("R", (), {"messages": msgs})())


class AlwaysClearWriteArgsTests(unittest.TestCase):
    def test_old_write_args_cleared_even_below_general_trigger(self):
        # 12 次 write（小入参，远低于通用阈值）→ 旧 4 个被恒清，最近 8 个保留
        msgs = _calls(0, 12, "write_game", "<html>小段</html>")
        edited = _edit(msgs)
        ai = [m for m in edited if isinstance(m, AIMessage)]
        tm = [m for m in edited if isinstance(m, ToolMessage)]
        self.assertTrue(all(m.tool_calls[0]["args"] == {} for m in ai[:4]), "旧入参应被清空")
        self.assertTrue(all(m.content == WRITE_PLACEHOLDER for m in tm[:4]))
        self.assertTrue(all(m.tool_calls[0]["args"] != {} for m in ai[4:]), "最近 8 个应保留")
        self.assertTrue(all(m.content.startswith("结果") for m in tm[4:]))

    def test_in_flight_segmented_write_protected(self):
        # 进行中的分段写（1 write + 4 append 是最近的调用，总数 ≤ keep）→ 全部不动
        msgs = _calls(0, 2, "view_code", "x") + _calls(10, 1, "write_game", "<html>段1") \
             + _calls(20, 4, "append_game", "<script>段n")
        edited = _edit(msgs)
        for m in edited:
            if isinstance(m, AIMessage):
                self.assertNotEqual(m.tool_calls[0]["args"], {})
            else:
                self.assertTrue(m.content.startswith("结果"))

    def test_other_tools_untouched_below_general_trigger(self):
        # 旧 write 被恒清，但同样老的 view_code/replace_code 在通用阈值之下不动
        msgs = _calls(0, 4, "write_game", "<html>旧代码</html>") \
             + _calls(100, 4, "replace_code", "old") + _calls(200, 4, "view_code", "1")
        edited = _edit(msgs)
        for m in edited:
            if not isinstance(m, ToolMessage):
                continue
            if m.name == "write_game":
                self.assertEqual(m.content, WRITE_PLACEHOLDER)
            else:
                self.assertTrue(m.content.startswith("结果"), f"{m.name} 不应被恒清")

    def test_original_state_untouched(self):
        msgs = _calls(0, 12, "write_game", "<html>x</html>")
        _edit(msgs)
        self.assertTrue(all(m.tool_calls[0]["args"] != {} for m in msgs if isinstance(m, AIMessage)),
                        "只能改请求副本，不能动 checkpoint 里的原消息")


if __name__ == "__main__":
    unittest.main()
