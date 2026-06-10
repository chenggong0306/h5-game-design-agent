"""AI H5游戏设计智能体 - 基于 LangGraph create_agent 重构"""

import asyncio
import re
import time
from collections import OrderedDict
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import aiosqlite
from langchain.tools import tool
from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import before_model, SummarizationMiddleware, ContextEditingMiddleware, ClearToolUsesEdit, AgentMiddleware, ModelRequest
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessageChunk
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.runtime import Runtime

from src.config import settings
from src.knowledge.knowledge_base import KnowledgeBase
from src.knowledge.phaser_skills import H5_GAME_SKILLS
from src.agent.code_editor import CodeEditor
from src.utils.logger import logger, log_tool_call, log_error, log_session_event
from src.utils.persistence import save_session_code, load_session_code, delete_session_code

# -------- 企业级：错误码定义 --------
class ErrorCode:
    """工具调用错误码枚举"""
    INPUT_TOO_LARGE = "INPUT_TOO_LARGE"
    CODE_SIZE_EXCEEDED = "CODE_SIZE_EXCEEDED"
    INVALID_PARAMS = "INVALID_PARAMS"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    APPEND_TO_EMPTY = "APPEND_TO_EMPTY"
    SYSTEM_ERROR = "SYSTEM_ERROR"


def format_error(code: str, message: str) -> str:
    """格式化错误返回。格式：[ERROR:CODE] message
    前端可通过正则 \\[ERROR:(\\w+)\\] 提取错误码做不同处理。
    """
    return f"[ERROR:{code}] {message}"

# -------- 全局知识库实例（工具函数需要访问） --------
_kb: KnowledgeBase | None = None
_current_session_id: ContextVar[str] = ContextVar("current_session_id", default="")
_code_by_session: dict[str, str] = {}
_staging_by_session: dict[str, str] = {}  # 分段写入新游戏的暂存区，校验通过后才提交到 _code_by_session
_code_session_last_access: dict[str, float] = {}  # 记录最后访问时间，用于清理
CODE_SESSION_TIMEOUT = 3600  # 1小时未访问的会话代码将被清理
# code_update 全量推送的最小间隔（秒）：一回合几十次编辑时去抖中间版本，
# 避免 O(编辑次数×代码体积) 的冗余 SSE 流量与前端 Monaco 反复全量重载
CODE_UPDATE_DEBOUNCE_SECONDS = 1.0

# -------- 企业级配置：资源限制 --------
MAX_CODE_SIZE = 5 * 1024 * 1024  # 单个代码文件最大 5MB
MAX_OLD_STR_SIZE = 100 * 1024  # str_replace 的 old_str 最大 100KB
MAX_TOTAL_CACHE_SIZE = 100 * 1024 * 1024  # 所有会话代码总缓存最大 100MB
MAX_SESSIONS = 1000  # 最多同时缓存 1000 个会话

# 限流已上移到 HTTP 层（src/api/routes.py）。原先按工具调用计数的限流会卡断长游戏的
# 分段写入，且层级不对；ErrorCode.RATE_LIMIT_EXCEEDED 保留给 HTTP 层使用。


def _get_current_code() -> str:
    """读取当前会话的编辑器代码，避免不同请求共享同一份全局代码。"""
    session_id = _current_session_id.get()
    if not session_id:
        return ""
    _code_session_last_access[session_id] = time.time()
    return _code_by_session.get(session_id, "")


def _set_current_code(code: str) -> None:
    """写入当前会话的编辑器代码，并自动持久化到磁盘。"""
    session_id = _current_session_id.get()
    if session_id:
        _code_by_session[session_id] = code
        _code_session_last_access[session_id] = time.time()
        _enforce_cache_limits()

        # 同步落盘（调用方负责线程化：@tool 跑在 executor 线程，
        # 协程路径经 asyncio.to_thread(_autocommit_staging/_reconcile_code_sync) 进来）
        try:
            save_session_code(session_id, code)
        except Exception as e:
            logger.warning("persist_failed", session_id=session_id, error=str(e))


def _enforce_cache_limits() -> None:
    """强制执行缓存限制：总大小不超过 MAX_TOTAL_CACHE_SIZE，会话数不超过 MAX_SESSIONS。
    使用 LRU 策略：优先清理最久未访问的会话。

    安全：先对 dict items 做 list 快照再操作，避免"dictionary changed size during
    iteration"——即使未来有代码在此函数内加了 await（单线程 asyncio 中同步代码是原子的，
    但快照模式更防御性）。
    """
    # 1. 检查会话数量限制
    if len(_code_by_session) > MAX_SESSIONS:
        # 快照：list() 消费 items view，后续 pop 不影响已拿到的列表
        sorted_sessions = sorted(
            list(_code_session_last_access.items()),
            key=lambda x: x[1],
        )
        to_remove = len(_code_by_session) - MAX_SESSIONS
        for session_id, _ in sorted_sessions[:to_remove]:
            _code_by_session.pop(session_id, None)
            _code_session_last_access.pop(session_id, None)

    # 2. 检查总大小限制
    total_size = sum(len(code) for code in _code_by_session.values())
    if total_size > MAX_TOTAL_CACHE_SIZE:
        sorted_sessions = sorted(
            list(_code_session_last_access.items()),
            key=lambda x: x[1],
        )
        for session_id, _ in sorted_sessions:
            if total_size <= MAX_TOTAL_CACHE_SIZE:
                break
            code_size = len(_code_by_session.get(session_id, ""))
            _code_by_session.pop(session_id, None)
            _code_session_last_access.pop(session_id, None)
            total_size -= code_size


def _reconcile_code_sync(session_id: str, code: str, client_dirty: bool = False) -> str:
    """协调出本回合的基准代码（纯同步，含磁盘读写，协程内请经 asyncio.to_thread 调用）。

    数据源唯一化：服务端权威代码 = 内存缓冲优先、其次磁盘 .html。前端传入的
    current_code 不再无脑覆盖服务端，而是按下列规则协调，避免 stale 标签页 / 渲染
    失败 / 多标签共享会话时用旧代码冲掉工具刚写好的权威代码：
    - 服务端没有代码 → 采用前端传入（新会话 / 新建场景）
    - client_dirty=True（用户确实在编辑器里手改过）→ 以前端为准
    - 前端没声明手改、但内容与服务端不一致 → 判为 stale，保留服务端权威代码并告警
    - 其余情况 → 两者一致或前端为空，取非空的一方

    注意：本函数不绑定 contextvar（to_thread 里 set 不会传播回协程），调用方负责
    先在协程内执行 `_current_session_id.set(session_id)`。
    """
    # 注意：不在回合开始清空分段写入暂存区。长游戏的分段写入可能跨回合（模型写了第1段
    # 就停下或分批），过早清空会让下一回合的 append_game 找不到上一段（APPEND_TO_EMPTY）。
    # 暂存区只在：提交成功 / 新的 write_game 覆盖 / clear_session / 会话过期 时清除。

    server_code = _code_by_session.get(session_id)
    if server_code is None:
        server_code = load_session_code(session_id) or ""
        if server_code:
            logger.info("session_restored_from_disk", session_id=session_id, size=len(server_code))

    if not server_code:
        chosen = code
    elif client_dirty:
        chosen = code if code else server_code
    elif code and code != server_code:
        logger.warning("frontend_code_diverged_kept_server",
            session_id=session_id, client_len=len(code), server_len=len(server_code))
        chosen = server_code
    else:
        chosen = code or server_code

    _code_by_session[session_id] = chosen
    _code_session_last_access[session_id] = time.time()
    _cleanup_old_sessions()

    if chosen:
        try:
            save_session_code(session_id, chosen)
        except Exception as e:
            logger.warning("persist_initial_code_failed", session_id=session_id, error=str(e))

    log_session_event(session_id, "session_started", code_size=len(chosen))

    return chosen


def _begin_code_session(session_id: str, code: str, client_dirty: bool = False):
    """绑定当前 async 上下文的会话 ID，并协调出本回合的基准代码（同步便捷入口）。

    ⚠️ 含同步磁盘 I/O（load/save_session_code），协程内不要直接调用——应改为：
    `token = _current_session_id.set(session_id)`（contextvar 必须留在协程内）
    + `await asyncio.to_thread(_reconcile_code_sync, session_id, code, client_dirty)`。
    本入口保留给同步调用方（测试 / 线程内工具路径）。
    """
    token = _current_session_id.set(session_id)
    _reconcile_code_sync(session_id, code, client_dirty)
    return token


def _end_code_session(token) -> None:
    """恢复 contextvar，保留会话代码供本次请求返回。"""
    _current_session_id.reset(token)


def _cleanup_old_sessions() -> None:
    """清理超过1小时未访问的会话代码，防止内存泄漏。"""
    current_time = time.time()
    expired_sessions = [
        sid for sid, last_access in _code_session_last_access.items()
        if current_time - last_access > CODE_SESSION_TIMEOUT
    ]

    if expired_sessions:
        logger.info("session_cleanup",
            expired_count=len(expired_sessions),
            total_sessions=len(_code_by_session))

    for sid in expired_sessions:
        _code_by_session.pop(sid, None)
        _staging_by_session.pop(sid, None)  # 暂存区不再每回合清空，过期时一并回收
        _code_session_last_access.pop(sid, None)
        _context_usage_by_session.pop(sid, None)  # 上下文圆环缓存同周期回收，防无限增长
        log_session_event(sid, "session_expired")


