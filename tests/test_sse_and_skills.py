"""SSE 流式端点 + Skills API 测试（mock agent 避免调真模型）。"""

import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api.routes as routes
import src.agent.game_agent as game_agent
from src.agent.game_agent import (
    SKILLS as _orig_skills,
    load_skill,
    load_skill_assets,
    load_skill_source,
    load_skill_web_bundle,
    port_skill_source,
    search_skill_source,
)


class SseStreamTests(unittest.TestCase):
    """SSE /api/chat/stream 端点测试（mock agent LLM 层）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_dir = routes.CHAT_HISTORY_DIR
        routes.CHAT_HISTORY_DIR = Path(self.tmp)
        routes._chat_request_times.clear()

        # Mock agent.chat_stream 返回可控 SSE 事件序列
        async def _mock_stream(session_id, user_message, current_code="", code_dirty=False):
            yield {"type": "session", "session_id": session_id}
            yield {"type": "token", "content": "你好"}
            yield {"type": "token", "content": "！"}
            yield {"type": "done", "code": "<html></html>", "action": "generate"}

        self._orig_stream = routes.agent.chat_stream
        routes.agent.chat_stream = _mock_stream

        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)

    def tearDown(self):
        routes.agent.chat_stream = self._orig_stream
        routes.CHAT_HISTORY_DIR = self._orig_dir
        routes._chat_request_times.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sse_stream_returns_events(self):
        with self.client.stream("POST", "/api/chat/stream", json={
            "session_id": "sse-test", "message": "做个游戏",
        }) as r:
            self.assertEqual(r.status_code, 200)
            body = "".join(r.iter_text())
            self.assertIn("session", body)
            self.assertIn("done", body)

    def test_sse_stream_rejects_invalid_session(self):
        r = self.client.post(
            "/api/chat/stream",
            json={"session_id": "../evil", "message": "x"},
        )
        self.assertIn(r.status_code, (400, 422))

    def test_sse_stream_with_code(self):
        with self.client.stream("POST", "/api/chat/stream", json={
            "session_id": "sse-code",
            "message": "加个功能",
            "current_code": "<html></html>",
            "code_dirty": False,
        }) as r:
            self.assertEqual(r.status_code, 200)


class GenerationDefaultsTests(unittest.TestCase):
    def test_generation_sampling_defaults_are_stable(self):
        from src.config import Settings

        self.assertEqual(Settings.model_fields["temperature"].default, 0.0)
        self.assertEqual(Settings.model_fields["top_p"].default, 1.0)

    def test_agent_passes_configured_sampling_values_to_model(self):
        prior_kb = game_agent._kb
        try:
            with mock.patch.object(game_agent, "init_chat_model", return_value=mock.Mock()) as init:
                game_agent.GameDesignAgent(mock.Mock())
            kwargs = init.call_args.kwargs
            self.assertEqual(kwargs["temperature"], game_agent.settings.temperature)
            self.assertEqual(kwargs["top_p"], game_agent.settings.top_p)
        finally:
            game_agent._kb = prior_kb


class SkillsApiTests(unittest.TestCase):
    """Skills CRUD API 测试。"""

    @classmethod
    def setUpClass(cls):
        cls._orig_skills = list(_orig_skills)

    @classmethod
    def tearDownClass(cls):
        _orig_skills.clear()
        _orig_skills.extend(cls._orig_skills)

    def setUp(self):
        _orig_skills.clear()
        _orig_skills.extend(SkillsApiTests._orig_skills)
        self.tmp = tempfile.mkdtemp()
        self._orig_dir = routes.CHAT_HISTORY_DIR
        routes.CHAT_HISTORY_DIR = Path(self.tmp)
        self._orig_skills_dir = routes._settings.skills_dir
        routes._settings.skills_dir = str(Path(self.tmp) / "skills")
        self._orig_custom_skills_file = game_agent._CUSTOM_SKILLS_FILE
        game_agent._CUSTOM_SKILLS_FILE = Path(routes._settings.skills_dir) / "custom_skills.json"
        routes._chat_request_times.clear()

        async def _mock_chat(session_id, msg, current_code="", code_dirty=False):
            return {"reply": "ok", "code": "", "action": "chat"}

        self._orig_chat = routes.agent.chat
        routes.agent.chat = _mock_chat

        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)

    def tearDown(self):
        routes.agent.chat = self._orig_chat
        routes.CHAT_HISTORY_DIR = self._orig_dir
        routes._settings.skills_dir = self._orig_skills_dir
        game_agent._CUSTOM_SKILLS_FILE = self._orig_custom_skills_file
        routes._chat_request_times.clear()
        _orig_skills.clear()
        _orig_skills.extend(SkillsApiTests._orig_skills)
        game_agent._rebuild_skills_prompt()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_skills(self):
        r = self.client.get("/api/skills")
        self.assertEqual(r.status_code, 200)
        skills = r.json()
        self.assertIsInstance(skills, list)

    def test_add_and_delete_custom_skill(self):
        r = self.client.post("/api/skills", json={
            "name": "test_skill",
            "description": "一个测试技能",
            "content": "# 测试\n这是测试内容。",
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])

        r2 = self.client.get("/api/skills")
        names = [s["name"] for s in r2.json()]
        self.assertIn("test_skill", names)

        r3 = self.client.delete("/api/skills/test_skill")
        self.assertEqual(r3.status_code, 200, r3.text)

        r4 = self.client.get("/api/skills")
        names4 = [s["name"] for s in r4.json()]
        self.assertNotIn("test_skill", names4)

    def test_scan_rejects_system_directories(self):
        r = self.client.post("/api/skills/scan", json={"path": "C:\\Windows"})
        self.assertIn(r.status_code, (400, 403, 404))

    def test_import_zip_partial_success(self):
        """zip 里单个坏 JSON 只跳过该文件并记入 errors，其余照常导入并持久化。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("good.json", json.dumps(
                {"name": "zip_good_skill", "description": "ok", "content": "x"}))
            zf.writestr("bad.json", "{ 不是合法JSON")
            zf.writestr("doc.md", "# md 技能说明\n正文内容")
        with mock.patch.object(routes, "_save_custom_skills") as save_mock:
            r = self.client.post(
                "/api/skills/import",
                files={"file": ("skills.zip", buf.getvalue(), "application/zip")},
            )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["added"], 2)
        self.assertEqual(len(body["errors"]), 1)
        self.assertIn("bad.json", body["errors"][0])
        names = [s["name"] for s in routes.SKILLS]
        self.assertIn("zip_good_skill", names)
        self.assertIn("doc", names)
        save_mock.assert_called_once()  # 部分导入成功也必须持久化（_sync_skills 被调用）

    def test_import_zip_invalid_zip_400(self):
        r = self.client.post(
            "/api/skills/import",
            files={"file": ("skills.zip", b"not a zip at all", "application/zip")},
        )
        self.assertEqual(r.status_code, 400)

    def test_import_source_folder_as_one_skill(self):
        index_html = b"""<!doctype html>
<html><head><link rel="stylesheet" href="css/style.css"></head>
<body><canvas id="game"></canvas>
<script src="js/jquery.min.js"></script>
<script src="js/game.js"></script></body></html>"""
        files = [
            ("files", ("demo/index.html", index_html, "text/html")),
            ("files", ("demo/css/style.css", b"canvas { display: block; }", "text/css")),
            ("files", ("demo/js/game.js", b"const PLAYER='images/player.png'; function startGame() { return true; }", "text/javascript")),
            ("files", ("demo/js/jquery.min.js", b"/*! vendor */", "text/javascript")),
            ("files", ("demo/images/player.png", b"\x89PNG\r\n", "image/png")),
        ]
        with mock.patch.object(routes, "_save_custom_skills"):
            r = self.client.post(
                "/api/skills/source",
                data={
                    "name": "platform_demo",
                    "description": "平台跳跃游戏参考",
                    "content": "重点参考关卡和跳跃手感。",
                },
                files=files,
            )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # jquery.min.js 不再是可读源码，改为可服务的运行时库资产。
        self.assertEqual(body["source_file_count"], 3)
        self.assertEqual(body["asset_file_count"], 2)   # player.png + jquery.min.js(library)
        self.assertEqual(body["usable_asset_count"], 2)
        self.assertEqual(body["entrypoint"], "demo/index.html")
        self.assertEqual(body["web_dependency_count"], 3)
        self.assertEqual(body["skipped_count"], 0)
        self.assertEqual(body["source_mode"], "faithful_port")

        detail = self.client.get("/api/skills/platform_demo").json()
        self.assertEqual(detail["source_file_count"], 3)
        self.assertEqual(
            [item["path"] for item in detail["source_files"]],
            ["demo/css/style.css", "demo/index.html", "demo/js/game.js"],
        )
        self.assertNotIn("content", detail["source_files"][0])
        # 运行时库以 kind=library 保存，带可回源 URL
        lib = next(a for a in detail["source_assets"] if a["path"] == "demo/js/jquery.min.js")
        self.assertEqual(lib["kind"], "library")
        lib_response = self.client.get(lib["url"])
        self.assertEqual(lib_response.status_code, 200)
        self.assertEqual(lib_response.content, b"/*! vendor */")
        self.assertTrue(lib_response.headers["content-type"].startswith("text/javascript"))
        self.assertEqual(lib_response.headers["x-content-type-options"], "nosniff")
        img = next(a for a in detail["source_assets"] if a["path"] == "demo/images/player.png")
        asset_url = img["url"]
        self.assertTrue(asset_url.startswith("/assets/source/"))
        asset_response = self.client.get(asset_url)
        self.assertEqual(asset_response.status_code, 200)
        self.assertEqual(asset_response.content, b"\x89PNG\r\n")
        self.assertEqual(asset_response.headers["content-type"], "image/png")
        self.assertEqual(asset_response.headers["access-control-allow-origin"], "*")
        unknown_asset_url = asset_url.rsplit("/", 1)[0] + "/not-registered.png"
        self.assertEqual(self.client.get(unknown_asset_url).status_code, 404)
        dependencies = detail["source_summary"]["web_bundle"]["dependencies"]
        self.assertEqual(
            [(item["resolved_path"], item["status"]) for item in dependencies],
            [
                ("demo/css/style.css", "readable"),
                ("demo/js/jquery.min.js", "library"),  # 运行时库：可服务、可运行、不可读
                ("demo/js/game.js", "readable"),
            ],
        )

        overview = load_skill.invoke({"skill_name": "platform_demo"})
        self.assertIn("demo/index.html", overview)
        self.assertIn("load_skill_source", overview)
        self.assertIn("load_skill_web_bundle", overview)
        self.assertIn("load_skill_assets", overview)
        self.assertIn("port_skill_source", overview)
        bundle = load_skill_web_bundle.invoke({"skill_name": "platform_demo"})
        self.assertIn('style data-source="demo/css/style.css"', bundle)
        self.assertIn("canvas { display: block; }", bundle)
        self.assertIn('script data-source="demo/js/game.js"', bundle)
        self.assertIn("function startGame", bundle)
        self.assertIn(asset_url, bundle)
        self.assertNotIn('href="css/style.css"', bundle)
        self.assertNotIn('src="js/game.js"', bundle)
        # 运行时库不再作为可读内容内联进组合视图；改由 load_skill 引导 <script src> 引入。
        self.assertNotIn("/*! vendor */", bundle)
        self.assertIn("platform_demo", game_agent._skills_prompt)
        assets = load_skill_assets.invoke({
            "skill_name": "platform_demo",
            "query": "player",
            "asset_type": "image",
        })
        self.assertIn("demo/images/player.png", assets)
        self.assertIn(asset_url, assets)
        self.assertIn(load_skill_assets, game_agent.SkillMiddleware.tools)
        self.assertIn(port_skill_source, game_agent.SkillMiddleware.tools)
        self.assertIn(search_skill_source, game_agent.SkillMiddleware.tools)
        source = load_skill_source.invoke({
            "skill_name": "platform_demo",
            "file_path": "demo/js/game.js",
            "start_line": 1,
            "line_count": 20,
        })
        self.assertIn("function startGame", source)
        self.assertIn(asset_url, source)

        skill = next(item for item in routes.SKILLS if item["name"] == "platform_demo")
        ported = game_agent.build_faithful_source_port(skill)
        self.assertIn('name="source-port-mode" content="faithful_port"', ported)
        # 运行时库不再内联其压缩内容，而是把 <script src> 改指向已保存的库副本 URL，
        # 浏览器 <script src> 加载即可（导出时该 /assets/ 引用会被内联为独立文件）。
        self.assertNotIn("/*! vendor */", ported)
        lib_url = next(a["url"] for a in skill["source_assets"]
                       if a["path"] == "demo/js/jquery.min.js")
        self.assertIn(f'src="{lib_url}"', ported)
        self.assertIn(asset_url, ported)
        self.assertNotIn('src="js/jquery.min.js"', ported)

    def test_source_tree_serve_and_port_base_href_for_dynamic_urls(self):
        # 模拟植物大战僵尸类：运行时动态拼 level/N.js（大小写不一：目录是 Level/），
        # 且把自己的 Engine.js 硬编码成外部绝对 URL。
        index_html = b"<!doctype html><html><head></head><body><script src=\"js/main.js\"></script></body></html>"
        main_js = (
            b"var e=document.createElement('script');"
            b"e.src=\"http://old.host/pvz/Engine.js\";"                 # 外部硬编码，本地有同名
            b"function loadLevel(n){var s=document.createElement('script');s.src=\"level/\"+n+\".js\";}"  # 动态拼
        )
        files = [
            ("files", ("pvz/index.html", index_html, "text/html")),
            ("files", ("pvz/js/main.js", main_js, "text/javascript")),
            ("files", ("pvz/js/Engine.js", b"/* engine */", "text/javascript")),
            ("files", ("pvz/Level/0.js", b"/* level zero */", "text/javascript")),  # 大写 Level
            ("files", ("pvz/images/bg.png", b"\x89PNG\r\n", "image/png")),
        ]
        with mock.patch.object(routes, "_save_custom_skills"):
            r = self.client.post(
                "/api/skills/source",
                data={"name": "pvz_demo", "description": "动态加载关卡的老游戏", "content": ""},
                files=files,
            )
        self.assertEqual(r.status_code, 200, r.text)
        skill = next(s for s in routes.SKILLS if s["name"] == "pvz_demo")
        bundle = skill["source_asset_bundle_id"]

        # 1) 源码树服务：小写请求 level/0.js 命中大写 Level/0.js（大小写不敏感）+ 正确 MIME
        #    文件带 pvz/ 公共前缀，故 tree 路径含前缀（对应机械移植的 base href = .../tree/pvz/）
        resp = self.client.get(f"/assets/source/{bundle}/tree/pvz/level/0.js")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"/* level zero */")
        self.assertTrue(resp.headers["content-type"].startswith("text/javascript"))
        self.assertEqual(resp.headers["x-content-type-options"], "nosniff")
        # 原始大写路径也行
        self.assertEqual(self.client.get(f"/assets/source/{bundle}/tree/pvz/Level/0.js").status_code, 200)
        # 嵌套文本源码也回源
        self.assertEqual(self.client.get(f"/assets/source/{bundle}/tree/pvz/js/main.js").status_code, 200)

        # 2) 穿越 / 未登记防护
        self.assertEqual(self.client.get(f"/assets/source/{bundle}/tree/nope.js").status_code, 404)
        self.assertEqual(
            self.client.get(f"/assets/source/{bundle}/tree/..%2fmanifest.json").status_code, 404
        )

        # 3) 机械移植：base href 指向入口目录、外部 Engine.js 改写到本地 tree、动态 level 相对路径原样保留
        ported = game_agent.build_faithful_source_port(skill)
        self.assertIn(f'<base href="/assets/source/{bundle}/tree/pvz/">', ported)
        self.assertNotIn("http://old.host/pvz/Engine.js", ported)               # 外部改写掉了
        self.assertIn(f"/assets/source/{bundle}/tree/pvz/js/Engine.js", ported)  # → 本地唯一同名
        self.assertIn('s.src="level/"+n+".js"', ported)                          # 动态相对路径保持原样

    def test_search_skill_source_is_stable_across_files_and_returns_numbered_context(self):
        skill = {
            "name": "source_search_demo",
            "description": "跨文件源码搜索",
            "content": "",
            # Intentionally reverse lexical path order; results must not depend on upload order.
            "source_files": [
                {
                    "path": "demo/z-input.js",
                    "language": "javascript",
                    "size": 120,
                    "lines": 4,
                    "content": (
                        "const ready = true;\n"
                        "canvas.addEventListener('pointerdown', deployCard);\n"
                        "function deployCard() { return true; }\n"
                        "const done = true;"
                    ),
                },
                {
                    "path": "demo/a-animation.js",
                    "language": "javascript",
                    "size": 130,
                    "lines": 4,
                    "content": (
                        "const sheet = new ig.AnimationSheet('hero.png', 32, 48);\n"
                        "hero.addAnim('sideWalk', 0.05, [4, 12, 20]);\n"
                        "hero.addAnim('sideAttack', 0.05, [1, 8, 9]);\n"
                        "const end = true;"
                    ),
                },
            ],
        }
        _orig_skills.append(skill)
        args = {
            "skill_name": "source_search_demo",
            "query": "sideWalk pointerdown",
            "context_lines": 1,
            "limit": 8,
        }
        first = search_skill_source.invoke(args)
        self.assertIn("demo/a-animation.js:2", first)
        self.assertIn("demo/z-input.js:2", first)
        self.assertIn(">      2 | hero.addAnim('sideWalk'", first)
        self.assertIn("load_skill_source", first)

        # Reordering the durable record cannot reorder search results.
        skill["source_files"].reverse()
        second = search_skill_source.invoke(args)
        self.assertEqual(first, second)

        narrowed = search_skill_source.invoke({
            **args,
            "file_path": "demo/z-input.js",
        })
        self.assertIn("pointerdown", narrowed)
        self.assertNotIn("a-animation.js", narrowed)

    def test_long_omitted_source_includes_landmark_index_and_recommended_lines(self):
        lines = [f"const filler_{index} = {index};" for index in range(1, 901)]
        lines[119] = 'ig.module("game.entities.card-input").defines(function() {'
        lines[120] = "canvas.addEventListener('pointerdown', deployCard);"
        lines[399] = 'const walk = new ig.AnimationSheet("troops/hero.png", 32, 48);'
        lines[400] = "hero.addAnim('sideWalk', 0.05, [4, 12, 20]);"
        lines[749] = 'ig.module("game.levels.arena").defines(function() {'
        lines[750] = "LevelGameArea = { entities: [{ type: 'EntityTowerBig', x: 10, y: 20 }] };"
        game_js = "\n".join(lines)
        _orig_skills.append({
            "name": "long_landmarks",
            "description": "超长源码索引",
            "content": "",
            "source_files": [
                {
                    "path": "game/index.html",
                    "language": "html",
                    "size": 40,
                    "lines": 1,
                    "content": '<script src="game.js"></script>',
                },
                {
                    "path": "game/game.js",
                    "language": "javascript",
                    "size": len(game_js.encode("utf-8")),
                    "lines": len(lines),
                    "content": game_js,
                },
            ],
            "source_summary": {"entrypoint": "game/index.html"},
        })

        overview = load_skill.invoke({"skill_name": "long_landmarks"})
        self.assertIn("超长/未内联源码的模块与符号索引", overview)
        self.assertIn("game.levels.arena", overview)
        self.assertIn("game/game.js:401", overview)
        self.assertIn("推荐先读的精确行段", overview)
        self.assertIn("search_skill_source", overview)

        bundle = load_skill_web_bundle.invoke({
            "skill_name": "long_landmarks",
            "max_chars": 12000,
        })
        self.assertLessEqual(len(bundle), 12000)
        self.assertIn("因长度上限未内联", bundle)
        self.assertIn("game.levels.arena", bundle)
        self.assertIn("sideWalk", bundle)
        self.assertIn("推荐先读的精确行段", bundle)

    def test_source_images_include_dimensions_sprite_sheet_and_atlas_metadata(self):
        from PIL import Image

        def png_bytes(width, height):
            buffer = io.BytesIO()
            Image.new("RGBA", (width, height), (0, 0, 0, 0)).save(buffer, format="PNG")
            return buffer.getvalue()

        game_js = b"""
const hero = new ig.AnimationSheet("media/hero.png", 32, 48);
const CARDS = {
  frames: {
    knight: { frame: { x: 0, y: 0, w: 16, h: 16 } },
    mage: { frame: { x: 16, y: 0, w: 16, h: 16 } }
  },
  meta: { image: "media/cards.png", size: { w: 32, h: 16 } }
};
"""
        with mock.patch.object(routes, "_save_custom_skills"):
            response = self.client.post(
                "/api/skills/source",
                data={"name": "sprite_metadata", "description": "精灵元数据", "content": ""},
                files=[
                    ("files", ("demo/index.html", b"<script src='game.js'></script>", "text/html")),
                    ("files", ("demo/game.js", game_js, "text/javascript")),
                    ("files", ("demo/media/hero.png", png_bytes(128, 96), "image/png")),
                    ("files", ("demo/media/cards.png", png_bytes(32, 16), "image/png")),
                ],
            )
        self.assertEqual(response.status_code, 200, response.text)
        assets = {
            item["file_name"]: item
            for item in self.client.get("/api/skills/sprite_metadata").json()["source_assets"]
        }
        hero = assets["hero.png"]
        self.assertEqual((hero["width"], hero["height"]), (128, 96))
        self.assertEqual(hero["layout_type"], "sprite_sheet")
        self.assertEqual((hero["frame_width"], hero["frame_height"]), (32, 48))
        self.assertEqual((hero["columns"], hero["rows"], hero["frame_count"]), (4, 2, 8))

        cards = assets["cards.png"]
        self.assertEqual((cards["width"], cards["height"]), (32, 16))
        self.assertEqual(cards["layout_type"], "atlas")
        self.assertEqual(cards["frame_count"], 2)
        self.assertEqual(cards["atlas_frames"]["knight"], {"x": 0, "y": 0, "w": 16, "h": 16})
        self.assertEqual(cards["atlas_frames"]["mage"], {"x": 16, "y": 0, "w": 16, "h": 16})

        # Metadata is part of the durable SKILLS record, not only synthesized by the API.
        stored = next(item for item in routes.SKILLS if item["name"] == "sprite_metadata")
        stored_hero = next(item for item in stored["source_assets"] if item["file_name"] == "hero.png")
        self.assertEqual(stored_hero["frame_width"], 32)

    def test_recognised_source_skill_exposes_canonical_contract_in_both_tools(self):
        source_js = """
ig.module('opaque.control').defines(function(){
 EntityControlLogic={cardDeck:[],cardHand:[],manaRegenation:2.5,
 replaceCardInHand:function(){},spawning:function(){}};
});
ig.module('opaque.area').defines(function(){
 LevelGameArea={entities:[EntityTowerSmall,EntityTowerBig,EntityBoardDeck,EntityManaBar]};
});
ig.module('opaque.card').defines(function(){
 EntityCard={holdingCard:null,manausage:3,drawingCard:function(){},update:function(){
  if(ig.game.pointer.isPressed||ig.game.pointer.isReleased)this.deck.callNextCard();}};
});
ig.module('opaque.tower').defines(function(){
 EntityTower={rangeShot:110,damageShot:60,targetEnemy:0,drawHealthBar:function(){}};
});
ig.module('opaque.troops').defines(function(){
 EntityBaseTroops={walkSheet:new ig.AnimationSheet('art/blue-walk.png',55,65),
 attackSheet:new ig.AnimationSheet('art/blue-attack.png',55,65),pathChoose:[],
 getBestDistace:function(){},initialize:function(){this.addAnim('sideWalk',.05,[11,12,3,0]);
 this.addAnim('sideAttack',.05,[12,3,8,13]);}};
});
ig.module('opaque.main').defines(function(){
 MyGame={holdingCard:null,arrayCardInHand:[5,1,2,3,7,11,13,9],level:LevelGameArea};
 ig.main('#canvas',MyGame,60,480,640,1);
});
"""
        asset_url = "/assets/source/0123456789abcdef0123456789abcdef/game-bg.png"
        _orig_skills.append({
            "name": "canonical_contract_fixture",
            "description": "契约参考",
            "content": "",
            "source_files": [
                {
                    "path": "game/index.html",
                    "language": "html",
                    "size": 31,
                    "lines": 1,
                    "content": "<script src='game.js'></script>",
                },
                {
                    "path": "game/game.js",
                    "language": "javascript",
                    "size": len(source_js),
                    "lines": source_js.count("\n") + 1,
                    "content": source_js,
                },
            ],
            "source_assets": [{
                "path": "game/media/graphics/game/game-bg.png",
                "file_name": "game-bg.png",
                "kind": "image",
                "size": 1,
                "url": asset_url,
            }],
            "source_summary": {"entrypoint": "game/index.html"},
        })

        overview = load_skill.invoke({"skill_name": "canonical_contract_fixture"})
        bundle = load_skill_web_bundle.invoke({
            "skill_name": "canonical_contract_fixture",
            "max_chars": 20000,
        })

        for output in (overview, bundle):
            self.assertIn("源码设计契约（必须遵守）", output)
            self.assertIn("clash-of-vikings@1", output)
            self.assertIn("source-reference-profile", output)
            self.assertIn('"total":6', output)
            self.assertIn('"hand_size":4', output)
        specs = game_agent._source_specs_for_code(
            f"<img src='{asset_url}'>"
        )
        self.assertEqual([spec["id"] for spec in specs], ["clash-of-vikings@1"])
        marker_specs = game_agent._source_specs_for_code(
            '<meta name="source-reference-profile" content="clash-of-vikings@1">'
        )
        self.assertEqual(
            [spec["id"] for spec in marker_specs],
            ["clash-of-vikings@1"],
        )

        session_id = "canonical-contract-memory"
        token = game_agent._begin_code_session(session_id, "")
        try:
            load_skill.invoke({"skill_name": "canonical_contract_fixture"})
            remembered = game_agent._source_specs_for_game(
                "<canvas></canvas><script></script>"
            )
            self.assertEqual(
                [spec["id"] for spec in remembered],
                ["clash-of-vikings@1"],
            )
        finally:
            game_agent._end_code_session(token)
            game_agent._source_specs_by_session.pop(session_id, None)
            game_agent._code_by_session.pop(session_id, None)
            game_agent._code_session_last_access.pop(session_id, None)

    def test_impact_image_draw_tile_metadata_and_developer_branding_role(self):
        from PIL import Image

        def png_bytes(width, height):
            buffer = io.BytesIO()
            Image.new("RGBA", (width, height), (0, 0, 0, 0)).save(buffer, format="PNG")
            return buffer.getvalue()

        game_js = b"""
EntityOpeningShield = ig.Entity.extend({
  titleImage:
    new ig.Image(
      "media/graphics/opening/title.png"
    ),
  ready: function() {
    if (_SETTINGS.DeveloperBranding.Splash.Enabled) this.start();
  },
  draw: function() {
    this.titleImage
      .drawTile(
        ig.system.width / 2 - 204,
        ig.system.height / 2 + 100,
        this.titleAnim,
        409,
        76
      );
  }
});

EntityMenu = ig.Entity.extend({
  titleImage: new ig.Image("media/graphics/game/title.png"),
  draw: function() {
    this.titleImage.drawTile(0, 0, this.frame, 256, 128);
  }
});
"""
        with mock.patch.object(routes, "_save_custom_skills"):
            response = self.client.post(
                "/api/skills/source",
                data={
                    "name": "draw_tile_metadata",
                    "description": "ImpactJS drawTile 元数据",
                    "content": "",
                },
                files=[
                    ("files", ("demo/index.html", b"<script src='game.js'></script>", "text/html")),
                    ("files", ("demo/game.js", game_js, "text/javascript")),
                    (
                        "files",
                        (
                            "demo/media/graphics/opening/title.png",
                            png_bytes(818, 456),
                            "image/png",
                        ),
                    ),
                    (
                        "files",
                        (
                            "demo/media/graphics/game/title.png",
                            png_bytes(512, 128),
                            "image/png",
                        ),
                    ),
                    (
                        "files",
                        (
                            "demo/branding/developer-logo.png",
                            png_bytes(100, 50),
                            "image/png",
                        ),
                    ),
                ],
            )
        self.assertEqual(response.status_code, 200, response.text)
        assets = {
            item["path"]: item
            for item in self.client.get("/api/skills/draw_tile_metadata").json()["source_assets"]
        }

        opening_title = assets["demo/media/graphics/opening/title.png"]
        self.assertEqual(opening_title["layout_type"], "sprite_sheet")
        self.assertEqual(
            (
                opening_title["frame_width"],
                opening_title["frame_height"],
                opening_title["columns"],
                opening_title["rows"],
                opening_title["frame_count"],
            ),
            (409, 76, 2, 6, 12),
        )
        self.assertEqual(opening_title["asset_role"], "developer_branding")

        game_title = assets["demo/media/graphics/game/title.png"]
        self.assertEqual((game_title["columns"], game_title["rows"]), (2, 1))
        self.assertNotIn("asset_role", game_title)
        self.assertEqual(
            assets["demo/branding/developer-logo.png"]["asset_role"],
            "developer_branding",
        )

        # Public metadata is also retained in the durable source-skill record.
        stored = next(item for item in routes.SKILLS if item["name"] == "draw_tile_metadata")
        stored_title = next(
            item
            for item in stored["source_assets"]
            if item["path"] == "demo/media/graphics/opening/title.png"
        )
        self.assertEqual(stored_title["asset_role"], "developer_branding")
        self.assertEqual(stored_title["frame_count"], 12)

        generic_assets = load_skill_assets.invoke({
            "skill_name": "draw_tile_metadata",
            "asset_type": "image",
        })
        self.assertNotIn("media/graphics/opening/title.png", generic_assets)
        self.assertIn("已隐藏 2 个开发者品牌/水印素材", generic_assets)
        branding_assets = load_skill_assets.invoke({
            "skill_name": "draw_tile_metadata",
            "query": "branding",
            "asset_type": "image",
        })
        self.assertIn("branding/developer-logo.png", branding_assets)

    def test_legacy_source_asset_metadata_is_backfilled_from_saved_bundle(self):
        from PIL import Image

        bundle_id = "a" * 32
        stored_name = "hero.png"
        bundle_dir = Path(routes._settings.skills_dir) / "source_assets" / bundle_id
        bundle_dir.mkdir(parents=True)
        Image.new("RGBA", (100, 80), (0, 0, 0, 0)).save(bundle_dir / stored_name)
        _orig_skills.append({
            "name": "legacy_sprite_metadata",
            "description": "旧精灵技能",
            "content": "旧记录没有图片元数据",
            "source_files": [{
                "path": "game/game.js",
                "language": "javascript",
                "size": 70,
                "lines": 1,
                "content": 'new ig.AnimationSheet("images/hero.png", 20, 40);',
            }],
            "asset_paths": ["game/images/hero.png"],
            "source_assets": [{
                "path": "game/images/hero.png",
                "file_name": "hero.png",
                "kind": "image",
                "size": (bundle_dir / stored_name).stat().st_size,
                "stored_name": stored_name,
                "url": f"/assets/source/{bundle_id}/{stored_name}",
            }],
            "source_asset_bundle_id": bundle_id,
            "source_summary": {"asset_metadata_version": 1},
        })

        response = self.client.get("/api/skills/legacy_sprite_metadata")
        self.assertEqual(response.status_code, 200, response.text)
        asset = response.json()["source_assets"][0]
        self.assertEqual((asset["width"], asset["height"]), (100, 80))
        self.assertEqual(asset["layout_type"], "sprite_sheet")
        self.assertEqual((asset["frame_width"], asset["frame_height"]), (20, 40))
        self.assertEqual((asset["columns"], asset["rows"]), (5, 2))
        self.assertEqual(response.json()["source_summary"]["asset_metadata_version"], 3)

    def test_large_uniform_placeholder_is_tagged_and_hidden_by_default(self):
        from PIL import Image, ImageDraw

        placeholder = io.BytesIO()
        mock_bg = Image.new("RGB", (640, 480), (242, 242, 242))
        ImageDraw.Draw(mock_bg).rectangle((250, 220, 390, 235), fill=(40, 40, 40))
        mock_bg.save(placeholder, format="JPEG", quality=92)

        real = io.BytesIO()
        real_bg = Image.new("RGB", (480, 640), (20, 80, 130))
        draw = ImageDraw.Draw(real_bg)
        draw.rectangle((0, 0, 240, 320), fill=(30, 150, 90))
        draw.rectangle((240, 320, 480, 640), fill=(170, 70, 40))
        real_bg.save(real, format="PNG")

        with mock.patch.object(routes, "_save_custom_skills"):
            response = self.client.post(
                "/api/skills/source",
                data={"name": "placeholder_filter", "description": "占位图过滤", "content": ""},
                files=[
                    ("files", ("game/index.html", b"<canvas></canvas>", "text/html")),
                    (
                        "files",
                        (
                            "game/media/graphics/background.jpg",
                            placeholder.getvalue(),
                            "image/jpeg",
                        ),
                    ),
                    (
                        "files",
                        ("game/media/graphics/game/game-bg.png", real.getvalue(), "image/png"),
                    ),
                ],
            )
        self.assertEqual(response.status_code, 200, response.text)
        assets = {
            item["file_name"]: item
            for item in self.client.get("/api/skills/placeholder_filter").json()["source_assets"]
        }
        self.assertEqual(assets["background.jpg"]["asset_role"], "placeholder")
        self.assertNotIn("asset_role", assets["game-bg.png"])

        generic = load_skill_assets.invoke({
            "skill_name": "placeholder_filter",
            "asset_type": "image",
        })
        self.assertNotIn("background.jpg", generic)
        self.assertIn("game-bg.png", generic)
        self.assertIn("已隐藏 1 个占位/示例素材", generic)

        explicit = load_skill_assets.invoke({
            "skill_name": "placeholder_filter",
            "query": "placeholder",
            "asset_type": "image",
        })
        self.assertIn("background.jpg", explicit)

    def test_source_manifest_resolves_root_paths_and_external_dependencies(self):
        index_html = b"""<!doctype html><html><head>
<base href="/">
<link rel="stylesheet" href="css/app.css?v=2">
<script src="https://cdn.example.com/engine.js"></script>
<script src="js/main.js#boot"></script>
</head></html>"""
        files = [
            ("files", ("webgame/index.html", index_html, "text/html")),
            ("files", ("webgame/css/app.css", b"body { margin: 0; }", "text/css")),
            ("files", ("webgame/js/main.js", b"boot();", "text/javascript")),
        ]
        with mock.patch.object(routes, "_save_custom_skills"):
            response = self.client.post(
                "/api/skills/source",
                data={"name": "root_refs", "description": "根路径参考", "content": ""},
                files=files,
            )
        self.assertEqual(response.status_code, 200, response.text)
        detail = self.client.get("/api/skills/root_refs").json()
        dependencies = detail["source_summary"]["web_bundle"]["dependencies"]
        self.assertEqual(
            [(item["resolved_path"], item["status"]) for item in dependencies],
            [
                ("webgame/css/app.css", "readable"),
                (None, "external"),
                ("webgame/js/main.js", "readable"),
            ],
        )

    def test_web_bundle_replaces_real_tag_not_same_text_inside_comment(self):
        index_html = b"""<!doctype html><html><body>
<!-- <script src="js/main.js"></script> -->
<script src="js/main.js"></script>
</body></html>"""
        with mock.patch.object(routes, "_save_custom_skills"):
            response = self.client.post(
                "/api/skills/source",
                data={"name": "comment_tag", "description": "注释标签测试", "content": ""},
                files=[
                    ("files", ("demo/index.html", index_html, "text/html")),
                    ("files", ("demo/js/main.js", b"window.started = true;", "text/javascript")),
                ],
            )
        self.assertEqual(response.status_code, 200, response.text)
        bundle = load_skill_web_bundle.invoke({"skill_name": "comment_tag"})
        self.assertIn('<!-- <script src="js/main.js"></script> -->', bundle)
        self.assertEqual(bundle.count('script data-source="demo/js/main.js"'), 1)
        self.assertIn("window.started = true", bundle)

    def test_web_bundle_caps_dependency_manifest_and_total_output(self):
        script_tags = "".join(
            f'<script src="missing/{index}.js"></script>' for index in range(1105)
        )
        index_html = f"<!doctype html><html><body>{script_tags}</body></html>".encode()
        with mock.patch.object(routes, "_save_custom_skills"):
            response = self.client.post(
                "/api/skills/source",
                data={"name": "many_refs", "description": "大量依赖测试", "content": ""},
                files={"files": ("many/index.html", index_html, "text/html")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        detail = self.client.get("/api/skills/many_refs").json()
        manifest = detail["source_summary"]["web_bundle"]
        self.assertEqual(len(manifest["dependencies"]), 1000)
        self.assertEqual(manifest["dependency_overflow_count"], 105)
        bundle = load_skill_web_bundle.invoke({
            "skill_name": "many_refs",
            "max_chars": 12000,
        })
        self.assertLessEqual(len(bundle), 12000)
        self.assertIn("末尾已截断", bundle)
        self.assertIn("另有", bundle)

    def test_web_bundle_enforces_token_budget(self):
        index_html = (
            "<!doctype html><html><body><!--" + ("中" * 40000) + "--></body></html>"
        ).encode("utf-8")
        with mock.patch.object(routes, "_save_custom_skills"):
            response = self.client.post(
                "/api/skills/source",
                data={"name": "token_cap", "description": "Token 上限测试", "content": ""},
                files={"files": ("token/index.html", index_html, "text/html")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        bundle = load_skill_web_bundle.invoke({"skill_name": "token_cap", "max_chars": 120000})
        self.assertLessEqual(game_agent._estimate_tokens(bundle), 30000)
        self.assertIn("token 安全上限", bundle)

    def test_legacy_source_skill_marks_filtered_minified_dependency(self):
        _orig_skills.append({
            "name": "legacy_source",
            "description": "旧源码记录",
            "content": "旧版没有 web_bundle 元数据",
            "source_files": [{
                "path": "legacy/index.html",
                "language": "html",
                "size": 51,
                "lines": 1,
                "content": '<script src="js/vendor.min.js"></script>',
            }],
            "asset_paths": ["legacy/images/player.png"],
            "source_summary": {"entrypoint": "legacy/index.html"},
        })
        overview = load_skill.invoke({"skill_name": "legacy_source"})
        self.assertIn("legacy/js/vendor.min.js", overview)
        self.assertIn("已跳过的第三方/压缩依赖", overview)
        self.assertIn("重新上传", overview)
        legacy_assets = load_skill_assets.invoke({"skill_name": "legacy_source"})
        self.assertIn("只有资源路径", legacy_assets)

    def test_replacing_or_deleting_skill_keeps_existing_asset_urls_alive(self):
        with mock.patch.object(routes, "_save_custom_skills"):
            created = self.client.post(
                "/api/skills/source",
                data={"name": "durable_assets", "description": "稳定素材", "content": ""},
                files=[
                    ("files", ("game/index.html", b"<canvas></canvas>", "text/html")),
                    ("files", ("game/old.png", b"old-png", "image/png")),
                ],
            )
            self.assertEqual(created.status_code, 200, created.text)
            old_url = self.client.get("/api/skills/durable_assets").json()["source_assets"][0]["url"]

            replaced = self.client.put(
                "/api/skills/durable_assets/source",
                data={"description": "新素材", "content": ""},
                files=[
                    ("files", ("game/index.html", b"<canvas></canvas>", "text/html")),
                    ("files", ("game/new.jpg", b"new-jpg", "image/jpeg")),
                ],
            )
            self.assertEqual(replaced.status_code, 200, replaced.text)
            new_url = self.client.get("/api/skills/durable_assets").json()["source_assets"][0]["url"]
            self.assertNotEqual(old_url, new_url)
            self.assertEqual(self.client.get(old_url).content, b"old-png")
            self.assertEqual(self.client.get(new_url).content, b"new-jpg")

            deleted = self.client.delete("/api/skills/durable_assets")
            self.assertEqual(deleted.status_code, 200, deleted.text)
            self.assertEqual(self.client.get(old_url).content, b"old-png")
            self.assertEqual(self.client.get(new_url).content, b"new-jpg")

    def test_source_import_does_not_serve_scriptable_svg(self):
        with mock.patch.object(routes, "_save_custom_skills"):
            response = self.client.post(
                "/api/skills/source",
                data={"name": "safe_assets", "description": "安全素材", "content": ""},
                files=[
                    ("files", ("game/index.html", b"<canvas></canvas>", "text/html")),
                    ("files", ("game/bad.svg", b"<svg><script>alert(1)</script></svg>", "image/svg+xml")),
                ],
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["usable_asset_count"], 0)
        self.assertTrue(any("不允许浏览器回源" in item for item in body["skipped"]))

    def test_source_zip_limit_rejects_whole_project_without_partial_skill(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("game/index.html", "<canvas></canvas>")
            zf.writestr("game/a.js", "a()")
            zf.writestr("game/b.js", "b()")
        with mock.patch.object(routes, "_SOURCE_MAX_ARCHIVE_ENTRIES", 2):
            response = self.client.post(
                "/api/skills/source",
                data={"name": "too_many", "description": "超限项目", "content": ""},
                files={"files": ("too-many.zip", buf.getvalue(), "application/zip")},
            )
        self.assertEqual(response.status_code, 413, response.text)
        self.assertFalse(any(item["name"] == "too_many" for item in routes.SKILLS))

    def test_source_skill_prompt_metadata_is_single_line_and_truncated(self):
        _orig_skills.append({
            "name": "META\nINJECTION" + ("N" * 200),
            "description": "DESC\nIGNORE " + ("D" * 600),
            "content": "x",
            "source_files": [{
                "path": "meta/index.html",
                "language": "html",
                "size": 13,
                "lines": 1,
                "content": "<html></html>",
            }],
        })
        prompt = game_agent._rebuild_skills_prompt()
        self.assertIn("META INJECTION", prompt)
        self.assertNotIn("META\nINJECTION", prompt)
        self.assertNotIn("DESC\nIGNORE", prompt)
        self.assertNotIn("D" * 300, prompt)

    def test_update_source_skill_preserves_files(self):
        with mock.patch.object(routes, "_save_custom_skills"):
            created = self.client.post(
                "/api/skills/source",
                data={"name": "source_edit", "description": "旧描述", "content": "旧说明"},
                files={"files": ("game/index.html", b"<h1>game</h1>", "text/html")},
            )
            self.assertEqual(created.status_code, 200, created.text)
            updated = self.client.put(
                "/api/skills/source_edit",
                json={"description": "新描述", "content": "新说明"},
            )
        self.assertEqual(updated.status_code, 200, updated.text)
        skill = next(s for s in routes.SKILLS if s["name"] == "source_edit")
        self.assertEqual(skill["description"], "新描述")
        self.assertEqual(skill["source_files"][0]["path"], "game/index.html")

    def test_source_zip_rejects_traversal_and_keeps_safe_source(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../outside.js", "alert('bad')")
            zf.writestr("project/index.html", "<canvas></canvas>")
        with mock.patch.object(routes, "_save_custom_skills"):
            r = self.client.post(
                "/api/skills/source",
                data={"name": "safe_zip", "description": "安全 ZIP", "content": ""},
                files={"files": ("safe.zip", buf.getvalue(), "application/zip")},
            )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["source_file_count"], 1)
        self.assertTrue(any("不安全路径" in item for item in body["skipped"]))
        detail = self.client.get("/api/skills/safe_zip").json()
        self.assertEqual(detail["source_files"][0]["path"], "project/index.html")


if __name__ == "__main__":
    unittest.main()
