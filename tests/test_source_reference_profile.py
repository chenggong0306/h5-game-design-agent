import copy
import unittest

from src.knowledge.source_reference_profile import (
    CLASH_OF_VIKINGS_PROFILE_ID,
    FISHJOY_PROFILE_ID,
    SOURCE_REFERENCE_PROFILE_VERSION,
    build_source_reference_profile,
    get_canonical_source_spec,
)


_FISHJOY_GEOMETRY = {
    "fish1": (55, 37, 4, 4),
    "fish2": (78, 64, 4, 4),
    "fish3": (72, 56, 4, 4),
    "fish4": (77, 59, 4, 4),
    "fish5": (107, 122, 4, 4),
    "fish6": (105, 79, 8, 4),
    "fish7": (92, 151, 6, 4),
    "fish8": (174, 126, 8, 4),
    "fish9": (166, 183, 8, 4),
    "fish10": (178, 187, 6, 4),
    "shark1": (509, 270, 8, 4),
    "shark2": (516, 273, 8, 4),
}

_FISHJOY_CANNON_HEIGHTS = (74, 76, 76, 83, 85, 90, 94)
_FISHJOY_CANNON_REG_Y = (45, 46, 46, 52, 55, 58, 60)


def _fishjoy_feature_sources():
    resources = {
        "mainbg": "images/game_bg_2_hd.jpg",
        "bottom": "images/bottom.png",
        **{name: f"images/{name}.png" for name in _FISHJOY_GEOMETRY},
        **{f"cannon{level}": f"images/cannon{level}.png" for level in range(1, 8)},
        "bullet": "images/bullet.png",
        "web": "images/web.png",
        "numBlack": "images/number_black.png",
        "coinAni1": "images/coinAni1.png",
        "coinAni2": "images/coinAni2.png",
        "coinText": "images/coinText.png",
    }
    resource_rows = "\n".join(
        f'{{id:"{resource_id}", src:"{path}?"+Math.random()}},'
        for resource_id, path in resources.items()
    )

    blocks = []
    for type_id, (width, height, swim_count, capture_count) in _FISHJOY_GEOMETRY.items():
        total = swim_count + capture_count
        frame_rows = []
        for frame in range(total):
            metadata = ""
            if frame == 0:
                metadata += ', label:"swim"'
            if frame == swim_count - 1:
                metadata += ', jump:"swim"'
            if frame == swim_count:
                metadata += ', label:"capture"'
            if frame == total - 1:
                metadata += ', jump:"capture"'
            frame_rows.append(
                f'{{rect:[0,{frame * height},{width},{height}]{metadata}}}'
            )
        blocks.append(
            f"""
var {type_id} = {{image:this.getImage(\"{type_id}\"),
frames:[{','.join(frame_rows)}],
polyArea:[{{x:0,y:0}},{{x:{width},y:{height}}}],
mixin:{{coin:1, captureRate:0.5, useFrames:true, interval:10}}}};
"""
        )

    cannon_blocks = []
    for level, (height, reg_y) in enumerate(
        zip(_FISHJOY_CANNON_HEIGHTS, _FISHJOY_CANNON_REG_Y),
        start=1,
    ):
        frame_rows = ",".join(
            f"{{rect:[0,{frame * height},74,{height}]"
            + (", stop:1" if frame == 4 else "")
            + "}"
            for frame in range(5)
        )
        cannon_blocks.append(
            f"""
var cannon{level} = {{image:this.getImage("cannon{level}"),
frames:[{frame_rows}],
mixin:{{regX:37, regY:{reg_y}, useFrames:true, interval:3, power:{level}}}}};
"""
        )

    return [{
        "path": "opaque/resources.payload",
        "language": "javascript",
        "content": f"""
var R = {{}};
R.sources = [{resource_rows}];
R.initResources = function() {{
{''.join(blocks)}
{''.join(cannon_blocks)}
this.fishTypes = [null, fish1, fish2, fish3, fish4, fish5, fish6, fish8, fish9, fish10, fish7, shark1, shark2];
this.cannonTypes = [null, cannon1, cannon2, cannon3, cannon4, cannon5, cannon6, cannon7];
this.bullets = [{{rect:[0,0,24,26]}}];
this.webs = [{{rect:[0,0,116,118]}}];
}};
""",
    }]


