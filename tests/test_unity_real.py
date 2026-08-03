# -*- coding: utf-8 -*-
"""Unity 双结合·真环境集成测试（零 mock）。

依赖真实环境：A 线要求 Unity 编辑器打开画布工程且 MCP server 在线（27099）；
B 线要求本机装有 Unity 编辑器（无头 batchmode，首跑自动创建工厂工程，较慢）。
环境不满足时 skip 并说明开启方法——但只要环境在，全部真打。

WebGL 真构建（10 分钟级）另用环境变量门控：RUN_UNITY_BUILD=1 pytest -k webgl
"""
import io
import json
import os
import unittest
from pathlib import Path

from src.agent import unity_line
from src.config import settings

_BRIDGE = unity_line.bridge_alive()
_EDITOR = Path(settings.unity_editor_exe).exists()


@unittest.skipUnless(_BRIDGE, "Unity 桥不在线：打开 demo3d 并 Start MCP server 后重跑")
class UnityBridgeRealTests(unittest.TestCase):
    """A 线：真编辑器工具往返。"""

    PROBE = "ForgeTest_ItProbe"

    def tearDown(self):
        # 无论断言结果如何都清场，不在用户场景留垃圾
        unity_line.run_tool(
            "gameobject-destroy", json.dumps({"gameObjectRef": {"path": self.PROBE}})
        )

    def test_create_find_destroy_roundtrip(self):
        out = unity_line.run_tool("gameobject-create", json.dumps({"name": self.PROBE}))
        self.assertIn('"status": "success"', out, out[:500])
        found = unity_line.run_tool("gameobject-find", json.dumps({"gameObjectRef": {"path": self.PROBE}}))
        self.assertIn(self.PROBE, found, found[:500])
        gone = unity_line.run_tool(
            "gameobject-destroy", json.dumps({"gameObjectRef": {"path": self.PROBE}})
        )
        self.assertIn('"status": "success"', gone, gone[:500])

    def test_console_logs_readable(self):
        out = unity_line.run_tool("console-get-logs", "{}")
        self.assertNotIn("桥不在线", out)
        self.assertNotIn("HTTP 404", out)

    def test_bad_tool_name_feeds_catalog_back(self):
        """错误工具名不再透传英文报错，而是喂回真实清单 + 禁止盲试的硬约束。"""
        out = unity_line.run_tool("no-such-tool-xyz", "{}")
        self.assertIn("不存在", out)
        self.assertIn("gameobject-create", out)  # 清单确实在场
        self.assertIn("不要猜", out)


class UnityOfflineBehaviorTests(unittest.TestCase):
    """降级路径也要真验：把桥地址指向死端口，工具必须给出清晰指引而非崩溃。"""

    def test_offline_hint_when_bridge_down(self):
        orig = settings.unity_bridge_url
        settings.unity_bridge_url = "http://localhost:1"
        try:
            out = unity_line.run_tool("gameobject-create", '{"name": "x"}')
            self.assertIn("桥不在线", out)
            self.assertIn("H5", out)  # 必须给降级指引
        finally:
            settings.unity_bridge_url = orig


@unittest.skipUnless(_EDITOR, "未找到 Unity 编辑器：装好后重跑")
class UnityFactoryRealTests(unittest.TestCase):
    """B 线：真无头渲染（首跑含创建工厂工程，分钟级）。"""

    def test_demo_cube_sheet_rendered_for_real(self):
        from src.knowledge import unity_factory
        result = unity_factory.render("demo-cube", frames=6, size=128)
        self.assertTrue(result.get("ok"), result.get("error"))
        sheet = Path(result["sheet_path"])
        self.assertTrue(sheet.exists())
        from PIL import Image
        img = Image.open(io.BytesIO(sheet.read_bytes())).convert("RGBA")
        self.assertEqual(img.size, (128 * 6, 128))          # 横向 6 帧 grid
        self.assertEqual(img.getpixel((2, 2))[3], 0)        # 角落透明底
        centers_opaque = sum(
            1 for i in range(6) if img.getpixel((i * 128 + 64, 64))[3] > 0
        )
        self.assertGreaterEqual(centers_opaque, 4)          # 大多数帧中心有实体


@unittest.skipUnless(
    _BRIDGE and os.environ.get("RUN_UNITY_BUILD") == "1",
    "WebGL 真构建 10 分钟级：RUN_UNITY_BUILD=1 且桥在线时才跑",
)
class UnityWebGLBuildRealTests(unittest.TestCase):
    def test_build_real_or_reports_missing_module(self):
        out = unity_line.build_webgl("unity-build-it-test")
        ok = "构建成功" in out
        missing = "WebGL Build Support" in out
        self.assertTrue(ok or missing, out[:800])
        if ok:
            build = Path(settings.chroma_persist_dir).parent / "projects" / "unity-build-it-test" / "build"
            self.assertTrue((build / "index.html").exists())


if __name__ == "__main__":
    unittest.main()
