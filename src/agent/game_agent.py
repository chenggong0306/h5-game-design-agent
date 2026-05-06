"""AI H5游戏设计智能体 - 核心 Agent 模块"""

import re
import json

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from src.config import settings
from src.knowledge.knowledge_base import KnowledgeBase
from src.agent.code_editor import CodeEditor

SYSTEM_PROMPT = """你是一个专业的 H5 页面游戏设计 AI 助手。你专门帮用户设计和生成可以在手机浏览器中直接运行的 H5 小游戏。

## 你的职责：
1. **理解用户需求**：通过对话了解用户想要什么类型的 H5 游戏
2. **生成游戏代码**：生成完整的单文件 HTML5 游戏页面
3. **精确编辑代码**：通过编辑指令对现有代码进行增删改查

## 代码编辑工具（非常重要）：
当用户已有代码（"当前项目代码"不为空），且用户要求修改/修复/添加/删除功能时，
你**必须使用编辑指令**，而不是重新输出全部代码。

### 可用的编辑指令（用 JSON 代码块包裹）：

**1. 替换代码 str_replace（最常用）：**
```json
{{"action": "str_replace", "old_str": "要替换的原始代码", "new_str": "替换后的新代码"}}
```

**2. 在指定行后插入 insert：**
```json
{{"action": "insert", "after_line": 42, "new_str": "要插入的新代码"}}
```

**3. 删除指定行 delete：**
```json
{{"action": "delete", "start_line": 10, "end_line": 15}}
```

**4. 搜索代码 search：**
```json
{{"action": "search", "query": "要搜索的关键字"}}
```

### 编辑指令使用规则：
- 一次回复中可以包含**多个编辑指令**，按顺序执行
- `old_str` 必须与代码中的内容**完全一致**（包括空格缩进），否则替换会失败
- `old_str` 要足够长以确保唯一匹配（至少包含3行上下文）
- `new_str` 为空字符串 "" 表示删除该代码段
- 先用 search 定位问题代码，再用 str_replace 修改
- 修复 bug 时，先解释问题原因，再给出编辑指令

### 什么时候用编辑指令 vs 完整代码：
- **无现有代码** → 输出完整 HTML 代码（用 ```html 包裹）
- **有现有代码 + 小修改**（改参数/修bug/加功能） → 使用编辑指令
- **有现有代码 + 大重构**（超过50%代码变动） → 输出完整新代码

## 技术要求：
- 完整单文件 HTML，包含 HTML + CSS + JavaScript
- HTML5 Canvas 或纯 CSS/DOM，不依赖外部框架
- 移动端适配：viewport meta、触摸事件、响应式布局
- 画布自适应：`canvas.width = window.innerWidth; canvas.height = window.innerHeight;`
- 同时支持触摸 + 键盘/鼠标
- 用 `requestAnimationFrame` 游戏循环
- Canvas 2D API 绘制图形，ellipse 半径必须用 Math.max(1, ...) 保护
- 中文注释

## 素材引用：
如果有素材，用 `new Image(); img.src = '/assets/image/xxx.png';` 加载，
用 `ctx.drawImage(img, x, y, w, h)` 绘制。没有素材就用 Canvas 图形。

## 可用素材信息：
{available_assets}

## 相关知识/模板：
{relevant_skills}

## 当前项目代码（如果有）：
{current_code}
"""


