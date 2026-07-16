"""无头输入可达性回归测试：双击开始手势 + "全屏遮罩拦截输入"探针。

背景（实测漏报案例）：AI 生成的 3D 魔方游戏里 #title-screen 是全屏 div 且
pointer-events:auto（父级 #ui 为 none 但子层重新开启），canvas 上的 dblclick
监听永远收不到事件，真实玩家无法开始游戏，而旧版 verify_game 判了通过。

三个 fixture 分别覆盖：坏模式（必须报 fullscreen_overlay_blocks_input high）、
DOM 按钮开始屏的合法模式（绝不能报）、修好的模式（不报，且双击手势确实触发——
游戏只在双击后绘制高方差画面，若手势缺失会退化成 blank_screen）。

真实 Chromium 冒烟测试：未装 playwright/chromium 的环境自动跳过（沿用
tests/test_verifier.py 中无头场景的处理惯例）。
"""

import asyncio
import unittest

from src.agent import verifier


def ids(issues):
    return {i["id"] for i in issues}


OVERLAY_ISSUE_ID = "fullscreen_overlay_blocks_input"

_BASE_STYLE = (
    "html,body{margin:0;width:100%;height:100%;background:#000}"
    "canvas{width:100vw;height:100vh;display:block}"
    # 复刻魔方案例的关键结构：#ui 整层 pointer-events:none，由子层各自重新开启
    "#ui{position:fixed;left:0;top:0;right:0;bottom:0;pointer-events:none}"
)


def _page(overlay_html: str, overlay_css: str, script: str) -> str:
    return (
        "<!DOCTYPE html><html><head><style>" + _BASE_STYLE + overlay_css +
        "</style></head><body>"
        "<canvas id='c' width='390' height='740'></canvas>"
        f"<div id='ui'>{overlay_html}</div>"
        "<script>"
        "const canvas=document.getElementById('c');"
        "const ctx=canvas.getContext('2d');"
        "function paintTitle(){ctx.fillStyle='#101820';ctx.fillRect(0,0,390,740);}"
        "function paintGame(){const g=ctx.createLinearGradient(0,0,390,740);"
        "g.addColorStop(0,'#102050');g.addColorStop(1,'#f09020');"
        "ctx.fillStyle=g;ctx.fillRect(0,0,390,740);}"
        + script +
        "</script></body></html>"
    )


# (a) 魔方式坏模式：全屏 pointer-events:auto 遮罩 + canvas 双击开始 → 双击永远到不了 canvas
BAD_OVERLAY_HTML = _page(
    "<div id='title-screen'><h1>3D 魔方</h1><p>双击屏幕开始</p></div>",
    "#title-screen{position:absolute;left:0;top:0;right:0;bottom:0;"
    "pointer-events:auto;background:rgba(10,20,40,0.55);color:#fff;text-align:center}",
    "paintGame();let state='title';"
    "canvas.addEventListener('dblclick',()=>{state='playing';"
    "document.getElementById('title-screen').style.display='none';});",
)

# (b) 合法模式：开始界面是 DOM 按钮（遮罩子树内有 button）→ 绝不能报。
# 按钮故意放在无头点击位置（50%/58%/70%/85% 高度）之外，保证探针采集时遮罩仍在，
# 从而直接验证"遮罩里有可交互元素就豁免"的防误报逻辑本身。
DOM_BUTTON_HTML = _page(
    "<div id='title-screen'><button id='start'>开始</button></div>",
    "#title-screen{position:absolute;left:0;top:0;right:0;bottom:0;"
    "pointer-events:auto;background:rgba(10,20,40,0.55)}"
    "#start{position:absolute;left:120px;top:80px;width:150px;height:48px}",
    "paintGame();let state='title';"
    "document.getElementById('start').addEventListener('click',()=>{state='playing';"
    "document.getElementById('title-screen').style.display='none';});",
)

# (c) 修好的魔方模式：遮罩 pointer-events:none，canvas 双击后才绘制高方差画面。
# 标题态是纯色（若双击手势缺失，无头检查会报 blank_screen"进不了游戏"类问题）。
FIXED_OVERLAY_HTML = _page(
    "<div id='title-screen'></div>",
    "#title-screen{position:absolute;left:0;top:0;right:0;bottom:0;pointer-events:none}",
    "paintTitle();let state='title';"
    "canvas.addEventListener('dblclick',()=>{if(state!=='title')return;"
    "state='playing';document.getElementById('title-screen').style.display='none';"
    "paintGame();});",
)


