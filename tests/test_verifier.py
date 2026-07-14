"""自检静态分析回归测试：复现并验证最近几轮真实出现的 Canvas 样板坑被检出。"""

import asyncio
import sys
import unittest

from src.agent import verifier


def ids(issues):
    return {i["id"] for i in issues}


class StaticAnalysisTests(unittest.TestCase):
    def test_looks_like_game(self):
        self.assertTrue(verifier.looks_like_game("<canvas></canvas><script>1</script>"))
        self.assertFalse(verifier.looks_like_game("<p>hello</p>"))

    def test_missing_canvas_css_size_flagged(self):
        # "看不到人物"：设了高分缓冲 + setTransform，却没设 canvas.style 尺寸
        html = ("<canvas id='c'></canvas><script>"
                "function resize(){const dpr=window.devicePixelRatio||1;"
                "canvas.width=innerWidth*dpr;canvas.height=innerHeight*dpr;"
                "ctx.setTransform(dpr,0,0,dpr,0,0);}</script></html>")
        self.assertIn("dpr_css_size", ids(verifier.analyze_static(html)))

    def test_canvas_css_size_ok_when_style_set(self):
        html = ("<canvas id='c'></canvas><script>"
                "function resize(){const dpr=window.devicePixelRatio||1;"
                "canvas.width=innerWidth*dpr;canvas.height=innerHeight*dpr;"
                "canvas.style.width=innerWidth+'px';canvas.style.height=innerHeight+'px';"
                "ctx.setTransform(dpr,0,0,dpr,0,0);}</script></html>")
        self.assertNotIn("dpr_css_size", ids(verifier.analyze_static(html)))

    def test_canvas_css_size_ok_when_css_fullscreen(self):
        html = ("<style>canvas{width:100vw;height:100vh}</style>"
                "<canvas id='c'></canvas><script>"
                "canvas.width=innerWidth*2;ctx.setTransform(2,0,0,2,0,0);</script></html>")
        self.assertNotIn("dpr_css_size", ids(verifier.analyze_static(html)))

    def test_touch_only_input_flagged(self):
        # "玩不了"：只绑 touch、没有鼠标/指针
        html = "<canvas id='c'></canvas><script>canvas.addEventListener('touchstart', e=>{});</script></html>"
        self.assertIn("no_mouse_input", ids(verifier.analyze_static(html)))

    def test_input_ok_with_pointer(self):
        html = "<canvas id='c'></canvas><script>canvas.addEventListener('pointerdown', e=>{});canvas.addEventListener('touchstart',e=>{});</script></html>"
        self.assertNotIn("no_mouse_input", ids(verifier.analyze_static(html)))

    def test_input_ok_with_click(self):
        html = "<canvas id='c'></canvas><script>canvas.addEventListener('touchstart',e=>{});btn.onclick=()=>{};</script></html>"
        self.assertNotIn("no_mouse_input", ids(verifier.analyze_static(html)))

    def test_ctx_scale_dpr_flagged(self):
        html = "<canvas id='c'></canvas><script>const dpr=2;ctx.scale(dpr,dpr);</script></html>"
        self.assertIn("ctx_scale_dpr", ids(verifier.analyze_static(html)))

    def test_missing_html_close_flagged(self):
        html = "<canvas id='c'></canvas><script>let x=1;</script>"
        self.assertIn("no_html_close", ids(verifier.analyze_static(html)))

    def test_clean_game_has_no_blocking_issues(self):
        html = ("<!DOCTYPE html><html><head><style>canvas{width:100vw;height:100vh;display:block}</style></head><body>"
                "<canvas id='c'></canvas><script>"
                "const canvas=document.getElementById('c');const ctx=canvas.getContext('2d');"
                "function resize(){const dpr=window.devicePixelRatio||1;"
                "canvas.width=innerWidth*dpr;canvas.height=innerHeight*dpr;"
                "canvas.style.width=innerWidth+'px';canvas.style.height=innerHeight+'px';"
                "ctx.setTransform(dpr,0,0,dpr,0,0);}resize();"
                "canvas.addEventListener('pointerdown', e=>{});"
                "</script></body></html>")
        res = asyncio.run(verifier.verify_game(html, use_headless=False))
        self.assertTrue(res["ok"], res["issues"])
        self.assertEqual(res["blocking"], [])

    def test_verify_game_reports_blocking(self):
        html = "<canvas id='c'></canvas><script>canvas.addEventListener('touchstart',e=>{});canvas.width=innerWidth*2;ctx.setTransform(2,0,0,2,0,0);</script></html>"
        res = asyncio.run(verifier.verify_game(html, use_headless=False))
        self.assertFalse(res["ok"])
        got = ids(res["blocking"])
        self.assertIn("no_mouse_input", got)
        self.assertIn("dpr_css_size", got)

    def test_recognised_source_contract_marker_is_required(self):
        html = (
            "<!doctype html><html><body><canvas></canvas>"
            "<script>canvas.addEventListener('pointerdown',()=>{});</script>"
            "</body></html>"
        )
        spec = {"id": "clash-of-vikings@1"}
        res = asyncio.run(verifier.verify_game(
            html,
            use_headless=False,
            source_specs=[spec],
        ))
        self.assertFalse(res["ok"])
        self.assertIn(
            "source_contract_missing:clash-of-vikings@1",
            ids(res["blocking"]),
        )

    def test_recognised_source_contract_marker_can_be_declared_in_any_attribute_order(self):
        for marker in (
            '<meta name="source-reference-profile" content="clash-of-vikings@1">',
            '<meta content="clash-of-vikings@1" name="source-reference-profile">',
        ):
            html = (
                f"<!doctype html><html><head>{marker}</head><body><canvas></canvas>"
                "<script>canvas.addEventListener('pointerdown',()=>{});</script>"
                "</body></html>"
            )
            res = asyncio.run(verifier.verify_game(
                html,
                use_headless=False,
                source_specs=[{"id": "clash-of-vikings@1"}],
            ))
            self.assertNotIn(
                "source_contract_missing:clash-of-vikings@1",
                ids(res["blocking"]),
            )

    @staticmethod
    def _asset_contract_fixture():
        return {
            "id": "clash-of-vikings@1",
            "assets": {
                "required_paths": [
                    "media/graphics/game/game-bg.png",
                    "media/graphics/game/tower.png",
                ],
                "required_team_asset_groups": {
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
                },
                "forbidden_roles": ["developer_branding", "placeholder"],
                "forbidden_paths": ["branding/"],
            },
            "animation": {
                "required_states": [
                    "upWalk", "downWalk", "sideWalk",
                    "upAttack", "downAttack", "sideAttack",
                ],
            },
        }

    def test_source_contract_requires_original_paths_and_both_team_variants(self):
        spec = self._asset_contract_fixture()
        records = [
            {
                "path": path,
                "url": f"/assets/source/0123456789abcdef0123456789abcdef/{index}.png",
            }
            for index, path in enumerate((
                "1/media/graphics/game/game-bg.png",
                "1/media/graphics/game/troops/walk-warrior-r.png",
                "1/media/graphics/game/troops/warrior-attack-r.png",
            ))
        ]
        html = (
            '<meta name="source-reference-profile" content="clash-of-vikings@1">'
            + "".join(item["url"] for item in records)
            + "<script>const states=['upWalk'];</script>"
        )

        got = ids(verifier._check_source_reference_contract(html, [spec], records))

        self.assertIn("source_contract_assets_missing:clash-of-vikings@1", got)
        self.assertIn("source_contract_team_assets_missing:clash-of-vikings@1", got)
        self.assertIn("source_contract_animation_states_missing:clash-of-vikings@1", got)

    def test_source_contract_accepts_prefixed_complete_asset_paths(self):
        spec = self._asset_contract_fixture()
        paths = [
            *spec["assets"]["required_paths"],
            *spec["assets"]["required_team_asset_groups"]["warrior"]["blue"],
            *spec["assets"]["required_team_asset_groups"]["warrior"]["red"],
        ]
        records = [
            {
                "path": "uploaded-root/" + path,
                "url": f"/assets/source/0123456789abcdef0123456789abcdef/{index}.png",
            }
            for index, path in enumerate(paths)
        ]
        states = " ".join(spec["animation"]["required_states"])
        html = (
            '<meta content="clash-of-vikings@1" name="source-reference-profile">'
            + "".join(item["url"] for item in records)
            + f"<script>const sourceStates='{states}';</script>"
        )

        got = ids(verifier._check_source_reference_contract(html, [spec], records))

        self.assertFalse(any(issue.startswith("source_contract_") for issue in got), got)

    def test_source_contract_rejects_referenced_placeholder_or_branding(self):
        spec = self._asset_contract_fixture()
        url = "/assets/source/0123456789abcdef0123456789abcdef/brand.png"
        records = [{
            "path": "uploaded-root/branding/logo.png",
            "url": url,
            "asset_role": "developer_branding",
        }]
        html = (
            '<meta name="source-reference-profile" content="clash-of-vikings@1">'
            + url
        )

        got = ids(verifier._check_source_reference_contract(html, [spec], records))

        self.assertIn("source_contract_forbidden_assets:clash-of-vikings@1", got)

    @staticmethod
    def _fishjoy_contract_fixture():
        return {
            "id": "fishjoy@1",
            "assets": {"required_paths": ["3/images/fish1.png"]},
            "animation": {
                "frame_axis": "vertical",
                "required_states": ["swim", "capture"],
                "fish_catalog": {
                    "fish1": {
                        "path": "3/images/fish1.png",
                        "frame_width": 55,
                        "frame_height": 37,
                        "frame_count": 8,
                        "state_sequences": {
                            "swim": [0, 1, 2, 3],
                            "capture": [4, 5, 6, 7],
                        },
                    },
                    "fish6": {
                        "path": "3/images/fish6.png",
                        "frame_width": 105,
                        "frame_height": 79,
                        "frame_count": 12,
                        "swim_frames": list(range(8)),
                        "capture_frames": [8, 9, 10, 11],
                    },
                },
            },
        }

    def test_fishjoy_contract_rejects_horizontal_crop_and_missing_state_config(self):
        spec = self._fishjoy_contract_fixture()
        url = "/assets/source/0123456789abcdef0123456789abcdef/fish1.png"
        assets = [{"path": "upload/3/images/fish1.png", "url": url, "width": 55, "height": 296}]
        html = (
            '<meta name="source-reference-profile" content="fishjoy@1">'
            f"<canvas></canvas>{url}<script>"
            "function drawFishes(){const fw=f.frameW,fh=f.frameH;"
            "const sx=f.frame*fw;const sy=0;"
            "ctx.drawImage(img,sx,sy,fw,fh,0,0,fw,fh);}"
            "</script></html>"
        )

        result = asyncio.run(verifier.verify_game(
            html,
            use_headless=False,
            source_assets=assets,
            source_specs=[spec],
        ))

        self.assertFalse(result["ok"])
        got = ids(result["blocking"])
        self.assertIn("source_contract_animation_axis_invalid:fishjoy@1", got)
        self.assertIn("source_contract_animation_sequences_invalid:fishjoy@1", got)
        self.assertIn("source_contract_animation_states_missing:fishjoy@1", got)

    def test_fishjoy_contract_accepts_vertical_crop_and_all_source_sequences(self):
        spec = self._fishjoy_contract_fixture()
        url = "/assets/source/0123456789abcdef0123456789abcdef/fish1.png"
        assets = [{"path": "upload/3/images/fish1.png", "url": url}]
        html = (
            '<meta content="fishjoy@1" name="source-reference-profile">'
            + url
            + "<script>const FISH_TYPES={"
            "a:{swimFrames:[0,1,2,3],captureFrames:[4,5,6,7]},"
            "b:{swimFrames:[0,1,2,3,4,5,6,7],captureFrames:[8,9,10,11]}};"
            "function drawFishes(){const sx=0;const sy=f.frame*f.frameHeight;"
            "ctx.drawImage(img,sx,sy,f.frameWidth,f.frameHeight,0,0,10,10);}"
            "</script>"
        )

        got = ids(verifier._check_source_reference_contract(html, [spec], assets))

        self.assertFalse(any(issue.startswith("source_contract_") for issue in got), got)

    def test_fishjoy_contract_rejects_guessed_swim_capture_sequences(self):
        spec = self._fishjoy_contract_fixture()
        html = (
            '<meta content="fishjoy@1" name="source-reference-profile">'
            "<script>const type={swim:[0,1,2,3,4],capture:[5,6,7]};"
            "function drawFish(){const sx=0,sy=fish.frame*fish.frameHeight;"
            "ctx.drawImage(img,sx,sy,fish.frameWidth,fish.frameHeight,0,0,10,10);}</script>"
        )

        got = ids(verifier._check_source_reference_contract(html, [spec]))

        self.assertIn("source_contract_animation_sequences_invalid:fishjoy@1", got)

    @staticmethod
    def _fishjoy_cannon_contract_fixture():
        heights = [74, 76, 76, 83, 85, 90, 94]
        return {
            "id": "fishjoy@1",
            "animation": {
                "cannon_catalog": {
                    f"cannon{level}": {
                        "path": f"3/images/cannon{level}.png",
                        "frame_width": 74,
                        "frame_height": height,
                        "frame_count": 5,
                        "fire_frames": [0, 1, 2, 3, 4],
                    }
                    for level, height in enumerate(heights, start=1)
                },
            },
        }

    def test_fishjoy_cannon_rejects_fixed_logical_origin_and_angle_frames(self):
        html = """
        <script>
        function createBullet(level, angle) { return {x:490, y:H-80, angle}; }
        function drawCannon() {
          const fi = Math.floor(((cannonAngle + Math.PI / 2) / Math.PI) * 4 + 0.5);
          const frameIdx = clamp(fi, 0, 4);
          ctx.translate(490, H - 80);
          ctx.rotate(cannonAngle);
          ctx.drawImage(img, 0, frameIdx * fh, 74, fh, -37, -fh / 2, 74, fh);
        }
        </script>
        """

        got = ids(verifier._check_fishjoy_cannon_semantics(
            html, self._fishjoy_cannon_contract_fixture()
        ))

        self.assertIn("source_contract_cannon_position_invalid:fishjoy@1", got)
        self.assertIn("source_contract_cannon_animation_invalid:fishjoy@1", got)

    def test_fishjoy_cannon_accepts_dynamic_origin_and_fire_frame(self):
        html = """
        <script>
        function cannonOrigin() { return {x:W/2, y:H-10}; }
        function createBullet(level, angle) {
          const origin = cannonOrigin(); return {x:origin.x, y:origin.y, angle};
        }
        function drawCannon() {
          const origin = cannonOrigin();
          const frameIndex = cannonFireFrame;
          ctx.translate(origin.x, origin.y);
          ctx.rotate(cannonAngle);
          ctx.drawImage(img, 0, frameIndex * fh, 74, fh, -37, -regY, 74, fh);
        }
        </script>
        """

        got = ids(verifier._check_fishjoy_cannon_semantics(
            html, self._fishjoy_cannon_contract_fixture()
        ))

        self.assertNotIn("source_contract_cannon_position_invalid:fishjoy@1", got)
        self.assertNotIn("source_contract_cannon_animation_invalid:fishjoy@1", got)

    def test_non_fish_profile_does_not_apply_vertical_fish_semantics(self):
        spec = {
            "id": "horizontal-runner@1",
            "animation": {"required_states": ["run"]},
        }
        html = (
            '<meta content="horizontal-runner@1" name="source-reference-profile">'
            "<script>const run=[0,1,2];function drawRunner(){"
            "const sx=frame*fw,sy=0;ctx.drawImage(img,sx,sy,fw,fh,0,0,fw,fh);}</script>"
        )

        got = ids(verifier._check_source_reference_contract(html, [spec]))

        self.assertNotIn("source_contract_animation_axis_invalid:horizontal-runner@1", got)
        self.assertNotIn("source_contract_animation_sequences_invalid:horizontal-runner@1", got)

    def test_declared_but_unused_deck_is_blocking(self):
        html = (
            "<canvas></canvas><script>"
            "const hand=[0,1,2,3];const deck=[4,5,6,7];"
            "canvas.addEventListener('pointerdown',()=>{});"
            "</script></html>"
        )
        self.assertIn("card_cycle_broken", ids(verifier.analyze_static(html)))

    def test_real_deck_rotation_is_not_flagged(self):
        html = (
            "<canvas></canvas><script>"
            "const hand=[0,1,2,3];const deck=[4,5,6,7];"
            "function play(){const used=hand.shift();hand.push(deck.shift());deck.push(used);}"
            "canvas.addEventListener('pointerdown',play);"
            "</script></html>"
        )
        self.assertNotIn("card_cycle_broken", ids(verifier.analyze_static(html)))

    def test_projectile_visual_hit_without_damage_is_blocking(self):
        html = (
            "<canvas></canvas><script>"
            "function updateProjectiles(dt){projectiles=projectiles.filter(p=>{"
            "const d=Math.hypot(p.targetX-p.x,p.targetY-p.y);"
            "if(d<10){spawnParticles(p.x,p.y,p.dmg);return false;}return true;});}"
            "</script></html>"
        )
        self.assertIn("projectile_no_damage", ids(verifier.analyze_static(html)))

    def test_projectile_damage_application_is_not_flagged(self):
        html = (
            "<canvas></canvas><script>"
            "function updateProjectiles(dt){projectiles=projectiles.filter(p=>{"
            "const d=Math.hypot(p.target.x-p.x,p.target.y-p.y);"
            "if(d<10){p.target.hp-=p.dmg;return false;}return true;});}"
            "</script></html>"
        )
        self.assertNotIn("projectile_no_damage", ids(verifier.analyze_static(html)))

    def test_large_unused_image_manifest_is_blocking(self):
        entries = "\n".join(
            f"asset{i}: '/assets/source/0123456789abcdef0123456789abcdef/{i}.png',"
            for i in range(12)
        )
        html = f"<canvas></canvas><script>const IMAGES={{\n{entries}\n}};</script></html>"
        self.assertIn("excessive_unused_assets", ids(verifier.analyze_static(html)))

    def test_computed_team_sprite_keys_are_not_treated_as_unused(self):
        entries = "\n".join(
            f"unit{i}{action}: '/assets/source/0123456789abcdef0123456789abcdef/unit{i}{action}.png',"
            for i in range(12)
            for action in ("WB", "AB")
        )
        html = (
            f"<canvas></canvas><script>const IMG_LIST={{\n{entries}\n}};"
            "const images={};"
            "function load(){Object.entries(IMG_LIST).forEach(([key,src])=>{"
            "images[key]=new Image();images[key].src=src;});}"
            "function troopSprites(cardId){return {"
            "walk:images[cardId + 'WB'],attack:images[cardId + \"AB\"]};}"
            "</script></html>"
        )
        self.assertNotIn("excessive_unused_assets", ids(verifier.analyze_static(html)))

    def test_template_sprite_keys_are_not_treated_as_unused(self):
        entries = "\n".join(
            f"unit{i}WB: '/assets/source/0123456789abcdef0123456789abcdef/unit{i}WB.png',"
            for i in range(12)
        )
        html = (
            f"<canvas></canvas><script>const IMG_LIST={{\n{entries}\n}};"
            "const images={};function sprite(cardId){return images[`${cardId}WB`];}"
            "</script></html>"
        )
        self.assertNotIn("excessive_unused_assets", ids(verifier.analyze_static(html)))

    def test_generic_cache_population_does_not_count_as_asset_use(self):
        entries = "\n".join(
            f"asset{i}: '/assets/source/0123456789abcdef0123456789abcdef/{i}.png',"
            for i in range(12)
        )
        html = (
            f"<canvas></canvas><script>const IMAGES={{\n{entries}\n}};"
            "const images={};Object.entries(IMAGES).forEach(([key,src])=>{"
            "images[key]=new Image();images[key].src=src;});"
            "</script></html>"
        )
        self.assertIn("excessive_unused_assets", ids(verifier.analyze_static(html)))

    def test_loader_and_timeout_need_shared_once_guard(self):
        html = (
            "<canvas></canvas><script>"
            "loadImages(()=>{state='start';requestAnimationFrame(loop);});"
            "setTimeout(()=>{if(state==='loading'){state='start';requestAnimationFrame(loop);}},3000);"
            "</script></html>"
        )
        self.assertIn("duplicate_animation_loop_risk", ids(verifier.analyze_static(html)))

    def test_loader_shared_finish_guard_is_not_flagged(self):
        html = (
            "<canvas></canvas><script>let settled=false,timer;"
            "function finish(){if(settled)return;settled=true;clearTimeout(timer);"
            "requestAnimationFrame(loop);}loadImages(finish);timer=setTimeout(finish,3000);"
            "</script></html>"
        )
        self.assertNotIn("duplicate_animation_loop_risk", ids(verifier.analyze_static(html)))


