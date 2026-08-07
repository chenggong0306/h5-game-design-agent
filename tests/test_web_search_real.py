# -*- coding: utf-8 -*-
"""联网搜索·真环境测试（零 mock）：真发请求验证 provider 链路与降级行为。

网络不可达时 skip 并说明（本机代理环境实测可达）。
"""
import unittest

import httpx

from src.agent import game_agent
from src.config import settings
from src.knowledge import web_search


def _network_ok() -> bool:
    try:
        httpx.head("https://duckduckgo.com", timeout=6)
        return True
    except Exception:
        return False


_NET = _network_ok()


@unittest.skipUnless(_NET, "外网不可达：检查代理后重跑")
class WebSearchRealTests(unittest.TestCase):
    def test_real_search_returns_linked_results(self):
        results = web_search.search("2048 game rules how to play", max_results=4)
        self.assertGreaterEqual(len(results), 1)
        for r in results:
            self.assertTrue(r["url"].startswith("http"), r)
        text = web_search.format_results(results)
        self.assertIn("http", text)

    def test_chinese_query_works(self):
        results = web_search.search("麻将 胡牌 规则", max_results=3)
        self.assertGreaterEqual(len(results), 1)

    def test_agent_tool_end_to_end(self):
        out = game_agent.web_search.invoke({"query": "tetris scoring rules", "max_results": 3})
        self.assertIn("http", out)
        self.assertNotIn("搜索失败", out)


class WebSearchBehaviorTests(unittest.TestCase):
    def test_disabled_flag_short_circuits(self):
        orig = settings.web_search_enabled
        settings.web_search_enabled = False
        try:
            self.assertEqual(web_search.search("anything"), [])
            out = game_agent.web_search.invoke({"query": "anything"})
            self.assertIn("关闭", out)
        finally:
            settings.web_search_enabled = orig

    def test_empty_query_and_caps(self):
        self.assertEqual(web_search.search("  "), [])
        self.assertEqual(web_search.format_results([]),
                         "没有搜到结果。换个关键词试试，或凭已有知识继续。")


if __name__ == "__main__":
    unittest.main()