def _clash_feature_sources():
    """Small feature-complete fixture deliberately using meaningless file/module names."""
    first = {
        "path": "opaque/alpha.payload",
        "language": "text",
        "content": """
ig.module('opaque.one').requires('impact.entity').defines(function() {
  EntityControlLogic = ig.Entity.extend({
    cardDeck: [], cardHand: [], manaRegenation: 2.5,
    replaceCardInHand: function() {}, spawning: function() {}
  });
});
ig.module('opaque.two').defines(function() {
  EntityTower = ig.Entity.extend({
    rangeShot: 110, damageShot: 60, targetEnemy: 0,
    drawHealthBar: function() {}
  });
});
ig.module('opaque.three').defines(function() {
  EntityBaseTroops = ig.Entity.extend({
    walkSheet: new ig.AnimationSheet('art/blue-walk.png', 55, 65),
    attackSheet: new ig.AnimationSheet('art/blue-attack.png', 55, 65),
    pathChoose: [],
    getBestDistace: function() {},
    initialize: function() {
      this.addAnim('sideWalk', 0.05, [11,12,12,3,3,0]);
      this.addAnim('sideWalk', 0.05, [11,12,12,3,3,0]);
      this.addAnim('sideAttack', 0.05, [12,3,8,13]);
    }
  });
});
""",
    }
    second = {
        "path": "opaque/omega.payload",
        "language": "text",
        "content": """
ig.module('opaque.four').defines(function() {
  LevelGameArea = {
    entities: [EntityTowerSmall, EntityTowerBig, EntityBoardDeck, EntityManaBar]
  };
});
ig.module('opaque.five').defines(function() {
  EntityCard = ig.Entity.extend({
    manausage: 3,
    drawingCard: function() {},
    update: function() {
      ig.game.holdingCard = this;
      if (ig.game.pointer.isPressed || ig.game.pointer.isReleased) {
        this.deck.callNextCard();
      }
    }
  });
});
ig.module('opaque.six').defines(function() {
  MyGame = ig.Game.extend({
    holdingCard: null,
    arrayCardInHand: [5,1,2,3,7,11,13,9],
    level: LevelGameArea
  });
  ig.main('#canvas', MyGame, 60, 480, 640, 1);
});
""",
    }
    return [first, second]