class GameDesignAgent:
    """游戏设计 AI 智能体"""

    def __init__(self, knowledge_base: KnowledgeBase):
        self.kb = knowledge_base
        self.llm = ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            temperature=0.7,
            max_tokens=8000,
            timeout=120,        # 超时 120 秒（DeepSeek 可能较慢）
            max_retries=2,      # 最多重试 2 次
        )
        # 会话历史 {session_id: [messages]}
        self.sessions: dict[str, list] = {}

    def _get_session(self, session_id: str) -> list:
        """获取或创建会话"""
        if session_id not in self.sessions:
            self.sessions[session_id] = []
        return self.sessions[session_id]

    def _search_context(self, user_message: str) -> tuple[str, str]:
        """根据用户消息搜索知识库中的相关素材和技能"""
        # 搜索相关素材
        assets = self.kb.search_assets(user_message, top_k=5)
        if assets:
            lines = []
            for a in assets:
                atype = a.get('asset_type', 'image')
                fname = a.get('file_name', '未知')
                aid = a.get('asset_id', '')
                ext = a.get('extension', '')
                # 生成可直接在代码中引用的 URL
                url = f"/assets/{atype}/{aid}{ext}"
                lines.append(f"- [{atype}] {fname} → URL: `{url}`")
            assets_text = "\n".join(lines)
        else:
            assets_text = "暂无上传素材，请使用 Canvas 2D API 绘制所有图形（fillRect/arc 等）。"

        # 搜索相关技能/模板
        skills = self.kb.search_skills(user_message, top_k=3)
        if skills:
            skills_text = "\n\n---\n".join(
                f"### {s.get('title', '未知')}\n{s.get('document', '')}"
                for s in skills
            )
        else:
            skills_text = "无特定相关知识。"

        return assets_text, skills_text

    def _build_messages(self, session_id: str, user_message: str, current_code: str = "") -> list:
        """构建 LLM 消息列表（chat 和 stream 共用）"""
        history = self._get_session(session_id)
        assets_text, skills_text = self._search_context(user_message)

        system_msg = SYSTEM_PROMPT.format(
            available_assets=assets_text,
            relevant_skills=skills_text,
            current_code=current_code[:3000] if current_code else "暂无项目代码",
        )

        messages = [SystemMessage(content=system_msg)]
        for msg in history[-20:]:
            if msg["role"] == "user":
                messages.append(HumanMessage(content=msg["content"]))
            else:
                messages.append(AIMessage(content=msg["content"]))
        messages.append(HumanMessage(content=user_message))
        return messages

    async def chat(
        self,
        session_id: str,
        user_message: str,
        current_code: str = "",
    ) -> dict:
        """非流式对话（保留兼容）"""
        messages = self._build_messages(session_id, user_message, current_code)
        history = self._get_session(session_id)

        response = await self.llm.ainvoke(messages)
        reply_text = response.content

        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": reply_text})

        code = self._extract_code(reply_text)
        action = "generate" if code and not current_code else "modify" if code else "chat"

        return {"reply": reply_text, "code": code, "action": action}

    async def chat_stream(
        self,
        session_id: str,
        user_message: str,
        current_code: str = "",
    ):
        """流式对话 - 逐 token 返回

        Yields:
            dict: {"type": "token", "content": "..."} 逐字输出
                  {"type": "done", "code": "...|null", "action": "..."}  完成信号
                  {"type": "error", "content": "..."}  错误信息
        """
        messages = self._build_messages(session_id, user_message, current_code)
        history = self._get_session(session_id)
        full_reply = ""

        try:
            async for chunk in self.llm.astream(messages):
                token = chunk.content
                if token:
                    full_reply += token
                    yield {"type": "token", "content": token}

            # 流结束后，记录历史并提取代码或编辑指令
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": full_reply})

            # 优先检查完整 HTML 代码
            code = self._extract_code(full_reply)
            if code:
                action = "generate" if not current_code else "replace"
                yield {"type": "done", "code": code, "action": action}
                return

            # 其次检查编辑指令
            edits = self._extract_edits(full_reply)
            if edits and current_code:
                new_code, logs = self.apply_edits(current_code, edits)
                yield {
                    "type": "done",
                    "code": new_code,
                    "action": "edit",
                    "edits_count": len(edits),
                    "edit_logs": logs,
                }
                return

            # 纯对话（无代码、无编辑）
            yield {"type": "done", "code": None, "action": "chat"}

        except Exception as e:
            yield {"type": "error", "content": str(e)}

    @staticmethod
    def _extract_code(text: str) -> str | None:
        """从 AI 回复中提取完整的 HTML 游戏代码"""
        matches = re.findall(r"```html\s*\n(.*?)```", text, re.DOTALL)
        if matches:
            code = max(matches, key=len).strip()
            if "<html" in code or "<!DOCTYPE" in code.upper() or "<canvas" in code:
                return code

        matches = re.findall(r"```\s*\n(.*?)```", text, re.DOTALL)
        for match in matches:
            code = match.strip()
            if ("<html" in code or "<!DOCTYPE" in code.upper()) and "<script" in code:
                return code
        return None

    @staticmethod
    def _extract_edits(text: str) -> list[dict]:
        """从 AI 回复中提取编辑指令（```json 代码块中的操作）"""
        edits = []
        # 匹配 ```json ... ```
        for m in re.finditer(r"```json\s*\n(.*?)```", text, re.DOTALL):
            raw = m.group(1).strip()
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict) and "action" in obj:
                    edits.append(obj)
                elif isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, dict) and "action" in item:
                            edits.append(item)
            except json.JSONDecodeError:
                continue
        return edits

    @staticmethod
    def apply_edits(code: str, edits: list[dict]) -> tuple[str, list[str]]:
        """在代码上执行编辑指令列表

        Returns:
            (修改后的代码, 操作日志列表)
        """
        logs = []
        editor = CodeEditor()
        for i, edit in enumerate(edits, 1):
            action = edit.get("action", "")

            if action == "str_replace":
                result = editor.str_replace(
                    code,
                    edit.get("old_str", ""),
                    edit.get("new_str", ""),
                )
            elif action == "insert":
                result = editor.insert_after(
                    code,
                    edit.get("after_line", 0),
                    edit.get("new_str", ""),
                )
            elif action == "delete":
                result = editor.delete_lines(
                    code,
                    edit.get("start_line", 1),
                    edit.get("end_line", 1),
                )
            elif action == "search":
                result = editor.search(code, edit.get("query", ""))
                logs.append(f"[{i}] search: {result['message']}")
                continue
            else:
                logs.append(f"[{i}] unknown action: {action}")
                continue

            if result["success"]:
                code = result["code"]
                logs.append(f"[{i}] {action}: {result['message']}")
            else:
                logs.append(f"[{i}] {action} FAILED: {result['message']}")

        return code, logs

    def clear_session(self, session_id: str):
        """清除会话历史"""
        if session_id in self.sessions:
            del self.sessions[session_id]

    def get_session_history(self, session_id: str) -> list:
        """获取会话历史"""
        return self._get_session(session_id)