CONTEXT_WINDOW_TOKENS = 131_072          # 默认（openai/qwen 128K）
CONTEXT_WINDOW_TOKENS_DEEPSEEK = 1_000_000  # deepseek 1M 上下文
MODEL_OUTPUT_TOKEN_BUDGET = settings.max_output_tokens   # 与各模型的 max_tokens 保持一致
CONTEXT_SAFETY_MARGIN_TOKENS = 8_000
# 可用输入预算 = 窗口 - 输出预留 - 安全余量。
# 注意：system prompt / 工具 schema / 技能列表 / 注入代码 等"固定开销"不再从这里写死扣除，
# 而是由 _get_system_overhead_tokens() 实测后计入圆环分子（见 track_context_usage），更准确。
CONTEXT_INPUT_BUDGET_TOKENS = (
    CONTEXT_WINDOW_TOKENS - MODEL_OUTPUT_TOKEN_BUDGET - CONTEXT_SAFETY_MARGIN_TOKENS
)
CONTEXT_INPUT_BUDGET_TOKENS_DEEPSEEK = (
    CONTEXT_WINDOW_TOKENS_DEEPSEEK - MODEL_OUTPUT_TOKEN_BUDGET - CONTEXT_SAFETY_MARGIN_TOKENS
)
# 运行时根据 provider 动态设置，供 track_context_usage 使用
_active_context_window: int = CONTEXT_WINDOW_TOKENS
_active_input_budget: int = CONTEXT_INPUT_BUDGET_TOKENS
# SummarizationMiddleware 配置（触发阈值会被 LangChain 用 usage_metadata 校正为真实 token，故按窗口比例即可）
SUMMARIZATION_KEEP_MESSAGES = 10  # 总结后保留最近10条消息
SUMMARIZATION_TRIGGER_TOKENS_DEEPSEEK = int(CONTEXT_INPUT_BUDGET_TOKENS_DEEPSEEK * 0.70)
SUMMARIZATION_TRIGGER_TOKENS_DEFAULT = int(CONTEXT_INPUT_BUDGET_TOKENS * 0.70)
# ClearToolUsesEdit 触发阈值：按 provider 缩放（旧的写死 40000 在 1M 上只占 4%，几乎每回合
# 都把工具输出清成占位符，致盲模型）。设在总结阈值之下、作为更便宜的第一道防线；keep 放大到 20，
# 给"搜索→查看→编辑"留足可见窗口。
CLEAR_TOOL_TRIGGER_TOKENS_DEEPSEEK = int(CONTEXT_INPUT_BUDGET_TOKENS_DEEPSEEK * 0.50)
CLEAR_TOOL_TRIGGER_TOKENS_DEFAULT = int(CONTEXT_INPUT_BUDGET_TOKENS * 0.50)
CLEAR_TOOL_KEEP = 20
_context_usage_by_session: dict[str, dict[str, int | bool]] = {}


# 每张图片的 token 估算（OpenAI vision: low-res≈85, high-res≈170-1105；保守取 300）
_IMAGE_TOKEN_ESTIMATE = 300

# CJK 字符段匹配（范围与旧的逐字符 ord() 判断完全一致）：
# 假名 3040-30FF / 扩展A 3400-4DBF / 统一表意 4E00-9FFF / 韩文音节 AC00-D7A3 / 全角 FF00-FFEF
_CJK_RE = re.compile(
    "[぀-ヿ㐀-䶿一-鿿가-힣＀-￯]+"
)


def _estimate_tokens(text: str) -> int:
    """CJK 感知的近似 token 估算。

    count_tokens_approximately 默认 ~4 字符/token（按英文调），会把中文低估 2-3 倍
    （中文约 1 字 ≈ 1 token）。本项目大量中文（system prompt / 技能 / 代码注释），
    故按字符类别分别估算：CJK 约 1 token/字，其余约 3.5 字符/token。

    此外处理 _message_text_for_budget 插入的 [IMAGE] 占位符：每张图按固定值估算，
    避免 base64 data URL 被逐字符计数（几百 KB → 虚假 70k+ token）。
    """
    if not text:
        return 0
    image_count = 0
    if "[IMAGE]" in text:
        image_count = text.count("[IMAGE]")
        text = text.replace("[IMAGE]", "")
    # 预编译正则按"CJK 连续段"匹配求和（C 速度）。⚠️ 不要改成单字符 findall：
    # 物化逐字符匹配列表后实测仅 ~2x 提速，连续段匹配 240KB 文本约 2-3ms（旧循环 ~30ms）。
    cjk = sum(len(m) for m in _CJK_RE.findall(text))
    other = len(text) - cjk
    text_tokens = max(1, int(cjk + other / 3.5))
    return text_tokens + image_count * _IMAGE_TOKEN_ESTIMATE


def _message_text_for_budget(message) -> str:
    """提取消息中「对上下文预算有贡献」的文本部分。

    关键：当 content 是多模态列表（文本 + 图片块）时，跳过 base64 image_url，
    用固定估算替代 —— 否则几百 KB 的 base64 被逐字符计数会虚假拉高 70k+ token。
    """
    content = getattr(message, "content", None) or ""
    if isinstance(content, list):
        # 多模态消息：只收文本块，图片用标记占位（后续 token 计数加固定开销）
        parts = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("type", "")
                if t in ("text", "input_text", "output_text"):
                    parts.append(str(block.get("text", "") or ""))
                elif t == "image_url":
                    parts.append("[IMAGE]")  # 用 ~300 token 固定成本，不计 base64
                elif isinstance(block, str):
                    parts.append(block)
            elif isinstance(block, str):
                parts.append(block)
        content = "\n".join(parts)
    content = str(content or "")
    parts = [content]
    for tc in getattr(message, "tool_calls", []) or []:
        parts.append(str(tc))
    return "\n".join(parts)


# 按消息的 token 计数 LRU 缓存：历史消息不可变（ClearToolUsesEdit 用 model_copy 生成
# 新对象），同一条消息每次模型调用都会被 track_context_usage / 清理 / 总结三个钩子
# 重复计数。key 取 (消息 id, 文本长度)：内容被清成占位符时（id 不变）长度必变、键自然
# 失效，避免脏缓存；消息无 id 时直接计算不缓存。
_msg_token_cache: OrderedDict[tuple[str, int], int] = OrderedDict()
_MSG_TOKEN_CACHE_MAX = 4096


def _estimate_message_tokens(message) -> int:
    """单条消息的 CJK 感知 token 估算（带按消息 id 的 LRU 缓存）。"""
    text = _message_text_for_budget(message)
    mid = getattr(message, "id", None)
    if not mid:
        return _estimate_tokens(text)
    key = (mid, len(text))
    cached = _msg_token_cache.get(key)
    if cached is not None:
        _msg_token_cache.move_to_end(key)
        return cached
    count = _estimate_tokens(text)
    _msg_token_cache[key] = count
    while len(_msg_token_cache) > _MSG_TOKEN_CACHE_MAX:
        _msg_token_cache.popitem(last=False)
    return count


def _count_message_tokens_cjk(messages) -> int:
    """CJK 感知的"消息列表"token 计数，喂给 LangChain 压缩中间件。

    LangChain 默认的 count_tokens_approximately 按 ~4 字符/token（英文标定）计数，
    会把中文低估 3-4 倍，且 SummarizationMiddleware 的 usage_metadata 校正被硬上限
    钳到 ≤1.25×，根本补不回中文的欠计 → 触发阈值在中文场景永远够不着、压缩从不触发。
    这里改用项目自带的 CJK 感知估算（中文≈1 token/字），让阈值真正生效。
    """
    total = 0
    for m in messages or []:
        total += _estimate_message_tokens(m) + 3  # 每条消息额外开销
    return total


class CJKContextEditingMiddleware(ContextEditingMiddleware):
    """ContextEditingMiddleware 的 CJK 计数版。

    父类只有两档计数：
    - "approximate"：英文 4 字符/token，中文欠计 3-4 倍 → 清理永不触发；
    - "model"：对 gemma/vLLM/deepseek 这类非 OpenAI 标准模型名，
      get_num_tokens_from_messages 直接抛 NotImplementedError → **每回合崩**。
    两者都不可用，故改用项目自带的 CJK 感知计数器（与 SummarizationMiddleware 一致）。
    """

    def _edit(self, request):
        # 浅拷贝即可：ClearToolUsesEdit 只替换列表元素、用 model_copy 生成新对象，
        # 不就地修改原消息，故不会污染 checkpoint 里的历史。
        edited = list(request.messages)
        for edit in self.edits:
            edit.apply(edited, count_tokens=_count_message_tokens_cjk)
        return edited

    def wrap_model_call(self, request, handler):
        if not request.messages:
            return handler(request)
        return handler(request.override(messages=self._edit(request)))

    async def awrap_model_call(self, request, handler):
        if not request.messages:
            return await handler(request)
        return await handler(request.override(messages=self._edit(request)))


class SafeSummarizationMiddleware(SummarizationMiddleware):
    """总结失败时【跳过】本回合压缩，而不是把 "Error generating summary: ..." 当摘要写回、
    清空历史（上下文最满时的灾难性丢失）。覆盖 DeepSeek 模型名写错 / 服务抖动 / 超限等情形。
    清理层（CJKContextEditingMiddleware）仍会照常工作，作为更便宜的兜底。"""

    @staticmethod
    def _summary_failed(result) -> bool:
        if not result:
            return False
        for m in (result.get("messages") or []):
            c = getattr(m, "content", "")
            if isinstance(c, str) and (
                "Error generating summary" in c
                or "too long to summarize" in c
            ):
                return True
        return False

    def before_model(self, state, runtime):
        try:
            result = super().before_model(state, runtime)
        except Exception as e:
            logger.warning("summarization_skipped_error", error=str(e)[:200])
            return None
        if self._summary_failed(result):
            logger.warning("summarization_skipped_bad_summary")
            return None
        return result

    async def abefore_model(self, state, runtime):
        try:
            result = await super().abefore_model(state, runtime)
        except Exception as e:
            logger.warning("summarization_skipped_error", error=str(e)[:200])
            return None
        if self._summary_failed(result):
            logger.warning("summarization_skipped_bad_summary")
            return None
        return result


# -------- 每次调用的固定开销（system prompt + 技能列表 + 工具 schema），实测并缓存 --------
_system_overhead_tokens: int | None = None


def _compute_system_overhead_tokens() -> int:
    """估算每次模型调用都会发送的固定开销，用作上下文圆环分子的一部分，
    取代以前写死的 SYSTEM_AND_TOOLS_OVERHEAD_TOKENS（对内置情形高估十几倍，
    又看不见自定义技能带来的增长）。"""
    overhead = _estimate_tokens(SYSTEM_PROMPT)
    overhead += _estimate_tokens(_skills_prompt) + 60  # SkillMiddleware 每次注入技能列表
    try:
        for t in ALL_TOOLS + [load_skill, search_skills]:
            desc = getattr(t, "description", "") or ""
            overhead += int(_estimate_tokens(desc) * 1.4) + 20  # 描述 + 参数 schema 放大
    except Exception:
        pass
    return overhead


def _get_system_overhead_tokens() -> int:
    global _system_overhead_tokens
    if _system_overhead_tokens is None:
        try:
            _system_overhead_tokens = _compute_system_overhead_tokens()
        except Exception:
            _system_overhead_tokens = 2000  # 兜底
    return _system_overhead_tokens


def _invalidate_system_overhead() -> None:
    """技能增删后调用（自定义技能会改变 system 注入大小），让开销下次重新计算。"""
    global _system_overhead_tokens
    _system_overhead_tokens = None


