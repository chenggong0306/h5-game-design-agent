"""AI H5游戏设计智能体 - 基于 LangGraph create_agent 重构"""

import asyncio
import re
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import aiosqlite
from langchain.tools import tool
from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import before_model, SummarizationMiddleware, ContextEditingMiddleware, ClearToolUsesEdit, AgentMiddleware, ModelRequest
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.runtime import Runtime

from src.config import settings
from src.knowledge.knowledge_base import KnowledgeBase
from src.knowledge.phaser_skills import H5_GAME_SKILLS
from src.agent.code_editor import CodeEditor

# -------- 全局知识库实例（工具函数需要访问） --------
_kb: KnowledgeBase | None = None
_current_session_id: ContextVar[str] = ContextVar("current_session_id", default="")
_code_by_session: dict[str, str] = {}
_code_session_last_access: dict[str, float] = {}  # 记录最后访问时间，用于清理
_code_write_buffer: dict[str, list[str]] = {}  # 分块写入临时缓冲区
_last_write_session_id: str = ""  # ContextVar 丢失时的 fallback
CODE_SESSION_TIMEOUT = 3600  # 1小时未访问的会话代码将被清理


def _get_current_code() -> str:
    """读取当前会话的编辑器代码，避免不同请求共享同一份全局代码。"""
    session_id = _current_session_id.get()
    if not session_id:
        return ""
    _code_session_last_access[session_id] = time.time()
    return _code_by_session.get(session_id, "")


def _set_current_code(code: str) -> None:
    """写入当前会话的编辑器代码。"""
    session_id = _current_session_id.get()
    if session_id:
        _code_by_session[session_id] = code
        _code_session_last_access[session_id] = time.time()


def _begin_code_session(session_id: str, code: str):
    """绑定当前 async 上下文的会话 ID，并初始化该会话代码。"""
    token = _current_session_id.set(session_id)
    _code_by_session[session_id] = code
    _code_session_last_access[session_id] = time.time()
    _cleanup_old_sessions()
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
    for sid in expired_sessions:
        _code_by_session.pop(sid, None)
        _code_session_last_access.pop(sid, None)
        _code_write_buffer.pop(sid, None)  # 同时清理未完成的分块写入缓冲区


CONTEXT_WINDOW_TOKENS = 131_072          # 默认（openai/qwen 128K）
CONTEXT_WINDOW_TOKENS_DEEPSEEK = 1_000_000  # deepseek 1M 上下文
MODEL_OUTPUT_TOKEN_BUDGET = 16_000   # 与 max_tokens 保持一致
CONTEXT_SAFETY_MARGIN_TOKENS = 8_000
SYSTEM_AND_TOOLS_OVERHEAD_TOKENS = 24_000
CONTEXT_INPUT_BUDGET_TOKENS = (
    CONTEXT_WINDOW_TOKENS
    - MODEL_OUTPUT_TOKEN_BUDGET
    - CONTEXT_SAFETY_MARGIN_TOKENS
    - SYSTEM_AND_TOOLS_OVERHEAD_TOKENS
)
CONTEXT_INPUT_BUDGET_TOKENS_DEEPSEEK = (
    CONTEXT_WINDOW_TOKENS_DEEPSEEK
    - MODEL_OUTPUT_TOKEN_BUDGET
    - CONTEXT_SAFETY_MARGIN_TOKENS
    - SYSTEM_AND_TOOLS_OVERHEAD_TOKENS
)
# 运行时根据 provider 动态设置，供 track_context_usage 使用
_active_context_window: int = CONTEXT_WINDOW_TOKENS
_active_input_budget: int = CONTEXT_INPUT_BUDGET_TOKENS
# SummarizationMiddleware 配置
SUMMARIZATION_KEEP_MESSAGES = 10  # 总结后保留最近10条消息
# fraction 需要 langchain 支持 profile 参数，当前版本不支持，改用绝对 token 数
# deepseek 1M 上下文触发阈值：(1,000,000 - 开销) * 0.70
SUMMARIZATION_TRIGGER_TOKENS_DEEPSEEK = int((1_000_000 - MODEL_OUTPUT_TOKEN_BUDGET - CONTEXT_SAFETY_MARGIN_TOKENS - SYSTEM_AND_TOOLS_OVERHEAD_TOKENS) * 0.70)
# qwen/openai 128K 上下文触发阈值：CONTEXT_INPUT_BUDGET_TOKENS * 0.70
SUMMARIZATION_TRIGGER_TOKENS_DEFAULT = int(CONTEXT_INPUT_BUDGET_TOKENS * 0.70)
_context_usage_by_session: dict[str, dict[str, int | bool]] = {}


