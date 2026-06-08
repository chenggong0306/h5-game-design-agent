"""清洗 gemma/harmony 控制标记（<|channel> 等）泄漏进正文的回归测试。"""

import unittest

import src.agent.game_agent as ga


class StripControlTokensTests(unittest.TestCase):
    """非流式整段清洗。"""

    def test_plain_text_unchanged(self):
        self.assertEqual(ga._strip_control_tokens("你好，这是一个游戏"), "你好，这是一个游戏")

    def test_strips_channel_variants(self):
        for tok in ("<|channel>", "<channel|>", "<|channel|>", "<|message|>", "<|end|>"):
            self.assertEqual(ga._strip_control_tokens(f"前{tok}后"), "前后", tok)

    def test_strips_turn_markers(self):
        self.assertEqual(ga._strip_control_tokens("<start_of_turn>正文<end_of_turn>"), "正文")

    def test_strips_channel_header_with_name(self):
        # <|channel>thought <channel|> 整段（含通道名）去掉，保留其后正文
        s = "<|channel>thought <channel|>我已经为你构建了游戏"
        self.assertEqual(ga._strip_control_tokens(s), "我已经为你构建了游戏")

    def test_harmony_style_header(self):
        s = "<|channel|>analysis<|message|>思考内容"
        self.assertEqual(ga._strip_control_tokens(s), "思考内容")

    def test_does_not_touch_normal_html_tags(self):
        # 正文里若出现普通标签（无竖线）不应被误删
        self.assertEqual(ga._strip_control_tokens("用 <canvas> 绘制"), "用 <canvas> 绘制")


class SanitizeStreamChunkTests(unittest.TestCase):
    """流式逐块清洗：拼接所有块输出应等于干净文本。"""

    def _run(self, chunks):
        ss = {"full_reply": ""}
        out = []
        for c in chunks:
            out.append(ga._sanitize_stream_chunk(ss, c))
        out.append(ga._sanitize_stream_flush(ss))
        return "".join(out)

    def test_token_within_one_chunk(self):
        self.assertEqual(self._run(["你好", "<|channel>", "世界"]), "你好世界")

    def test_token_split_across_chunks(self):
        # 控制标记被切成两块：'<|chan' + 'nel>' 仍应被完整清掉
        self.assertEqual(self._run(["开始<|chan", "nel>结束"]), "开始结束")

    def test_channel_name_split_across_chunks(self):
        # '<|channel>' / 'thought ' / '<channel|>' / '正文' 分四块到达
        self.assertEqual(
            self._run(["<|channel>", "thought ", "<channel|>", "正文内容"]),
            "正文内容",
        )

    def test_trailing_lt_is_flushed(self):
        # 末尾真实存在一个未闭合 '<'（不是控制标记）应在 flush 时吐出
        self.assertEqual(self._run(["价格 < ", "100 元"]), "价格 < 100 元")

    def test_plain_stream_unchanged(self):
        self.assertEqual(self._run(["这是", "一个", "贪吃蛇游戏"]), "这是一个贪吃蛇游戏")


if __name__ == "__main__":
    unittest.main()
