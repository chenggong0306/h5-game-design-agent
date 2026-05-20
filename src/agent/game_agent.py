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
def list_all_assets() -> str:
    """列出知识库中所有可用的游戏素材（图片、音频等），生成游戏前必须调用此工具。"""
    if not _kb:
        return "知识库未初始化"
    results = _kb.list_assets()
    if not results:
        return "【无素材】知识库中暂无上传的素材，请用 Canvas 2D API 绘制所有图形。"
    lines = ["【可用素材列表】以下素材可直接在代码中引用："]
    for a in results:
        atype = a.get("asset_type", "image")
        fname = a.get("file_name", "未知")
        aid = a.get("asset_id", "")
        ext = a.get("extension", "")
        url = f"/assets/{atype}/{aid}{ext}"
        lines.append(f"  - [{atype}] {fname} → src: \"{url}\"")
    lines.append("\n⚠️ 生成代码时必须通过 loadImages() 预加载后使用这些素材！")
    return "\n".join(lines)



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

## 【强制工作流 - 必须按顺序执行】

### 新建游戏时（用户首次描述游戏）：
1. **必须先调用 `list_all_assets()`** → 查看所有可用素材，有素材就用，没有就用 Canvas 绘图
2. **必须调用 `search_knowledge("游戏类型 模板")`** → 获取完整代码模板和防 bug 规则
3. **必须调用 `search_knowledge("bug 预防 碰撞 边界")`** → 获取防 bug 清单
4. 综合以上信息，生成完整 HTML 代码（用 ```html 包裹）

### 修改/修 bug 时：
1. 调用 `search_code("关键字")` → 精确定位问题代码行号
2. 调用 `str_replace_code(old, new)` → 精确替换，**绝不重新输出全部代码**

### 加新功能时：
1. 调用 `search_code("插入点关键字")` → 找到插入位置行号
2. 调用 `insert_code(after_line, new_str)` → 插入新代码

---

## 【代码质量硬性规则 - 违反任何一条都会出 bug】

### 1. 状态机（必须实现）
```javascript
let state = 'start'; // 'start' | 'playing' | 'over'
// 点击事件：
// state==='start' → resetGame() → state='playing'
// state==='over'  → resetGame() → state='playing'
// update/draw 只在 state==='playing' 时执行
```

### 2. deltaTime 帧率无关移动（必须用）
```javascript
let lastTime = 0;
function loop(ts) {
  const dt = Math.min((ts - lastTime) / 1000, 0.05); // 最大0.05秒防卡顿
  lastTime = ts;
  if (state === 'playing') update(dt);
  draw();
  requestAnimationFrame(loop);
}
// 移动写法：obj.x += speed * dt  （speed单位是像素/秒，如 200）
// 禁止：obj.x += 5  （固定像素，帧率不同速度不同）
```

### 3. 数值安全保护（必须全部加）
```javascript
// ellipse/arc 半径必须保护
ctx.arc(x, y, Math.max(1, r), 0, Math.PI*2);
ctx.ellipse(x, y, Math.max(1, rx), Math.max(1, ry), 0, 0, Math.PI*2);
// 除法前检查分母
const v = denom !== 0 ? num / denom : 0;
// 随机数范围
const x = Math.random() * (max - min) + min; // 不要 Math.random()*max+offset 可能越界
```

### 4. 数组清理（防内存泄漏）
```javascript
// ✅ 用 filter 清理越界对象
bullets = bullets.filter(b => b.y > -50 && b.y < canvas.height + 50);
enemies = enemies.filter(e => e.x > -100 && e.x < canvas.width + 100);
// ❌ 禁止在 for 循环中直接 splice（会跳过元素）
```

### 5. 触摸坐标转换（必须用 getBoundingClientRect）
```javascript
function getPos(clientX, clientY) {
  const r = canvas.getBoundingClientRect();
  return { x: (clientX-r.left)*(canvas.width/r.width), y: (clientY-r.top)*(canvas.height/r.height) };
}
canvas.addEventListener('touchstart', e => { e.preventDefault(); const p=getPos(e.touches[0].clientX,e.touches[0].clientY); /* 使用p.x,p.y */ }, {passive:false});
```

### 6. 图片素材加载（有素材时必须用预加载模板）
```javascript
const imgs = {};
function loadImages(map, cb) {
  const keys = Object.keys(map);
  if (!keys.length) { cb(); return; }
  let n = 0;
  keys.forEach(k => {
    const img = new Image();
    img.onload = img.onerror = () => { imgs[k]=img; if(++n===keys.length) cb(); };
    img.src = map[k];
  });
}
// 绘制时降级保护：
function drawObj(img, x, y, w, h) {
  if (img && img.complete && img.naturalWidth > 0) ctx.drawImage(img, x, y, w, h);
  else { ctx.fillStyle='#88f'; ctx.fillRect(x, y, w, h); } // 降级图形
}
```

### 7. 平台类游戏必须验证（跑酷/跳跃）
- 起始平台宽度 >= 角色宽度 × 5
- 相邻平台水平间距 <= 角色最大跳跃距离
- 相邻平台高度差 <= 角色单次跳跃高度
- 首个浮空平台高度接近地面（差距 <= 80px）

### 8. Canvas 绘制顺序（每帧必须）
```javascript
ctx.clearRect(0, 0, canvas.width, canvas.height); // 1. 清空
// 2. 画背景
// 3. 画游戏对象（从远到近）
// 4. 画 UI（分数、血条等，最后画，不被遮挡）
```


### 9. 启动与初始化顺序（必须严格遵守）
```javascript
// 推荐顺序：
// 1) 先声明所有全局状态（player、enemies、bullets、state 等）
// 2) 再定义依赖这些状态的函数（resize / update / draw / input handlers）
// 3) 再初始化状态对象
// 4) 再调用 resize() / init() / resetGame()
// 5) 最后注册事件监听和开启 gameLoop()
//
// 禁止：在 player / state / canvas 还未初始化时就调用 resize()、update()、draw() 或事件回调
// 禁止：使用 let/const 声明的变量在声明前被函数访问（避免 Temporal Dead Zone）
// 禁止：把 window.addEventListener('resize', resize) 放在 player 初始化之前
//
// 更安全的写法：
// let player = null;
// function resize() { if (!player) return; /* ... */ }
// player = createPlayer();
// resize();
// window.addEventListener('resize', resize);
```

### 10. 黑屏防护清单（生成前自检）
- 所有全局状态必须先初始化，再进入首帧绘制
- 所有 resize 逻辑必须空值保护，不能直接读未初始化对象
- 所有 requestAnimationFrame / setInterval 启动必须放在 init 完成之后
- 如果报错涉及 `Cannot access 'xxx' before initialization`，优先检查声明顺序，而不是先改样式或素材

---

## 【素材使用规则】
- 生成游戏前**必须先调用 `list_all_assets()`**
- 有素材 → 用 `loadImages()` 预加载，用 `drawObj()` 绘制，绘制失败自动降级为图形
- 无素材 → 用 Canvas 图形代替（矩形/圆形/三角形），用颜色区分不同对象

---

## 【禁止事项】
- ❌ 修改代码时禁止重新输出全部代码，必须用 str_replace_code/insert_code 工具
- ❌ 禁止直接 `ctx.arc(x,y,r,...)` 而不保护 r 值
- ❌ 禁止 `arr.splice()` 在正向 for 循环中（用 filter 代替）
- ❌ 禁止忽略 deltaTime 直接写固定像素移动
- ❌ 禁止图片加载完成前启动游戏循环"""


# ============ Agent 类 ============

ALL_TOOLS = [
    list_all_assets,
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
            model = ChatOpenAI(
                model=model_name,
                api_key=api_key,
                base_url=base_url,
                temperature=0.6,
                max_tokens=8000,
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
            model = ChatOpenAI(
                model=model_name,
                api_key=api_key,
                base_url=base_url,
                temperature=0.6,
                max_tokens=8000,
                top_p=0.95,
                presence_penalty=0.0,
                frequency_penalty=0.0,
                timeout=120,
                max_retries=2,
            )
        else:
            model = ChatOpenAI(
                model=settings.openai_model,
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
                temperature=0.6,
                max_tokens=8000,
                top_p=0.95,
                presence_penalty=0.0,
                frequency_penalty=0.0,
                timeout=120,
                max_retries=2,
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
                stream_mode=["messages", "updates"],
                version="v2",
            ):
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
                        if "messages" in update:
                            for msg in update["messages"]:
                                # 工具调用（在 model 节点）
                                if hasattr(msg, 'tool_calls') and msg.tool_calls:
                                    for tc in msg.tool_calls:
                                        tool_name = tc.get('name') if isinstance(tc, dict) else getattr(tc, 'name', None)
                                        tool_args = tc.get('args') if isinstance(tc, dict) else getattr(tc, 'args', {})
                                        if tool_name:
                                            yield {
                                                "type": "tool_call",
                                                "tool": tool_name,
                                                "args": str(tool_args)[:100] + "..." if len(str(tool_args)) > 100 else str(tool_args)
                                            }

                                # 工具结果（在 tools 节点）
                                if type(msg).__name__ == "ToolMessage":
                                    tool_name = getattr(msg, 'name', 'unknown')
                                    tool_content = getattr(msg, 'content', '')
                                    yield {
                                        "type": "tool_result",
                                        "tool": tool_name,
                                        "result": tool_content[:200] + "..." if len(str(tool_content)) > 200 else str(tool_content)
                                    }

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

    def clear_session(self, _session_id: str):
        del _session_id
        # MemorySaver 不支持删除，但新 thread_id 即可
        pass
