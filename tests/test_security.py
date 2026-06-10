"""Tier 0 安全修复回归测试：路径穿越、session_id 校验、上传白名单、持久化守卫。"""

import unittest
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import src.api.routes as routes
import src.utils.persistence as persistence


class AssetPathTraversalTests(unittest.TestCase):
    def _raises(self, fn):
        with self.assertRaises(HTTPException):
            fn()

    def test_traversal_payloads_blocked(self):
        for atype, fname in [
            ("image", "../../etc/passwd"),
            ("image", ".."),
            ("image", "a/b.png"),
            ("image", "a\\b.png"),
            ("image", ".env"),
            ("..", "x.png"),
            ("unknown_type", "x.png"),
        ]:
            self._raises(lambda a=atype, f=fname: routes._resolve_asset_path(a, f))

    def test_valid_asset_path_allowed(self):
        base = Path(routes._settings.assets_dir).resolve() / "image"
        base.mkdir(parents=True, exist_ok=True)
        f = base / "abc123.png"
        f.write_bytes(b"x")
        try:
            self.assertEqual(Path(routes._resolve_asset_path("image", "abc123.png")), f)
        finally:
            f.unlink()


class SessionIdValidationTests(unittest.TestCase):
    def test_accepts_uuid_like_ids(self):
        self.assertEqual(routes._require_safe_session_id("3f9a-2b_OK"), "3f9a-2b_OK")

    def test_rejects_unsafe_ids(self):
        for bad in ["../x", "a/b", "a\\b", "a.json", "", "x" * 200, "a b"]:
            with self.assertRaises(HTTPException):
                routes._require_safe_session_id(bad)


class UploadAllowlistTests(unittest.TestCase):
    # 浏览器可同源渲染/执行的扩展名：旧黑名单 + 审计发现的漏网项（.xhtml/.xht/.svgz
    # 会被 mimetypes 推断成 application/xhtml+xml / image/svg+xml，内联渲染即执行脚本）
    _RENDERABLE_EXTS = [".html", ".htm", ".svg", ".svgz", ".xhtml", ".xht",
                        ".xml", ".js", ".mjs", ".css"]

    def _client(self):
        app = FastAPI()
        app.include_router(routes.router)
        return TestClient(app)

    def test_asset_type_allowlist(self):
        self.assertEqual(routes._ALLOWED_ASSET_TYPES, {"image", "audio", "spritesheet", "tilemap", "font"})
        # 每个素材类型都必须有对应的扩展名白名单
        self.assertEqual(set(routes._ALLOWED_ASSET_EXTS), routes._ALLOWED_ASSET_TYPES)

    def test_renderable_extensions_not_in_any_whitelist(self):
        for ext in self._RENDERABLE_EXTS:
            for atype, allowed in routes._ALLOWED_ASSET_EXTS.items():
                self.assertNotIn(ext, allowed, f"{atype} 白名单不应包含 {ext}")

    def test_upload_bypass_extensions_rejected(self):
        """黑名单时代的绕过用例：.xhtml/.xht/.svgz 等可同源执行的类型必须被白名单挡掉。"""
        client = self._client()
        for fname in ["evil.xhtml", "evil.xht", "evil.svgz", "evil.svg",
                      "evil.html", "evil.js", "evil.XHTML", "noext"]:
            r = client.post(
                "/api/assets/upload",
                files={"file": (fname, b"<script>alert(1)</script>", "application/octet-stream")},
                data={"asset_type": "image"},
            )
            self.assertEqual(r.status_code, 400, f"{fname} 应被拒绝: {r.text}")

    def test_serve_rejects_non_whitelisted_extension(self):
        """出口兜底：磁盘上历史遗留的危险类型文件也不回源。"""
        base = Path(routes._settings.assets_dir).resolve() / "image"
        base.mkdir(parents=True, exist_ok=True)
        f = base / "deadbeef.xhtml"
        f.write_text("<html xmlns='http://www.w3.org/1999/xhtml'><script/></html>", encoding="utf-8")
        try:
            with self.assertRaises(HTTPException):
                routes._resolve_asset_path("image", "deadbeef.xhtml")
        finally:
            f.unlink()

    def test_upload_size_limit_413(self):
        client = self._client()
        orig = routes._settings.max_upload_bytes
        routes._settings.max_upload_bytes = 16
        try:
            r = client.post(
                "/api/assets/upload",
                files={"file": ("big.png", b"x" * 64, "image/png")},
                data={"asset_type": "image"},
            )
            self.assertEqual(r.status_code, 413)
        finally:
            routes._settings.max_upload_bytes = orig


class PersistenceGuardTests(unittest.TestCase):
    def test_unsafe_session_ids_rejected(self):
        self.assertFalse(persistence.save_session_code("../evil", "x"))
        self.assertIsNone(persistence.load_session_code("../evil"))
        self.assertFalse(persistence.delete_session_code("../evil"))

    def test_safe_session_ids_pass_validator(self):
        self.assertTrue(persistence._is_safe_session_id("good_id-1"))
        self.assertFalse(persistence._is_safe_session_id("a/b"))


if __name__ == "__main__":
    unittest.main()