class BlankDetectionTests(unittest.TestCase):
    """无头检查的空屏判定：纯色截图=空屏，高方差画面=非空。"""

    def _png(self, painter):
        from io import BytesIO
        from PIL import Image
        img = Image.new("RGB", (64, 64))
        px = img.load()
        for x in range(64):
            for y in range(64):
                px[x, y] = painter(x, y)
        buf = BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def test_solid_color_is_blank(self):
        self.assertTrue(verifier._is_blank_png(self._png(lambda x, y: (20, 30, 40))))

    def test_high_variance_is_not_blank(self):
        png = self._png(lambda x, y: ((x * 4) % 256, (y * 4) % 256, ((x + y) * 4) % 256))
        self.assertFalse(verifier._is_blank_png(png))


class PreviewAssetNetworkTests(unittest.TestCase):
    def test_only_current_local_asset_endpoint_is_allowed(self):
        origin = verifier._preview_asset_origin()
        self.assertTrue(verifier._is_allowed_preview_request("data:image/png;base64,AA=="))
        self.assertTrue(verifier._is_allowed_preview_request(
            f"{origin}/assets/source/0123456789abcdef0123456789abcdef/a.png"
        ))
        self.assertTrue(verifier._is_allowed_preview_request(f"{origin}/assets/image/a.png"))
        self.assertFalse(verifier._is_allowed_preview_request(f"{origin}/api/skills"))
        self.assertFalse(verifier._is_allowed_preview_request(f"{origin}/assets/../api/skills"))
        self.assertFalse(verifier._is_allowed_preview_request(f"{origin}/assets/%2e%2e/api/skills"))
        self.assertFalse(verifier._is_allowed_preview_request(f"{origin}/assets//image/a.png"))
        self.assertFalse(verifier._is_allowed_preview_request(f"{origin}/assets/source/not-a-bundle/a.png"))
        self.assertFalse(verifier._is_allowed_preview_request("http://169.254.169.254/latest/meta-data"))
        self.assertFalse(verifier._is_allowed_preview_request("https://example.com/assets/a.png"))

    def test_preview_base_replaces_untrusted_base(self):
        prepared = verifier._with_preview_asset_base(
            '<html><head><base href="https://evil.example/"></head><body></body></html>'
        )
        self.assertNotIn("evil.example", prepared)
        self.assertEqual(prepared.count("<base "), 1)
        self.assertIn(verifier._preview_asset_origin(), prepared)

    def test_only_failed_local_assets_are_reported_and_examples_are_capped(self):
        origin = verifier._preview_asset_origin()
        failures = [
            {"url": f"{origin}/assets/font/missing-a.woff2", "status": 404},
            {"url": f"{origin}/assets/image/missing-b.png", "reason": "net::ERR_FAILED"},
            {"url": f"{origin}/assets/audio/missing-c.mp3", "status": 500},
            {"url": f"{origin}/assets/tilemap/missing-d.json", "status": 404},
            {"url": "https://example.com/tracker.js", "reason": "blockedbyclient"},
            {"url": f"{origin}/assets/font/missing-a.woff2", "reason": "duplicate"},
        ]
        issues = verifier._analyze_asset_load_failures(failures)
        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertEqual(issue["id"], "asset_load_failure")
        self.assertEqual(issue["severity"], "medium")
        self.assertIn("共 4 个", issue["msg"])
        self.assertIn("missing-a.woff2", issue["msg"])
        self.assertIn("missing-c.mp3", issue["msg"])
        self.assertNotIn("missing-d.json", issue["msg"])
        self.assertNotIn("example.com", issue["msg"])

    def test_browser_resource_console_diagnostics_are_not_runtime_errors(self):
        self.assertTrue(verifier._is_browser_resource_console_error(
            "Failed to load resource: the server responded with a status of 404"
        ))
        self.assertTrue(verifier._is_browser_resource_console_error(
            "Access to font at 'x.woff2' has been blocked by CORS policy"
        ))
        self.assertFalse(verifier._is_browser_resource_console_error(
            "ReferenceError: initGame is not defined"
        ))