class SourceReferenceProfileTests(unittest.TestCase):
    def test_detects_clash_by_module_features_and_builds_canonical_contract(self):
        profile = build_source_reference_profile(_clash_feature_sources())

        self.assertEqual(profile["profile_version"], SOURCE_REFERENCE_PROFILE_VERSION)
        self.assertEqual(profile["detected_profile"], CLASH_OF_VIKINGS_PROFILE_ID)
        self.assertEqual(
            set(profile["landmark_index"]),
            {"control_logic", "game_area", "card", "tower", "base_troops", "main"},
        )

        spec = profile["canonical_spec"]
        self.assertEqual(spec["id"], CLASH_OF_VIKINGS_PROFILE_ID)
        self.assertEqual(spec["coordinate_system"]["orientation"], "portrait")
        self.assertEqual(spec["factions"]["player"]["side"], "bottom")
        self.assertEqual(spec["factions"]["opponent"]["side"], "top")
        self.assertEqual(spec["lanes"]["ids"], ["left", "right"])
        self.assertEqual([item["x"] for item in spec["lanes"]["bridges"]], [144, 328])
        self.assertEqual(spec["towers"]["total"], 6)
        self.assertEqual(
            [(tower["x"], tower["y"]) for tower in spec["towers"]["positions"]["red"]],
            [(132, 124), (316, 124), (216, 60)],
        )
        self.assertEqual(
            [(tower["x"], tower["y"]) for tower in spec["towers"]["positions"]["blue"]],
            [(132, 396), (316, 396), (218, 428)],
        )
        self.assertEqual(
            [card["id"] for card in spec["cards"]["default_deck"]],
            ["giant", "archer", "ars", "warrior", "fireball", "hammer", "mage", "axeman"],
        )
        self.assertEqual(spec["cards"]["hand_size"], 4)
        self.assertTrue(spec["cards"]["must_cycle_indefinitely"])
        self.assertEqual(spec["mana"]["initial"], 3)
        self.assertEqual(spec["mana"]["normal_seconds_per_point"], 2.5)
        self.assertTrue(spec["loader"]["settled_once_guard"])
        self.assertEqual(spec["loader"]["animation_loop_chains"], 1)
        self.assertEqual(spec["assets"]["forbidden_roles"], ["developer_branding", "placeholder"])
        self.assertIn("media/graphics/background.jpg", spec["assets"]["forbidden_paths"])
        team_groups = spec["assets"]["required_team_asset_groups"]
        self.assertEqual(
            set(team_groups),
            {"giant", "archer", "warrior", "hammer", "mage", "axeman"},
        )
        self.assertEqual(
            team_groups["warrior"]["blue"],
            [
                "media/graphics/game/troops/walk-warrior-b.png",
                "media/graphics/game/troops/warrior-attack-b.png",
            ],
        )
        self.assertEqual(len(spec["assets"]["always_visible_paths"]), 8)
        self.assertTrue(spec["assets"]["binding_rule"]["explicit_map_required"])
        self.assertTrue(spec["assets"]["binding_rule"]["literal_keys_required"])
        self.assertIn("protected dependencies", spec["assets"]["repair_rule"])
        self.assertIn("never a response", spec["assets"]["fallback_rule"])
        self.assertEqual(
            spec["verification"]["repair_precedence"][0],
            "canonical source contract and required source art",
        )

    def test_module_index_is_stable_and_extracts_animation_landmarks(self):
        source_files = _clash_feature_sources()
        profile = build_source_reference_profile(source_files)
        reversed_profile = build_source_reference_profile(list(reversed(source_files)))

        self.assertEqual(profile, reversed_profile)
        self.assertEqual(len(profile["module_index"]), 6)
        self.assertEqual(
            [item["source_path"] for item in profile["module_index"]],
            sorted(item["source_path"] for item in profile["module_index"]),
        )

        troop_landmark = profile["landmark_index"]["base_troops"]
        troop_module = next(
            item
            for item in profile["module_index"]
            if item["key"] == troop_landmark["module_key"]
        )
        self.assertEqual(
            troop_module["animation_sheets"],
            [
                {
                    "property": "walkSheet",
                    "path": "art/blue-walk.png",
                    "frame_width": 55,
                    "frame_height": 65,
                },
                {
                    "property": "attackSheet",
                    "path": "art/blue-attack.png",
                    "frame_width": 55,
                    "frame_height": 65,
                },
            ],
        )
        self.assertEqual(
            troop_module["animation_sequences"],
            [
                {"name": "sideWalk", "frame_time": 0.05, "sequence": [11, 12, 12, 3, 3, 0]},
                {"name": "sideAttack", "frame_time": 0.05, "sequence": [12, 3, 8, 13]},
            ],
        )
        self.assertEqual(
            troop_module["asset_references"],
            ["art/blue-walk.png", "art/blue-attack.png"],
        )
        self.assertGreaterEqual(troop_module["end_line"], troop_module["start_line"])
        self.assertEqual(len(troop_module["sha256_16"]), 16)

    def test_canonical_contract_contains_source_derived_troop_animation_catalog(self):
        sources = _clash_feature_sources()
        sources[0]["content"] += """
ig.module('opaque.troopers.archer').defines(function() {
  EntityArcher = EntityBaseTroops.extend({
    attackSheet: new ig.AnimationSheet('media/troops/archer-a.png', 40, 50),
    attackRSheet: new ig.AnimationSheet('media/troops/archer-ar.png', 40, 50),
    walkSheet: new ig.AnimationSheet('media/troops/archer-w.png', 40, 50),
    walkRSheet: new ig.AnimationSheet('media/troops/archer-wr.png', 40, 50),
    initialize: function() {
      this.addAnim('sideAttack', 0.05, [6,8,0,10,3]);
      this.addAnim('downAttack', 0.05, [1,9,4,5,2]);
      this.addAnim('upAttack', 0.05, [7,11,12,13,14]);
      this.addAnim('downWalk', 0.05, [1,21,6,7,2]);
      this.addAnim('sideWalk', 0.05, [9,15,18,19,20]);
      this.addAnim('upWalk', 0.05, [5,11,17,23,24]);
    }
  });
});
"""

        profile = build_source_reference_profile(sources)
        catalog = profile["canonical_spec"]["animation"]["troop_catalog"]

        self.assertEqual(set(catalog), {"archer"})
        self.assertEqual(
            catalog["archer"]["blue"]["walk"],
            {
                "path": "media/troops/archer-w.png",
                "frame_width": 40,
                "frame_height": 50,
            },
        )
        self.assertEqual(
            catalog["archer"]["red"]["attack"]["path"],
            "media/troops/archer-ar.png",
        )
        self.assertEqual(
            set(catalog["archer"]["states"]),
            {"upWalk", "downWalk", "sideWalk", "upAttack", "downAttack", "sideAttack"},
        )
        self.assertEqual(
            catalog["archer"]["states"]["sideAttack"]["sequence"],
            [6, 8, 0, 10, 3],
        )

    def test_names_and_paths_alone_do_not_trigger_profile(self):
        source_files = [
            {
                "path": "clash-of-vikings/game.js",
                "language": "javascript",
                "content": """
ig.module('game.entities.control-logic').defines(function() {});
ig.module('game.levels.game-area').defines(function() {});
ig.module('game.entities.card').defines(function() {});
ig.module('game.entities.tower').defines(function() {});
ig.module('game.entities.troopers.base-troops').defines(function() {});
ig.module('game.main').defines(function() {});
""",
            }
        ]

        profile = build_source_reference_profile(source_files)

        self.assertEqual(len(profile["module_index"]), 6)
        self.assertEqual(profile["landmark_index"], {})
        self.assertIsNone(profile["detected_profile"])
        self.assertIsNone(profile["canonical_spec"])
        self.assertIsNone(get_canonical_source_spec(source_files))

    def test_detects_fishjoy_and_extracts_exact_vertical_animation_catalog(self):
        profile = build_source_reference_profile(_fishjoy_feature_sources())

        self.assertEqual(profile["detected_profile"], FISHJOY_PROFILE_ID)
        self.assertEqual(profile["module_index"], [])
        spec = profile["canonical_spec"]
        self.assertEqual(spec["id"], FISHJOY_PROFILE_ID)
        self.assertEqual(spec["animation"]["frame_axis"], "vertical")
        self.assertEqual(spec["animation"]["source_fps"], 60)
        self.assertEqual(spec["animation"]["interval_unit"], "source-update-ticks")
        self.assertEqual(spec["animation"]["required_states"], ["swim", "capture"])
        self.assertIn("horizontal sprite-sheet cropping", spec["animation"]["forbidden"])

        catalog = spec["animation"]["fish_catalog"]
        self.assertEqual(
            list(catalog),
            [
                "fish1", "fish2", "fish3", "fish4", "fish5", "fish6",
                "fish8", "fish9", "fish10", "fish7", "shark1", "shark2",
            ],
        )
        self.assertEqual(
            {item["orientation"] for item in catalog.values()},
            {"vertical"},
        )
        self.assertEqual(catalog["fish1"]["frame_width"], 55)
        self.assertEqual(catalog["fish1"]["frame_height"], 37)
        self.assertEqual(catalog["fish1"]["state_sequences"]["swim"], [0, 1, 2, 3])
        self.assertEqual(catalog["fish1"]["state_sequences"]["capture"], [4, 5, 6, 7])
        self.assertEqual(catalog["fish5"]["frame_height"], 122)
        self.assertEqual(catalog["fish7"]["frame_height"], 151)
        self.assertEqual(catalog["fish10"]["frame_height"], 187)
        self.assertEqual(catalog["fish10"]["swim_frames"], [0, 1, 2, 3, 4, 5])
        self.assertEqual(catalog["fish10"]["capture_frames"], [6, 7, 8, 9])
        self.assertEqual(catalog["shark2"]["frame_width"], 516)
        self.assertEqual(catalog["shark2"]["frame_height"], 273)
        self.assertEqual(catalog["shark2"]["source_rects"][-1], [0, 3003, 516, 273])
        self.assertTrue(all(item["interval"] == 10 for item in catalog.values()))
        self.assertTrue(
            all(item["interval_unit"] == "source-update-ticks" for item in catalog.values())
        )

        cannon_catalog = spec["animation"]["cannon_catalog"]
        self.assertEqual(list(cannon_catalog), [f"cannon{i}" for i in range(1, 8)])
        self.assertEqual(cannon_catalog["cannon1"]["frame_width"], 74)
        self.assertEqual(cannon_catalog["cannon1"]["frame_height"], 74)
        self.assertEqual(cannon_catalog["cannon1"]["reg_y"], 45)
        self.assertEqual(cannon_catalog["cannon7"]["frame_height"], 94)
        self.assertEqual(cannon_catalog["cannon7"]["reg_y"], 60)
        self.assertEqual(cannon_catalog["cannon7"]["fire_frames"], [0, 1, 2, 3, 4])
        self.assertEqual(cannon_catalog["cannon7"]["stop_frame"], 4)
        self.assertEqual(
            cannon_catalog["cannon7"]["source_rects"][-1],
            [0, 376, 74, 94],
        )
        self.assertEqual(spec["layout"]["bottom"]["source_rect"], [0, 0, 765, 72])
        self.assertEqual(spec["layout"]["bottom"]["logical_y"], 475)
        self.assertEqual(spec["layout"]["cannon"]["logical_x"], 532.5)
        self.assertEqual(spec["layout"]["cannon"]["logical_y"], 535)

        required_paths = spec["assets"]["required_paths"]
        self.assertEqual(len(required_paths), 27)
        self.assertIn("images/game_bg_2_hd.jpg", required_paths)
        self.assertIn("images/web.png", required_paths)
        self.assertIn("images/number_black.png", required_paths)
        self.assertIn("images/shark2.png", required_paths)
        self.assertEqual(
            spec["assets"]["resource_catalog"]["numBlack"],
            "images/number_black.png",
        )

    def test_fishjoy_requires_complete_source_declared_roster(self):
        incomplete = _fishjoy_feature_sources()
        incomplete[0]["content"] = incomplete[0]["content"].replace(
            "var shark2 = {image:this.getImage(\"shark2\")",
            "var missingShark = {image:this.getImage(\"shark2\")",
        )

        profile = build_source_reference_profile(incomplete)

        self.assertIsNone(profile["detected_profile"])
        self.assertIsNone(profile["canonical_spec"])

    def test_fishjoy_callers_receive_fresh_canonical_spec(self):
        sources = _fishjoy_feature_sources()
        first = get_canonical_source_spec(sources)
        first["animation"]["fish_catalog"]["fish1"]["frame_height"] = 999
        first["animation"]["cannon_catalog"]["cannon1"]["frame_height"] = 999
        first["assets"]["required_paths"].clear()

        second = get_canonical_source_spec(sources)

        self.assertEqual(second["animation"]["fish_catalog"]["fish1"]["frame_height"], 37)
        self.assertEqual(second["animation"]["cannon_catalog"]["cannon1"]["frame_height"], 74)
        self.assertEqual(len(second["assets"]["required_paths"]), 27)

    def test_callers_receive_fresh_canonical_spec(self):
        sources = _clash_feature_sources()
        first = get_canonical_source_spec(sources)
        original = copy.deepcopy(first)
        first["towers"]["total"] = 99
        first["cards"]["default_deck"].clear()

        second = get_canonical_source_spec(sources)

        self.assertEqual(second, original)
        self.assertEqual(second["towers"]["total"], 6)
        self.assertEqual(len(second["cards"]["default_deck"]), 8)


if __name__ == "__main__":
    unittest.main()
