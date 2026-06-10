"""CJK token 估算回归测试：新正则实现与旧逐字符实现一致性 + 按消息 id 的计数缓存。

背景（第4次审计）：_estimate_tokens 原为纯 Python 逐字符 ord() 循环，被三个
每次模型调用都触发的中间件钩子在事件循环上反复全量执行。改为预编译正则按
CJK 连续段匹配求和 + 按消息 id 的 LRU 计数缓存。本文件锁定两点：
1) 新实现与旧实现对任意输入结果完全一致（含 [IMAGE] 占位、区间边界字符）；
2) 缓存按 (id, 文本长度) 生效：命中复用 / 无 id 不缓存 / 同 id 换内容（被
   ClearToolUsesEdit 清成占位符）不会返回脏值 / 容量有界（LRU 淘汰）。
"""

import unittest

import src.agent.game_agent as game_agent


def _legacy_estimate_tokens(text: str) -> int:
    """旧实现的忠实拷贝（逐字符 ord() + 5 段区间比较），作为新实现的对照基准。"""
    if not text:
        return 0
    image_count = 0
    if "[IMAGE]" in text:
        image_count = text.count("[IMAGE]")
        text = text.replace("[IMAGE]", "")
    cjk = 0
    for ch in text:
        o = ord(ch)
        if (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF   # 中日韩统一表意
                or 0x3040 <= o <= 0x30FF                       # 日文假名
                or 0xAC00 <= o <= 0xD7A3                       # 韩文音节
                or 0xFF00 <= o <= 0xFFEF):                     # 全角符号
            cjk += 1
    other = len(text) - cjk
    text_tokens = max(1, int(cjk + other / 3.5))
    return text_tokens + image_count * game_agent._IMAGE_TOKEN_ESTIMATE


class _Msg:
    """最小消息桩：只带 content / id / tool_calls，够 _message_text_for_budget 用。"""

    def __init__(self, content, mid=None, tool_calls=None):
        self.content = content
        self.id = mid
        self.tool_calls = tool_calls or []


class EstimateTokensParityTests(unittest.TestCase):
    """新正则实现必须与旧逐字符实现对所有输入逐一相等。"""

    CASES = [
        "",
        "hello world, plain ascii only.",
        "你好，世界！这是一段纯中文文本。",
        "mixed 中英文混排 with kana かなカナ and hangul 한글 plus ＦＵＬＬ width！",
        "[IMAGE]",
        "before [IMAGE] 中文 [IMAGE] after",
        "[IMAGE][IMAGE][IMAGE]",
        # 五个区间的上下边界字符（应计为 CJK）
        "぀ヿ㐀䶿一鿿가힣＀￯",
        # 紧贴区间外侧的字符（不应计为 CJK）
        "〿㄀㏿䷀ꀀ꯿힤﻿￰",
        "\n\t  whitespace\r\nandé latin-1 ́ combining",
        "中" * 1000 + "a" * 1000 + "が" * 37,
        "<html><body><script>const 速度 = 100; // 注释</script></body></html>",
    ]

    def test_regex_matches_legacy_charwise_loop(self):
        for case in self.CASES:
            with self.subTest(case=case[:40]):
                self.assertEqual(
                    game_agent._estimate_tokens(case),
                    _legacy_estimate_tokens(case),
                )

    def test_plain_string_still_supported(self):
        # _estimate_tokens 必须保持对纯字符串可用（_compute_system_overhead_tokens 等直接调）
        self.assertEqual(game_agent._estimate_tokens(""), 0)
        self.assertGreater(game_agent._estimate_tokens("中文中文"), 0)


class MessageTokenCacheTests(unittest.TestCase):
    """_estimate_message_tokens 的按消息 id LRU 缓存行为。"""

    def setUp(self):
        game_agent._msg_token_cache.clear()

    def tearDown(self):
        game_agent._msg_token_cache.clear()

    def test_result_matches_uncached_path(self):
        m = _Msg("你好世界 hello world", mid="msg-1")
        expected = game_agent._estimate_tokens(game_agent._message_text_for_budget(m))
        self.assertEqual(game_agent._estimate_message_tokens(m), expected)
        # 第二次（缓存命中）结果不变
        self.assertEqual(game_agent._estimate_message_tokens(m), expected)

    def test_cache_hit_skips_recount(self):
        m = _Msg("你好世界 hello", mid="msg-hit")
        game_agent._estimate_message_tokens(m)
        self.assertEqual(len(game_agent._msg_token_cache), 1)
        # 篡改缓存值后再次调用：返回缓存值 ⇒ 证明命中而非重算
        key = next(iter(game_agent._msg_token_cache))
        game_agent._msg_token_cache[key] = 99999
        self.assertEqual(game_agent._estimate_message_tokens(m), 99999)

    def test_message_without_id_is_not_cached(self):
        m = _Msg("没有 id 的消息", mid=None)
        expected = game_agent._estimate_tokens(game_agent._message_text_for_budget(m))
        self.assertEqual(game_agent._estimate_message_tokens(m), expected)
        self.assertEqual(len(game_agent._msg_token_cache), 0)

    def test_same_id_different_content_not_stale(self):
        # ClearToolUsesEdit 把工具输出清成占位符时 id 不变、内容变短：
        # key 含文本长度 ⇒ 不会拿大内容的旧计数当新值
        big = _Msg("工具输出内容" * 500, mid="m-cleared")
        v_big = game_agent._estimate_message_tokens(big)
        cleared = _Msg("[已清理]", mid="m-cleared")
        v_small = game_agent._estimate_message_tokens(cleared)
        self.assertLess(v_small, v_big)
        self.assertEqual(
            v_small,
            game_agent._estimate_tokens(game_agent._message_text_for_budget(cleared)),
        )

    def test_lru_eviction_keeps_cache_bounded(self):
        orig_max = game_agent._MSG_TOKEN_CACHE_MAX
        game_agent._MSG_TOKEN_CACHE_MAX = 4
        try:
            for i in range(10):
                game_agent._estimate_message_tokens(_Msg(f"消息内容 {i}", mid=f"id-{i}"))
            self.assertLessEqual(len(game_agent._msg_token_cache), 4)
            # 最旧的条目被淘汰，最新的仍在
            keys = list(game_agent._msg_token_cache)
            self.assertNotIn(("id-0", len(game_agent._message_text_for_budget(_Msg("消息内容 0")))),
                             keys)
            self.assertTrue(any(k[0] == "id-9" for k in keys))
        finally:
            game_agent._MSG_TOKEN_CACHE_MAX = orig_max

    def test_count_message_tokens_cjk_consistent_with_and_without_cache(self):
        msgs = [
            _Msg("第一条很长的中文消息内容" * 10, mid="a"),
            _Msg("second english message", mid="b"),
            _Msg("无 id 的消息", mid=None),
        ]
        expected = sum(
            game_agent._estimate_tokens(game_agent._message_text_for_budget(m)) + 3
            for m in msgs
        )
        first = game_agent._count_message_tokens_cjk(msgs)   # 冷缓存
        second = game_agent._count_message_tokens_cjk(msgs)  # 全命中
        self.assertEqual(first, expected)
        self.assertEqual(second, expected)


if __name__ == "__main__":
    unittest.main()