class PreviewRuntimeProbeTests(unittest.TestCase):
    def test_runtime_probe_precedes_generated_scripts_and_contains_storage_shims(self):
        html = (
            "<html><head></head><body>"
            "<script data-user-script>localStorage.getItem('score')</script>"
            "</body></html>"
        )
        prepared = verifier._with_preview_runtime_probe(html)
        probe_at = prepared.index('data-verifier-probe="runtime"')
        user_at = prepared.index("data-user-script")
        self.assertLess(probe_at, user_at)
        self.assertIn("installStorageShim('localStorage')", prepared)
        self.assertIn("installStorageShim('sessionStorage')", prepared)
        self.assertIn("pendingByCallback", prepared)

    def test_duplicate_pending_callback_is_blocking(self):
        state = {
            "duplicate_count": 2,
            "examples": [{"callback": "loop", "pending_before": 1, "at_ms": 3012}],
        }
        issues = verifier._analyze_animation_loop_probe(state)
        self.assertIn("duplicate_animation_loop", ids(issues))
        self.assertEqual(issues[0]["severity"], "high")
        self.assertIn("loop", issues[0]["msg"])

    def test_single_animation_chain_is_allowed(self):
        self.assertEqual(
            verifier._analyze_animation_loop_probe({"duplicate_count": 0, "examples": []}),
            [],
        )

    def test_headless_observation_window_exceeds_three_seconds(self):
        total = verifier._HEADLESS_STARTUP_WAIT_MS + verifier._HEADLESS_POST_INPUT_WAIT_MS
        self.assertGreater(total, 3000)


