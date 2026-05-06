"""AI H5游戏设计智能体 - 基于 LangGraph create_agent 重构"""

import re

from langchain.tools import tool
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessageChunk
from langgraph.checkpoint.memory import MemorySaver

from src.config import settings
from src.knowledge.knowledge_base import KnowledgeBase
from src.agent.code_editor import CodeEditor

# -------- 全局知识库实例（工具函数需要访问） --------
_kb: KnowledgeBase | None = None
_current_code: str = ""  # 当前编辑器中的代码（每次请求时更新）


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


@tool
def search_knowledge(query: str) -> str:
    """搜索 H5 游戏开发知识库（Canvas技巧、碰撞检测、模板等）。

    Args:
        query: 搜索关键词，如 "碰撞检测" "触摸输入" "游戏循环"
    """
    if not _kb:
        return "知识库未初始化"
    results = _kb.search_skills(query, top_k=3)
    if not results:
        return "未找到相关知识。"
    return "\n\n---\n".join(
        f"### {s.get('title', '')}\n{s.get('document', '')}" for s in results
    )


@tool
def str_replace_code(old_str: str, new_str: str) -> str:
    """精确替换当前游戏代码中的一段内容。用于修 bug、改参数、小范围修改。
    old_str 必须与代码中的内容完全一致（包括空格缩进）。

    Args:
        old_str: 要被替换的原始代码片段（必须精确匹配）
        new_str: 替换后的新代码（空字符串表示删除）
    """
    global _current_code
    result = CodeEditor.str_replace(_current_code, old_str, new_str)
    if result["success"]:
        _current_code = result["code"]
    return result["message"]


@tool
def insert_code(after_line: int, new_str: str) -> str:
    """在当前游戏代码的指定行号之后插入新代码。

    Args:
        after_line: 在此行之后插入（0表示插到最前面）
        new_str: 要插入的代码内容
    """
    global _current_code
    result = CodeEditor.insert_after(_current_code, after_line, new_str)
    if result["success"]:
        _current_code = result["code"]
    return result["message"]


@tool
def delete_code(start_line: int, end_line: int) -> str:
    """删除当前游戏代码中指定行范围的代码。

    Args:
        start_line: 起始行号（从1开始）
        end_line: 结束行号（包含此行）
    """
    global _current_code
    result = CodeEditor.delete_lines(_current_code, start_line, end_line)
    if result["success"]:
        _current_code = result["code"]
    return result["message"]


@tool
def search_code(query: str) -> str:
    """在当前游戏代码中搜索包含关键字的行，返回行号和内容。

    Args:
        query: 要搜索的关键字
    """
    result = CodeEditor.search(_current_code, query)
    if not result["matches"]:
        return f"未找到包含 '{query}' 的代码"
    lines = [f"  L{m['line']}: {m['content']}" for m in result["matches"][:15]]
    return f"找到 {len(result['matches'])} 处匹配:\n" + "\n".join(lines)


# ============ System Prompt ============

SYSTEM_PROMPT = """你是一个专业的 H5 页面游戏设计 AI 助手，帮用户设计可在手机浏览器中运行的 H5 小游戏。

## 你有以下工具可用：
- **search_assets**: 搜索用户上传的游戏素材
- **search_knowledge**: 搜索 H5 游戏开发知识（Canvas/碰撞/输入等）
- **str_replace_code**: 精确替换代码片段（修bug/改参数）
- **insert_code**: 在指定行后插入新代码
- **delete_code**: 删除指定行范围
- **search_code**: 搜索代码中的关键字

## 工作流程：
1. 用户要求新建游戏 → 先 search_knowledge 查模板 → 生成完整 HTML（```html 包裹）
2. 用户要求修改/修bug → 先 search_code 定位 → 再 str_replace_code 精确修改
3. 用户要求加功能 → search_code 找插入点 → insert_code 插入新代码
4. 用户问素材 → search_assets 查找可用素材

## 技术要求：
- 完整单文件 HTML（HTML + CSS + JS），不依赖外部框架
- HTML5 Canvas 游戏，移动端适配（viewport/touch/响应式）
- requestAnimationFrame 游戏循环
- ellipse 半径用 Math.max(1, ...) 保护，避免负数
- 中文注释
- 素材用 new Image() + img.src 加载

## 重要：修改代码时必须用工具，不要重新输出全部代码！"""


# ============ Agent 类 ============

ALL_TOOLS = [
    search_assets,
    search_knowledge,
    str_replace_code,
    insert_code,
    delete_code,
    search_code,
]


class GameDesignAgent:
    """基于 LangGraph create_agent 的游戏设计智能体"""

    def __init__(self, knowledge_base: KnowledgeBase):
        global _kb
        _kb = knowledge_base

        model = ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            temperature=0.7,
            max_tokens=8000,
            timeout=120,
            max_retries=2,
            model_kwargs={"thinking": False},  # 关闭 DeepSeek thinking mode，避免 reasoning_content 回传报错
        )

        self.checkpointer = MemorySaver()
        self.agent = create_agent(
            model,
            ALL_TOOLS,
            system_prompt=SYSTEM_PROMPT,
            checkpointer=self.checkpointer,
        )

    async def chat(self, session_id: str, user_message: str, current_code: str = "") -> dict:
        """非流式对话"""
        global _current_code
        _current_code = current_code

        config = {"configurable": {"thread_id": session_id}}
        result = await self.agent.ainvoke(
            {"messages": [{"role": "user", "content": user_message}]},
            config=config,
        )

        reply = result["messages"][-1].content
        code = self._extract_code(reply)
        # 如果工具修改了代码，用修改后的
        if not code and _current_code != current_code:
            code = _current_code

        action = "generate" if code and not current_code else "edit" if code else "chat"
        return {"reply": reply, "code": code, "action": action}

    async def chat_stream(self, session_id: str, user_message: str, current_code: str = ""):
        """流式对话 - 逐 token 返回"""
        global _current_code
        _current_code = current_code

        config = {"configurable": {"thread_id": session_id}}
        full_reply = ""

        try:
            async for chunk in self.agent.astream(
                {"messages": [{"role": "user", "content": user_message}]},
                config=config,
                stream_mode="messages",
            ):
                msg, metadata = chunk
                # 只输出 AI 的文本 token（跳过工具调用/结果）
                if isinstance(msg, AIMessageChunk) and msg.content:
                    # 跳过纯工具调用的 chunk
                    if not msg.tool_call_chunks:
                        full_reply += msg.content
                        yield {"type": "token", "content": msg.content}

            # 流结束，提取代码
            code = self._extract_code(full_reply)
            if not code and _current_code != current_code:
                code = _current_code

            action = "generate" if code and not current_code else "edit" if code else "chat"
            yield {"type": "done", "code": code, "action": action}

        except Exception as e:
            yield {"type": "error", "content": str(e)}

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

    def clear_session(self, session_id: str):
        """清除会话历史"""
        # MemorySaver 不支持删除，但新 thread_id 即可
        pass
