import asyncio
import unittest
from unittest import mock

import src.agent.game_agent as game_agent
from src.agent.game_agent import GameDesignAgent
from src.knowledge.source_modes import (
    SOURCE_MODE_FAITHFUL,
    default_source_mode,
    web_bundle_readiness,
)


class SourceModeTests(unittest.TestCase):
    def test_complete_local_web_project_defaults_to_faithful_port(self):
        manifest = {
            "entrypoint": "game/index.html",
            "dependencies": [
                {"reference": "app.css", "status": "readable"},
                {"reference": "app.min.js", "status": "readable"},
            ],
        }
        self.assertEqual(default_source_mode(manifest), SOURCE_MODE_FAITHFUL)
        self.assertEqual(web_bundle_readiness(manifest), (True, []))

    def test_missing_or_external_runtime_dependency_cannot_claim_faithful(self):
        manifest = {
            "entrypoint": "game/index.html",
            "dependencies": [
                {"reference": "missing.js", "status": "missing"},
                {"reference": "https://cdn.example/lib.js", "status": "external"},
            ],
        }
        runnable, reasons = web_bundle_readiness(manifest)
        self.assertFalse(runnable)
        self.assertEqual(default_source_mode(manifest), "inspired")
        self.assertTrue(any("missing.js" in reason for reason in reasons))
        self.assertTrue(any("cdn.example" in reason for reason in reasons))


class SourceBaselineRollbackTests(unittest.TestCase):
    def setUp(self):
        self.session_id = "source-baseline-test"
        self.token = game_agent._current_session_id.set(self.session_id)
        game_agent._code_by_session.pop(self.session_id, None)
        game_agent._source_baselines_by_session.pop(self.session_id, None)

    def tearDown(self):
        game_agent._code_by_session.pop(self.session_id, None)
        game_agent._source_baselines_by_session.pop(self.session_id, None)
        game_agent._current_session_id.reset(self.token)

    def test_failed_enhancement_rolls_back_verified_source_baseline(self):
        baseline = (
            '<!doctype html><html><head><meta name="source-port-mode" content="extend">'
            '<meta name="source-port-skill" content="Fish"></head><body>'
            '<canvas></canvas><script>const working=true;</script></body></html>'
        )
        broken = baseline.replace("const working=true", "throw new Error('broken')")
        game_agent._code_by_session[self.session_id] = broken
        game_agent._source_baselines_by_session[self.session_id] = {
            "code": baseline,
            "skill_name": "Fish",
            "mode": "extend",
            "verification": None,
        }
        passed = {"ok": True, "blocking": [], "warnings": [], "issues": []}
        failed = {
            "ok": False,
            "blocking": [{"id": "runtime_error", "severity": "high", "msg": "broken", "fix": "fix"}],
            "warnings": [],
            "issues": [],
        }

        agent = GameDesignAgent.__new__(GameDesignAgent)

        async def fake_verify(code):
            return passed if code == baseline else failed

        agent._verify_code = fake_verify
        with (
            mock.patch.object(game_agent.settings, "self_check_max_rounds", 0),
            mock.patch.object(game_agent, "save_session_code", return_value=True),
        ):
            reply, result, status = asyncio.run(
                agent._self_check_nonstream(self.session_id, {}, "done")
            )

        self.assertEqual(reply, "done")
        self.assertEqual(status, "rolled_back")
        self.assertIs(result, passed)
        self.assertEqual(game_agent._code_by_session[self.session_id], baseline)

    def test_existing_port_becomes_next_turn_baseline(self):
        code = (
            '<html><head><meta content="faithful_port" name="source-port-mode">'
            '<meta content="小鱼" name="source-port-skill"></head></html>'
        )
        game_agent._prepare_turn_source_baseline(self.session_id, code)
        baseline = game_agent._source_baselines_by_session[self.session_id]
        self.assertEqual(baseline["code"], code)
        self.assertEqual(baseline["skill_name"], "小鱼")
        self.assertEqual(baseline["mode"], "faithful_port")


if __name__ == "__main__":
    unittest.main()