class AtlasDrawDetectionTests(unittest.TestCase):
    """图集规则只依赖显式元数据和 drawImage 源矩形，避免从画面纹理猜测。"""

    _META = {
        "url": "/assets/source/0123456789abcdef0123456789abcdef/warrior.png",
        "path": "media/troops/walk-warrior.png",
        "usage": "sprite_sheet",
        "width": 275,
        "height": 325,
        "frame_width": 55,
        "frame_height": 65,
    }

    def _record(self, argc, **overrides):
        record = {
            **self._META,
            "natural_width": 275,
            "natural_height": 325,
            "argc": argc,
            "sx": 0,
            "sy": 0,
            "sw": 275,
            "sh": 325,
            "count": 1,
        }
        record.update(overrides)
        return record

    def test_three_and_five_arg_whole_atlas_draws_are_blocking(self):
        for argc in (3, 5):
            with self.subTest(argc=argc):
                issues = verifier._analyze_atlas_draw_records([self._record(argc)])
                self.assertIn("uncropped_sprite_atlas", ids(issues))
                self.assertEqual(issues[0]["severity"], "high")

    def test_nine_arg_whole_source_rectangle_is_blocking(self):
        issues = verifier._analyze_atlas_draw_records([self._record(9)])
        self.assertIn("uncropped_sprite_atlas", ids(issues))

    def test_nine_arg_cropped_frame_is_allowed(self):
        cropped = self._record(9, sx=55, sy=65, sw=55, sh=65)
        self.assertEqual(verifier._analyze_atlas_draw_records([cropped]), [])

    def test_source_rectangle_past_image_width_is_blocking(self):
        cropped = self._record(9, sx=275, sy=0, sw=55, sh=65)
        issues = verifier._analyze_source_rect_bounds([cropped])
        self.assertIn("source_rect_out_of_bounds", ids(issues))
        self.assertEqual(issues[0]["severity"], "high")

    def test_source_rectangle_inside_image_is_allowed(self):
        cropped = self._record(9, sx=220, sy=260, sw=55, sh=65)
        self.assertEqual(verifier._analyze_source_rect_bounds([cropped]), [])

    def test_legal_regular_grid_frame_is_valid(self):
        cropped = self._record(9, sx=55, sy=65, sw=55, sh=65, columns=5, rows=5)
        self.assertEqual(verifier._analyze_atlas_frame_records([cropped]), [])

    def test_misaligned_regular_grid_frame_is_invalid(self):
        cropped = self._record(9, sx=56, sy=65, sw=55, sh=65, columns=5, rows=5)
        issues = verifier._analyze_atlas_frame_records([cropped])
        self.assertIn("invalid_atlas_frame", ids(issues))

    def test_card_big_rejects_coordinates_declared_for_small_card_atlas(self):
        big_frames = {
            "archer": {"x": 259, "y": 104, "w": 84, "h": 103},
            "warrior": {"x": 254, "y": 208, "w": 85, "h": 103},
        }
        wrong = self._record(
            9,
            path="media/graphics/game/card-big.png",
            width=355,
            height=424,
            natural_width=355,
            natural_height=424,
            frame_width=0,
            frame_height=0,
            atlas_frames=big_frames,
            sx=199,
            sy=80,
            sw=64,
            sh=79,
        )
        issues = verifier._analyze_atlas_frame_records([wrong])
        self.assertIn("invalid_atlas_frame", ids(issues))

    def test_card_big_accepts_its_own_declared_frame(self):
        big_frames = {
            "archer": {"x": 259, "y": 104, "w": 84, "h": 103},
        }
        legal = self._record(
            9,
            path="media/graphics/game/card-big.png",
            width=355,
            height=424,
            natural_width=355,
            natural_height=424,
            frame_width=0,
            frame_height=0,
            atlas_frames=big_frames,
            sx=259,
            sy=104,
            sw=84,
            sh=103,
        )
        self.assertEqual(verifier._analyze_atlas_frame_records([legal]), [])

    def test_card_atlas_accepts_partial_reveal_inside_its_declared_frame(self):
        frames = {
            "giant": {"x": 134, "y": 80, "w": 64, "h": 79},
        }
        partial = self._record(
            9,
            path="media/graphics/game/card.png",
            width=273,
            height=326,
            natural_width=273,
            natural_height=326,
            frame_width=0,
            frame_height=0,
            atlas_frames=frames,
            sx=134,
            sy=112,
            sw=64,
            sh=47,
        )
        self.assertEqual(verifier._analyze_atlas_frame_records([partial]), [])

    def test_card_atlas_rejects_partial_crop_crossing_a_declared_frame_boundary(self):
        frames = {
            "giant": {"x": 134, "y": 80, "w": 64, "h": 79},
        }
        crossing = self._record(
            9,
            path="media/graphics/game/card.png",
            width=273,
            height=326,
            natural_width=273,
            natural_height=326,
            frame_width=0,
            frame_height=0,
            atlas_frames=frames,
            sx=134,
            sy=112,
            sw=65,
            sh=47,
        )
        self.assertIn(
            "invalid_atlas_frame",
            ids(verifier._analyze_atlas_frame_records([crossing])),
        )

    def test_one_pixel_crop_in_tiny_atlas_is_allowed(self):
        cropped = self._record(
            9,
            width=2,
            height=1,
            natural_width=2,
            natural_height=1,
            frame_width=1,
            frame_height=1,
            sx=0,
            sy=0,
            sw=1,
            sh=1,
        )
        self.assertEqual(verifier._analyze_atlas_draw_records([cropped]), [])

    def test_large_single_background_is_not_treated_as_atlas(self):
        background = self._record(5, usage="single", path="media/background.png")
        self.assertEqual(verifier._analyze_atlas_draw_records([background]), [])

    def test_probe_embeds_only_explicit_atlases_and_escapes_metadata(self):
        html = "<html><head></head><body><canvas></canvas></body></html>"
        assets = [
            self._META,
            {"url": "/assets/image/bg.png", "path": "</script><b>bg</b>", "usage": "single"},
            {"url": "/assets/image/atlas.png", "path": "</script><b>atlas</b>", "usage": "atlas"},
        ]
        prepared = verifier._with_draw_image_probe(html, assets)
        self.assertIn('data-verifier-probe="draw-image"', prepared)
        self.assertIn("/assets/image/atlas.png", prepared)
        self.assertNotIn("/assets/image/bg.png", prepared)
        self.assertNotIn("</script><b>atlas</b>", prepared)

    def test_layout_type_is_the_supported_canonical_metadata_field(self):
        canonical = {
            key: value for key, value in self._META.items() if key != "usage"
        }
        canonical["layout_type"] = "sprite_sheet"
        normalised = verifier._normalise_source_atlases([canonical])
        self.assertEqual(len(normalised), 1)
        self.assertEqual(normalised[0]["usage"], "sprite_sheet")

    def test_source_contract_catalog_enables_probe_for_plain_uploaded_image(self):
        assets = [{
            "path": "upload/3/images/fish1.png",
            "url": "/assets/source/0123456789abcdef0123456789abcdef/fish1.png",
            "width": 55,
            "height": 296,
        }]
        specs = [{
            "id": "fishjoy@1",
            "animation": {
                "frame_axis": "vertical",
                "fish_catalog": {
                    "fish1": {
                        "path": "3/images/fish1.png",
                        "frame_width": 55,
                        "frame_height": 37,
                        "frame_count": 8,
                    },
                },
            },
        }]

        enriched = verifier._source_assets_with_contract_metadata(assets, specs)
        normalised = verifier._normalise_source_atlases(enriched)

        self.assertEqual(len(normalised), 1)
        self.assertEqual(normalised[0]["frame_width"], 55)
        self.assertEqual(normalised[0]["frame_height"], 37)
        self.assertEqual(normalised[0]["columns"], 1)
        self.assertEqual(normalised[0]["rows"], 8)

    def test_fishjoy_cannon_and_bottom_contract_enable_role_probe(self):
        assets = [
            {
                "path": "upload/3/images/cannon1.png",
                "url": "/assets/source/0123456789abcdef0123456789abcdef/cannon1.png",
                "width": 74,
                "height": 370,
            },
            {
                "path": "upload/3/images/bottom.png",
                "url": "/assets/source/0123456789abcdef0123456789abcdef/bottom.png",
                "width": 765,
                "height": 122,
            },
        ]
        specs = [{
            "id": "fishjoy@1",
            "animation": {
                "cannon_catalog": {
                    "cannon1": {
                        "path": "3/images/cannon1.png",
                        "frame_width": 74,
                        "frame_height": 74,
                        "frame_count": 5,
                    },
                },
            },
            "layout": {
                "bottom": {
                    "resource_id": "bottom",
                    "path": "3/images/bottom.png",
                    "source_rect": [0, 0, 765, 72],
                },
            },
        }]

        enriched = verifier._source_assets_with_contract_metadata(assets, specs)
        normalised = verifier._normalise_source_atlases(enriched)

        by_role = {item["contract_role"]: item for item in normalised}
        self.assertEqual(by_role["cannon"]["resource_id"], "cannon1")
        self.assertEqual(by_role["cannon"]["frame_height"], 74)
        self.assertEqual(by_role["bottom"]["resource_id"], "bottom")
        self.assertEqual(by_role["bottom"]["frame_height"], 72)
        self.assertEqual(by_role["bottom"]["contract_profile_id"], "fishjoy@1")

    def test_fishjoy_cannon_draw_probe_reports_missing_offscreen_and_order(self):
        base = {"contract_profile_id": "fishjoy@1"}
        missing = verifier._analyze_fishjoy_cannon_draw_records([
            {**base, "contract_role": "fish", "visible_count": 3},
        ])
        self.assertIn("source_contract_cannon_not_drawn:fishjoy@1", ids(missing))

        offscreen_and_covered = verifier._analyze_fishjoy_cannon_draw_records([
            {
                **base,
                "contract_role": "cannon",
                "visible_count": 0,
                "offscreen_count": 8,
                "last_draw_index": 20,
            },
            {
                **base,
                "contract_role": "bottom",
                "visible_count": 8,
                "last_draw_index": 21,
            },
        ])
        got = ids(offscreen_and_covered)
        self.assertIn("source_contract_cannon_offscreen:fishjoy@1", got)
        self.assertIn("source_contract_cannon_draw_order_invalid:fishjoy@1", got)

        correct = verifier._analyze_fishjoy_cannon_draw_records([
            {
                **base,
                "contract_role": "bottom",
                "visible_count": 8,
                "last_draw_index": 20,
            },
            {
                **base,
                "contract_role": "cannon",
                "visible_count": 8,
                "last_draw_index": 21,
            },
        ])
        self.assertEqual(correct, [])

    def test_no_metadata_keeps_html_unchanged(self):
        html = "<html><body><canvas></canvas></body></html>"
        self.assertEqual(verifier._with_draw_image_probe(html), html)


