"""Deterministic profiles for large uploaded source-reference projects.

Source skills can contain a single bundled JavaScript file that is much larger than
the model context used by ``load_skill_web_bundle``.  This module indexes ImpactJS
modules without executing them and exposes compact semantic landmarks.  A narrowly
matched Clash of Vikings source project also receives a machine-readable canonical
contract so downstream prompt and verification layers do not have to rediscover its
core rules from thousands of source lines on every generation.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Iterable
from typing import Any


SOURCE_REFERENCE_PROFILE_VERSION = 1
CLASH_OF_VIKINGS_PROFILE_ID = "clash-of-vikings@1"
FISHJOY_PROFILE_ID = "fishjoy@1"

_IMPACT_MODULE_RE = re.compile(
    r"\big\.module\s*\(\s*(?P<quote>['\"])(?P<name>[^'\"\r\n]+)(?P=quote)\s*\)",
    re.IGNORECASE,
)
_REQUIRES_RE = re.compile(
    r"\.requires\s*\((?P<body>.*?)\)\s*\.defines\s*\(",
    re.IGNORECASE | re.DOTALL,
)
_QUOTED_STRING_RE = re.compile(r"(?P<quote>['\"])(?P<value>[^'\"\r\n]+)(?P=quote)")
_ASSET_REFERENCE_RE = re.compile(
    r"new\s+ig\.(?:Image|AnimationSheet)\s*\(\s*"
    r"(?P<quote>['\"])(?P<path>[^'\"\r\n]+)(?P=quote)",
    re.IGNORECASE,
)
_ANIMATION_SHEET_RE = re.compile(
    r"(?:(?P<property>[A-Za-z_$][\w$]*)\s*:\s*)?"
    r"new\s+ig\.AnimationSheet\s*\(\s*"
    r"(?P<quote>['\"])(?P<path>[^'\"\r\n]+)(?P=quote)\s*,\s*"
    r"(?P<frame_width>\d+)\s*,\s*(?P<frame_height>\d+)",
    re.IGNORECASE,
)
_ADD_ANIM_RE = re.compile(
    r"(?:this\s*\.\s*)?addAnim\s*\(\s*"
    r"(?P<quote>['\"])(?P<name>[^'\"\r\n]+)(?P=quote)\s*,\s*"
    r"(?P<frame_time>\d+(?:\.\d+)?)\s*,\s*"
    r"\[(?P<sequence>[\d\s,]+)\]",
    re.IGNORECASE,
)

_FISHJOY_RESOURCE_RE = re.compile(
    r"\bid\s*:\s*(?P<id_quote>['\"])(?P<id>[A-Za-z][\w-]*)(?P=id_quote)\s*,\s*"
    r"[^{}]*?\bsrc\s*:\s*(?P<path_quote>['\"])(?P<path>[^'\"\r\n]+)(?P=path_quote)",
    re.IGNORECASE | re.DOTALL,
)
_FISHJOY_TYPE_BLOCK_RE = re.compile(
    r"\bvar\s+(?P<type_id>fish(?:10|[1-9])|shark[12])\s*=\s*\{\s*"
    r"image\s*:\s*this\.getImage\s*\(\s*(?P<quote>['\"])(?P<image_id>[^'\"\r\n]+)(?P=quote)\s*\)\s*,\s*"
    r"frames\s*:\s*\[(?P<frames>.*?)\]\s*,\s*"
    r"polyArea\s*:\s*\[.*?\]\s*,\s*"
    r"mixin\s*:\s*\{(?P<mixin>.*?)\}\s*\}\s*;",
    re.IGNORECASE | re.DOTALL,
)
_FISHJOY_CANNON_BLOCK_RE = re.compile(
    r"\bvar\s+(?P<type_id>cannon[1-7])\s*=\s*\{\s*"
    r"image\s*:\s*this\.getImage\s*\(\s*(?P<quote>['\"])(?P<image_id>[^'\"\r\n]+)(?P=quote)\s*\)\s*,\s*"
    r"frames\s*:\s*\[(?P<frames>.*?)\]\s*,\s*"
    r"mixin\s*:\s*\{(?P<mixin>.*?)\}\s*\}\s*;",
    re.IGNORECASE | re.DOTALL,
)
_FISHJOY_FRAME_RE = re.compile(
    r"\{\s*rect\s*:\s*\[\s*(?P<x>\d+)\s*,\s*(?P<y>\d+)\s*,\s*"
    r"(?P<width>\d+)\s*,\s*(?P<height>\d+)\s*\](?P<meta>[^{}]*)\}",
    re.IGNORECASE,
)
_FISHJOY_STATE_LABEL_RE = re.compile(
    r"\blabel\s*:\s*(?P<quote>['\"])(?P<state>swim|capture)(?P=quote)",
    re.IGNORECASE,
)
_FISHJOY_STATE_JUMP_RE = re.compile(
    r"\bjump\s*:\s*(?P<quote>['\"])(?P<state>swim|capture)(?P=quote)",
    re.IGNORECASE,
)
_FISHJOY_INTERVAL_RE = re.compile(r"\binterval\s*:\s*(?P<value>\d+(?:\.\d+)?)", re.IGNORECASE)
_FISHJOY_TYPE_ORDER_RE = re.compile(
    r"\bthis\.fishTypes\s*=\s*\[\s*null\s*,(?P<body>[^\]]+)\]",
    re.IGNORECASE | re.DOTALL,
)
_FISHJOY_EXPECTED_TYPES = frozenset(
    [*(f"fish{index}" for index in range(1, 11)), "shark1", "shark2"]
)
_FISHJOY_EXPECTED_CANNONS = tuple(f"cannon{index}" for index in range(1, 8))


# Each landmark requires every feature group.  A group matches when any one of its
# strings occurs in the module body.  Module-name hints only break ties; detection
# never depends on an uploaded file path or a particular fully qualified module name.
_LANDMARK_SIGNATURES: tuple[dict[str, Any], ...] = (
    {
        "id": "control_logic",
        "name_hints": ("control-logic",),
        "feature_groups": (
            ("cardDeck",),
            ("cardHand",),
            ("manaRegenation", "manaRegen"),
            ("replaceCardInHand",),
            ("spawning",),
        ),
    },
    {
        "id": "game_area",
        "name_hints": ("game-area",),
        "feature_groups": (
            ("LevelGameArea",),
            ("EntityTowerSmall",),
            ("EntityTowerBig",),
            ("EntityBoardDeck",),
            ("EntityManaBar",),
        ),
    },
    {
        "id": "card",
        "name_hints": (".card", "entities.card"),
        "feature_groups": (
            ("holdingCard",),
            ("pointer.isPressed", "pointer.isReleased"),
            ("callNextCard",),
            ("drawingCard",),
            ("manausage", "manaUsage"),
        ),
    },
    {
        "id": "tower",
        "name_hints": (".tower", "entities.tower"),
        "feature_groups": (
            ("EntityTower",),
            ("rangeShot",),
            ("damageShot",),
            ("drawHealthBar",),
            ("targetEnemy",),
        ),
    },
    {
        "id": "base_troops",
        "name_hints": ("base-troops",),
        "feature_groups": (
            ("EntityBaseTroops",),
            ("pathChoose",),
            ("getBestDistace", "getBestDistance"),
            ("sideWalk",),
            ("sideAttack",),
        ),
    },
    {
        "id": "main",
        "name_hints": (".main",),
        "feature_groups": (
            ("MyGame",),
            ("arrayCardInHand",),
            ("LevelGameArea",),
            ("ig.main",),
            ("holdingCard",),
        ),
    },
)


_CLASH_OF_VIKINGS_CANONICAL_SPEC: dict[str, Any] = {
    "id": CLASH_OF_VIKINGS_PROFILE_ID,
    "title": "Clash of Vikings",
    "source_profile_version": SOURCE_REFERENCE_PROFILE_VERSION,
    "coordinate_system": {
        "logical_width": 480,
        "logical_height": 640,
        "orientation": "portrait",
        "arena_bottom": 520,
        "hud_region": {"top": 520, "bottom": 640},
    },
    "factions": {
        "player": {"id": "blue", "side": "bottom", "advance": "up"},
        "opponent": {"id": "red", "side": "top", "advance": "down"},
    },
    "lanes": {
        "count": 2,
        "orientation": "vertical",
        "ids": ["left", "right"],
        "bridges": [
            {"lane": "left", "x": 144, "top_y": 228, "bottom_y": 300},
            {"lane": "right", "x": 328, "top_y": 228, "bottom_y": 300},
        ],
        "forbidden_interpretation": "top-and-bottom horizontal lanes",
    },
    "towers": {
        "total": 6,
        "per_faction": 3,
        "small": {"count_per_faction": 2, "health": 2100, "damage": 60, "range": 110},
        "king": {"count_per_faction": 1, "health": 3500, "damage": 60},
        "positions": {
            "red": [
                {"id": "left", "kind": "small", "x": 132, "y": 124},
                {"id": "right", "kind": "small", "x": 316, "y": 124},
                {"id": "king", "kind": "king", "x": 216, "y": 60},
            ],
            "blue": [
                {"id": "left", "kind": "small", "x": 132, "y": 396},
                {"id": "right", "kind": "small", "x": 316, "y": 396},
                {"id": "king", "kind": "king", "x": 218, "y": 428},
            ],
        },
        "state_rule": "Each tower has independent health, target, destroyed, and animation state.",
        "king_destroyed_rule": "End the match immediately and resolve remaining allied lane towers.",
    },
    "cards": {
        "battle_deck_size": 8,
        "hand_size": 4,
        "queue_rule": "Replace a used hand card from the queue, then append the used card to its tail.",
        "must_cycle_indefinitely": True,
        "default_deck": [
            {
                "id": "giant",
                "name": "保卫者",
                "cost": 5,
                "kind": "troop",
                "health": 2100,
                "damage": 75,
                "speed": 15,
                "targeting": "towers-only",
            },
            {
                "id": "archer",
                "name": "疯狂射手",
                "cost": 3,
                "kind": "troop",
                "health": 360,
                "damage": 45,
                "speed": 30,
                "range": 70,
            },
            {"id": "ars", "name": "万雨箭", "cost": 3, "kind": "spell", "damage": 300},
            {
                "id": "warrior",
                "name": "愤怒特工",
                "cost": 4,
                "kind": "troop",
                "health": 850,
                "damage": 95,
                "speed": 25,
            },
            {"id": "fireball", "name": "火球", "cost": 4, "kind": "spell", "damage": 500},
            {
                "id": "hammer",
                "name": "疯狂锤子",
                "cost": 4,
                "kind": "troop",
                "health": 1050,
                "damage": 35,
                "speed": 25,
            },
            {
                "id": "mage",
                "name": "巫师",
                "cost": 4,
                "kind": "troop",
                "health": 420,
                "damage": 75,
                "speed": 25,
                "range": 60,
            },
            {
                "id": "axeman",
                "name": "狂暴者",
                "cost": 2,
                "kind": "troop",
                "health": 420,
                "damage": 65,
                "speed": 32,
            },
        ],
    },
    "mana": {
        "initial": 3,
        "maximum": 10,
        "normal_seconds_per_point": 2.5,
        "double_mana_after_seconds": 120,
        "double_seconds_per_point": 1.0,
    },
    "match": {
        "opening_countdown_seconds": 3,
        "normal_duration_seconds": 180,
        "sudden_death_seconds": 60,
        "score_unit": "destroyed tower",
        "immediate_win": "destroy opposing king tower",
    },
    "assets": {
        "required_paths": [
            "media/graphics/game/game-bg.png",
            "media/graphics/game/card.png",
            "media/graphics/game/cardbw.png",
            "media/graphics/game/troops-card.png",
            "media/graphics/game/tower.png",
            "media/graphics/game/balista.png",
            "media/graphics/game/red-anim-tower.png",
            "media/graphics/game/blue-anim-tower.png",
            "media/graphics/game/ui/board-deck.png",
            "media/graphics/game/ui/mana-bar.png",
            "media/graphics/game/ui/mana.png",
        ],
        # Every troop in the fixed eight-card deck must use the two source actions for
        # both teams.  Exact relative paths make this machine-verifiable without
        # depending on generated JavaScript variable names or hashed serving URLs.
        "required_team_asset_groups": {
            "giant": {
                "blue": [
                    "media/graphics/game/troops/giant-w.png",
                    "media/graphics/game/troops/giant-a.png",
                ],
                "red": [
                    "media/graphics/game/troops/giant-wr.png",
                    "media/graphics/game/troops/giant-ar.png",
                ],
            },
            "archer": {
                "blue": [
                    "media/graphics/game/troops/archer-w.png",
                    "media/graphics/game/troops/archer-a.png",
                ],
                "red": [
                    "media/graphics/game/troops/archer-wr.png",
                    "media/graphics/game/troops/archer-ar.png",
                ],
            },
            "warrior": {
                "blue": [
                    "media/graphics/game/troops/walk-warrior-b.png",
                    "media/graphics/game/troops/warrior-attack-b.png",
                ],
                "red": [
                    "media/graphics/game/troops/walk-warrior-r.png",
                    "media/graphics/game/troops/warrior-attack-r.png",
                ],
            },
            "hammer": {
                "blue": [
                    "media/graphics/game/troops/hammer-w.png",
                    "media/graphics/game/troops/hammer-a.png",
                ],
                "red": [
                    "media/graphics/game/troops/hammer-wr.png",
                    "media/graphics/game/troops/hammer-ar.png",
                ],
            },
            "mage": {
                "blue": [
                    "media/graphics/game/troops/mage-w.png",
                    "media/graphics/game/troops/mage-a.png",
                ],
                "red": [
                    "media/graphics/game/troops/mage-wr.png",
                    "media/graphics/game/troops/mage-ar.png",
                ],
            },
            # The original project names the axeman art "xmen".
            "axeman": {
                "blue": [
                    "media/graphics/game/troops/xmen-w.png",
                    "media/graphics/game/troops/xmen-a.png",
                ],
                "red": [
                    "media/graphics/game/troops/xmen-wr.png",
                    "media/graphics/game/troops/xmen-ar.png",
                ],
            },
        },
        "always_visible_paths": [
            "media/graphics/game/game-bg.png",
            "media/graphics/game/tower.png",
            "media/graphics/game/balista.png",
            "media/graphics/game/red-anim-tower.png",
            "media/graphics/game/blue-anim-tower.png",
            "media/graphics/game/ui/board-deck.png",
            "media/graphics/game/ui/mana-bar.png",
            "media/graphics/game/ui/mana.png",
        ],
        "background": {"path": "media/graphics/game/game-bg.png", "width": 480, "height": 640},
        "card_atlas": {
            "path": "media/graphics/game/card.png",
            "width": 273,
            "height": 326,
            "coordinate_set": "frame",
        },
        "large_card_atlas": {
            "path": "media/graphics/game/card-big.png",
            "width": 355,
            "height": 424,
            "coordinate_set": "frameBig",
        },
        "team_variants": {
            "blue": ["*-w.png", "*-a.png", "*-b.png"],
            "red": ["*-wr.png", "*-ar.png", "*-r.png"],
        },
        "forbidden_roles": ["developer_branding", "placeholder"],
        "forbidden_paths": [
            "branding/",
            "media/graphics/opening/title.png",
            "media/graphics/background.jpg",
            "media/graphics/mobile/background.jpg",
        ],
        "url_rule": "Use only exact URLs returned by the source-asset tool; never infer hashed URLs.",
        "binding_rule": {
            "explicit_map_required": True,
            "shape": "gameplay-id -> faction -> state -> {url, frame_width, frame_height}",
            "literal_keys_required": True,
            "forbidden": [
                "computed asset keys such as images[cardId + suffix]",
                "one generic warrior sheet standing in for every troop",
            ],
            "reason": (
                "Keep each source dependency statically visible and preserve the source-specific "
                "team, action, frame-size, and animation-sequence mapping."
            ),
        },
        "repair_rule": (
            "required_paths and the selected blue/red troop sheets are protected dependencies. "
            "A generic unused-asset warning must be resolved by fixing explicit references or "
            "draw paths, never by deleting protected source art."
        ),
        "fallback_rule": (
            "Programmatic drawing is allowed only for one asset proven unavailable by a missing "
            "tool result or that asset's runtime load error. It is never a replacement for "
            "available source art and never a response to a generic unused-asset warning."
        ),
    },
    "animation": {
        "required_states": ["upWalk", "downWalk", "sideWalk", "upAttack", "downAttack", "sideAttack"],
        "direction_rule": "Choose up/down/side state from movement or target heading.",
        "sequence_source": "Use the indexed addAnim sequences from the matching troop module.",
        "team_rule": "Use separate blue and red source sheets; do not mirror one blue sheet for both factions.",
        "draw_rule": "Crop sprite sheets with the nine-argument drawImage form and their declared frame size.",
        "forbidden": ["whole-sheet-as-entity", "implicit-sequential-0-to-N-playback"],
    },
    "input": {
        "primary_gesture": "drag-card-to-battlefield",
        "events": ["pointerdown", "pointermove", "pointerup", "pointercancel"],
        "single_event_family": True,
        "coordinate_rule": "Convert DOM pointer coordinates to logical canvas coordinates.",
        "deployment_rule": "Spend mana and rotate the card only after an affordable valid drop.",
        "troop_zone": "Player-controlled lower territory; advance a lane boundary after its red small tower falls.",
        "spell_zone": "Valid battlefield target area.",
    },
    "loader": {
        "handlers_before_src": True,
        "count_load_and_error": True,
        "settled_once_guard": True,
        "clear_timeout_on_finish": True,
        "initialize_exactly_once": True,
        "animation_loop_chains": 1,
        "late_completion_must_be_noop": True,
        "storage_access": "guarded",
        "audio_start": "after-user-gesture",
    },
    "verification": {
        "required_assertions": [
            "portrait-red-top-blue-bottom",
            "two-left-right-vertical-lanes",
            "three-independent-towers-per-faction",
            "fixed-eight-card-deck-and-four-card-cycling-hand",
            "mana-consumption-and-regeneration",
            "blue-and-red-assets-both-drawn",
            "source-animation-sequences-used",
            "no-developer-branding",
            "valid-drag-deploys-and-rotates-card",
            "one-animation-loop-after-loader-timeout",
        ],
        "repair_precedence": [
            "canonical source contract and required source art",
            "source gameplay semantics and explicit asset bindings",
            "generic quality and unused-preload warnings",
        ],
        "conflict_rule": (
            "When a generic warning conflicts with the canonical contract, preserve the contract "
            "and repair the code or explicit binding that made the dependency invisible."
        ),
    },
}


_FISHJOY_REQUIRED_RESOURCE_IDS: tuple[str, ...] = (
    "mainbg",
    "bottom",
    "fish1",
    "fish2",
    "fish3",
    "fish4",
    "fish5",
    "fish6",
    "fish7",
    "fish8",
    "fish9",
    "fish10",
    "shark1",
    "shark2",
    "cannon1",
    "cannon2",
    "cannon3",
    "cannon4",
    "cannon5",
    "cannon6",
    "cannon7",
    "bullet",
    "web",
    "numBlack",
    "coinAni1",
    "coinAni2",
    "coinText",
)


_FISHJOY_CANONICAL_SPEC: dict[str, Any] = {
    "id": FISHJOY_PROFILE_ID,
    "title": "Fishjoy / 小鱼",
    "source_profile_version": SOURCE_REFERENCE_PROFILE_VERSION,
    "coordinate_system": {
        "logical_width": 980,
        "logical_height": 545,
        "orientation": "landscape",
    },
    "gameplay": {
        "genre": "fishing-shooter",
        "primary_action": "Aim the cannon and fire a paid bullet toward the pointer.",
        "capture_rule": (
            "A bullet creates its matching source web; fish inside the web use the "
            "source captureRate adjusted by cannon level, rather than hit points."
        ),
        "reward_rule": "Captured fish award their source coin value and play capture/coin animation.",
        "cannon_levels": 7,
        "insufficient_coin_rule": (
            "Do not silently deadlock. Prevent unaffordable shots and expose a restart/game-over path."
        ),
    },
    "assets": {
        # Populated from R.sources only after all canonical resource ids are present.
        "required_paths": [],
        "required_resource_ids": list(_FISHJOY_REQUIRED_RESOURCE_IDS),
        "forbidden_roles": ["developer_branding", "placeholder"],
        "forbidden_paths": ["branding/"],
        "url_rule": "Use only exact URLs returned by the source-asset tool; never infer hashed URLs.",
        "binding_rule": {
            "explicit_map_required": True,
            "shape": "fish-id -> {path, frame_width, frame_height, state_sequences, interval}",
            "literal_keys_required": True,
            "forbidden": [
                "guessed fish frame dimensions",
                "one generic fish sheet standing in for multiple species",
                "computed or inferred hashed source URLs",
            ],
        },
        "repair_rule": (
            "Fish, shark, cannon, bullet, web, HUD, number, and coin art listed in required_paths "
            "are protected source dependencies. Fix their explicit bindings; do not delete them "
            "in response to a generic unused-preload warning."
        ),
        "fallback_rule": (
            "Programmatic placeholder fish are allowed only for an individual asset proven "
            "unavailable by the source tool or that asset's runtime load error."
        ),
    },
    "animation": {
        "frame_axis": "vertical",
        "layout": "vertical",
        "source_fps": 60,
        "interval_unit": "source-update-ticks",
        "required_states": ["swim", "capture"],
        # Populated deterministically from R.js frame rects, labels, jumps, and mixin intervals.
        "fish_catalog": {},
        # Populated from the seven source cannon MovieClip declarations.  Cannon frames
        # are a five-frame firing animation, not five aim directions.
        "cannon_catalog": {},
        "state_rule": (
            "Loop only the declared swim sequence while moving; after capture, switch to and "
            "loop only the declared capture sequence until the fish is removed."
        ),
        "draw_rule": (
            "Use nine-argument drawImage with sx=0 and sy=frame_index*frame_height, or the "
            "equivalent exact source rect from fish_catalog."
        ),
        "forbidden": [
            "horizontal sprite-sheet cropping",
            "sx=frame_index*frame_width for fish or shark sheets",
            "guessed frame width or height",
            "mixing swim and capture frames in one playback loop",
            "whole-sheet-as-entity",
        ],
    },
    "layout": {
        "bottom": {
            "resource_id": "bottom",
            "path": "",
            "source_rect": [0, 0, 765, 72],
            "atlas_frames": {
                "bar": {"x": 0, "y": 0, "w": 765, "h": 72},
                "minus_up": {"x": 132, "y": 72, "w": 44, "h": 31},
                "minus_down": {"x": 88, "y": 72, "w": 44, "h": 31},
                "plus_up": {"x": 44, "y": 72, "w": 44, "h": 31},
                "plus_down": {"x": 0, "y": 72, "w": 44, "h": 31},
            },
            "logical_x": 107.5,
            "logical_y": 475,
            "draw_order": "before-cannon",
        },
        "cannon": {
            "logical_x": 532.5,
            "logical_y": 535,
            "x_formula": "(logical_width-765)/2+425",
            "y_formula": "logical_height-10",
            "responsive_rule": "logical-scene-transform-or-current-viewport-equivalent",
            "draw_order": "after-bottom",
            "must_intersect_viewport": True,
        },
    },
    "loader": {
        "handlers_before_src": True,
        "count_load_and_error_separately": True,
        "settled_once_guard": True,
        "initialize_exactly_once": True,
        "animation_loop_chains": 1,
        "storage_access": "guarded",
        "audio_start": "after-user-gesture",
    },
    "verification": {
        "required_assertions": [
            "all-required-source-assets-resolve",
            "twelve-distinct-fish-and-shark-sheets",
            "vertical-fish-frame-crops-stay-in-bounds",
            "swim-and-capture-sequences-remain-separate",
            "seven-cannon-levels-use-source-art",
            "matching-web-art-is-drawn-on-impact",
            "number-atlases-are-cropped-per-glyph",
            "one-animation-loop-after-loader-settles",
        ],
        "repair_precedence": [
            "canonical source frame geometry and required source art",
            "source capture and cannon gameplay semantics",
            "generic quality and unused-preload warnings",
        ],
        "conflict_rule": (
            "Never dismiss a missing or out-of-bounds source frame as a browser/CORS issue "
            "without checking the exact R.sources path and fish_catalog geometry."
        ),
    },
}


def _ordered_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _parse_requires(module_text: str) -> list[str]:
    match = _REQUIRES_RE.search(module_text)
    if not match:
        return []
    return _ordered_unique(
        item.group("value") for item in _QUOTED_STRING_RE.finditer(match.group("body"))
    )


def _parse_animation_sheets(module_text: str) -> list[dict[str, Any]]:
    sheets: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int, int]] = set()
    for match in _ANIMATION_SHEET_RE.finditer(module_text):
        item = (
            match.group("property") or "",
            match.group("path"),
            int(match.group("frame_width")),
            int(match.group("frame_height")),
        )
        if item in seen:
            continue
        seen.add(item)
        sheets.append(
            {
                "property": item[0] or None,
                "path": item[1],
                "frame_width": item[2],
                "frame_height": item[3],
            }
        )
    return sheets


def _parse_animation_sequences(module_text: str) -> list[dict[str, Any]]:
    animations: list[dict[str, Any]] = []
    seen: set[tuple[str, float, tuple[int, ...]]] = set()
    for match in _ADD_ANIM_RE.finditer(module_text):
        sequence = tuple(
            int(value.strip())
            for value in match.group("sequence").split(",")
            if value.strip()
        )
        if not sequence:
            continue
        item = (match.group("name"), float(match.group("frame_time")), sequence)
        if item in seen:
            continue
        seen.add(item)
        animations.append(
            {"name": item[0], "frame_time": item[1], "sequence": list(item[2])}
        )
    return animations


def _index_modules(source_files: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    indexed: list[dict[str, Any]] = []
    bodies: dict[str, str] = {}
    candidates: list[tuple[str, str]] = []
    for source in source_files or []:
        content = source.get("content")
        if not isinstance(content, str) or "ig.module" not in content:
            continue
        candidates.append((str(source.get("path") or ""), content))

    for source_path, content in sorted(candidates, key=lambda item: item[0].casefold()):
        matches = list(_IMPACT_MODULE_RE.finditer(content))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
            module_text = content[match.start():end]
            start_line = content.count("\n", 0, match.start()) + 1
            if index + 1 < len(matches):
                end_line = max(start_line, content.count("\n", 0, end))
            else:
                end_line = content.count("\n") + 1
            name = match.group("name")
            module_key = f"{source_path}:{start_line}:{name}"
            body_hash = hashlib.sha256(module_text.encode("utf-8")).hexdigest()[:16]
            asset_references = _ordered_unique(
                asset.group("path") for asset in _ASSET_REFERENCE_RE.finditer(module_text)
            )
            entry = {
                "key": module_key,
                "name": name,
                "source_path": source_path,
                "start_line": start_line,
                "end_line": end_line,
                "char_count": len(module_text),
                "sha256_16": body_hash,
                "requires": _parse_requires(module_text),
                "asset_references": asset_references,
                "animation_sheets": _parse_animation_sheets(module_text),
                "animation_sequences": _parse_animation_sequences(module_text),
            }
            indexed.append(entry)
            bodies[module_key] = module_text

    indexed.sort(
        key=lambda item: (
            item["source_path"].casefold(),
            item["start_line"],
            item["name"].casefold(),
        )
    )
    return indexed, bodies


def _match_landmarks(
    modules: list[dict[str, Any]],
    bodies: dict[str, str],
) -> dict[str, dict[str, Any]]:
    landmarks: dict[str, dict[str, Any]] = {}
    for signature in _LANDMARK_SIGNATURES:
        matches: list[tuple[int, str, int, str, dict[str, Any], list[str]]] = []
        for module in modules:
            text = bodies[module["key"]]
            matched_features: list[str] = []
            for group in signature["feature_groups"]:
                feature = next((candidate for candidate in group if candidate in text), None)
                if feature is None:
                    break
                matched_features.append(feature)
            else:
                lowered_name = module["name"].casefold()
                hint_score = sum(
                    1 for hint in signature["name_hints"] if hint.casefold() in lowered_name
                )
                matches.append(
                    (
                        -hint_score,
                        module["source_path"].casefold(),
                        module["start_line"],
                        module["name"].casefold(),
                        module,
                        matched_features,
                    )
                )
        if not matches:
            continue
        matches.sort(key=lambda item: item[:4])
        selected = matches[0]
        module = selected[4]
        landmarks[signature["id"]] = {
            "module_key": module["key"],
            "module_name": module["name"],
            "source_path": module["source_path"],
            "start_line": module["start_line"],
            "end_line": module["end_line"],
            "matched_features": selected[5],
        }
    return landmarks


_CANONICAL_TROOP_MODULES: tuple[tuple[str, str], ...] = (
    ("giant", "giant"),
    ("archer", "archer"),
    ("warrior", "warrior"),
    ("hammer", "hammer"),
    ("mage", "mage"),
    ("axeman", "axeman"),
)
_TROOP_SHEET_BINDINGS: tuple[tuple[str, str, str], ...] = (
    ("walkSheet", "blue", "walk"),
    ("attackSheet", "blue", "attack"),
    ("walkRSheet", "red", "walk"),
    ("attackRSheet", "red", "attack"),
)


def _build_troop_animation_catalog(
    modules: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Build deterministic source-derived sheet and addAnim data for deck troops.

    The uploaded project uses different frame sizes and non-sequential frame orders for
    nearly every troop.  Shipping that information in the compact canonical contract is
    both cheaper and more reliable than asking every generation turn to rediscover it in
    the large bundled ``game.js`` file.
    """
    catalog: dict[str, dict[str, Any]] = {}
    by_suffix: dict[str, list[dict[str, Any]]] = {}
    for module in modules:
        lowered = str(module.get("name") or "").casefold()
        for _troop_id, module_suffix in _CANONICAL_TROOP_MODULES:
            if lowered == module_suffix or lowered.endswith("." + module_suffix):
                by_suffix.setdefault(module_suffix, []).append(module)

    required_states = set(
        _CLASH_OF_VIKINGS_CANONICAL_SPEC["animation"]["required_states"]
    )
    for troop_id, module_suffix in _CANONICAL_TROOP_MODULES:
        candidates = by_suffix.get(module_suffix) or []
        if not candidates:
            continue
        # Prefer a module that declares all four team/action sheets, then fall back to the
        # stable source-path/line ordering used by the main module index.
        candidates = sorted(
            candidates,
            key=lambda item: (
                -len({
                    str(sheet.get("property") or "")
                    for sheet in item.get("animation_sheets") or []
                } & {binding[0] for binding in _TROOP_SHEET_BINDINGS}),
                str(item.get("source_path") or "").casefold(),
                int(item.get("start_line") or 0),
            ),
        )
        module = candidates[0]
        sheets_by_property = {
            str(sheet.get("property") or ""): sheet
            for sheet in module.get("animation_sheets") or []
            if sheet.get("property") and sheet.get("path")
        }
        teams: dict[str, dict[str, Any]] = {"blue": {}, "red": {}}
        for property_name, team, action in _TROOP_SHEET_BINDINGS:
            sheet = sheets_by_property.get(property_name)
            if not sheet:
                continue
            teams[team][action] = {
                "path": sheet["path"],
                "frame_width": int(sheet["frame_width"]),
                "frame_height": int(sheet["frame_height"]),
            }

        states = {}
        for animation in module.get("animation_sequences") or []:
            name = str(animation.get("name") or "")
            if name not in required_states or name in states:
                continue
            states[name] = {
                "frame_time": float(animation["frame_time"]),
                "sequence": list(animation["sequence"]),
            }

        if not any(teams.values()) and not states:
            continue
        catalog[troop_id] = {
            "module": {
                "name": module.get("name"),
                "source_path": module.get("source_path"),
                "start_line": module.get("start_line"),
            },
            "blue": teams["blue"],
            "red": teams["red"],
            "states": states,
        }
    return catalog


