"""联网搜索：给生成 agent 查玩法规则/技术资料用。

Provider 自动选择（沿用生图/视觉评审的配置模式）：
- TAVILY_API_KEY 配置时走 Tavily（为 agent 设计的搜索 API，结果干净带摘要）
- 未配置时走 DuckDuckGo（ddgs 包，免 key；本机代理环境实测可达）
失败一律抛 RuntimeError（带可读原因），由工具层转成提示——搜索挂了不阻断生成。
"""

import httpx

from src.config import settings

_TIMEOUT_S = 25
_MAX_RESULTS_CAP = 8


def _search_tavily(query: str, max_results: int) -> list[dict]:
    resp = httpx.post(
        "https://api.tavily.com/search",
        json={
            "api_key": settings.tavily_api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        },
        timeout=_TIMEOUT_S,
    )
    resp.raise_for_status()
    return [
        {"title": r.get("title") or "", "url": r.get("url") or "",
         "snippet": (r.get("content") or "")[:300]}
        for r in resp.json().get("results", [])
    ]


def _search_ddg(query: str, max_results: int) -> list[dict]:
    from ddgs import DDGS
    with DDGS(timeout=_TIMEOUT_S) as ddgs:
        raw = ddgs.text(query, max_results=max_results)
    return [
        {"title": r.get("title") or "", "url": r.get("href") or r.get("url") or "",
         "snippet": (r.get("body") or "")[:300]}
        for r in (raw or [])
    ]


def search(query: str, max_results: int = 5) -> list[dict]:
    """返回 [{title, url, snippet}]。禁用/无结果返回 []；出错抛 RuntimeError。"""
    if not settings.web_search_enabled:
        return []
    query = (query or "").strip()
    if not query:
        return []
    max_results = max(1, min(int(max_results), _MAX_RESULTS_CAP))
    last_err: Exception | None = None
    for attempt in range(2):  # 免 key 引擎对连发限流敏感，退避重试一次
        try:
            if settings.tavily_api_key:
                return _search_tavily(query, max_results)
            results = _search_ddg(query, max_results)
            if results or attempt == 1:
                return results
        except Exception as e:
            last_err = e
        import time
        time.sleep(2.5)
    raise RuntimeError(
        f"搜索失败（{type(last_err).__name__ if last_err else 'Empty'}: "
        f"{str(last_err)[:120] if last_err else '连续空结果'}）"
    )


def format_results(results: list[dict]) -> str:
    if not results:
        return "没有搜到结果。换个关键词试试，或凭已有知识继续。"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. **{r['title']}**\n   {r['url']}\n   {r['snippet']}")
    return "\n".join(lines)