class _FakeMouse:
    async def click(self, x, y):
        pass


class _FakeKeyboard:
    async def press(self, key):
        pass


class _FakeResponse:
    def __init__(self, url, status):
        self.url = url
        self.status = status


class _FakeRequest:
    def __init__(self, url, failure="net::ERR_FAILED"):
        self.url = url
        self.failure = failure


class _FakePage:
    def __init__(
        self,
        shot,
        errors,
        draw_records=None,
        runtime_probe=None,
        asset_responses=None,
        failed_requests=None,
    ):
        self._shot = shot
        self._errors = errors
        self._draw_records = draw_records or []
        self._runtime_probe = runtime_probe or {}
        self._asset_responses = asset_responses or []
        self._failed_requests = failed_requests or []
        self._handlers = {}
        self.last_html = ""
        self.waits = []
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard()

    async def route(self, pattern, handler):
        pass

    def on(self, event, cb):
        self._handlers.setdefault(event, []).append(cb)

    async def set_content(self, html, wait_until=None):
        self.last_html = html
        for e in self._errors:
            for cb in self._handlers.get("pageerror", []):
                cb(e)
        for response in self._asset_responses:
            for cb in self._handlers.get("response", []):
                cb(response)
        for request in self._failed_requests:
            for cb in self._handlers.get("requestfailed", []):
                cb(request)

    async def wait_for_timeout(self, ms):
        self.waits.append(ms)

    async def screenshot(self):
        return self._shot

    async def evaluate(self, expression):
        if verifier._RUNTIME_PROBE_GLOBAL in expression:
            return self._runtime_probe
        return self._draw_records

    async def close(self):
        pass