class InputReachabilityProbeAnalysisTests(unittest.TestCase):
    """纯函数聚合逻辑：无需浏览器，任何环境都跑。"""

    # 哨兵：区分"没传 blocker"与"显式传 blocker=None"（None 也是要测的合法输入）
    _UNSET = object()

    @classmethod
    def _probe(cls, blocker=_UNSET, **overrides):
        probe = {
            "total_points": 5,
            "blocked_points": 5,
            "blocker": {
                "selector": "div#title-screen",
                "coverage": 1.0,
                "has_interactive": False,
            },
        }
        probe.update(overrides)
        if blocker is not cls._UNSET:
            if isinstance(blocker, dict):
                probe["blocker"] = {**probe["blocker"], **blocker}
            else:
                probe["blocker"] = blocker
        return probe

    def test_full_block_without_interactive_children_is_high(self):
        issues = verifier._analyze_input_reachability_probe(self._probe())
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["id"], OVERLAY_ISSUE_ID)
        self.assertEqual(issues[0]["severity"], "high")
        self.assertIn("title-screen", issues[0]["msg"])
        self.assertIn("pointer-events:none", issues[0]["fix"])

    def test_overlay_with_interactive_children_is_not_reported(self):
        issues = verifier._analyze_input_reachability_probe(
            self._probe(blocker={"has_interactive": True})
        )
        self.assertEqual(issues, [])

    def test_partial_block_or_small_overlay_is_not_reported(self):
        self.assertEqual(
            verifier._analyze_input_reachability_probe(self._probe(blocked_points=4)),
            [],
        )
        self.assertEqual(
            verifier._analyze_input_reachability_probe(
                self._probe(blocker={"coverage": 0.5})
            ),
            [],
        )

    def test_missing_or_malformed_probe_is_ignored(self):
        self.assertEqual(verifier._analyze_input_reachability_probe({}), [])
        self.assertEqual(verifier._analyze_input_reachability_probe(None), [])
        self.assertEqual(
            verifier._analyze_input_reachability_probe(self._probe(blocker=None)),
            [],
        )


class HeadlessInputReachabilityTests(unittest.TestCase):
    """真实 Chromium 冒烟：verify_game(use_headless=True) 走完整无头链路。"""

    # 类级缓存 chromium 可用性，避免每个用例都额外冷跑一次探测页
    _chromium_checked = False
    _chromium_ok = False

    @classmethod
    def tearDownClass(cls):
        # 复位常驻浏览器实例，避免影响其它测试模块的 fake 生命周期测试
        try:
            asyncio.run(verifier.aclose_browser())
        except Exception:
            pass

    def _verify(self, html: str) -> dict:
        try:
            import playwright  # noqa: F401
        except Exception:
            self.skipTest("playwright 未安装")

        cls = type(self)
        if not cls._chromium_checked:
            cls._chromium_checked = True

            async def probe():
                return await verifier.run_headless(
                    "<canvas></canvas><script>1</script></html>", timeout_s=30
                )

            cls._chromium_ok = asyncio.run(probe()) is not None
        if not cls._chromium_ok:
            self.skipTest("chromium 浏览器不可用，跳过")

        return asyncio.run(verifier.verify_game(html, use_headless=True))

    def test_fullscreen_pointer_auto_overlay_with_canvas_dblclick_is_blocking(self):
        # 无头环境偶发慢启动会触发 run_headless 的墙钟超时（返回 None → 无头 issue
        # 缺失）；沿用现有惯例重试而不是删断言。
        got = set()
        result = None
        for _attempt in range(2):
            result = self._verify(BAD_OVERLAY_HTML)
            got = ids(result["issues"])
            if OVERLAY_ISSUE_ID in got:
                break
        self.assertIn(OVERLAY_ISSUE_ID, got, result["issues"])
        issue = next(i for i in result["issues"] if i["id"] == OVERLAY_ISSUE_ID)
        self.assertEqual(issue["severity"], "high")
        self.assertIn(OVERLAY_ISSUE_ID, ids(result["blocking"]))
        self.assertFalse(result["ok"])

    def test_dom_button_start_screen_is_not_reported(self):
        result = self._verify(DOM_BUTTON_HTML)
        self.assertNotIn(OVERLAY_ISSUE_ID, ids(result["issues"]), result["issues"])

    def test_fixed_overlay_lets_dblclick_reach_canvas_and_start_game(self):
        got = set()
        result = None
        for _attempt in range(2):
            result = self._verify(FIXED_OVERLAY_HTML)
            got = ids(result["issues"])
            # blank_screen 缺席才证明双击手势确实触发（游戏只在双击后绘制画面）；
            # 偶发慢环境下重试一次而不是放宽断言
            if OVERLAY_ISSUE_ID not in got and "blank_screen" not in got:
                break
        self.assertNotIn(OVERLAY_ISSUE_ID, got, result["issues"])
        self.assertNotIn("blank_screen", got, result["issues"])


if __name__ == "__main__":
    unittest.main()
