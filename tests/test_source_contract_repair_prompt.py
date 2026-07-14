import unittest

from src.agent.game_agent import GameDesignAgent, SYSTEM_PROMPT


class SourceContractRepairPromptTests(unittest.TestCase):
    def test_unused_asset_repair_preserves_contract_art_and_demands_explicit_mapping(self):
        spec = {
            "id": "clash-of-vikings@1",
            "assets": {
                "required_paths": ["media/graphics/game/game-bg.png"],
                "binding_rule": {"explicit_map_required": True},
            },
        }
        message = GameDesignAgent._build_repair_message(
            [{
                "id": "excessive_unused_assets",
                "severity": "medium",
                "msg": "预加载清单中有 22/31 个图片键从未被绘制或读取",
                "fix": "删除未使用素材，只保留少量图片",
            }],
            [spec],
        )

        self.assertNotIn("删除未使用素材，只保留少量图片", message)
        self.assertIn("源码契约优先", message)
        self.assertIn("严禁删除 required_paths", message)
        self.assertIn("animation catalog", message)
        self.assertIn("字面量静态映射", message)
        self.assertIn("images[cardId + suffix]", message)
        self.assertIn("严禁删除契约 required_paths", message)
        self.assertIn("通用品类/未使用预加载告警", message)

    def test_generation_prompt_protects_contract_assets_from_generic_unused_lint(self):
        self.assertIn("通用“未使用素材”告警的优先级低于契约", SYSTEM_PROMPT)
        self.assertIn("字面量静态映射", SYSTEM_PROMPT)
        self.assertIn("animation catalog", SYSTEM_PROMPT)
        self.assertIn("捕鱼纵向精灵", SYSTEM_PROMPT)
        self.assertIn("sx=0, sy=frame*frameHeight", SYSTEM_PROMPT)
        self.assertIn("炮台、炮弹出生点和瞄准必须共用随当前画布变化的动态原点", SYSTEM_PROMPT)
        self.assertIn("禁止把 980 宽源码中的 `x=490`", SYSTEM_PROMPT)
        self.assertIn("必须先画底栏再画炮台", SYSTEM_PROMPT)

    def test_fishjoy_repair_uses_exact_vertical_frame_contract(self):
        spec = {
            "id": "fishjoy@1",
            "animation": {
                "frame_axis": "vertical",
                "required_states": ["swim", "capture"],
            },
        }
        message = GameDesignAgent._build_repair_message(
            [{
                "id": "source_rect_out_of_bounds",
                "severity": "high",
                "msg": "鱼精灵裁帧越界",
                "fix": "检查裁帧",
            }],
            [spec],
        )

        self.assertIn("鱼/鲨 → swim/capture", message)
        self.assertIn("naturalWidth/naturalHeight", message)
        self.assertIn("禁止根据图片总尺寸猜帧高", message)
        self.assertIn("不得称为误报", message)

    def test_fishjoy_cannon_repair_demands_responsive_shared_origin_and_recoil_frames(self):
        message = GameDesignAgent._build_repair_message(
            [{
                "id": "source_contract_cannon_position_invalid:fishjoy@1",
                "severity": "high",
                "msg": "炮台位于响应式画布外",
                "fix": "修正炮台位置",
            }],
            [{"id": "fishjoy@1"}],
        )

        self.assertIn("纵向五帧是开火/后坐力动画", message)
        self.assertIn("炮台、炮弹出生点和瞄准必须共用", message)
        self.assertIn("禁止直接使用源码桌面坐标 x=490/532.5", message)
        self.assertIn("先画底栏再画炮台", message)

    def test_asset_failure_repair_does_not_invent_cors_explanation(self):
        message = GameDesignAgent._build_repair_message(
            [{
                "id": "asset_load_failure",
                "severity": "medium",
                "msg": "本地素材加载失败",
                "fix": "检查图片",
            }],
            [{"id": "fishjoy@1"}],
        )

        self.assertIn("逐字符对照", message)
        self.assertIn("不得归因于 CORS、ORB、缓存或误报", message)


if __name__ == "__main__":
    unittest.main()