class _FakeBrowser:
    def __init__(self, page):
        self._page = page
        self._closed = False

    def is_connected(self):
        return not self._closed

    async def new_page(self, **kw):
        return self._page

    async def close(self):
        self._closed = True


class _FakeChromium:
    def __init__(self, owner):
        self._owner = owner

    async def launch(self, **kw):
        self._owner.launch_count += 1
        return _FakeBrowser(self._owner._page)


class _FakePW:
    def __init__(self, owner):
        self.chromium = _FakeChromium(owner)

    async def stop(self):
        pass


class _FakePlaywright:
    """模拟 async_playwright().start()/stop() 生命周期（verifier 常驻复用浏览器实例）。"""

    def __init__(self, page):
        self._page = page
        self.launch_count = 0

    def __call__(self):
        return self

    async def start(self):
        return _FakePW(self)


class RunHeadlessTests(unittest.TestCase):
    """run_headless 后处理路径回归：截图非空白时结果绝不能被静默丢弃。

    背景：曾因 _run() 只返回 (errs, blank) 而外层引用局部变量 shot，导致
    非空白截图必抛 NameError → except 吞掉 → return None → 运行时报错全部丢失。
    """

    def _png(self, size, painter):
        from io import BytesIO
        from PIL import Image
        img = Image.new("RGB", size)
        px = img.load()
        for x in range(size[0]):
            for y in range(size[1]):
                px[x, y] = painter(x, y)
        buf = BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def _gradient_png(self):  # 非空白（高方差）、非撕裂（相邻平滑）
        return self._png((64, 64), lambda x, y: ((x * 4) % 256, (y * 4) % 256, ((x + y) * 2) % 256))

    def _solid_png(self):  # 空白（纯色）
        return self._png((64, 64), lambda x, y: (20, 30, 40))

    def _noise_png(self):  # 撕裂（相邻像素剧烈跳变）；40×30 与检测分辨率一致，避免缩放平滑
        import random
        rng = random.Random(42)
        return self._png((40, 30), lambda x, y: (rng.randrange(256), rng.randrange(256), rng.randrange(256)))

    def _install_fakes(self, fake):
        import sys
        import types
        mod = types.ModuleType("playwright.async_api")
        mod.async_playwright = fake
        pkg = types.ModuleType("playwright")
        pkg.async_api = mod
        saved = {k: sys.modules.get(k) for k in ("playwright", "playwright.async_api")}
        sys.modules["playwright"] = pkg
        sys.modules["playwright.async_api"] = mod
        return saved

    def _restore_modules(self, saved):
        import sys
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    _HTML = "<canvas></canvas><script>1</script></html>"

    def _run_with_fake(
        self,
        shot,
        errors,
        source_assets=None,
        draw_records=None,
        runtime_probe=None,
        asset_responses=None,
        failed_requests=None,
    ):
        page = _FakePage(
            shot,
            errors,
            draw_records,
            runtime_probe,
            asset_responses,
            failed_requests,
        )
        saved = self._install_fakes(_FakePlaywright(page))

        async def _go():
            try:
                return await verifier.run_headless(self._HTML, source_assets=source_assets)
            finally:
                await verifier.aclose_browser()  # 复位常驻实例，避免测试间串台

        try:
            return asyncio.run(_go())
        finally:
            self._restore_modules(saved)

    def test_idle_close_timer_paused_while_check_active(self):
        """空闲关停定时器在检查进行中必须取消：否则上一轮排的定时器到点，
        会从正在进行的检查脚下把浏览器关掉（检查失败、自检静默退化）。"""
        fake = _FakePlaywright(_FakePage(self._gradient_png(), []))
        saved = self._install_fakes(fake)

        async def _go():
            try:
                b = await verifier._acquire_browser()
                during = verifier._idle_close_task
                await verifier._release_browser(b)
                after = verifier._idle_close_task
                return during, after
            finally:
                await verifier.aclose_browser()

        try:
            during, after = asyncio.run(_go())
        finally:
            self._restore_modules(saved)
        self.assertIsNone(during, "检查进行中不应存在空闲关停定时器")
        self.assertIsNotNone(after, "全部检查结束后应重新排定空闲关停")

    def test_retire_does_not_close_browser_under_concurrent_check(self):
        """异常报废 = 换代语义：并发检查还在用旧实例时不得 close（否则一个会话的
        超时会把另一个会话的自检一起打挂），由最后一个使用者真正关闭。"""
        fake = _FakePlaywright(_FakePage(self._gradient_png(), []))
        saved = self._install_fakes(fake)

        async def _go():
            try:
                a = await verifier._acquire_browser()
                b = await verifier._acquire_browser()
                assert a is b, "并发检查应共享同一常驻实例"
                await verifier._retire_browser(a)    # A 的检查异常/超时 → 报废
                await verifier._release_browser(a)   # A 结束：B 还在用，不能关
                still_open = a.is_connected()
                c = await verifier._acquire_browser()  # 换代：新检查拿到新实例
                fresh = c is not a
                await verifier._release_browser(b)   # 旧实例最后一个使用者退出 → 真正 close
                closed = not a.is_connected()
                await verifier._release_browser(c)
                return still_open, fresh, closed
            finally:
                await verifier.aclose_browser()

        try:
            still_open, fresh, closed = asyncio.run(_go())
        finally:
            self._restore_modules(saved)
        self.assertTrue(still_open, "报废时并发检查仍在用，不能立即 close")
        self.assertTrue(fresh, "报废后新检查应拿到换代的新实例")
        self.assertTrue(closed, "最后一个使用者退出后报废实例才被关闭")
        self.assertEqual(fake.launch_count, 2)

    def test_browser_instance_reused_across_checks(self):
        # 同一常驻实例下，连续两次检查只 launch 一次 Chromium
        fake = _FakePlaywright(_FakePage(self._gradient_png(), []))
        saved = self._install_fakes(fake)

        async def _go():
            try:
                return await verifier.run_headless(self._HTML), await verifier.run_headless(self._HTML)
            finally:
                await verifier.aclose_browser()

        try:
            r1, r2 = asyncio.run(_go())
        finally:
            self._restore_modules(saved)
        self.assertEqual(r1, [])
        self.assertEqual(r2, [])
        self.assertEqual(fake.launch_count, 1)

    def test_runtime_error_reported_on_nonblank_screen(self):
        issues = self._run_with_fake(self._gradient_png(), ["ReferenceError: h is not defined"])
        self.assertIsNotNone(issues, "非空白截图时结果被整体丢弃（shot 回归复现）")
        got = ids(issues)
        self.assertIn("runtime_error", got)
        self.assertTrue(any("h is not defined" in i["msg"] for i in issues))

    def test_clean_nonblank_screen_returns_empty_list(self):
        issues = self._run_with_fake(self._gradient_png(), [])
        self.assertEqual(issues, [])

    def test_blank_screen_reported(self):
        issues = self._run_with_fake(self._solid_png(), [])
        self.assertIsNotNone(issues)
        self.assertIn("blank_screen", ids(issues))

    def test_runtime_error_suppresses_secondary_blank_screen(self):
        issues = self._run_with_fake(
            self._solid_png(), ["ReferenceError: init is not defined"]
        )
        self.assertIsNotNone(issues)
        self.assertIn("runtime_error", ids(issues))
        self.assertNotIn("blank_screen", ids(issues))

    def test_visual_tearing_reported(self):
        issues = self._run_with_fake(self._noise_png(), [])
        self.assertIsNotNone(issues)
        self.assertIn("visual_tearing", ids(issues))

    def test_uncropped_atlas_reported_from_headless_probe(self):
        meta = {
            "url": "/assets/source/0123456789abcdef0123456789abcdef/warrior.png",
            "path": "media/troops/walk-warrior.png",
            "usage": "sprite_sheet",
            "width": 275,
            "height": 325,
            "frame_width": 55,
            "frame_height": 65,
        }
        record = {
            **meta,
            "natural_width": 275,
            "natural_height": 325,
            "argc": 5,
            "sx": 0,
            "sy": 0,
            "sw": 275,
            "sh": 325,
            "count": 4,
        }
        issues = self._run_with_fake(
            self._gradient_png(), [], source_assets=[meta], draw_records=[record]
        )
        self.assertIsNotNone(issues)
        self.assertIn("uncropped_sprite_atlas", ids(issues))
        issue = next(i for i in issues if i["id"] == "uncropped_sprite_atlas")
        self.assertIn("共 4 次", issue["msg"])

    def test_invalid_atlas_frame_reported_from_headless_probe(self):
        meta = {
            "url": "/assets/source/0123456789abcdef0123456789abcdef/card-big.png",
            "path": "media/graphics/game/card-big.png",
            "usage": "atlas",
            "width": 355,
            "height": 424,
            "atlas_frames": {
                "archer": {"x": 259, "y": 104, "w": 84, "h": 103},
            },
        }
        record = {
            **meta,
            "natural_width": 355,
            "natural_height": 424,
            "argc": 9,
            "sx": 199,
            "sy": 80,
            "sw": 64,
            "sh": 79,
            "count": 2,
        }
        issues = self._run_with_fake(
            self._gradient_png(), [], source_assets=[meta], draw_records=[record]
        )
        self.assertIn("invalid_atlas_frame", ids(issues))

    def test_source_rect_out_of_bounds_reported_from_headless_probe(self):
        meta = {
            "url": "/assets/source/0123456789abcdef0123456789abcdef/fish1.png",
            "path": "3/images/fish1.png",
            "usage": "sprite_sheet",
            "width": 55,
            "height": 296,
            "frame_width": 55,
            "frame_height": 37,
        }
        record = {
            **meta,
            "natural_width": 55,
            "natural_height": 296,
            "argc": 9,
            "sx": 55,
            "sy": 0,
            "sw": 55,
            "sh": 37,
            "count": 5,
        }

        issues = self._run_with_fake(
            self._gradient_png(), [], source_assets=[meta], draw_records=[record]
        )

        self.assertIn("source_rect_out_of_bounds", ids(issues))

    def test_duplicate_animation_loop_reported_from_runtime_probe(self):
        issues = self._run_with_fake(
            self._gradient_png(),
            [],
            runtime_probe={
                "duplicate_count": 1,
                "examples": [{"callback": "loop", "pending_before": 1}],
            },
        )
        self.assertIsNotNone(issues)
        self.assertIn("duplicate_animation_loop", ids(issues))

    def test_drawn_placeholder_asset_is_blocking(self):
        meta = {
            "url": "/assets/source/0123456789abcdef0123456789abcdef/background.jpg",
            "path": "media/graphics/backgrounds/desktop/background.jpg",
            "width": 640,
            "height": 480,
            "asset_role": "placeholder",
        }
        record = {
            **meta,
            "natural_width": 640,
            "natural_height": 480,
            "argc": 5,
            "sx": 0,
            "sy": 0,
            "sw": 640,
            "sh": 480,
            "count": 3,
        }
        issues = self._run_with_fake(
            self._gradient_png(), [], source_assets=[meta], draw_records=[record]
        )
        self.assertIn("placeholder_asset_drawn", ids(issues))

    def test_single_animation_loop_is_not_reported_from_runtime_probe(self):
        issues = self._run_with_fake(
            self._gradient_png(),
            [],
            runtime_probe={"duplicate_count": 0, "examples": []},
        )
        self.assertNotIn("duplicate_animation_loop", ids(issues))

    def test_failed_local_asset_is_medium_and_blocked_external_request_is_ignored(self):
        origin = verifier._preview_asset_origin()
        issues = self._run_with_fake(
            self._gradient_png(),
            [],
            asset_responses=[
                _FakeResponse(f"{origin}/assets/font/missing.woff2", 404),
            ],
            failed_requests=[
                _FakeRequest("https://example.com/tracker.js"),
            ],
        )
        self.assertIn("asset_load_failure", ids(issues))
        issue = next(i for i in issues if i["id"] == "asset_load_failure")
        self.assertEqual(issue["severity"], "medium")
        self.assertIn("missing.woff2", issue["msg"])
        self.assertNotIn("example.com", issue["msg"])


