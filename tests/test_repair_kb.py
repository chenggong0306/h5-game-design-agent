# -*- coding: utf-8 -*-
"""修复知识库：签名归一化、成功经验入库、修复 prompt 附历史做法、容量与容错。

学自 OpenGame Debug Skill 的 living protocol：自修成功 → 入库 → 同类问题复用。
知识库必须 fail-open——它自身的任何故障都不能反过来阻断自检闭环。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.agent import game_agent, repair_kb
from src.config import settings


class RepairKbBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = settings.chroma_persist_dir
        settings.chroma_persist_dir = str(Path(self._tmp.name) / "chroma_db")
        repair_kb.reset_cache_for_tests()

    def tearDown(self):
        settings.chroma_persist_dir = self._orig_dir
        repair_kb.reset_cache_for_tests()
        self._tmp.cleanup()


class SignatureTests(RepairKbBase):
    def test_normalizes_line_numbers_urls_paths(self):
        a = {"id": "runtime_error", "msg": "TypeError: x is undefined at http://localhost:8010/play/abc line 123"}
        b = {"id": "runtime_error", "msg": "TypeError: x is undefined at http://localhost:9999/play/xyz line 456"}
        self.assertEqual(repair_kb.signature(a), repair_kb.signature(b))

    def test_different_issue_ids_differ(self):
        a = {"id": "no_html_close", "msg": "同样的文案"}
        b = {"id": "dpr_css_size", "msg": "同样的文案"}
        self.assertNotEqual(repair_kb.signature(a), repair_kb.signature(b))


class RecordAndHintTests(RepairKbBase):
    def test_record_then_hint_roundtrip(self):
        issue = {"id": "ctx_scale_dpr", "msg": "使用了 ctx.scale 而非 setTransform"}
        n = repair_kb.record_success([issue], "把 ctx.scale(dpr,dpr) 换成 ctx.setTransform(dpr,0,0,dpr,0,0)")
        self.assertEqual(n, 1)
        hint = repair_kb.hints_for([issue])
        self.assertIn("历史修复成功 1 次", hint)
        self.assertIn("setTransform", hint)
        # 磁盘上真实存在且原子写入
        kb_file = Path(settings.chroma_persist_dir).parent / "repair_kb.json"
        self.assertTrue(kb_file.exists())
        self.assertIn("ctx_scale_dpr", kb_file.read_text(encoding="utf-8"))

    def test_hits_accumulate_and_summary_updates(self):
        issue = {"id": "runtime_error", "msg": "ReferenceError: foo is not defined line 10"}
        repair_kb.record_success([issue], "第一次的做法")
        repair_kb.record_success([{"id": "runtime_error", "msg": "ReferenceError: foo is not defined line 99"}], "更好的做法")
        hint = repair_kb.hints_for([issue])
        self.assertIn("历史修复成功 2 次", hint)
        self.assertIn("更好的做法", hint)
        self.assertNotIn("第一次的做法", hint)

    def test_no_match_returns_empty(self):
        self.assertEqual(repair_kb.hints_for([{"id": "别的", "msg": "没见过"}]), "")
        self.assertEqual(repair_kb.hints_for([]), "")

    def test_empty_summary_not_recorded(self):
        self.assertEqual(repair_kb.record_success([{"id": "x", "msg": "y"}], "   "), 0)

    def test_capacity_eviction_keeps_strongest(self):
        with mock.patch.object(repair_kb, "_KB_MAX_ENTRIES", 3):
            strong = {"id": "keep", "msg": "高频问题"}
            repair_kb.record_success([strong], "做法")
            repair_kb.record_success([strong], "做法")
            for i in range(4):
                repair_kb.record_success([{"id": f"weak{i}", "msg": f"低频{i}"}], "做法")
            self.assertIn("高频问题", repair_kb.hints_for([strong]))
            entries = repair_kb._load()["entries"]
            self.assertLessEqual(len(entries), 3)

    def test_corrupted_file_starts_fresh(self):
        kb_file = Path(settings.chroma_persist_dir).parent / "repair_kb.json"
        kb_file.parent.mkdir(parents=True, exist_ok=True)
        kb_file.write_text("不是JSON{{{", encoding="utf-8")
        repair_kb.reset_cache_for_tests()
        self.assertEqual(repair_kb.hints_for([{"id": "x", "msg": "y"}]), "")
        self.assertEqual(repair_kb.record_success([{"id": "x", "msg": "y"}], "做法"), 1)


class WiringTests(RepairKbBase):
    def test_repair_message_carries_kb_hint(self):
        issue = {"id": "no_mouse_input", "msg": "只绑定了 touchstart，桌面无法开始", "fix": "加 pointerdown"}
        repair_kb.record_success([issue], "改用 Pointer Events 统一鼠标+触摸")
        msg = game_agent.GameDesignAgent._build_repair_message([issue])
        self.assertIn("修复知识库", msg)
        self.assertIn("Pointer Events", msg)

    def test_repair_message_clean_when_no_history(self):
        msg = game_agent.GameDesignAgent._build_repair_message(
            [{"id": "首次见", "msg": "全新问题", "fix": ""}]
        )
        self.assertNotIn("修复知识库", msg)

    def test_record_repair_outcome_filters_unfixed(self):
        fixed = {"id": "a", "msg": "修掉了"}
        unfixed = {"id": "b", "msg": "还在"}
        game_agent._record_repair_outcome(
            [fixed, unfixed],
            {"ok": False, "blocking": [unfixed]},
            "只修好了 a",
        )
        self.assertIn("只修好了 a", repair_kb.hints_for([fixed]))
        self.assertEqual(repair_kb.hints_for([unfixed]), "")

    def test_record_repair_outcome_fail_open(self):
        # record_success 抛异常也不能让闭环崩
        with mock.patch.object(repair_kb, "record_success", side_effect=RuntimeError("磁盘炸了")):
            game_agent._record_repair_outcome(
                [{"id": "a", "msg": "x"}], {"ok": True, "blocking": []}, "做法"
            )  # 不抛即通过


if __name__ == "__main__":
    unittest.main()
