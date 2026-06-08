"""Tier 0 安全修复回归测试：路径穿越、session_id 校验、上传白名单、持久化守卫。"""

import unittest
from pathlib import Path

from fastapi import HTTPException

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
    def test_dangerous_extensions_listed(self):
        for ext in [".html", ".svg", ".js", ".xml"]:
            self.assertIn(ext, routes._DANGEROUS_ASSET_EXTS)

    def test_asset_type_allowlist(self):
        self.assertEqual(routes._ALLOWED_ASSET_TYPES, {"image", "audio", "spritesheet", "tilemap", "font"})


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