def _parse_fishjoy_resource_paths(source_files: list[dict[str, Any]]) -> dict[str, str]:
    """Return stable ``R.sources`` id-to-path bindings without query cache busters."""
    resources: dict[str, str] = {}
    candidates = sorted(
        (
            str(source.get("path") or ""),
            source.get("content"),
        )
        for source in source_files or []
        if isinstance(source.get("content"), str)
    )
    for _source_path, content in candidates:
        for match in _FISHJOY_RESOURCE_RE.finditer(content):
            resource_id = match.group("id")
            path = match.group("path").split("?", 1)[0].replace("\\", "/")
            while path.startswith("./"):
                path = path[2:]
            if resource_id and path:
                resources.setdefault(resource_id, path)
    return resources


def _fishjoy_sequence(
    frames: list[dict[str, Any]],
    state: str,
) -> list[int]:
    start = next(
        (index for index, frame in enumerate(frames) if frame.get("label") == state),
        None,
    )
    if start is None:
        return []
    end = next(
        (
            index
            for index, frame in enumerate(frames[start:], start=start)
            if frame.get("jump") == state
        ),
        None,
    )
    if end is None or end < start:
        return []
    return list(range(start, end + 1))


def _fishjoy_type_order(source_files: list[dict[str, Any]]) -> list[str]:
    for source in sorted(source_files or [], key=lambda item: str(item.get("path") or "").casefold()):
        content = source.get("content")
        if not isinstance(content, str):
            continue
        match = _FISHJOY_TYPE_ORDER_RE.search(content)
        if not match:
            continue
        order = re.findall(r"\b(?:fish(?:10|[1-9])|shark[12])\b", match.group("body"))
        if len(order) == len(_FISHJOY_EXPECTED_TYPES) and set(order) == _FISHJOY_EXPECTED_TYPES:
            return order
    return [*(f"fish{index}" for index in range(1, 11)), "shark1", "shark2"]