class HeadlessRuntimeProbeIntegrationTests(unittest.TestCase):
    """Real Chromium coverage for preview parity and animation-loop tracing."""

    @staticmethod
    def _html(script):
        return (
            "<!DOCTYPE html><html><head><style>"
            "html,body,canvas{margin:0;width:100%;height:100%;display:block}"
            "</style></head><body><canvas id='c' width='390' height='740'></canvas><script>"
            "const canvas=document.getElementById('c');"
            "const ctx=canvas.getContext('2d');"
            "function paint(){const g=ctx.createLinearGradient(0,0,390,740);"
            "g.addColorStop(0,'#102050');g.addColorStop(1,'#f09020');"
            "ctx.fillStyle=g;ctx.fillRect(0,0,390,740);}" + script +
            "</script></body></html>"
        )

    def _run_real(self, html):
        try:
            import playwright  # noqa: F401
        except Exception:
            self.skipTest("playwright 未安装")

        async def scenario():
            try:
                return await verifier.run_headless(html, timeout_s=30)
            finally:
                await verifier.aclose_browser()

        result = asyncio.run(scenario())
        if result is None:
            self.skipTest("chromium 浏览器不可用，跳过")
        return result

    def test_local_and_session_storage_match_preview_environment(self):
        html = self._html(
            "localStorage.setItem('score','7');"
            "sessionStorage.setItem('round','2');"
            "if(localStorage.getItem('score')!=='7')throw new Error('storage mismatch');"
            "paint();"
        )
        issues = self._run_real(html)
        self.assertEqual(issues, [])

    def test_duplicate_animation_loop_is_caught_after_three_second_timeout(self):
        html = self._html(
            "function loop(){paint();requestAnimationFrame(loop);}"
            "requestAnimationFrame(loop);"
            "setTimeout(()=>requestAnimationFrame(loop),3000);"
        )
        issues = self._run_real(html)
        self.assertIn("duplicate_animation_loop", ids(issues))

    def test_single_animation_loop_is_not_flagged(self):
        html = self._html(
            "function loop(){paint();requestAnimationFrame(loop);}"
            "requestAnimationFrame(loop);"
        )
        issues = self._run_real(html)
        self.assertNotIn("duplicate_animation_loop", ids(issues))
        self.assertNotIn("runtime_error", ids(issues))
        self.assertNotIn("blank_screen", ids(issues))

    def test_start_button_at_fifty_eight_percent_enters_gameplay(self):
        html = self._html(
            "paint();let state='start';"
            "canvas.addEventListener('pointerdown',e=>{"
            "if(e.clientY>410&&e.clientY<450){state='playing';"
            "requestAnimationFrame(()=>missingGameplayFunction());}});"
        )
        issues = self._run_real(html)
        self.assertIn("runtime_error", ids(issues))
        self.assertTrue(any("missingGameplayFunction" in i["msg"] for i in issues))


