"""自检静态分析回归测试：复现并验证最近几轮真实出现的 Canvas 样板坑被检出。"""

import asyncio
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


if __name__ == "__main__":
    unittest.main()