def _build_fishjoy_animation_catalog(
    source_files: list[dict[str, Any]],
    resource_paths: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Extract exact vertical fish sheets from the readable ``R.js`` declarations.

    The source uses labelled/jumping Quark ``MovieClip`` frames.  Fish species do not
    share one frame height or one swim length, so accepting a partial or guessed catalog
    would recreate the flicker bug this contract is designed to prevent.
    """
    parsed: dict[str, dict[str, Any]] = {}
    candidates = sorted(
        (
            str(source.get("path") or ""),
            source.get("content"),
        )
        for source in source_files or []
        if isinstance(source.get("content"), str)
    )
    for source_path, content in candidates:
        for block in _FISHJOY_TYPE_BLOCK_RE.finditer(content):
            type_id = block.group("type_id").lower()
            image_id = block.group("image_id")
            if type_id in parsed or image_id != type_id:
                continue
            frames: list[dict[str, Any]] = []
            for frame_match in _FISHJOY_FRAME_RE.finditer(block.group("frames")):
                meta = frame_match.group("meta")
                label_match = _FISHJOY_STATE_LABEL_RE.search(meta)
                jump_match = _FISHJOY_STATE_JUMP_RE.search(meta)
                frames.append({
                    "rect": [
                        int(frame_match.group("x")),
                        int(frame_match.group("y")),
                        int(frame_match.group("width")),
                        int(frame_match.group("height")),
                    ],
                    "label": label_match.group("state").lower() if label_match else None,
                    "jump": jump_match.group("state").lower() if jump_match else None,
                })
            interval_match = _FISHJOY_INTERVAL_RE.search(block.group("mixin"))
            if not frames or not interval_match:
                continue
            frame_width = frames[0]["rect"][2]
            frame_height = frames[0]["rect"][3]
            is_exact_vertical_sheet = all(
                frame["rect"]
                == [0, index * frame_height, frame_width, frame_height]
                for index, frame in enumerate(frames)
            )
            swim = _fishjoy_sequence(frames, "swim")
            capture = _fishjoy_sequence(frames, "capture")
            path = resource_paths.get(image_id)
            if not is_exact_vertical_sheet or not swim or not capture or not path:
                continue
            interval_number = float(interval_match.group("value"))
            interval: int | float = (
                int(interval_number) if interval_number.is_integer() else interval_number
            )
            parsed[type_id] = {
                "path": path,
                "frame_width": frame_width,
                "frame_height": frame_height,
                "frame_count": len(frames),
                "orientation": "vertical",
                "interval": interval,
                "interval_unit": "source-update-ticks",
                "state_sequences": {"swim": swim, "capture": capture},
                # Readable aliases are kept because generated games commonly model the
                # two states as fields rather than a nested animation object.
                "swim_frames": swim,
                "capture_frames": capture,
                "source_rects": [frame["rect"] for frame in frames],
                "source": {"path": source_path},
            }

    if set(parsed) != _FISHJOY_EXPECTED_TYPES:
        return {}
    return {
        type_id: parsed[type_id]
        for type_id in _fishjoy_type_order(source_files)
    }


def _fishjoy_mixin_number(mixin: str, name: str) -> int | float | None:
    match = re.search(
        rf"\b{re.escape(name)}\s*:\s*(-?\d+(?:\.\d+)?)",
        mixin or "",
        re.IGNORECASE,
    )
    if not match:
        return None
    number = float(match.group(1))
    return int(number) if number.is_integer() else number


def _build_fishjoy_cannon_catalog(
    source_files: list[dict[str, Any]],
    resource_paths: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Extract the seven vertical five-frame firing animations from ``R.js``.

    The frames represent recoil/fire progression.  Treating them as aim directions is a
    subtle but visible source-semantics error, so the catalogue keeps ``fire_frames`` and
    the registration point alongside the crop geometry.
    """
    parsed: dict[str, dict[str, Any]] = {}
    candidates = sorted(
        (
            str(source.get("path") or ""),
            source.get("content"),
        )
        for source in source_files or []
        if isinstance(source.get("content"), str)
    )
    for source_path, content in candidates:
        for block in _FISHJOY_CANNON_BLOCK_RE.finditer(content):
            type_id = block.group("type_id").lower()
            image_id = block.group("image_id")
            if type_id in parsed or image_id != type_id:
                continue
            frames = []
            stop_frames = []
            for frame_match in _FISHJOY_FRAME_RE.finditer(block.group("frames")):
                if re.search(r"\bstop\s*:\s*1\b", frame_match.group("meta"), re.IGNORECASE):
                    stop_frames.append(len(frames))
                frames.append([
                    int(frame_match.group("x")),
                    int(frame_match.group("y")),
                    int(frame_match.group("width")),
                    int(frame_match.group("height")),
                ])
            mixin = block.group("mixin")
            interval = _fishjoy_mixin_number(mixin, "interval")
            reg_x = _fishjoy_mixin_number(mixin, "regX")
            reg_y = _fishjoy_mixin_number(mixin, "regY")
            power = _fishjoy_mixin_number(mixin, "power")
            path = resource_paths.get(image_id)
            if not frames or None in (interval, reg_x, reg_y, power) or not path:
                continue
            frame_width, frame_height = frames[0][2], frames[0][3]
            if len(frames) != 5 or stop_frames != [4] or any(
                rect != [0, index * frame_height, frame_width, frame_height]
                for index, rect in enumerate(frames)
            ):
                continue
            parsed[type_id] = {
                "path": path,
                "frame_width": frame_width,
                "frame_height": frame_height,
                "frame_count": len(frames),
                "orientation": "vertical",
                "interval": interval,
                "interval_unit": "source-update-ticks",
                "idle_frame": 0,
                "fire_frames": list(range(len(frames))),
                "stop_frame": stop_frames[0],
                "source_rects": frames,
                "reg_x": reg_x,
                "reg_y": reg_y,
                "power": power,
                "source": {"path": source_path},
            }
    if set(parsed) != set(_FISHJOY_EXPECTED_CANNONS):
        return {}
    return {type_id: parsed[type_id] for type_id in _FISHJOY_EXPECTED_CANNONS}


def _build_fishjoy_canonical_spec(
    source_files: list[dict[str, Any]],
) -> dict[str, Any] | None:
    resource_paths = _parse_fishjoy_resource_paths(source_files)
    if not all(resource_id in resource_paths for resource_id in _FISHJOY_REQUIRED_RESOURCE_IDS):
        return None
    combined = "\n".join(
        source.get("content")
        for source in source_files or []
        if isinstance(source.get("content"), str)
    )
    if not all(
        landmark in combined
        for landmark in ("this.fishTypes", "this.cannonTypes", "this.bullets", "this.webs")
    ):
        return None
    catalog = _build_fishjoy_animation_catalog(source_files, resource_paths)
    if set(catalog) != _FISHJOY_EXPECTED_TYPES:
        return None
    cannon_catalog = _build_fishjoy_cannon_catalog(source_files, resource_paths)
    if set(cannon_catalog) != set(_FISHJOY_EXPECTED_CANNONS):
        return None

    spec = copy.deepcopy(_FISHJOY_CANONICAL_SPEC)
    spec["assets"]["required_paths"] = [
        resource_paths[resource_id] for resource_id in _FISHJOY_REQUIRED_RESOURCE_IDS
    ]
    spec["assets"]["resource_catalog"] = {
        resource_id: resource_paths[resource_id]
        for resource_id in _FISHJOY_REQUIRED_RESOURCE_IDS
    }
    spec["animation"]["fish_catalog"] = catalog
    spec["animation"]["cannon_catalog"] = cannon_catalog
    spec["layout"]["bottom"]["path"] = resource_paths["bottom"]
    return spec


def build_source_reference_profile(source_files: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a deterministic, JSON-serialisable source-reference profile.

    Files are scanned by content and sorted before indexing, so changing upload order
    does not change the result.  Clash requires all six independent ImpactJS gameplay
    landmarks. Fishjoy requires the complete readable ``R.js`` resource table, all
    twelve exact vertical fish/shark frame declarations, cannon/bullet/web registries.
    """
    modules, bodies = _index_modules(source_files)
    landmarks = _match_landmarks(modules, bodies)
    clash_landmark_ids = {signature["id"] for signature in _LANDMARK_SIGNATURES}
    clash_detected = clash_landmark_ids.issubset(landmarks)
    if clash_detected:
        detected_profile = CLASH_OF_VIKINGS_PROFILE_ID
        canonical_spec = copy.deepcopy(_CLASH_OF_VIKINGS_CANONICAL_SPEC)
        canonical_spec["animation"]["troop_catalog"] = _build_troop_animation_catalog(modules)
    else:
        canonical_spec = _build_fishjoy_canonical_spec(source_files)
        detected_profile = FISHJOY_PROFILE_ID if canonical_spec else None
    return {
        "profile_version": SOURCE_REFERENCE_PROFILE_VERSION,
        "module_index": modules,
        "landmark_index": landmarks,
        "detected_profile": detected_profile,
        "canonical_spec": canonical_spec,
    }


def get_canonical_source_spec(source_files: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return a fresh canonical contract for a recognised source project, if any."""
    return build_source_reference_profile(source_files)["canonical_spec"]


__all__ = [
    "CLASH_OF_VIKINGS_PROFILE_ID",
    "FISHJOY_PROFILE_ID",
    "SOURCE_REFERENCE_PROFILE_VERSION",
    "build_source_reference_profile",
    "get_canonical_source_spec",
]