def _estimate_tokens(text: str) -> int:
    """使用 LangChain 的近似 token 计数。

    注意：这是快速估算，真实 token 数可能略有偏差。
    对于精确计数，应使用 model.get_num_tokens_from_messages()。
    """
    if not text:
        return 0
    # 使用 LangChain 的官方近似计数
    # 这比自定义的字符计数更准确
    try:
        msg = HumanMessage(content=text)
        return count_tokens_approximately([msg])
    except Exception:
        # 降级到简单估算
        ascii_chars = sum(1 for ch in text if ord(ch) < 128)
        non_ascii_chars = len(text) - ascii_chars
        base = ascii_chars / 2.5 + non_ascii_chars
        return max(1, int(base * 1.25))


def _message_text_for_budget(message) -> str:
    parts = [str(getattr(message, "content", "") or "")]
    for tc in getattr(message, "tool_calls", []) or []:
        parts.append(str(tc))
    return "\n".join(parts)


@before_model
def track_context_usage(state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
    """跟踪上下文使用情况，用于前端显示。不再手动压缩，由 SummarizationMiddleware 自动处理。"""
    messages = state["messages"]

    # 使用 LangChain 的官方 token 计数（更准确）
    try:
        used_tokens = count_tokens_approximately(messages)
    except Exception:
        # 降级到自定义估算
        used_tokens = sum(_estimate_tokens(_message_text_for_budget(m)) for m in messages)

    # used_tokens 是纯消息 tokens，max_tokens 已经扣除了系统开销，直接对比即可
    effective_used_tokens = used_tokens

    session_id = ""
    try:
        session_id = runtime.config.get("configurable", {}).get("thread_id", "")
    except Exception:
        session_id = _current_session_id.get()

    # 检测是否已总结：通过消息数量和 token 使用率的变化
    prev_usage = _context_usage_by_session.get(session_id, {})
    prev_percent = prev_usage.get("percent", 0)
    prev_msg_count = prev_usage.get("message_count", 0)
    current_percent = round(effective_used_tokens / _active_input_budget * 100)
    current_msg_count = len(messages)

    # 启发式检测：
    # 1. 消息数量突然减少到接近 KEEP 阈值
    # 2. 或者之前超过70%，现在降到60%以下
    msg_dropped = prev_msg_count > SUMMARIZATION_KEEP_MESSAGES + 5 and current_msg_count <= SUMMARIZATION_KEEP_MESSAGES + 3
    token_dropped = prev_percent >= 70 and current_percent < prev_percent * 0.6
    summarized = msg_dropped or token_dropped

    if session_id:
        _context_usage_by_session[session_id] = {
            "used_tokens": effective_used_tokens,
            "raw_message_tokens": used_tokens,
            "max_tokens": _active_input_budget,
            "context_window_tokens": _active_context_window,
            "reserved_output_tokens": MODEL_OUTPUT_TOKEN_BUDGET,
            "reserved_overhead_tokens": SYSTEM_AND_TOOLS_OVERHEAD_TOKENS + CONTEXT_SAFETY_MARGIN_TOKENS,
            "percent": current_percent,
            "message_count": current_msg_count,  # 记录消息数量
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
        "reserved_overhead_tokens": SYSTEM_AND_TOOLS_OVERHEAD_TOKENS + CONTEXT_SAFETY_MARGIN_TOKENS,
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
        return "未找到匹配的素材。请使用 Canvas 2D API 绘制图形。"
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
        except Exception:
            pass
    return []

def _save_custom_skills() -> None:
    """将自定义技能（非内置的）保存到磁盘"""
    _CUSTOM_SKILLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    builtin_names = {skill["category"] for skill in H5_GAME_SKILLS}
    custom = [s for s in SKILLS if s["name"] not in builtin_names]
    _CUSTOM_SKILLS_FILE.write_text(
        _json.dumps(custom, ensure_ascii=False, indent=2), encoding="utf-8"
    )

# 内置技能 + 磁盘上的自定义技能
SKILLS: list[dict] = [
    {
        "name": skill["category"],
        "description": skill["title"],
        "content": skill["content"],
    }
    for skill in H5_GAME_SKILLS
]
# 启动时恢复自定义技能
for _cs in _load_custom_skills():
    if not any(s["name"] == _cs["name"] for s in SKILLS):
        SKILLS.append(_cs)


@tool
def load_skill(skill_name: str) -> str:
    """加载指定技能的完整内容到上下文。当需要某个技能的详细指南时调用。
    可用技能列表见 system prompt 中的「可用技能」部分。

    Args:
        skill_name: 技能名称（见 system prompt 列出的可用技能）
    """
    for skill in SKILLS:
        if skill["name"] == skill_name:
            return f"## 技能: {skill['description']}\n\n{skill['content']}"
    available = ", ".join(s["name"] for s in SKILLS)
    return f"技能 '{skill_name}' 不存在。可用技能: {available}"


_skills_prompt = "\n".join(
    f"- **{s['name']}**: {s['description']}" for s in SKILLS
)


def _inject_skills_into_request(request: ModelRequest) -> ModelRequest:
    """将技能列表注入到 system prompt（同步/异步共用逻辑）。"""
    from langchain_core.messages import SystemMessage
    skills_addendum = (
        f"\n\n## 可用技能\n\n{_skills_prompt}\n\n"
        "需要某个技能的详细指南时，调用 load_skill(skill_name) 工具加载。"
    )
    new_content = list(request.system_message.content_blocks) + [
        {"type": "text", "text": skills_addendum}
    ]
    new_system_message = SystemMessage(content=new_content)
    return request.override(system_message=new_system_message)


class SkillMiddleware(AgentMiddleware):
    """将技能列表注入到 system prompt，AI 需要时通过 load_skill 工具加载完整内容。"""

    tools = [load_skill]

    def wrap_model_call(self, request, handler):
        return handler(_inject_skills_into_request(request))

    async def awrap_model_call(self, request, handler):
        return await handler(_inject_skills_into_request(request))


# ============ 代码编辑工具 ============


@tool
def str_replace_code(old_str: str, new_str: str) -> str:
    """替换当前游戏代码中的一段内容。用于修 bug、改参数、小范围修改。
    支持空白归一化匹配：缩进不完全一致也能匹配，匹配后保留原始缩进。
    插入新代码：将 new_str 设为 old_str + 新内容 即可在目标位置后追加。
    删除代码：将 new_str 设为空字符串即可。

    Args:
        old_str: 要被替换的原始代码片段
        new_str: 替换后的新代码（空字符串表示删除）
    """
    result = CodeEditor.str_replace(_get_current_code(), old_str, new_str)
    if result["success"]:
        _set_current_code(result["code"])
    return result["message"]


def _detect_startup_order_issues(code: str) -> list[str]:
    """检测常见黑屏启动顺序问题，尤其是 let/const 变量声明前被顶层调用访问。
    只检测顶层调用（script 标签内缩进为0的行），忽略函数体内的调用。"""
    issues = []
    startup_calls = ["resize()", "init()", "resetGame()", "gameLoop()", "loop()", "requestAnimationFrame("]
    guarded_vars = ["player", "state", "canvas", "ctx"]

    lines = code.split('\n')

    # 找每个变量的首次顶层声明行号
    var_declared_line: dict[str, int] = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        for var_name in guarded_vars:
            if var_name not in var_declared_line:
                if stripped.startswith(f"let {var_name}") or stripped.startswith(f"const {var_name}") or stripped.startswith(f"var {var_name}"):
                    var_declared_line[var_name] = i

    # 找每个启动调用的首次顶层出现行号（缩进为0，不在函数体内）
    for call in startup_calls:
        for i, line in enumerate(lines):
            stripped = line.strip()
            # 只检测顶层调用：行首无缩进（或只有0-1层缩进），且不在函数/if/for 块内
            if call in stripped and not line.startswith(' ') and not line.startswith('\t'):
                for var_name, decl_line in var_declared_line.items():
                    if i < decl_line:
                        issues.append(f"顶层 {call} (第{i+1}行) 出现在 {var_name} 声明 (第{decl_line+1}行) 之前，可能触发 Cannot access '{var_name}' before initialization")

    if "ctx.scale(" in code and "ctx.setTransform(" not in code:
        issues.append("resize 中使用 ctx.scale() 可能重复叠加缩放，请改用 ctx.setTransform(dpr,0,0,dpr,0,0)")
    return issues


@tool
def start_code_write(reason: str = "") -> str:
    """开始分块写入游戏代码。用于写入超长代码（>500行）时，先调用此工具初始化。

    Args:
        reason: 本次写入代码的原因或摘要
    """
    global _last_write_session_id
    session_id = _current_session_id.get()
    if not session_id:
        return "错误：无法获取会话ID"
    _last_write_session_id = session_id
    _code_write_buffer[session_id] = []
    return f"已初始化代码写入缓冲区。{f'原因：{reason}' if reason else ''}\n请依次调用 append_code_chunk 写入代码块，最后调用 finish_code_write 完成。"

@tool
def append_code_chunk(chunk: str) -> str:
    """追加一块代码到缓冲区。用于分块写入长代码。

    Args:
        chunk: 代码片段（可以是 HTML 的一部分）
    """
    session_id = _current_session_id.get() or _last_write_session_id
    if not session_id:
        return "错误：无法获取会话ID"
    if session_id not in _code_write_buffer:
        return "错误：请先调用 start_code_write 初始化缓冲区"

    _code_write_buffer[session_id].append(chunk)
    total_chars = sum(len(c) for c in _code_write_buffer[session_id])
    return f"已追加 {len(chunk)} 字符，当前缓冲区共 {total_chars} 字符。继续调用 append_code_chunk 或调用 finish_code_write 完成。"

@tool
def finish_code_write() -> str:
    """完成分块写入，将缓冲区的所有代码块合并并写入编辑器。"""
    session_id = _current_session_id.get() or _last_write_session_id
    if not session_id:
        return "错误：无法获取会话ID"
    if session_id not in _code_write_buffer:
        return "错误：请先调用 start_code_write 初始化缓冲区"

    chunks = _code_write_buffer.pop(session_id)
    if not chunks:
        return "错误：缓冲区为空，没有代码可写入"

    clean_code = "".join(chunks).strip()
    startup_issues = _detect_startup_order_issues(clean_code)
    if startup_issues:
        # 放回缓冲区，让 AI 可以用 str_replace_code 修复后重新 finish
        _code_write_buffer[session_id] = chunks
        return (
            "写入失败：生成的 HTML 可能黑屏，必须先修复启动顺序问题：\n"
            + "\n".join(f"- {issue}" for issue in startup_issues)
            + "\n\n缓冲区已保留，请修正代码后重新调用 finish_code_write()"
        )

    _set_current_code(clean_code)
    lines = clean_code.count("\n") + 1
    return f"✅ 已成功写入右侧编辑器 game.html，共 {lines} 行。"

@tool
def write_game_code(code: str = "", reason: str = "") -> str:
    """将完整 H5 游戏代码写入当前会话的右侧代码编辑器。

    ⚠️ 重要：如果代码超过 500 行或生成时被截断，请改用分块写入：
    1. start_code_write(reason) - 初始化
    2. append_code_chunk(chunk) - 多次调用，每次传入一部分代码
    3. finish_code_write() - 完成写入

    Args:
        code: 完整 HTML 代码，必须可直接在浏览器运行。不可为空。
        reason: 本次写入代码的原因或摘要
    """
    clean_code = code.strip()
    if not clean_code:
        return (
            "写入失败：缺少 code 参数。\n\n"
            "💡 提示：如果代码太长导致被截断，请使用分块写入：\n"
            "1. start_code_write(reason)\n"
            "2. append_code_chunk(chunk) × N\n"
            "3. finish_code_write()"
        )

    # 检查是否可能被截断：行数少且没有闭合的 </html> 标签
    lines = clean_code.count("\n") + 1
    if not clean_code.rstrip().endswith("</html>") and lines < 50:
        return (
            f"⚠️ 代码似乎被截断了（只有 {lines} 行，且未以 </html> 结尾）。\n\n"
            "请使用分块写入机制：\n"
            "1. start_code_write(reason='重新生成完整游戏')\n"
            "2. append_code_chunk(chunk) - 多次调用，每次传入一部分代码\n"
            "3. finish_code_write()\n\n"
            "示例：先传 <!DOCTYPE html>...<style>...</style>，再传 <script>...</script>，最后传 </body></html>"
        )

    startup_issues = _detect_startup_order_issues(clean_code)
    if startup_issues:
        return (
            "写入失败：生成的 HTML 可能黑屏，必须先修复启动顺序问题后重新调用 write_game_code：\n"
            + "\n".join(f"- {issue}" for issue in startup_issues)
        )
    _set_current_code(clean_code)
    suffix = f"\n原因：{reason}" if reason else ""
    return f"已写入右侧编辑器 game.html，共 {lines} 行。{suffix}"




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

SYSTEM_PROMPT = """你是一个专业的 H5 页面游戏设计 AI 助手，帮用户设计可在手机浏览器中运行的 H5 小游戏。

## 【强制工作流 - 必须按顺序执行】

### 新建游戏时（用户首次描述游戏）：
1. **必须调用 `search_assets("图片 音频 素材")`** → 搜索可用素材，有素材就用，没有就用 Canvas 绘图
2. **必须调用 `load_skill("base")`** → 加载基础游戏模板
3. **必须调用 `load_skill("startup")`** → 加载启动顺序规则（防黑屏）
4. **必须调用 `load_skill("quality")`** → 加载代码质量规则集
5. **必须调用 `load_skill("bugfix")`** → 加载防 bug 规则
6. 根据游戏类型按需加载其他技能（如 `load_skill("physics")` 获取碰撞检测）
7. 综合以上信息生成完整 HTML，然后：
    - **如果代码较短（<500行）**：调用 `write_game_code(code, reason)` 一次性写入
    - **如果代码较长（≥500行）或生成时被截断**：使用分块写入机制：
      1. `start_code_write(reason)` - 初始化缓冲区
      2. `append_code_chunk(chunk)` - 多次调用，每次传入一部分代码
      3. `finish_code_write()` - 完成写入并检查
    - 禁止用空 `code` 或 `{}` 调用工具
    - 如果 `write_game_code` 返回"代码似乎被截断"，立即改用分块写入
8. 最终回复只总结游戏玩法、操作方式和完成内容，**不要在聊天中输出完整 HTML 代码块**

### 修改/修 bug 时：
1. 调用 `search_code("关键字")` → 搜索定位，返回匹配行及上下文，无需再调用 view_code
2. 如需查看更多上下文，调用 `view_code(start_line, end_line)`（每次最多100行）
3. 调用 `str_replace_code(old_str, new_str)` 替换，支持空白归一化匹配，无需缩进完全一致
4. **绝不重新输出全部代码**

### 加新功能时：
1. 调用 `search_code("插入点关键字")` → 定位插入位置
2. 调用 `str_replace_code(old_str, old_str + new_str)` → 在目标内容后追加新代码

---

## 【代码质量 - 通过技能加载详细规则】

生成代码前**必须**调用以下技能获取完整规则（不要凭记忆写，规则很多）：
- `load_skill("startup")` → 启动顺序与黑屏防护（**最常出 bug 的地方**）
- `load_skill("quality")` → 代码质量完整规则集（状态机/deltaTime/数值安全/数组/触摸/绘制）
- `load_skill("bugfix")` → 常见 Bug 预防（ellipse半径/NaN/splice/图片加载）

**核心禁令（即使不加载技能也必须遵守）：**
- ❌ 所有 let/const 全局变量必须在 resize()/init()/resetGame() 调用之前声明
- ❌ resize 中必须用 ctx.setTransform(dpr,0,0,dpr,0,0)，禁止 ctx.scale
- ❌ 所有移动必须用 deltaTime：obj.x += speed * dt，禁止固定像素
- ❌ ctx.arc/ellipse 半径必须 Math.max(1, value)

---

## 【素材使用规则】
- 生成游戏前**必须先调用 `search_assets("图片 音频 素材")`** 搜索可用素材
- 有素材 → 用 `loadImages()` 预加载，用 `drawObj()` 绘制，绘制失败自动降级为图形
- 无素材 → 用 Canvas 图形代替（矩形/圆形/三角形），用颜色区分不同对象

---

## 【禁止事项】
- ❌ 新建游戏时禁止把完整 HTML 作为聊天正文输出，必须用 `write_game_code` 写入右侧编辑器
- ❌ 修改代码时禁止重新输出全部代码，必须用 view_code 查看后配合 str_replace_code 工具修改

---

## 【工具调用行为规则 - 必须严格遵守】
- ✅ 修改代码时必须一次性完成所有步骤（搜索→查看→替换），不要中途停下来回复用户等"继续"
- ✅ 如果需要多处修改，在一轮对话中连续调用工具完成全部修改，最后再统一回复
- ✅ 只有全部修改完成后，才输出文字总结修改内容
- ❌ 禁止调用一两个工具后就停下来告诉用户"我已经找到问题了"或"接下来我会..."——直接做完"""

# ============ Agent 类 ============

ALL_TOOLS = [
    search_assets,
    str_replace_code,
    write_game_code,
    start_code_write,
    append_code_chunk,
    finish_code_write,
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
        if provider == "openai" and ("api.deepseek.com" in base_url_hint or model_hint.startswith("deepseek")):
            provider = "deepseek"
        elif provider == "openai" and ("qwen" in model_hint or "autodl" in base_url_hint):
            provider = "qwen"

        if provider == "deepseek":
            api_key = settings.deepseek_api_key or settings.openai_api_key
            base_url = settings.deepseek_base_url or settings.openai_base_url
            model_name = settings.deepseek_model or settings.openai_model
            model = init_chat_model(
                model=model_name,
                model_provider="openai",
                api_key=api_key,
                base_url=base_url,
                temperature=0.6,
                max_tokens=16000,
                top_p=0.95,
                presence_penalty=0.0,
                frequency_penalty=0.0,
                timeout=120,
                max_retries=2,
                extra_body={"thinking": {"type": "disabled"}},
            )
        elif provider == "qwen":
            api_key = settings.qwen_api_key or settings.openai_api_key
            base_url = settings.qwen_base_url or settings.openai_base_url
            model_name = settings.qwen_model or settings.openai_model
            model = init_chat_model(
                model=model_name,
                model_provider="openai",
                api_key=api_key,
                base_url=base_url,
                temperature=0.6,
                max_tokens=16000,
                top_p=0.95,
                presence_penalty=0.0,
                frequency_penalty=0.0,
                timeout=120,
                max_retries=2,
            )
        else:
            model = init_chat_model(
                model=settings.openai_model,
                model_provider="openai",
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
                temperature=0.6,
                max_tokens=16000,
                top_p=0.95,
                presence_penalty=0.0,
                frequency_penalty=0.0,
                timeout=120,
                max_retries=2,
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
        self._provider = provider

    def _detect_provider(self) -> str:
        """返回当前使用的 LLM provider"""
        return self._provider

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

            # 统一使用 DeepSeek 作为总结模型
            provider = self._detect_provider()
            summarization_model = init_chat_model(
                model=settings.deepseek_model or "deepseek-v4-flash",
                model_provider="openai",
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url or "https://api.deepseek.com",
                temperature=0.3,
                max_tokens=2000,
            )

            self.agent = create_agent(
                self.model,
                ALL_TOOLS,
                system_prompt=SYSTEM_PROMPT,
                middleware=[
                    track_context_usage,
                    SkillMiddleware(),
                    ContextEditingMiddleware(
                        edits=[
                            ClearToolUsesEdit(
                                trigger=40000,
                                keep=5,
                                placeholder="[已清理]",
                            ),
                        ],
                    ),
                    SummarizationMiddleware(
                        model=summarization_model,
                        trigger=("tokens", SUMMARIZATION_TRIGGER_TOKENS_DEEPSEEK if provider == "deepseek" else SUMMARIZATION_TRIGGER_TOKENS_DEFAULT),
                        keep=("messages", SUMMARIZATION_KEEP_MESSAGES),
                        trim_tokens_to_summarize=32000,
                    ),
                ],
                checkpointer=self.checkpointer,
            )


    async def chat(self, session_id: str, user_message, current_code: str = "") -> dict:
        """非流式对话"""
        token = _begin_code_session(session_id, current_code)
        await self._ensure_agent()

        try:
            config = {"configurable": {"thread_id": session_id}, "recursion_limit": 500}
            result = await self.agent.ainvoke(
                {"messages": [{"role": "user", "content": user_message}]},
                config=config,
            )

            reply = result["messages"][-1].content
            edited_code = _get_current_code()
            code = edited_code if edited_code != current_code else self._extract_code(reply)

            action = "generate" if code and not current_code else "edit" if code else "chat"
            return {"reply": reply, "code": code, "action": action}
        finally:
            _end_code_session(token)

    async def chat_stream(self, session_id: str, user_message, current_code: str = ""):
        """流式对话 - 逐 token 返回"""
        token = _begin_code_session(session_id, current_code)
        await self._ensure_agent()

        config = {"configurable": {"thread_id": session_id}, "recursion_limit": 500}
        full_reply = ""

        last_code_sent = current_code
        last_context_usage_key = None

        try:
            async for chunk in self.agent.astream(
                {"messages": [{"role": "user", "content": user_message}]},
                config=config,
                stream_mode=["messages", "updates"],
                version="v2",
            ):
                context_usage = _get_context_usage(session_id)
                context_usage_key = tuple(context_usage.items())
                if context_usage_key != last_context_usage_key:
                    last_context_usage_key = context_usage_key
                    yield {"type": "context_usage", **context_usage}

                # 处理不同类型的 chunk
                if chunk["type"] == "messages":
                    # 流式 token
                    msg = chunk["data"][0]

                    if not isinstance(msg, AIMessageChunk):
                        continue

                    # 处理文本内容
                    if msg.content:
                        # 跳过开头的纯空白 token（reasoning 后的换行符）
                        if not full_reply and not msg.content.strip():
                            continue

                        full_reply += msg.content
                        yield {"type": "token", "content": msg.content}

                elif chunk["type"] == "updates":
                    # 完整的节点更新（工具调用和工具结果）
                    for update in chunk["data"].values():
                        if not isinstance(update, dict):
                            continue
                        if "messages" in update:
                            for msg in update["messages"]:
                                # 工具调用（在 model 节点）
                                if hasattr(msg, 'tool_calls') and msg.tool_calls:
                                    for tc in msg.tool_calls:
                                        tool_name = tc.get('name') if isinstance(tc, dict) else getattr(tc, 'name', None)
                                        tool_args = tc.get('args') if isinstance(tc, dict) else getattr(tc, 'args', {})
                                        tool_id = tc.get('id') if isinstance(tc, dict) else getattr(tc, 'id', None)
                                        if tool_name:
                                            yield {
                                                "type": "tool_call",
                                                "id": tool_id,
                                                "tool": tool_name,
                                                "args": str(tool_args)[:100] + "..." if len(str(tool_args)) > 100 else str(tool_args)
                                            }

                                # 工具结果（在 tools 节点）
                                if type(msg).__name__ == "ToolMessage":
                                    tool_name = getattr(msg, 'name', 'unknown')
                                    tool_call_id = getattr(msg, 'tool_call_id', None)
                                    tool_content = getattr(msg, 'content', '')
                                    yield {
                                        "type": "tool_result",
                                        "id": tool_call_id,
                                        "tool": tool_name,
                                        "result": tool_content[:200] + "..." if len(str(tool_content)) > 200 else str(tool_content)
                                    }

                                    edited_code = _get_current_code()
                                    if edited_code != last_code_sent:
                                        last_code_sent = edited_code
                                        yield {
                                            "type": "code_update",
                                            "code": edited_code,
                                            "source": tool_name,
                                        }

            # 流结束：优先使用工具写入/编辑后的代码；仅在未使用工具时保留文本提取兜底
            edited_code = _get_current_code()
            code = edited_code if edited_code != current_code else self._extract_code(full_reply)

            action = "generate" if code and not current_code else "edit" if code else "chat"
            yield {"type": "done", "code": code, "action": action}

        except Exception as e:
            yield {"type": "error", "content": str(e)}
        finally:
            _end_code_session(token)

    @staticmethod
    def _extract_code(text: str) -> str | None:
        """从回复中提取完整 HTML 代码"""
        matches = re.findall(r"```html\s*\n(.*?)```", text, re.DOTALL)
        if matches:
            code = max(matches, key=len).strip()
            if "<html" in code or "<!DOCTYPE" in code.upper() or "<canvas" in code:
                return code
        matches = re.findall(r"```\s*\n(.*?)```", text, re.DOTALL)
        for m in matches:
            code = m.strip()
            if ("<html" in code or "<!DOCTYPE" in code.upper()) and "<script" in code:
                return code
        return None

    async def clear_session(self, session_id: str):
        try:
            await self._ensure_agent()
            if self.checkpointer:
                await self.checkpointer.adelete_thread(session_id)
        except Exception:
            pass
        # 清理上下文使用率缓存，让前端圆环归零
        _context_usage_by_session.pop(session_id, None)
        _code_by_session.pop(session_id, None)
        _code_session_last_access.pop(session_id, None)
        _code_write_buffer.pop(session_id, None)
