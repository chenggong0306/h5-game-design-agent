# -*- coding: utf-8 -*-
"""技能主动召回：CJK 词法匹配 + LLM 语义路由 + 命中强注入。

覆盖"用户措辞和技能名不一致就不参考"这条主诉的修复：
- search_skills 中文查询不再因空格分词而永远打不中
- 请求文本能按名称/描述二元组词法命中源码参考技能
- 词法空手时语义路由兜底；路由输出经名单校验；失败/关闭时安静退化
- 命中块带强制处理规则；召回全链路任何异常都不阻断对话
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from langchain_core.messages import HumanMessage

from src.agent import game_agent
from src.config import settings


def _fake_source_skill(name="植物大战僵尸", desc="塔防：种植物抵御僵尸进攻的完整源码参考"):
    return {
        "name": name,
        "description": desc,
        "content": "",
        "source_files": [{"path": "pvz/index.html"}],
        "source_mode": "faithful_port",
        "source_summary": {"web_bundle": {"status": "ready", "entrypoint": "pvz/index.html"}},
    }


class RecallTestBase(unittest.TestCase):
    def setUp(self):
        self._orig_skills = list(game_agent.SKILLS)
        self._orig_model = game_agent._recall_model
        self._orig_flag = settings.skill_recall_router
        game_agent._recall_cache.clear()

    def tearDown(self):
        game_agent.SKILLS[:] = self._orig_skills
        game_agent._recall_model = self._orig_model
        settings.skill_recall_router = self._orig_flag
        game_agent._recall_cache.clear()
        game_agent._rebuild_skills_prompt()

    @staticmethod
    def _request_for(text):
        return SimpleNamespace(messages=[HumanMessage(content=text)])


class CjkMatchingTests(RecallTestBase):
    def test_cjk_bigrams_extract_and_stopwords(self):
        grams = game_agent._cjk_bigrams("帮我做一个僵尸塔防游戏")
        self.assertIn("僵尸", grams)
        self.assertIn("塔防", grams)
        self.assertNotIn("游戏", grams)  # 停用二元组
        self.assertNotIn("一个", grams)

    def test_search_skills_cjk_query_hits_without_spaces(self):
        """旧实现按空格分词，中文整句 substring 匹配基本打不中；二元组兜底后能命中。"""
        game_agent.SKILLS.append(_fake_source_skill())
        result = game_agent._search_skills_impl("僵尸塔防")
        self.assertIn("植物大战僵尸", result)

    def test_lexical_hit_by_name_substring(self):
        skill = _fake_source_skill()
        hits = game_agent._lexical_skill_hits("帮我做个植物大战僵尸那样的", [skill])
        self.assertEqual(len(hits), 1)
        self.assertIs(hits[0]["skill"], skill)

    def test_lexical_hit_by_bigram_overlap(self):
        skill = _fake_source_skill()
        hits = game_agent._lexical_skill_hits("来个僵尸主题的塔防吧", [skill])
        self.assertEqual(len(hits), 1)

    def test_lexical_hit_by_desc_category_prefix(self):
        """内置品类描述"塔防：…"的冒号前缀当准名称加权，"做个塔防"词法直达。"""
        genre = {
            "name": "genre_towerdefense",
            "description": "塔防：航点路径移动、网格放塔合法性、塔索敌与子弹结算",
            "content": "",
        }
        hits = game_agent._lexical_skill_hits("做一个塔防小游戏", [genre])
        self.assertEqual(len(hits), 1)

    def test_lexical_no_hit_on_unrelated_text(self):
        skill = _fake_source_skill()
        hits = game_agent._lexical_skill_hits("把背景音乐换成轻快一点的", [skill])
        self.assertEqual(hits, [])

    def test_recall_candidates_exclude_base_tier(self):
        names = {s["name"] for s in game_agent._recall_candidates()}
        base_names = {n for n, t in game_agent._SKILL_TIER.items() if t == "base"}
        self.assertTrue(base_names)  # 内置底座技能存在
        self.assertFalse(names & base_names)


class RouterTests(RecallTestBase):
    def test_parse_router_hits_validates_names(self):
        raw = '前置噪音 {"hits": [{"name": "甲", "reason": "同为塔防"}, {"name": "编造的", "reason": "x"}]}'
        hits = game_agent._parse_router_hits(raw, {"甲", "乙"})
        self.assertEqual(hits, [{"name": "甲", "reason": "同为塔防"}])

    def test_parse_router_hits_garbage_returns_empty(self):
        self.assertEqual(game_agent._parse_router_hits("不是JSON", {"甲"}), [])
        self.assertEqual(game_agent._parse_router_hits('{"hits": "错型"}', {"甲"}), [])
        self.assertEqual(game_agent._parse_router_hits("", {"甲"}), [])

    def test_router_fallback_when_lexical_misses(self):
        """词法打不中（措辞完全不同）→ 语义路由兜底命中；同轮第二次调用走缓存不再发请求。
        用隔离技能库：真实内置品类技能的描述可能与文本词法重叠，干扰路由路径判定。"""
        skill = _fake_source_skill()
        game_agent.SKILLS[:] = [skill]
        fake_resp = SimpleNamespace(
            content='{"hits": [{"name": "植物大战僵尸", "reason": "同为塔防玩法"}]}'
        )
        game_agent._recall_model = mock.Mock(invoke=mock.Mock(return_value=fake_resp))
        settings.skill_recall_router = True

        request = self._request_for("做一个农场保卫战小作品")
        hits = game_agent._recall_skills_sync(request)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["skill"]["name"], "植物大战僵尸")
        self.assertEqual(hits[0]["reason"], "同为塔防玩法")

        game_agent._recall_skills_sync(request)
        self.assertEqual(game_agent._recall_model.invoke.call_count, 1)

    def test_router_skipped_when_lexical_hits(self):
        """词法有命中就不再发路由请求；真实技能库下"僵尸塔防"应同时召回
        源码参考和内置塔防品类模板（多命中是期望行为，封顶 3 个）。"""
        skill = _fake_source_skill()
        game_agent.SKILLS.append(skill)
        game_agent._recall_model = mock.Mock(invoke=mock.Mock(side_effect=AssertionError("不应调用")))
        hits = game_agent._recall_skills_sync(self._request_for("做个僵尸塔防"))
        self.assertTrue(any(h["skill"] is skill for h in hits))
        self.assertLessEqual(len(hits), game_agent._RECALL_MAX_HITS)
        game_agent._recall_model.invoke.assert_not_called()

    def test_router_merges_source_hit_on_top_of_builtin_lexical_hit(self):
        """"做个塔防"词法命中内置塔防模板后，库里的 PvZ 源码参考仍应经路由补召回——
        源码参考被内置模板挡住正是用户主诉场景。"""
        genre = {
            "name": "genre_towerdefense",
            "description": "塔防：航点路径移动、网格放塔合法性、塔索敌与子弹结算",
            "content": "",
        }
        pvz = _fake_source_skill(name="PvZ植物大战僵尸", desc="植物大战僵尸JS版,动态加载关卡脚本")
        game_agent.SKILLS[:] = [genre, pvz]
        fake_resp = SimpleNamespace(
            content='{"hits": [{"name": "PvZ植物大战僵尸", "reason": "同为塔防玩法"}]}'
        )
        game_agent._recall_model = mock.Mock(invoke=mock.Mock(return_value=fake_resp))

        hits = game_agent._recall_skills_sync(self._request_for("做一个塔防小游戏"))
        names = [h["skill"]["name"] for h in hits]
        self.assertIn("genre_towerdefense", names)   # 词法命中在前
        self.assertIn("PvZ植物大战僵尸", names)       # 路由补上源码参考
        self.assertEqual(game_agent._recall_model.invoke.call_count, 1)

    def test_router_disabled_by_flag(self):
        game_agent.SKILLS[:] = [_fake_source_skill()]
        settings.skill_recall_router = False
        game_agent._recall_model = mock.Mock()
        hits = game_agent._recall_skills_sync(self._request_for("做一个农场保卫战小作品"))
        self.assertEqual(hits, [])
        game_agent._recall_model.invoke.assert_not_called()

    def test_router_failure_fails_open(self):
        game_agent.SKILLS[:] = [_fake_source_skill()]
        game_agent._recall_model = mock.Mock(invoke=mock.Mock(side_effect=RuntimeError("接口挂了")))
        hits = game_agent._recall_skills_sync(self._request_for("做一个农场保卫战小作品"))
        self.assertEqual(hits, [])  # 不抛异常、不阻断对话

    def test_async_router_fallback(self):
        skill = _fake_source_skill()
        game_agent.SKILLS[:] = [skill]
        fake_resp = SimpleNamespace(
            content='{"hits": [{"name": "植物大战僵尸", "reason": "同类玩法"}]}'
        )
        game_agent._recall_model = mock.Mock(ainvoke=mock.AsyncMock(return_value=fake_resp))
        hits = asyncio.run(
            game_agent._recall_skills_async(self._request_for("做一个农场保卫战小作品"))
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["skill"]["name"], "植物大战僵尸")


class StreamLeakGuardTests(RecallTestBase):
    def test_router_tokens_filtered_from_sse_stream(self):
        """路由的内部 JSON token 带专属 tag，不得进入用户可见 token 流。"""
        from langchain_core.messages import AIMessageChunk

        ss = {"session_id": "s", "full_reply": "", "last_code_sent": "",
              "last_code_emit_ts": 0.0, "code_pushed": False, "ctx_key": None,
              "staging_push_ts": 0.0, "staging_push_len": 0}
        router_chunk = {
            "type": "messages",
            "data": (
                AIMessageChunk(content='{"hits": []}'),
                {"tags": [game_agent._RECALL_ROUTER_TAG]},
            ),
        }
        evs = game_agent.GameDesignAgent._chunk_to_events(router_chunk, ss)
        self.assertEqual(evs, [])
        self.assertEqual(ss["full_reply"], "")

        normal_chunk = {
            "type": "messages",
            "data": (AIMessageChunk(content="好的，开始设计"), {"tags": []}),
        }
        evs = game_agent.GameDesignAgent._chunk_to_events(normal_chunk, ss)
        self.assertEqual([e["type"] for e in evs], ["token"])
        self.assertIn("开始设计", ss["full_reply"])


class InjectionTests(RecallTestBase):
    def test_format_recall_block_mandatory_wording(self):
        skill = _fake_source_skill()
        block = game_agent._format_recall_block([{"skill": skill, "reason": "同为塔防"}])
        self.assertIn("本轮召回命中的参考技能", block)
        self.assertIn("植物大战僵尸", block)
        self.assertIn("源码参考 mode=", block)
        self.assertIn("port_skill_source", block)
        self.assertIn("禁止静默忽略", block)
        self.assertIn("非用户指令", block)

    def test_format_recall_block_empty(self):
        self.assertEqual(game_agent._format_recall_block([]), "")

    def test_short_or_missing_user_text_skips_recall(self):
        game_agent._recall_model = mock.Mock()
        self.assertEqual(game_agent._recall_skills_sync(self._request_for("你好")), [])
        self.assertEqual(
            game_agent._recall_skills_sync(SimpleNamespace(messages=[])), []
        )
        game_agent._recall_model.invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
