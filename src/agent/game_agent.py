"""AI H5游戏设计智能体 - 核心 Agent 模块"""

import re

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from src.config import settings
from src.knowledge.knowledge_base import KnowledgeBase

SYSTEM_PROMPT = """你是一个专业的 H5 页面游戏设计 AI 助手。你专门帮用户设计和生成可以在手机浏览器中直接运行的 H5 小游戏。

## 你的职责：
1. **理解用户需求**：通过对话了解用户想要什么类型的 H5 游戏
2. **生成游戏代码**：生成完整的、可直接运行的单文件 HTML5 游戏页面
3. **支持编辑修改**：帮助用户增加、修改或删除游戏功能

## 技术要求（非常重要）：
- 生成的代码必须是 **完整的单文件 HTML**，包含所有 HTML + CSS + JavaScript
- 使用 **HTML5 Canvas** 或 **纯 CSS/DOM** 实现游戏画面，不依赖任何外部游戏框架
- 必须做好 **移动端适配**：使用 viewport meta、触摸事件(touchstart/touchmove/touchend)、响应式布局
- 画布大小自适应屏幕：`canvas.width = window.innerWidth; canvas.height = window.innerHeight;`
- 同时支持 **触摸操作 + 键盘/鼠标操作**，让 PC 和手机都能玩
- 用 `requestAnimationFrame` 做游戏循环
- 所有图形用 Canvas 2D API 绘制（fillRect, arc, drawImage 等），不需要外部图片
- 代码中加入 **中文注释**
- 每次生成或修改代码时，输出 **完整代码**，不要用省略号

## HTML 模板结构：
```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <title>游戏名称</title>
    <style>
        * {{ margin: 0; padding: 0; }}
        body {{ overflow: hidden; background: #000; touch-action: none; }}
        canvas {{ display: block; }}
    </style>
</head>
<body>
    <canvas id="gameCanvas"></canvas>
    <script>
        // 游戏代码
    </script>
</body>
</html>
```

## 常见 H5 游戏类型参考：
- 跳跃类（Flappy Bird 式）、跑酷类、消除类（2048/三消）
- 射击类、弹球类、接东西类、打地鼠类
- 答题类、抽奖转盘、刮刮卡
- 拼图类、记忆翻牌、贪吃蛇、俄罗斯方块

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
            assets_text = "\n".join(
                f"- [{a.get('asset_type', '未知')}] {a.get('file_name', '未知')} "
                f"(路径: /assets/{a.get('asset_type', '')}/{a.get('asset_id', '')}{a.get('extension', '')})"
                for a in assets
            )
        else:
            assets_text = "暂无上传素材，请使用 Canvas 2D API 绘制所有图形。"

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

    async def chat(
        self,
        session_id: str,
        user_message: str,
        current_code: str = "",
    ) -> dict:
        """与用户对话，理解需求并生成/修改游戏代码

        Returns:
            {
                "reply": str,       # AI 回复文本
                "code": str | None, # 生成的代码（如果有）
                "action": str       # "chat" | "generate" | "modify"
            }
        """
        history = self._get_session(session_id)

        # 搜索知识库获取上下文
        assets_text, skills_text = self._search_context(user_message)

        # 构建系统提示
        system_msg = SYSTEM_PROMPT.format(
            available_assets=assets_text,
            relevant_skills=skills_text,
            current_code=current_code[:3000] if current_code else "暂无项目代码",
        )

        # 构建消息列表
        messages = [SystemMessage(content=system_msg)]
        # 添加历史消息（最近 10 轮）
        for msg in history[-20:]:
            if msg["role"] == "user":
                messages.append(HumanMessage(content=msg["content"]))
            else:
                messages.append(AIMessage(content=msg["content"]))
        messages.append(HumanMessage(content=user_message))

        # 调用 LLM
        response = await self.llm.ainvoke(messages)
        reply_text = response.content

        # 记录到历史
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": reply_text})

        # 解析回复，提取代码
        code = self._extract_code(reply_text)
        action = "generate" if code and not current_code else "modify" if code else "chat"

        return {
            "reply": reply_text,
            "code": code,
            "action": action,
        }

    @staticmethod
    def _extract_code(text: str) -> str | None:
        """从 AI 回复中提取 HTML 游戏代码"""
        # 1. 匹配 ```html ... ```
        matches = re.findall(r"```html\s*\n(.*?)```", text, re.DOTALL)
        if matches:
            # 取最长的那个（通常是完整代码）
            code = max(matches, key=len).strip()
            if "<html" in code or "<!DOCTYPE" in code.upper() or "<canvas" in code:
                return code

        # 2. 匹配 ``` ... ```（无语言标记）
        matches = re.findall(r"```\s*\n(.*?)```", text, re.DOTALL)
        for match in matches:
            code = match.strip()
            if ("<html" in code or "<!DOCTYPE" in code.upper()) and "<script" in code:
                return code

        return None

    def clear_session(self, session_id: str):
        """清除会话历史"""
        if session_id in self.sessions:
            del self.sessions[session_id]

    def get_session_history(self, session_id: str) -> list:
        """获取会话历史"""
        return self._get_session(session_id)