@before_model
def track_context_usage(state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
    """跟踪上下文使用情况，用于前端显示。不再手动压缩，由 SummarizationMiddleware 自动处理。"""
    messages = state["messages"]

    # CJK 感知地统计消息 token（count_tokens_approximately 会把中文低估 2-3 倍）。
    # 每条消息再加 ~4 token 估算 role/结构开销。走按消息 id 的计数缓存，历史只算一次。
    used_tokens = sum(_estimate_message_tokens(m) + 4 for m in messages)

    # 真实占用 = 消息 + 注入代码（CodeContextMiddleware 放在 system，不在 messages 里）
    #          + 固定开销（system prompt / 技能列表 / 工具 schema）。
    # 三者都计入分子，分母为窗口-输出-安全余量（不再写死扣开销），圆环才反映真实占用。
    overhead_tokens = _get_system_overhead_tokens()
    try:
        code_tokens = _estimate_tokens(_get_current_code())
    except Exception:
        code_tokens = 0
    effective_used_tokens = used_tokens + code_tokens + overhead_tokens

    session_id = ""
    try:
        session_id = runtime.config.get("configurable", {}).get("thread_id", "")
    except Exception:
        session_id = _current_session_id.get()

    current_percent = round(effective_used_tokens / max(1, _active_input_budget) * 100)

    # 是否已压缩：直接检测 SummarizationMiddleware 插入摘要时打的标记，
    # 取代之前靠"消息数/百分比骤降"猜测的脆弱启发式（会误报/漏报）。
    summarized = any(
        getattr(m, "additional_kwargs", {}).get("lc_source") == "summarization"
        for m in messages
    )

    if session_id:
        _context_usage_by_session[session_id] = {
            "used_tokens": effective_used_tokens,
            "raw_message_tokens": used_tokens,
            "max_tokens": _active_input_budget,
            "context_window_tokens": _active_context_window,
            "reserved_output_tokens": MODEL_OUTPUT_TOKEN_BUDGET,
            "reserved_overhead_tokens": overhead_tokens + CONTEXT_SAFETY_MARGIN_TOKENS,
            "percent": current_percent,
            "message_count": len(messages),
            "compacted": summarized,
        }

    # 不返回任何修改，让 SummarizationMiddleware 处理
    return None


def _get_context_usage(session_id: str) -> dict[str, int | bool]:
    return _context_usage_by_session.get(session_id, {
        "used_tokens": 0,
        "raw_message_tokens": 0,
        "max_tokens": _active_input_budget,
        "context_window_tokens": _active_context_window,
        "reserved_output_tokens": MODEL_OUTPUT_TOKEN_BUDGET,
        "reserved_overhead_tokens": _get_system_overhead_tokens() + CONTEXT_SAFETY_MARGIN_TOKENS,
        "percent": 0,
        "message_count": 0,
        "compacted": False,
    })


# ============ 用 @tool 定义工具 ============

@tool
def search_assets(query: str) -> str:
    """搜索知识库中的游戏素材（图片、音频等）。

    Args:
        query: 搜索关键词，如 "玩家" "背景" "爆炸音效"
    """
    if not _kb:
        return "知识库未初始化"
    results = _kb.search_assets(query, top_k=5)
    if not results:
        return "知识库中暂无素材。"
    lines = []
    for a in results:
        atype = a.get("asset_type", "image")
        fname = a.get("file_name", "未知")
        aid = a.get("asset_id", "")
        ext = a.get("extension", "")
        url = f"/assets/{atype}/{aid}{ext}"
        lines.append(f"- [{atype}] {fname} → URL: {url}")
    return "\n".join(lines)

# ============ Skills 机制 ============

import json as _json

_CUSTOM_SKILLS_FILE = Path(settings.skills_dir) / "custom_skills.json"

def _load_custom_skills() -> list[dict]:
    """从磁盘加载用户自定义技能"""
    if _CUSTOM_SKILLS_FILE.exists():
        try:
            return _json.loads(_CUSTOM_SKILLS_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            # 不能静默吞错：损坏时所有自定义技能凭空消失，且下次 _save_custom_skills
            # 会用"不含丢失技能"的列表直接覆盖坏文件、原始数据二次销毁。
            logger.error("custom_skills_corrupt", file=str(_CUSTOM_SKILLS_FILE), error=str(e))
            try:
                # 改名保留原始数据供手工恢复；带时间戳避免 Windows 上目标已存在 rename 失败
                backup = _CUSTOM_SKILLS_FILE.with_name(
                    f"{_CUSTOM_SKILLS_FILE.name}.corrupt.{int(time.time())}.bak")
                _CUSTOM_SKILLS_FILE.rename(backup)
                logger.error("custom_skills_backed_up", backup=str(backup))
            except Exception as be:
                logger.error("custom_skills_backup_failed", error=str(be))
    return []

def _save_custom_skills() -> None:
    """将自定义技能（非内置、非品类模板的）保存到磁盘"""
    _CUSTOM_SKILLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    builtin_names = {skill["category"] for skill in H5_GAME_SKILLS}
    reserved = builtin_names | _GENRE_SKILL_NAMES  # 内置 + 品类模板都不算自定义
    custom = [s for s in SKILLS if s["name"] not in reserved]
    _CUSTOM_SKILLS_FILE.write_text(
        _json.dumps(custom, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# 随仓库分发的品类模板技能（动作/平台/射击/消除/跑酷/塔防），由 load_skill 按需加载
_GENRE_SKILLS_FILE = Path(__file__).resolve().parent.parent / "knowledge" / "genre_skills.json"


def _load_genre_skills() -> list[dict]:
    if _GENRE_SKILLS_FILE.exists():
        try:
            return _json.loads(_GENRE_SKILLS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


# 内置技能 + 品类模板 + 磁盘上的自定义技能
SKILLS: list[dict] = [
    {
        "name": skill["category"],
        "description": skill["title"],
        "content": skill["content"],
    }
    for skill in H5_GAME_SKILLS
]
# 品类模板（聚焦各品类承重代码，模型做对应游戏时 load_skill 加载并 lift）
_GENRE_SKILL_NAMES: set[str] = set()
for _gs in _load_genre_skills():
    if _gs.get("name") and _gs.get("content") and not any(s["name"] == _gs["name"] for s in SKILLS):
        SKILLS.append({
            "name": _gs["name"],
            "description": _gs.get("description", _gs["name"]),
            "content": _gs["content"],
        })
        _GENRE_SKILL_NAMES.add(_gs["name"])
# 启动时恢复自定义技能
for _cs in _load_custom_skills():
    if not any(s["name"] == _cs["name"] for s in SKILLS):
        SKILLS.append(_cs)


@tool
def load_skill(skill_name: str) -> str:
    """加载指定技能的完整内容到上下文。需要某个技能的详细指南/代码时调用。
    常用技能见 system prompt 的「可用技能」；未列出的先用 search_skills 检索拿到名称。

    Args:
        skill_name: 技能名称
    """
    for skill in SKILLS:
        if skill["name"] == skill_name:
            return f"## 技能: {skill['description']}\n\n{skill['content']}"
    # 名称不存在：给出相近建议而非全量列表（技能可能上百个）
    return f"技能 '{skill_name}' 不存在。可能想找：\n{_search_skills_impl(skill_name, limit=8)}"


def _search_skills_impl(query: str, limit: int = 12) -> str:
    q = (query or "").lower().strip()
    if not q:
        return f"技能库共 {len(SKILLS)} 个，名称如下：\n" + ", ".join(s["name"] for s in SKILLS)
    terms = [t for t in q.split() if t]
    scored = []
    for s in SKILLS:
        name = s["name"].lower()
        desc = (s.get("description") or "").lower()
        score = 0
        if q in name: score += 4
        if q in desc: score += 2
        for w in terms:
            if w in name: score += 2
            elif w in desc: score += 1
        if score > 0:
            scored.append((score, s))
    if not scored:
        return f"未找到与 '{query}' 相关的技能。用 search_skills(\"\") 可列出全部技能名。"
    scored.sort(key=lambda x: -x[0])
    top = scored[:limit]
    body = "\n".join(f"- **{s['name']}**: {s.get('description', '')}" for _, s in top)
    return f"找到 {len(scored)} 个相关技能（显示前 {len(top)}）：\n{body}"


@tool
def search_skills(query: str) -> str:
    """在技能库里按关键词检索技能（匹配名称/描述），返回最相关的若干条名称+描述。
    找到后用 load_skill(名称) 加载完整内容。query 传空字符串可列出全部技能名。

    Args:
        query: 关键词，如 "射击" "碰撞" "粒子" "对话" "排行榜"
    """
    return _search_skills_impl(query)


# 常用技能（内置 + 品类模板）始终列在 system；其余（如大量导入的自定义技能）改为按需 search_skills 检索，
# 避免每轮把上百条技能名注入 prompt（显著降低每次调用的固定开销）。
_CURATED_SKILL_NAMES: set[str] = {s["category"] for s in H5_GAME_SKILLS} | _GENRE_SKILL_NAMES


def _rebuild_skills_prompt() -> str:
    """重建注入用的技能列表（只列常用技能 + 一行检索提示）。技能增删后调用。"""
    global _skills_prompt
    curated = [s for s in SKILLS if s["name"] in _CURATED_SKILL_NAMES]
    lines = "\n".join(f"- **{s['name']}**: {s['description']}" for s in curated)
    extra = len(SKILLS) - len(curated)
    if extra > 0:
        lines += f"\n\n（技能库另有 {extra} 个技能未列出，用 search_skills(\"关键词\") 检索后再 load_skill 加载）"
    _skills_prompt = lines
    return _skills_prompt


_skills_prompt = _rebuild_skills_prompt()


def _inject_skills_into_request(request: ModelRequest) -> ModelRequest:
    """将技能列表注入到 system prompt（同步/异步共用逻辑）。"""
    from langchain_core.messages import SystemMessage
    skills_addendum = (
        f"\n\n## 可用技能\n\n{_skills_prompt}\n\n"
        "需要技能的详细指南/代码时调用 load_skill(skill_name)；"
        "未列出的技能先用 search_skills(\"关键词\") 检索。"
    )
    new_content = list(request.system_message.content_blocks) + [
        {"type": "text", "text": skills_addendum}
    ]
    new_system_message = SystemMessage(content=new_content)
    return request.override(system_message=new_system_message)


class SkillMiddleware(AgentMiddleware):
    """将技能列表注入到 system prompt，AI 需要时通过 load_skill 工具加载完整内容。"""

    tools = [load_skill, search_skills]

    def wrap_model_call(self, request, handler):
        return handler(_inject_skills_into_request(request))

    async def awrap_model_call(self, request, handler):
        return await handler(_inject_skills_into_request(request))


# ============ 代码上下文中间件（临时注入，不写入持久化历史） ============

_CODE_INJECT_MARKER = "【当前编辑器中的完整代码】"
_CODE_INJECT_HEADER = "## 当前编辑器中的完整代码"


def _strip_persisted_code_blocks(messages: list) -> tuple[list, bool]:
    """剥离历史消息里旧版本注入的整份代码块（兼容旧会话，避免重复累积）。

    旧实现把代码拼进用户消息（marker=【当前编辑器中的完整代码】）并被 checkpointer
    持久化，导致每回合多存一份。这里在请求副本上把这些块还原成纯用户输入，
    使得即便是改造前创建的老会话，也不会把多份历史代码喂给模型。
    """
    cleaned = []
    changed = False
    for m in messages:
        content = getattr(m, "content", None)
        if isinstance(content, str) and _CODE_INJECT_MARKER in content:
            idx = content.find("【用户消息】")
            new_content = content[idx + len("【用户消息】"):].strip() if idx != -1 else ""
            try:
                cleaned.append(m.model_copy(update={"content": new_content}))
            except Exception:
                cleaned.append(m)
            changed = True
        else:
            cleaned.append(m)
    return cleaned, changed


def _inject_code_into_request(request: ModelRequest) -> ModelRequest:
    """把当前会话的实时代码临时注入到 system message，仅本次模型调用可见。

    关键：注入发生在 wrap_model_call，作用于请求对象而非 AgentState，因此不会被
    checkpointer 持久化——历史里永远 0 份代码，每次调用最多 1 份，彻底消除累积。
    """
    req = request
    # 1) 清理旧会话遗留在历史里的代码块（防止历史残留多份）
    messages, changed = _strip_persisted_code_blocks(list(request.messages))
    if changed:
        req = req.override(messages=messages)

    # 2) 注入当前实时代码到 system message（临时、不持久化）
    code = _get_current_code()
    if not code or not code.strip():
        return req

    from langchain_core.messages import SystemMessage
    code_addendum = (
        f"\n\n{_CODE_INJECT_HEADER}\n```html\n{code}\n```\n"
        "（以上为编辑器实时代码，仅本次回答可见、不计入对话历史。"
        "修改时直接基于它定位，用 replace_code/insert_code/delete_code 写回；不要把它复述到聊天正文。）"
    )
    new_content = list(req.system_message.content_blocks) + [
        {"type": "text", "text": code_addendum}
    ]
    return req.override(system_message=SystemMessage(content=new_content))


class CodeContextMiddleware(AgentMiddleware):
    """每次模型调用前把当前编辑器代码临时注入 system message。

    取代旧的 _build_message 持久化注入：代码不再写进消息历史，从根本上消除
    「整份代码在 checkpoint 里累积 N 份」造成的上下文膨胀。
    """

    def wrap_model_call(self, request, handler):
        return handler(_inject_code_into_request(request))

    async def awrap_model_call(self, request, handler):
        return await handler(_inject_code_into_request(request))


# ============ 代码编辑工具 ============


# -------- 新建/重写游戏的事务化分段写入 --------

def _validate_staged_html(html: str) -> tuple[bool, str]:
    """判断暂存的 HTML 是否是一份写完整的文档（提交前的完整性校验）。"""
    low = html.lower()
    if not (low.lstrip().startswith("<!doctype") or "<html" in low):
        return False, "缺少 <!DOCTYPE html> / <html> 文档头"
    if "</html>" not in low:
        return False, "缺少 </html> 结束标签，文档可能尚未写完"
    return True, ""


def _strip_trailing_close_tags(html: str) -> str:
    """去掉文档结尾的 </html>/</body>（保留正文），以便在已闭合文档后继续 append_game。"""
    s = html.rstrip()
    while True:
        m = re.search(r"</\s*(?:html|body)\s*>\s*$", s, re.IGNORECASE)
        if not m:
            break
        s = s[:m.start()].rstrip()
    return s


def _commit_staging(session_id: str) -> str:
    """校验并提交暂存区到生效代码；失败时保留暂存区不动当前代码。"""
    staged = _staging_by_session.get(session_id, "")
    if not staged.strip():
        return format_error(ErrorCode.INVALID_PARAMS, "暂存区为空，没有可提交的内容")
    ok, reason = _validate_staged_html(staged)
    if not ok:
        return format_error(ErrorCode.INVALID_PARAMS,
            f"游戏代码尚不完整：{reason}。请用 append_game(html=..., more=True) 继续补全，"
            "并在最后一段传 more=False 再提交。")
    _set_current_code(staged)
    _staging_by_session.pop(session_id, None)
    lines = staged.count(chr(10)) + 1
    log_tool_call(session_id or "unknown", "write_game/commit", True, f"lines={lines}")
    return f"游戏代码已写入并生效，共 {lines} 行。"


def _stage_or_commit(session_id: str) -> str:
    """内容驱动的提交：暂存区一旦出现闭合的 </html> 就提交生效；
    否则保留在暂存区并提示继续（这是正常流程，不是错误，避免把中间段标记为失败）。
    """
    staged = _staging_by_session.get(session_id, "")
    ok, reason = _validate_staged_html(staged)
    if ok:
        return _commit_staging(session_id)
    lines = staged.count(chr(10)) + 1 if staged else 0
    return (f"已暂存至第 {lines} 行（文档尚未闭合：{reason}）。"
            "继续用 append_game 写后续片段即可；写到出现 </html> 会自动校验并整份生效。")


def _autocommit_staging(session_id: str) -> None:
    """回合结束兜底：
    - 文档完整（有 </html>）→ 提交生效并清空暂存。
    - 有文档头+脚本但缺 </html>（末段被截断）→ 补全闭合标签再提交，避免整局丢失。
    - 其余残缺 → 【保留】暂存，等下个回合继续 append（不再丢弃，避免分段跨回合时丢失进度）。
    """
    staged = _staging_by_session.get(session_id)
    if not staged or not staged.strip():
        return
    ok, _ = _validate_staged_html(staged)
    if not ok:
        low = staged.lower()
        if ("<!doctype" in low or "<html" in low) and "<script" in low and "</html>" not in low:
            patched = staged.rstrip()
            plow = patched.lower()
            # 末段可能截断在未闭合的 <script> 里：先补 </script> 再补 body/html，
            # 否则补出来的 </body></html> 会落在 script 内被当成 JS、整页解析坏掉。
            if plow.count("<script") > plow.count("</script>"):
                patched += "\n</script>"
                plow = patched.lower()
            if "</body>" not in plow:
                patched += "\n</body>"
            staged = patched + "\n</html>"
            ok = True
            logger.info("staging_autoclosed", session_id=session_id, size=len(staged))
    if ok:
        _set_current_code(staged)
        _staging_by_session.pop(session_id, None)
        logger.info("staging_autocommitted", session_id=session_id, size=len(staged))
    else:
        # 残缺但保留，等下个回合 append_game 续写（不丢弃）
        logger.info("staging_kept_for_continuation", session_id=session_id, size=len(staged))


# ============ gemma / harmony 控制标记清洗 ============
# 某些本地模型（gemma4 / harmony 风格）会把"通道"控制标记泄漏进正文，例如
#   <|channel|>  <|channel>  <channel|>  <|message|>  <|end|>  <start_of_turn> …
# 它们不是给用户看的，需在流式与非流式回复里都剥掉，否则聊天里会出现 <|channel> 等乱码。
_CTRL_TOKEN_RE = re.compile(
    r"<\|[a-zA-Z_]*\|?>"                          # <|channel|> <|channel> <|message|> <|end|> <|>
    r"|<[a-zA-Z_]+\|>"                            # <channel|>
    r"|</?\s*(?:start_of_turn|end_of_turn)\s*>",  # gemma 轮次标记
    re.IGNORECASE,
)
_CHANNEL_MARK_RE = re.compile(r"<\|?channel\|?>", re.IGNORECASE)
_CHANNEL_NAMES = ("analysis", "thinking", "thought", "commentary", "reasoning", "final")
_LEADING_CHANNEL_NAME_RE = re.compile(
    r"^\s*(?:" + "|".join(_CHANNEL_NAMES) + r")\b[ \t]*", re.IGNORECASE)


def _content_to_text(content) -> str:
    """把消息 content 规整成纯文本，兼容 str / dict / list / 嵌套 list。

    部分模型（gemma4/harmony 通道、带 reasoning、多模态）会把 content 返回成
    dict/嵌套 list 而非纯字符串，之前的流式清洗直接对它做 str 拼接 →
    `can only concatenate str (not "list") to str` 崩溃。这里统一抽取文本块、
    跳过 reasoning/thinking 等不该展示的块。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    # 裸 dict：{"type":"text","text":"hello"} 或 {"text":"..."}
    if isinstance(content, dict):
        text = content.get("text", "") or content.get("content", "") or ""
        if text:
            return str(text)
        # 递归处理 dict 内可能藏着的 content 列表
        inner = content.get("content")
        if inner is not None:
            return _content_to_text(inner)
        return str(content)
    # 列表（可能嵌套）：统一扫描抽取所有 text 块
    if isinstance(content, list):
        _SKIP = {"reasoning", "thinking", "thought", "redacted_thinking", "image", "image_url"}
        parts: list[str] = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                btype = b.get("type")
                if btype in (None, "text", "output_text", "input_text"):
                    parts.append(str(b.get("text", "") or ""))
                elif btype in _SKIP:
                    pass  # reasoning / thinking 块：本就不该展示给用户
                else:
                    # 未知 type，但有 text / content 字段就收进来（避免静默丢数据）
                    t = b.get("text") or b.get("content") or ""
                    if t:
                        parts.append(str(t))
            elif isinstance(b, list):
                # 嵌套 list：递归处理
                parts.append(_content_to_text(b))
        # 兜底：若已知类型一个都没提取到，再扫一遍把非 skip 块的 text 收进来
        if not "".join(parts).strip():
            for b in content:
                if isinstance(b, dict) and b.get("type") not in _SKIP:
                    t = b.get("text") or b.get("content") or ""
                    if t:
                        parts.append(str(t))
        return "".join(parts)
    # 其他未知类型：兜底 str() + 防御（如果 str() 返回了类名的 dump 就丢弃）
    s = str(content)
    return "" if s.startswith("<") and s.endswith(">") else s


def _strip_control_tokens(text) -> str:
    """一次性剥离全部控制标记（用于非流式整段文本）。"""
    text = _content_to_text(text)  # 容错：content 可能是分块 list
    if not text or "<" not in text:
        return text
    # 通道头整体去掉（保留其后正文）：<|channel|>analysis<|message|> / <|channel>thought<channel|>
    text = re.sub(
        r"<\|?channel\|?>[ \t]*(?:" + "|".join(_CHANNEL_NAMES) + r")?[ \t]*"
        r"(?:<\|?(?:channel|message)\|?>)?",
        "", text, flags=re.IGNORECASE)
    return _CTRL_TOKEN_RE.sub("", text)


def _sanitize_stream_chunk(ss: dict, raw) -> str:
    """流式清洗：累积缓冲，剥离完整控制标记；尾部可能被截断的 '<...' 暂存到下一块再处理。
    跨块场景（如 '<|channel>'、'thought'、'<channel|>' 分三块到达）也能正确清掉通道名。
    raw 可能是 str / dict / list / 嵌套 list（多模态/通道格式），先规整成纯文本再处理。
    """
    try:
        if not isinstance(raw, str):
            raw = _content_to_text(raw)
    except Exception:
        raw = str(raw or "")
    buf = ss.get("_san_buf", "") + raw
    if _CHANNEL_MARK_RE.search(buf):
        ss["_san_expect_name"] = True  # 见到通道标记 → 紧随其后的通道名也要清掉
    buf = _CTRL_TOKEN_RE.sub("", buf)
    if ss.get("_san_expect_name"):
        new = _LEADING_CHANNEL_NAME_RE.sub("", buf, count=1)
        if new != buf:
            ss["_san_expect_name"] = False
            buf = new
        elif buf.strip():  # 出现了真正的正文 → 不再等待通道名
            ss["_san_expect_name"] = False
    idx = buf.rfind("<")
    if idx != -1 and ">" not in buf[idx:] and (len(buf) - idx) <= 32:
        ss["_san_buf"] = buf[idx:]   # 暂存疑似被截断的标记
        return buf[:idx]
    ss["_san_buf"] = ""
    return buf


def _sanitize_stream_flush(ss: dict) -> str:
    """流结束时吐出暂存的尾部并清理清洗状态。"""
    rest = ss.get("_san_buf", "")
    ss["_san_buf"] = ""
    ss["_san_expect_name"] = False
    return _CTRL_TOKEN_RE.sub("", rest) if rest else ""


@tool
def write_game(html: str, more: bool = False) -> str:
    """写入一个全新的游戏，覆盖当前代码。新建游戏 / 整体重写时用它。

    高质量游戏代码很长，单次输出会被截断，所以支持分段写入：
    - 一次写完（短游戏）：write_game(html="<!DOCTYPE html>...完整...</html>")
    - 分段写入（长游戏）：
        write_game(html="<!DOCTYPE html>...<head>...<style>...", more=True)  ← 第1段
        append_game(html="<script>...游戏逻辑...", more=True)                 ← 续写，可多次
        append_game(html="...</script></body></html>", more=False)           ← 最后一段
    分段期间右侧预览不会刷新、原有代码不受影响；**写到出现闭合的 `</html>` 时
    自动校验并整份生效**（无需手动控制 more 标志）。中间段会回复"已暂存…继续"，
    那是正常进度提示、不是失败。每段不要太长（建议 ≤ 300 行）以免被截断。

    Args:
        html: 这一段 HTML 文本（第1段应以 <!DOCTYPE html> 开头）
        more: 可选、仅作语义提示；是否生效由内容是否包含 </html> 决定，不依赖此标志
    """
    session_id = _current_session_id.get()
    if not session_id:  # ContextVar 未设 → 会写到空键、与真实会话串味，直接拒绝
        return format_error(ErrorCode.SYSTEM_ERROR, "无活动会话，无法写入游戏")
    if len(html) > MAX_CODE_SIZE:
        return format_error(ErrorCode.INPUT_TOO_LARGE,
            f"html 超过 {MAX_CODE_SIZE // (1024*1024)}MB 限制")
    _staging_by_session[session_id] = html
    return _stage_or_commit(session_id)


@tool
def append_game(html: str, more: bool = False) -> str:
    """续写正在分段写入的游戏（接在 write_game 之后）。写到出现 </html> 自动生效。

    Args:
        html: 这一段 HTML 文本
        more: 可选、仅作语义提示；是否生效由内容是否包含 </html> 决定，不依赖此标志
    """
    session_id = _current_session_id.get()
    if not session_id:  # ContextVar 未设 → 拒绝，避免写到空键
        return format_error(ErrorCode.SYSTEM_ERROR, "无活动会话，无法写入游戏")
    staged = _staging_by_session.get(session_id, "")
    if not staged:
        # 容错自愈：模型常把第 1 段写成完整文档（含 </html>），它已自动提交、暂存被清空，
        # 随后的 append 找不到暂存就会报 APPEND_TO_EMPTY、在前端弹"续写失败"。改为无缝续写：
        low_html = html.lower().lstrip()
        if low_html.startswith("<!doctype") or low_html.startswith("<html"):
            # 续写内容本身就是一份完整新文档 → 按整体重写处理
            _staging_by_session[session_id] = html
            return _stage_or_commit(session_id)
        existing = _get_current_code()
        if existing and "<html" in existing.lower():
            # 从已生效代码恢复：去掉结尾闭合标签，把这段接上去，写到 </html> 再整份生效
            staged = _strip_trailing_close_tags(existing)
            logger.info("append_recovered_from_committed", session_id=session_id, base_len=len(staged))
        else:
            return format_error(ErrorCode.APPEND_TO_EMPTY,
                "当前没有正在写入的游戏。请先调用 write_game(html=第1段)。")
    if len(staged) + len(html) > MAX_CODE_SIZE:
        return format_error(ErrorCode.CODE_SIZE_EXCEEDED,
            f"代码总大小将超过 {MAX_CODE_SIZE // (1024*1024)}MB 限制")
    _staging_by_session[session_id] = staged + html
    return _stage_or_commit(session_id)


# -------- 修改已有代码的工具（直接作用于生效代码） --------

@tool
def replace_code(old_str: str, new_str: str = "", replace_all: bool = False) -> str:
    """在当前游戏代码中查找并替换片段——修 bug / 改参数 / 删片段的主力工具。

    - 修改：old_str=要替换的原片段，new_str=新内容
    - 删除：old_str=要删的片段，new_str="" (留空)
    - 替换全部匹配：replace_all=True
    支持空白归一化匹配（缩进不必完全一致，替换后保留原缩进）。
    若匹配到多处且未开 replace_all，会报错要求提供更精确的片段。

    Args:
        old_str: 要替换/删除的原始片段（必填、非空）
        new_str: 新内容；留空表示删除 old_str
        replace_all: True=替换所有匹配，False=只替换第一处（默认）
    """
    session_id = _current_session_id.get()
    start_time = time.time()
    if not old_str:
        return format_error(ErrorCode.INVALID_PARAMS, "old_str 不能为空；新建游戏请用 write_game。")
    if len(old_str) > MAX_OLD_STR_SIZE:
        return format_error(ErrorCode.INPUT_TOO_LARGE, f"old_str 超过 {MAX_OLD_STR_SIZE // 1024}KB 限制")
    if len(new_str) > MAX_CODE_SIZE:
        return format_error(ErrorCode.INPUT_TOO_LARGE, f"new_str 超过 {MAX_CODE_SIZE // (1024*1024)}MB 限制")

    result = CodeEditor.str_replace(_get_current_code(), old_str, new_str, replace_all=replace_all)
    if result["success"]:
        _set_current_code(result["code"])
    log_tool_call(session_id or "unknown", "replace_code", result["success"],
        f"old_len={len(old_str)}, new_len={len(new_str)}, replace_all={replace_all}",
        error=None if result["success"] else result["message"],
        duration_ms=(time.time() - start_time) * 1000)
    return result["message"]


@tool
def insert_code(after_line: int, new_str: str) -> str:
    """在指定行号之后插入代码（after_line=0 表示插到文件最前面）。
    建议先用 view_code / search_code 确认行号再插入。

    Args:
        after_line: 在第几行之后插入（0=最前面）
        new_str: 要插入的代码
    """
    session_id = _current_session_id.get()
    if len(new_str) > MAX_CODE_SIZE:
        return format_error(ErrorCode.INPUT_TOO_LARGE, "new_str 过大")
    result = CodeEditor.insert_after(_get_current_code(), after_line, new_str)
    if result["success"]:
        _set_current_code(result["code"])
    log_tool_call(session_id or "unknown", "insert_code", result["success"],
        f"after_line={after_line}, new_len={len(new_str)}",
        error=None if result["success"] else result["message"])
    return result["message"]


@tool
def delete_code(start_line: int, end_line: int) -> str:
    """删除指定行范围（含两端，行号从 1 开始）。建议先用 view_code 确认行号。

    Args:
        start_line: 起始行号（1-based）
        end_line: 结束行号（1-based，包含此行）
    """
    session_id = _current_session_id.get()
    result = CodeEditor.delete_lines(_get_current_code(), start_line, end_line)
    if result["success"]:
        _set_current_code(result["code"])
    log_tool_call(session_id or "unknown", "delete_code", result["success"],
        f"range={start_line}-{end_line}",
        error=None if result["success"] else result["message"])
    return result["message"]


@tool
def search_code(query: str, context_lines: int = 5) -> str:
    """在当前游戏代码中搜索关键字，返回匹配行及其上下文。
    用于定位需要修改的代码位置，避免盲目查看大段代码。

    Args:
        query: 要搜索的关键字
        context_lines: 每个匹配行前后显示的行数（默认5行）
    """
    code = _get_current_code()
    if not code:
        return "当前没有游戏代码"
    lines = code.split('\n')
    total = len(lines)
    query_lower = query.lower()
    match_indices = [i for i, line in enumerate(lines) if query_lower in line.lower()]
    if not match_indices:
        return f"未找到包含 '{query}' 的代码"

    # 合并相邻的上下文范围，避免重复输出
    ranges = []
    for idx in match_indices:
        start = max(0, idx - context_lines)
        end = min(total - 1, idx + context_lines)
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], end)
        else:
            ranges.append((start, end))

    parts = []
    for start, end in ranges:
        snippet = '\n'.join(
            f"{'>>>' if lines[i].lower().find(query_lower) >= 0 else '   '} {i+1:4d} | {lines[i]}"
            for i in range(start, end + 1)
        )
        parts.append(snippet)

    header = f"找到 {len(match_indices)} 处匹配（共 {total} 行）：\n"
    return header + '\n---\n'.join(parts)


@tool
def view_code(start_line: int = 1, end_line: int = -1) -> str:
    """查看当前游戏代码的指定行范围，返回带行号代码。修改前应先调用此工具确认上下文。
    每次最多返回 100 行，超出请分段查看。

    Args:
        start_line: 起始行号（从1开始）
        end_line: 结束行号（包含此行，-1表示到末尾）
    """
    # 限制单次最多返回 100 行，防止撑满上下文
    MAX_LINES = 100
    if end_line == -1 or end_line - start_line + 1 > MAX_LINES:
        end_line = start_line + MAX_LINES - 1
    result = CodeEditor.view_lines(_get_current_code(), start_line, end_line)
    total = result['total_lines']
    r = result['range']
    suffix = f"\n（共 {total} 行，当前显示 {r[0]}-{r[1]}，如需继续请调用 view_code({r[1]+1}, {r[1]+MAX_LINES})）" if r[1] < total else ""
    return f"共 {total} 行，当前显示 {r[0]}-{r[1]} 行：\n{result['content']}{suffix}"


# ============ System Prompt ============

SYSTEM_PROMPT = """你是一个专业的 H5 页面游戏设计 AI 助手，帮用户设计可在手机浏览器中运行的 H5 高质量游戏。

## 【工作流】

### 新建游戏时（用户首次描述游戏）：
1. **必须调用 `search_assets("图片 音频 素材")`** → 搜索可用素材
   - **有素材** → 调用 `load_skill("assets")` 获取 loadImages/loadSounds/drawSprite 用法，用图片渲染游戏对象
   - **暂无素材** → 自行用 Canvas API 绘制
2. 明确用户需求后再写 HTML，且必须满足下方【质量底线】
3. **强烈建议**：先判断游戏品类，加载对应**品类模板**技能（含该品类经过验证的承重代码，直接 lift 改写远胜从零写）：
   - 横版动作/格斗 → `load_skill("genre_action")`
   - 平台跳跃 → `load_skill("genre_platformer")`
   - 飞行/弹幕射击 → `load_skill("genre_shooter")`
   - 三消/消除 → `load_skill("genre_match3")`
   - 跑酷/无限横版 → `load_skill("genre_runner")`
   - 塔防 → `load_skill("genre_towerdefense")`
   - 其它品类或需要通用范式 → 按需 `load_skill("gameloop"|"polish"|"gamedesign")`（下方质量底线已含关键规则，简单游戏可不加载）
4. **写入完整 HTML**（高质量游戏代码很长，单次输出会被截断，需分段）：
   - 一次写完（短游戏）：`write_game(html="<!DOCTYPE html>...完整...</html>")`
   - 能一次写完就**优先一次** `write_game` 写完整份（含 `</html>`），最省事最可靠
   - 分段写入（确实超长时）：
     - 第1段：`write_game(html="<!DOCTYPE html>...<head>...<style>...")`
     - 续写段：`append_game(html="<script>...游戏逻辑...")`（可多次）
     - 末段以 `...</script></body></html>` 结尾 → 系统检测到 `</html>` **自动整份生效**
   - 🚫 **严禁在同一条消息里同时发出多个 write_game/append_game 调用（并行工具调用）**——
     它们会被并发执行、乱序，导致 append 跑在 write 前面而失败（APPEND_TO_EMPTY）。
     **必须一段一段来：发一个写入工具 → 等它的结果返回 → 再发下一段**，严格串行。
   - ⚠️ 每段控制在 ~150 行以内：整段要连同工具调用一起塞进单次输出预算，过长会被截断，
     导致该段工具调用报 "Error invoking tool"（参数被截坏）
   - 若某段写入工具报错（多半是该段太长被截断）：把**这一段拆得更小、重新发一次**即可，
     前面已暂存的段不受影响、不用重来
   - 中间段会回复"已暂存…继续写"，那是**正常进度**不是失败；只有出现 `</html>` 才生效
   - 是否生效只看内容里有没有闭合的 `</html>`，**不需要纠结 more 标志**；暂存会跨回合保留，可继续 append
5. 最终回复只总结游戏玩法、操作方式和完成内容，**不要在聊天中输出完整 HTML 代码块**

## 【质量底线】（每个游戏都必须做到，否则黑屏或手感差；需要完整模板时 load_skill）
- **结构顺序（不可乱，否则黑屏）**：canvas/ctx → `resize()` 定义并立即调用 → 全局状态(`state`/`score`/`lastTime`) → 工具函数 → update/draw → `resetGame` → 输入事件绑定 → 主循环 `loop` → 最后一行启动（图片加载完成才进 start）
- **DPR 适配（漏了就"看不到人物"！）**：`resize()` 里**这 4 行缺一不可**：① `canvas.width = W*dpr` ② `canvas.height = H*dpr`（物理缓冲）③ **`canvas.style.width = W+'px'; canvas.style.height = H+'px'`（CSS 显示尺寸，漏掉会让高分屏画面放大≈2倍、地面和人物全跑到屏幕外）** ④ `ctx.setTransform(dpr,0,0,dpr,0,0)`（**禁止 `ctx.scale`**）。保险起见 CSS 里再加 `canvas{width:100vw;height:100vh;display:block}`
- **坐标系（最易玩不了！）**：`resize()` 里存逻辑尺寸 `W=innerWidth, H=innerHeight`；之后**定位/地面/相机/UI/clearRect 一律用 `W`、`H`，绝不用 `canvas.width`/`canvas.height`**（那是物理像素=CSS×DPR，已 setTransform 后拿来定位会让角色/地面跑到屏幕外）。角色/敌人要有明确 y（统一站 `groundY = H - 地面高`），别让 y 留 0 或用 `canvas.height`
- **正确性**：移动一律乘 dt（`x += speed*dt`，禁止 `x += 5`）；`dt` 上限 0.05；`arc/ellipse` 半径用 `Math.max(1,r)`；删数组元素用 `filter`，**禁止 for 循环里 `splice`**
- **输入（桌面也要能玩！）**：游戏会在**桌面预览里用鼠标**测试、在手机上用触摸——**两者都必须支持**。优先用 Pointer Events（`pointerdown/pointermove/pointerup`，自动覆盖鼠标+触摸），或鼠标+触摸各绑一套。**只绑 `touchstart` 会导致桌面点击无反应、连开始都点不动**。坐标换算用 `(e.clientX - rect.left) * (W / rect.width)`（`rect=getBoundingClientRect()`，用 `rect.width` 不是 `canvas.width`）；开始/结束界面要能点击或按键进入
- **手感**：粒子、屏幕震动、缓动、得分浮动文字、渐变/光效背景、分数平滑滚动——击中/得分/死亡都要有视觉反馈
- **视觉（别做成方块！）**：角色/敌人**严禁用单个 `fillRect`**——至少分层（身体+头+眼睛+脚下椭圆阴影+黑色描边），配深浅渐变 + 简单程序化动画（呼吸起伏 / 走路四肢摆动 / 受击白闪）；背景用渐变 + 多层视差 + 氛围（暗角/光带），不要大片纯色；配色用同色系深浅，别直接填纯红纯绿纯蓝。**做角色、特效或场景时先 `load_skill("visual")` 取画法与配色**
- **设计**：难度随 `gameTime` 提升；完整 start/over 界面（标题 + 操作说明 + 本局得分 + 最高分 + 重玩提示）；HUD（分数左上、最高分右上）

### 修改/修 bug 时：
当前完整代码已在上下文中（system 区可见），直接定位后修改：
1. 改内容/参数：`replace_code(old_str=原片段, new_str=新内容)`，支持空白归一化匹配，无需缩进完全一致
2. 删片段：`replace_code(old_str=要删的片段, new_str="")`
3. 按行号增删：先 `view_code`/`search_code` 拿到行号，再 `insert_code(after_line, 新代码)` 或 `delete_code(start, end)`
4. 整体重做（仅当用户明确要求时）：用 `write_game` + `append_game` 重写
5. **绝不把完整代码输出到聊天正文**

### 加新功能时：
1. `search_code("插入点关键字")` 或 `view_code(...)` → 定位行号
2. `insert_code(after_line, 新代码)` 在该行之后插入；或用 `replace_code` 在某片段后扩写

---

## 【禁止事项】
- ❌ 新建游戏时禁止把完整 HTML 作为聊天正文输出，必须用 `write_game`/`append_game` 写入右侧编辑器
- ❌ 禁止在一条消息里并行发多个 `write_game`/`append_game`（会乱序失败）；一段一段、等结果再发下一段
- ❌ 最终总结只写**你真正实现并写进代码**的功能，不要罗列计划里却没实现的系统（不要夸大完成度）

## 【行为规则】
- ✅ 修改代码时必须一次性完成所有步骤（搜索→查看→替换），不要中途停下来回复用户等"继续"
- ✅ 如果需要多处修改，在一轮对话中连续调用工具完成全部修改，最后再统一回复
- ❌ 禁止调用一两个工具后就停下来告诉用户"我已经找到问题了"或"接下来我会..."——直接做完"""

# ============ Agent 类 ============

ALL_TOOLS = [
    search_assets,
    write_game,
    append_game,
    replace_code,
    insert_code,
    delete_code,
    view_code,
    search_code,
]


class GameDesignAgent:
    """基于 LangGraph create_agent 的游戏设计智能体"""

    def __init__(self, knowledge_base: KnowledgeBase):
        global _kb
        _kb = knowledge_base

        provider = settings.llm_provider.lower().strip()
        base_url_hint = (settings.openai_base_url or "").lower()
        model_hint = (settings.openai_model or "").lower()
        # 本地 vLLM / OpenAI 兼容端点（qwen / gemma 等自托管模型）统一走 "vllm" 分支
        _COMPATIBLE = {"qwen", "vllm", "gemma", "compatible"}
        if provider == "openai" and ("api.deepseek.com" in base_url_hint or model_hint.startswith("deepseek")):
            provider = "deepseek"
        elif provider == "openai" and (
            "qwen" in model_hint or "gemma" in model_hint
            or "autodl" in base_url_hint or ":6006" in base_url_hint
        ):
            provider = "vllm"
        elif provider in _COMPATIBLE:
            provider = "vllm"

        # 三个 provider 仅差 api_key/base_url/model 取值来源（deepseek 额外关 thinking），
        # 其余 9 个公共参数收敛为一份，避免改 timeout/max_retries 等漏改某分支造成
        # provider 间静默行为分叉。⚠️ 不要提成模块级常量：会在 import 时固化 settings。
        common_kwargs: dict[str, Any] = dict(
            model_provider="openai",
            temperature=settings.temperature,
            max_tokens=settings.max_output_tokens,
            top_p=0.95,
            presence_penalty=0.0,
            frequency_penalty=0.0,
            timeout=120,
            max_retries=2,
            stream_usage=settings.stream_usage,  # 流式时也回传 usage_metadata（OpenAI 兼容/自定义 base_url 端点默认会关；用于压缩计数的 ≤1.25x 校正与未来按比例触发）
        )
        extra_kwargs: dict[str, Any] = {}
        if provider == "deepseek":
            api_key = settings.deepseek_api_key or settings.openai_api_key
            base_url = settings.deepseek_base_url or settings.openai_base_url
            model_name = settings.deepseek_model or settings.openai_model
            extra_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        elif provider == "vllm":
            # 本地 vLLM / OpenAI 兼容端点（如 qwen、gemma-4）。沿用 QWEN_* 配置位，
            # 缺省回退到 OPENAI_*。vLLM 不需要真实 key，留任意非空字符串即可。
            api_key = settings.qwen_api_key or settings.openai_api_key or "EMPTY"
            base_url = settings.qwen_base_url or settings.openai_base_url
            model_name = settings.qwen_model or settings.openai_model
        else:
            api_key = settings.openai_api_key
            base_url = settings.openai_base_url
            model_name = settings.openai_model
        model = init_chat_model(
            model=model_name,
            api_key=api_key,
            base_url=base_url,
            **common_kwargs,
            **extra_kwargs,
        )

        checkpoint_dir = Path(settings.chroma_persist_dir).parent
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path = checkpoint_dir / "langgraph_checkpoints.sqlite"
        self.model = model
        # 根据 provider 设置活跃上下文窗口大小，供前端圆环显示
        global _active_context_window, _active_input_budget
        if provider == "deepseek":
            _active_context_window = CONTEXT_WINDOW_TOKENS_DEEPSEEK
            _active_input_budget = CONTEXT_INPUT_BUDGET_TOKENS_DEEPSEEK
        else:
            _active_context_window = CONTEXT_WINDOW_TOKENS
            _active_input_budget = CONTEXT_INPUT_BUDGET_TOKENS
        self.checkpoint_conn: aiosqlite.Connection | None = None
        self.checkpointer: AsyncSqliteSaver | None = None
        self.agent = None
        self._agent_lock: asyncio.Lock | None = None
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._provider = provider

    def _detect_provider(self) -> str:
        """返回当前使用的 LLM provider"""
        return self._provider

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """每会话一把锁，串行化同一会话的并发回合，防止读改写竞态与基准代码被冲掉。
        懒创建在单线程 asyncio 下无竞态（创建到使用之间无 await）。"""
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    async def _ensure_agent(self) -> None:
        """懒初始化持久化 LangGraph agent，避免 MemorySaver 重启丢上下文。"""
        if self.agent is not None:
            return
        if self._agent_lock is None:
            self._agent_lock = asyncio.Lock()
        async with self._agent_lock:
            if self.agent is not None:
                return
            self.checkpoint_conn = await aiosqlite.connect(str(self.checkpoint_path))
            self.checkpointer = AsyncSqliteSaver(self.checkpoint_conn)
            await self.checkpointer.setup()

            # 总结模型：仅在确实配置了 DeepSeek key 时用 DeepSeek（便宜、长上下文）；
            # 否则回退到主模型。避免用空 key 调 DeepSeek 失败 → 总结返回错误字符串
            # → RemoveMessage 把历史替换成报错（上下文最满时的灾难性丢失）。
            provider = self._detect_provider()
            if settings.deepseek_api_key:
                summarization_model = init_chat_model(
                    model=settings.deepseek_model or "deepseek-v4-flash",
                    model_provider="openai",
                    api_key=settings.deepseek_api_key,
                    base_url=settings.deepseek_base_url or "https://api.deepseek.com",
                    temperature=0.3,
                    max_tokens=8000,
                )
            else:
                summarization_model = self.model

            # 触发阈值按 provider 缩放（DeepSeek 1M vs 128K 差 7 倍，写死一个值必然失衡）
            is_deepseek = provider == "deepseek"
            clear_trigger = CLEAR_TOOL_TRIGGER_TOKENS_DEEPSEEK if is_deepseek else CLEAR_TOOL_TRIGGER_TOKENS_DEFAULT
            summ_trigger = SUMMARIZATION_TRIGGER_TOKENS_DEEPSEEK if is_deepseek else SUMMARIZATION_TRIGGER_TOKENS_DEFAULT

            self.agent = create_agent(
                self.model,
                ALL_TOOLS,
                system_prompt=SYSTEM_PROMPT,
                middleware=[
                    track_context_usage,
                    SkillMiddleware(),
                    CodeContextMiddleware(),
                    CJKContextEditingMiddleware(
                        edits=[
                            ClearToolUsesEdit(
                                trigger=clear_trigger,
                                keep=CLEAR_TOOL_KEEP,
                                clear_tool_inputs=True,  # 同时清理旧工具调用入参（write_game 的整份 HTML），生效代码已在编辑器/磁盘
                                placeholder="[已清理]",
                            ),
                        ],
                        # ⚠️ 不能用 "approximate"(中文欠计3-4倍/永不触发) 或 "model"
                        # (gemma/vLLM 非标准模型名→get_num_tokens_from_messages 抛 NotImplementedError/每回合崩)，
                        # CJKContextEditingMiddleware 用 CJK 感知计数器，二者皆免。
                    ),
                    SafeSummarizationMiddleware(
                        model=summarization_model,
                        trigger=("tokens", summ_trigger),
                        keep=("messages", SUMMARIZATION_KEEP_MESSAGES),
                        # ⚠️ 必传 CJK 感知计数器，否则默认英文标定计数把中文低估 3-4 倍、
                        # 且 usage_metadata 校正被钳到 ≤1.25×，触发阈值在中文场景永远够不着。
                        token_counter=_count_message_tokens_cjk,
                        trim_tokens_to_summarize=32000,
                    ),
                ],
                checkpointer=self.checkpointer,
            )


    @staticmethod
    def _build_message(user_message, current_code: str = ""):
        """直接透传用户消息，不再把代码拼进去。

        当前代码改由 CodeContextMiddleware 在每次模型调用时临时注入到 system
        message（不写入持久化历史），从根本上避免整份代码在 checkpoint 里累积。

        user_message 可能是字符串，也可能是多模态 content 块列表（带图片）——
        这里都原样返回，因此天然兼容「发截图 + 已有长代码」场景，
        不会再触发 list.lower() 的 AttributeError 崩溃。
        """
        return user_message

    async def chat(self, session_id: str, user_message, current_code: str = "", code_dirty: bool = False) -> dict:
        """非流式对话"""
        await self._ensure_agent()
        async with self._get_session_lock(session_id):  # 串行化同一会话的并发回合
            # contextvar 绑定必须留在协程内（to_thread 里 set 不会传播回来）；
            # 磁盘读写（load/save_session_code）下沉线程，不卡事件循环上的并发 SSE 流
            token = _current_session_id.set(session_id)
            await asyncio.to_thread(_reconcile_code_sync, session_id, current_code, code_dirty)
            base_code = _get_current_code()  # 协调后的基准代码
            try:
                config = {"configurable": {"thread_id": session_id}, "recursion_limit": 500}
                result = await asyncio.wait_for(  # 单回合墙钟上限
                    self.agent.ainvoke(
                        {"messages": [{"role": "user", "content": self._build_message(user_message, base_code)}]},
                        config=config,
                    ),
                    settings.turn_deadline_seconds,
                )

                reply = _strip_control_tokens(result["messages"][-1].content)  # 兼容 str / 分块 list
                # 兜底提交忘了 more=False 的分段写入；命中提交分支会整份落盘，下沉线程
                await asyncio.to_thread(_autocommit_staging, session_id)

                # 自检 + 自修闭环（非流式）：质检→有问题就让模型修→再判
                if settings.self_check_enabled:
                    from src.agent.verifier import verify_game, looks_like_game
                    for _ in range(max(0, settings.self_check_max_rounds)):
                        cur = _get_current_code()
                        if not cur or not looks_like_game(cur):
                            break
                        res = await verify_game(cur, use_headless=settings.self_check_headless)
                        if res["ok"]:
                            break
                        rep = await asyncio.wait_for(
                            self.agent.ainvoke(
                                {"messages": [{"role": "user", "content": self._build_repair_message(res["blocking"])}]},
                                config=config,
                            ),
                            settings.turn_deadline_seconds,
                        )
                        await asyncio.to_thread(_autocommit_staging, session_id)
                        new_reply = _strip_control_tokens(rep["messages"][-1].content)  # 兼容 str / 分块 list
                        if new_reply.strip():  # 修复回合可能以工具调用收尾、正文为空 → 别用空串覆盖主回合总结
                            reply = new_reply

                code = self._resolve_final_code(base_code, reply)
                action = "generate" if code and not base_code else "edit" if code else "chat"
                return {"reply": reply, "code": code, "action": action}
            finally:
                _end_code_session(token)

    @staticmethod
    def _resolve_final_code(base_code: str, reply: str) -> str:
        """回合结束后确定要返回/落库的代码，工具写好的缓冲区为唯一真相源。

        仅当缓冲区为空且本会话此前也没有代码时，才用正则从聊天正文里兜底抢救一份
        完整文档（模型违规把整份代码贴进聊天的情况）；缓冲区非空时绝不被聊天片段覆盖。
        """
        edited_code = _get_current_code()
        if edited_code:
            return edited_code
        if not base_code:  # 全新空会话才允许兜底抢救
            return GameDesignAgent._extract_code(reply) or ""
        return ""  # 缓冲区为空但会话本有代码 → 纯聊天，不要凭空生成代码

    @staticmethod
    def _chunk_to_events(chunk, ss) -> list:
        """把一个 astream chunk 映射成前端事件列表，并更新流状态 ss（主回合/自修回合共用）。"""
        evs = []
        if chunk.get("type") == "messages":
            msg = chunk["data"][0]
            if isinstance(msg, AIMessageChunk) and msg.content:
                clean = _sanitize_stream_chunk(ss, msg.content)  # 剥离 gemma/harmony 控制标记
                if clean and (ss["full_reply"] or clean.strip()):  # 跳过开头纯空白 token
                    ss["full_reply"] += str(clean)
                    evs.append({"type": "token", "content": clean})
        elif chunk.get("type") == "updates":
            for update in chunk["data"].values():
                if not isinstance(update, dict):
                    continue
                for msg in (update.get("messages") or []):
                    tcs = getattr(msg, "tool_calls", None)
                    if tcs:
                        for tc in tcs:
                            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                            args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
                            tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                            if name:
                                a = str(args)
                                evs.append({"type": "tool_call", "id": tid, "tool": name,
                                            "args": a[:100] + "..." if len(a) > 100 else a})
                    if type(msg).__name__ == "ToolMessage":
                        name = getattr(msg, "name", "unknown")
                        tid = getattr(msg, "tool_call_id", None)
                        c = str(getattr(msg, "content", ""))
                        evs.append({"type": "tool_result", "id": tid, "tool": name,
                                    "result": c[:200] + "..." if len(c) > 200 else c})
                        edited = _get_current_code()
                        if edited != ss["last_code_sent"]:
                            now = time.time()
                            if now - ss.get("last_code_emit_ts", 0.0) >= CODE_UPDATE_DEBOUNCE_SECONDS:
                                ss["last_code_sent"] = edited
                                ss["last_code_emit_ts"] = now
                                ss["code_pushed"] = True
                                evs.append({"type": "code_update", "code": edited, "source": name})
                            # else: 距上次推送太近，跳过中间版本（不更新 last_code_sent，
                            # 回合结束/自修后的兜底推送会补发最终代码，正确性不受影响）
        return evs

    async def _run_agent_stream(self, user_content, config, ss):
        """运行一轮 agent.astream，逐块产出前端事件。主回合与自修回合共用此逻辑。"""
        async for chunk in self.agent.astream(
            {"messages": [{"role": "user", "content": user_content}]},
            config=config,
            stream_mode=["messages", "updates"],
            version="v2",
        ):
            cu = _get_context_usage(ss["session_id"])
            key = tuple(cu.items())
            if key != ss["ctx_key"]:
                ss["ctx_key"] = key
                yield {"type": "context_usage", **cu}
            for ev in self._chunk_to_events(chunk, ss):
                yield ev
        # 流尾：吐出清洗缓冲里暂存的残尾，避免末尾几个字符被吞
        tail = _sanitize_stream_flush(ss)
        if tail and (ss["full_reply"] or tail.strip()):
            ss["full_reply"] += tail
            yield {"type": "token", "content": tail}

    @staticmethod
    def _build_repair_message(issues) -> str:
        lines = "\n".join(f"- {i['msg']}（建议：{i.get('fix', '')}）" for i in issues)
        return (
            "[自动质检] 刚生成的游戏经自动检测发现下列问题，请在**当前代码**上用 "
            "replace_code/insert_code 等工具针对性修复（不要重写整份）：\n"
            + lines +
            "\n修完用一句话说明改了什么即可，不要把完整代码贴到聊天里。"
        )

    async def _self_check_and_repair(self, session_id, config, base_code, ss):
        """质检当前代码；有 high/medium 问题就让模型自修，最多 self_check_max_rounds 轮，最后再判一次。"""
        from src.agent.verifier import verify_game, looks_like_game
        code = _get_current_code()
        if not code or not looks_like_game(code):
            return
        for _ in range(max(0, settings.self_check_max_rounds)):
            result = await verify_game(code, use_headless=settings.self_check_headless)
            if result["ok"]:
                yield {"type": "self_check", "status": "passed", "issues": []}
                return
            issues = result["blocking"]
            yield {"type": "self_check", "status": "repairing", "issues": [i["msg"] for i in issues]}
            ss["full_reply"] = ""  # 自修回合的正文不要拼接到主回合总结后面（与非流式 chat 的"替换"语义一致）
            async for ev in self._run_agent_stream(self._build_repair_message(issues), config, ss):
                yield ev
            await asyncio.to_thread(_autocommit_staging, session_id)
            new_code = _get_current_code()
            if new_code != ss["last_code_sent"]:
                ss["last_code_sent"] = new_code
                ss["code_pushed"] = True
                yield {"type": "code_update", "code": new_code, "source": "self_repair"}
            code = new_code
        final = await verify_game(code, use_headless=settings.self_check_headless)
        yield {"type": "self_check",
               "status": "passed" if final["ok"] else "issues_remain",
               "issues": [i["msg"] for i in final["blocking"]]}

    async def chat_stream(self, session_id: str, user_message, current_code: str = "", code_dirty: bool = False):
        """流式对话 - 逐 token 返回"""
        await self._ensure_agent()
        lock = self._get_session_lock(session_id)  # 串行化同一会话的并发回合
        config = {"configurable": {"thread_id": session_id}, "recursion_limit": 500}
        token = None
        acquired = False

        try:
            await lock.acquire()
            acquired = True
            # contextvar 绑定必须留在协程内（to_thread 里 set 不会传播回来）；
            # 磁盘读写（load/save_session_code）下沉线程，不卡事件循环上的并发 SSE 流
            token = _current_session_id.set(session_id)
            await asyncio.to_thread(_reconcile_code_sync, session_id, current_code, code_dirty)
            base_code = _get_current_code()  # 协调后的基准代码
            # 流状态：full_reply 累积文本、last_code_sent/last_code_emit_ts 去重+去抖 code_update、
            # code_pushed 标记本回合是否真的推送过 code_update（done 事件据此省掉重复全量代码）、
            # ctx_key 去重 context_usage
            ss = {"session_id": session_id, "full_reply": "", "last_code_sent": base_code,
                  "last_code_emit_ts": 0.0, "code_pushed": False, "ctx_key": None}

            async with asyncio.timeout(settings.turn_deadline_seconds):  # 单回合墙钟上限（含自修），防失控长跑
                # 主回合：模型生成
                async for ev in self._run_agent_stream(self._build_message(user_message, base_code), config, ss):
                    yield ev

                # 主回合结束：兜底提交分段写入（命中提交分支会整份落盘，下沉线程），并把新代码补发给前端
                await asyncio.to_thread(_autocommit_staging, session_id)
                edited_code = _get_current_code()
                if edited_code != ss["last_code_sent"] and edited_code != base_code:
                    ss["last_code_sent"] = edited_code
                    ss["code_pushed"] = True
                    yield {"type": "code_update", "code": edited_code, "source": "write_game"}

                # ★ 自检 + 自修闭环：生成游戏后自动质检，发现"看不到/玩不了/报错"类问题让模型自修后再返回
                if settings.self_check_enabled:
                    async for ev in self._self_check_and_repair(session_id, config, base_code, ss):
                        yield ev

            code = self._resolve_final_code(base_code, ss["full_reply"])
            action = "generate" if code and not base_code else "edit" if code else "chat"
            # 本回合已通过 code_update 推过同样内容时，done 不再重复携带整份代码
            # （app.js 仅在未收到过 code_update 时才用 done.code 兜底；routes.py 对空值有 or 兜底）
            done_code = None if (code and ss["code_pushed"] and code == ss["last_code_sent"]) else code
            yield {"type": "done", "code": done_code, "action": action}

        except asyncio.CancelledError:
            # 用户取消/断开：客户端已收不到任何事件，且取消必须向上传播
            # （吞掉它继续 yield 会触发 "async generator ignored GeneratorExit"）
            raise
        except MemoryError as e:
            # 内存不足
            log_error(session_id, ErrorCode.SYSTEM_ERROR, "内存不足", e)
            yield {"type": "error", "content": "内存不足，请减少代码长度或重启服务", "error_code": ErrorCode.SYSTEM_ERROR}
        except TimeoutError as e:
            # 超时
            log_error(session_id, "TIMEOUT", "请求超时", e)
            yield {"type": "error", "content": "请求超时，请稍后重试", "error_code": "TIMEOUT"}
        except Exception as e:
            # 其他未知错误，记录详细信息
            log_error(session_id, ErrorCode.SYSTEM_ERROR, f"chat_stream 异常: {str(e)}", e)
            yield {"type": "error", "content": f"系统错误: {str(e)}", "error_code": ErrorCode.SYSTEM_ERROR}
        finally:
            if token is not None:
                _end_code_session(token)
            # 只释放自己真正持有的锁：asyncio.Lock 不跟踪持有者，等锁中被取消时
            # 无条件 release() 会把"别的请求正持有的锁"放掉，串行化随之失效
            if acquired:
                lock.release()

    @staticmethod
    def _extract_code(text: str) -> str | None:
        """从回复中抢救一份完整 HTML 文档（仅用于全新空会话的兜底）。

        要求是写完整的文档（有文档头 + </html>），不再接受裸 <canvas> 或半截片段，
        避免把模型贴在聊天里的局部示例当成完整代码覆盖编辑器。
        """
        def _is_complete(code: str) -> bool:
            low = code.lower()
            return ("<!doctype" in low or "<html" in low) and "</html>" in low

        for pattern in (r"```html\s*\n(.*?)```", r"```\s*\n(.*?)```"):
            matches = [m.strip() for m in re.findall(pattern, text, re.DOTALL)]
            complete = [m for m in matches if _is_complete(m)]
            if complete:
                return max(complete, key=len)
        return None

    async def clear_session(self, session_id: str):
        try:
            await self._ensure_agent()
            if self.checkpointer:
                await self.checkpointer.adelete_thread(session_id)
        except Exception as e:
            # 不能静默吞：删除失败时完整对话历史仍残留在 checkpoint sqlite 里
            # （内存清理不碰 checkpoint DB），必须在日志里可排查
            logger.warning("checkpoint_delete_failed", session_id=session_id, error=str(e))
        # ⚠️ 必须持有会话锁再清理：否则与正在进行的 chat/chat_stream 回合（读改写生效代码）
        # 交错执行，会丢编辑/留下不一致状态。也【不要】pop 锁对象——pop 掉会让正在等锁的
        # 并发回合拿到一把新锁、失去串行化。
        async with self._get_session_lock(session_id):
            _context_usage_by_session.pop(session_id, None)
            _code_by_session.pop(session_id, None)
            _staging_by_session.pop(session_id, None)
            _code_session_last_access.pop(session_id, None)
            try:
                # 删磁盘文件，否则清空后重开会"复活"旧代码；unlink 下沉线程，不在持锁的事件循环上做磁盘 I/O
                await asyncio.to_thread(delete_session_code, session_id)
            except Exception as e:
                logger.warning("delete_session_code_failed", session_id=session_id, error=str(e))
