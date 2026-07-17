"""API 安全默认值策略测试（require_token）。

策略：公开路径（/play/、/assets/、/api/assets/file/）永不鉴权；回环与 TestClient
哨兵放行；真实局域网客户端默认 403 TOKEN_REQUIRED，仅当配置 API_TOKEN 且请求头
X-API-Token 精确匹配才放行。

用 httpx.ASGITransport 伪造客户端地址（scope["client"]）模拟局域网/回环请求，
不起真实服务器、不调真模型（打的都是只读/404 路径）。
"""

import asyncio
import unittest

import httpx

import src.main as main
from src.config import settings

LAN_CLIENT = ("10.0.0.9", 12345)
LOOPBACK_CLIENT = ("127.0.0.1", 54321)


def _request(method: str, path: str, client=LAN_CLIENT, headers=None, json_body=None):
    """以指定客户端地址向 app 发一次进程内请求（同步封装，遵循项目 asyncio.run 惯例）。"""

    async def _go():
        transport = httpx.ASGITransport(app=main.app, client=client)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            return await ac.request(method, path, headers=headers, json=json_body)

    return asyncio.run(_go())


class AuthPolicyTests(unittest.TestCase):
    """settings 是模块级单例：每个用例前置空 api_token，用例后还原原值。"""

    def setUp(self):
        self._orig_token = settings.api_token
        settings.api_token = ""

    def tearDown(self):
        settings.api_token = self._orig_token

    # ---- ① 局域网默认拒绝：403 + TOKEN_REQUIRED ----

    def test_lan_get_skills_403_token_required(self):
        r = _request("GET", "/api/skills")
        self.assertEqual(r.status_code, 403, r.text)
        detail = r.json()["detail"]
        self.assertEqual(detail["code"], "TOKEN_REQUIRED")
        self.assertIn("API_TOKEN", detail["message"])
        self.assertIn("X-API-Token", detail["message"])

    def test_lan_post_projects_403_token_required(self):
        r = _request("POST", "/api/projects",
                     json_body={"name": "t", "code": "<!DOCTYPE html><html></html>"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["detail"]["code"], "TOKEN_REQUIRED")

    # ---- ② 公开路径豁免：/play/ 走到路由本身（404），不是 403 ----

    def test_lan_play_public_returns_404_not_403(self):
        r = _request("GET", "/play/00000000-0000-4000-8000-000000000000")
        self.assertEqual(r.status_code, 404, r.text)

    # ---- ③ 公开路径豁免：/assets/ 不因鉴权拒绝 ----

    def test_lan_assets_not_403(self):
        r = _request("GET", "/assets/image/x.png")
        self.assertNotEqual(r.status_code, 403, r.text)

    def test_lan_api_assets_file_not_403(self):
        r = _request("GET", "/api/assets/file/image/x.png")
        self.assertNotEqual(r.status_code, 403, r.text)

    # ---- ④ 配置 API_TOKEN 后：局域网带正确 token 放行，带错 token 仍 403 ----

    def test_lan_with_correct_token_200(self):
        settings.api_token = "sekrit-123"
        r = _request("GET", "/api/skills", headers={"X-API-Token": "sekrit-123"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_lan_with_wrong_token_403(self):
        settings.api_token = "sekrit-123"
        r = _request("GET", "/api/skills", headers={"X-API-Token": "wrong"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["detail"]["code"], "TOKEN_REQUIRED")

    def test_lan_token_configured_but_header_missing_403(self):
        settings.api_token = "sekrit-123"
        r = _request("GET", "/api/skills")
        self.assertEqual(r.status_code, 403, r.text)

    # ---- ⑤ 回环 / TestClient 哨兵：零配置放行 ----

    def test_loopback_200_without_token(self):
        r = _request("GET", "/api/skills", client=LOOPBACK_CLIENT)
        self.assertEqual(r.status_code, 200, r.text)

    def test_loopback_200_even_when_token_configured(self):
        settings.api_token = "sekrit-123"  # 本机 UI 零配置：配了 token 回环也不用带
        r = _request("GET", "/api/skills", client=LOOPBACK_CLIENT)
        self.assertEqual(r.status_code, 200, r.text)

    def test_testclient_sentinel_200(self):
        from fastapi.testclient import TestClient

        client = TestClient(main.app)  # 不进 lifespan；client.host 即哨兵 "testclient"
        r = client.get("/api/skills")
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