class HeadlessSelectorLoopTests(unittest.TestCase):
    """回归：Windows 下 uvicorn 跑的是 Selector 事件循环（不支持子进程），playwright
    曾因此 NotImplementedError、无头检查静默降级成"从不报问题"。现在 playwright 被
    调度到专属 Proactor 循环线程，Selector 主循环下也必须能跑（真实浏览器冒烟测试；
    未装 playwright/浏览器的环境自动跳过，CI 上不跑）。"""

    HTML = (
        "<!DOCTYPE html><html><head><style>canvas{width:100vw;height:100vh;display:block}"
        "</style></head><body><canvas id='c'></canvas><script>"
        "const canvas=document.getElementById('c');const ctx=canvas.getContext('2d');"
        "canvas.addEventListener('pointerdown',e=>{});"
        "function loop(){ drawCharacter(); requestAnimationFrame(loop); }"  # 故意未定义
        "loop();</script></body></html>"
    )

    def tearDown(self):
        # 复位 verifier 的循环绑定全局，别让真实浏览器测试影响后面的 fake 生命周期测试
        verifier._browser_lock = None
        verifier._idle_close_task = None

    def test_headless_catches_runtime_error_under_selector_loop(self):
        try:
            import playwright  # noqa: F401
        except Exception:
            self.skipTest("playwright 未安装")
        old_policy = asyncio.get_event_loop_policy()
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # 复现 uvicorn 环境
        try:
            async def scenario():
                res = await verifier.run_headless(self.HTML, timeout_s=30)
                await verifier.aclose_browser()
                return res
            res = asyncio.run(scenario())
        finally:
            asyncio.set_event_loop_policy(old_policy)
        if res is None:
            self.skipTest("chromium 浏览器不可用，跳过")
        self.assertIn("runtime_error", ids(res))
        self.assertTrue(any("drawCharacter" in i["msg"] for i in res))


if __name__ == "__main__":
    unittest.main()
