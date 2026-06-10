"""静态资源缓存版本号回归测试。

背景（第4次审计）：index.html 曾硬编码 ?v= 手工常量且双处同步，git 历史上因忘改
导致浏览器拿旧 JS 的事故（767a7c3 补救提交）。现改为服务端按 JS/CSS mtime 自动
注入版本号，并给 index 与 /static 响应加 Cache-Control: no-cache 兜底。
"""

import unittest

from fastapi.testclient import TestClient

import src.main as main


class StaticVersionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 不进 lifespan（不需要素材索引/技能加载），只打 "/" 与 /static
        cls.client = TestClient(main.app)

    def test_index_injects_auto_version(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        # 两处静态引用都带自动版本号
        self.assertIn(f"/static/css/style.css?v={main._STATIC_VERSION}", r.text)
        self.assertIn(f"/static/js/app.js?v={main._STATIC_VERSION}", r.text)
        # 模板变量必须被渲染（不能把 {{ v }} 原样吐给浏览器）
        self.assertNotIn("{{", r.text)
        # 手工常量已删除
        self.assertNotIn("20250610-paste", r.text)

    def test_index_response_no_cache(self):
        r = self.client.get("/")
        self.assertEqual(r.headers.get("cache-control"), "no-cache")

    def test_static_version_matches_file_mtime(self):
        base = main._BASE
        expected = str(int(max(
            (base / "static" / "js" / "app.js").stat().st_mtime,
            (base / "static" / "css" / "style.css").stat().st_mtime,
        )))
        self.assertEqual(main._STATIC_VERSION, expected)

    def test_static_files_send_no_cache(self):
        for path in ("/static/js/app.js", "/static/css/style.css"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertEqual(r.headers.get("cache-control"), "no-cache", path)


if __name__ == "__main__":
    unittest.main()
