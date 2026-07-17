"""暂存区代码实时流入前端（partial code_update）的节流与事件映射测试。

不起真模型：直接单测 _staging_partial_event（节流判定，时钟可注入）与
_chunk_to_events 的 ToolMessage 分支接线（partial 帧不污染正式提交状态）。

契约：{"type": "code_update", "code": <完整暂存内容>, "partial": true, "source": "staging"}；
正式提交的 code_update 形状不变（无 partial 字段）。
"""

import unittest

import src.agent.game_agent as ga


def _fresh_ss(session_id="staging-test"):
    """按 chat_stream 里 ss 的初始形状构造流状态。"""
    return {"session_id": session_id, "full_reply": "", "last_code_sent": "",
            "last_code_emit_ts": 0.0, "code_pushed": False, "ctx_key": None,
            "staging_push_ts": 0.0, "staging_push_len": 0}


class StagingPartialThrottleTests(unittest.TestCase):
    """节流函数本体：间隔 ≥600ms 且增长 ≥200 字符才推；提交（暂存清空）后复位。"""

    def test_first_push_emits_full_staged_content(self):
        ss = _fresh_ss()
        staged = "<!DOCTYPE html><html>" + "x" * 300
        ev = ga._staging_partial_event(ss, staged, now=100.0)
        self.assertEqual(ev, {"type": "code_update", "code": staged,
                              "partial": True, "source": "staging"})

    def test_interval_too_short_no_push(self):
        ss = _fresh_ss()
        first = "a" * 300
        self.assertIsNotNone(ga._staging_partial_event(ss, first, now=100.0))
        # 增长足够（+500）但距上次仅 0.3s < 0.6s → 不推
        ev = ga._staging_partial_event(ss, first + "b" * 500, now=100.3)
        self.assertIsNone(ev)

    def test_growth_too_small_no_push(self):
        ss = _fresh_ss()
        first = "a" * 300
        self.assertIsNotNone(ga._staging_partial_event(ss, first, now=100.0))
        # 间隔足够（1s）但仅增长 100 字符 < 200 → 不推
        ev = ga._staging_partial_event(ss, first + "b" * 100, now=101.0)
        self.assertIsNone(ev)

    def test_push_when_both_thresholds_met_content_is_full_staging(self):
        ss = _fresh_ss()
        first = "a" * 300
        self.assertIsNotNone(ga._staging_partial_event(ss, first, now=100.0))
        staged2 = first + "b" * 250
        ev = ga._staging_partial_event(ss, staged2, now=100.7)  # 间隔 0.7s、增长 250
        self.assertIsNotNone(ev)
        self.assertEqual(ev["code"], staged2)  # 完整暂存内容，不是增量
        self.assertIs(ev["partial"], True)
        self.assertEqual(ev["source"], "staging")

    def test_below_first_growth_threshold_no_push(self):
        ss = _fresh_ss()
        # 首帧也要满足增长阈值（相对初始 0）：不足 200 字符不推
        self.assertIsNone(ga._staging_partial_event(ss, "tiny", now=100.0))

    def test_commit_resets_throttle_state(self):
        ss = _fresh_ss()
        self.assertIsNotNone(ga._staging_partial_event(ss, "a" * 300, now=100.0))
        # 提交成功：_commit_staging 已 pop 暂存 → staged 为空 → 复位节流状态
        self.assertIsNone(ga._staging_partial_event(ss, "", now=100.1))
        self.assertEqual(ss["staging_push_ts"], 0.0)
        self.assertEqual(ss["staging_push_len"], 0)
        # 复位后同会话新一轮 write_game：即使离上次推送 <600ms 也按全新状态放行
        ev = ga._staging_partial_event(ss, "c" * 250, now=100.2)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["code"], "c" * 250)


class StagingChunkToEventsTests(unittest.TestCase):
    """接线：ToolMessage 到达时从 _staging_by_session 轮询出 partial 帧。"""

    SID = "staging-chunk-events-test"

    def tearDown(self):
        ga._staging_by_session.pop(self.SID, None)

    @staticmethod
    def _tool_chunk(tool_name="write_game", content="已暂存至第 10 行"):
        class ToolMessage:  # _chunk_to_events 按 type(msg).__name__ 识别
            pass

        msg = ToolMessage()
        msg.name = tool_name
        msg.tool_call_id = "call-1"
        msg.content = content
        msg.tool_calls = None
        return {"type": "updates", "data": {"tools": {"messages": [msg]}}}

    def test_tool_message_emits_partial_and_keeps_commit_state_clean(self):
        staged = "<!DOCTYPE html><html><head>" + "x" * 400
        ga._staging_by_session[self.SID] = staged
        ss = _fresh_ss(self.SID)
        evs = ga.GameDesignAgent._chunk_to_events(self._tool_chunk(), ss)
        partials = [e for e in evs if e.get("partial")]
        self.assertEqual(len(partials), 1)
        self.assertEqual(partials[0], {"type": "code_update", "code": staged,
                                       "partial": True, "source": "staging"})
        # partial 帧不得污染正式提交的去重/兜底状态（done 事件与回合末补发依赖它们）
        self.assertEqual(ss["last_code_sent"], "")
        self.assertFalse(ss["code_pushed"])
        # 正式 code_update（source 非 staging）不携带 partial 字段
        for e in evs:
            if e.get("type") == "code_update" and e.get("source") != "staging":
                self.assertNotIn("partial", e)

    def test_no_staging_no_partial_event(self):
        ss = _fresh_ss(self.SID)  # 未写入 _staging_by_session
        evs = ga.GameDesignAgent._chunk_to_events(self._tool_chunk("replace_code"), ss)
        self.assertFalse([e for e in evs if e.get("partial")])

    def test_throttled_second_tool_message_no_partial(self):
        ga._staging_by_session[self.SID] = "a" * 400
        ss = _fresh_ss(self.SID)
        evs1 = ga.GameDesignAgent._chunk_to_events(self._tool_chunk(), ss)
        self.assertEqual(len([e for e in evs1 if e.get("partial")]), 1)
        # 同一暂存内容立刻再来一个 ToolMessage：增长 0 < 200 → 不重复推
        evs2 = ga.GameDesignAgent._chunk_to_events(self._tool_chunk("append_game"), ss)
        self.assertFalse([e for e in evs2 if e.get("partial")])


if __name__ == "__main__":
    unittest.main()
