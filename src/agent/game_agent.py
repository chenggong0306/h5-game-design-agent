"""AI H5游戏设计智能体 - 基于 LangGraph create_agent 重构"""

import asyncio
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import aiosqlite
from langchain.tools import tool
from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import before_model
from langchain.messages import RemoveMessage
from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessageChunk
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

from src.config import settings
from src.knowledge.knowledge_base import KnowledgeBase
from src.agent.code_editor import CodeEditor

# -------- 全局知识库实例（工具函数需要访问） --------
_kb: KnowledgeBase | None = None
_current_session_id: ContextVar[str] = ContextVar("current_session_id", default="")
_code_by_session: dict[str, str] = {}


def _get_current_code() -> str:
    """读取当前会话的编辑器代码，避免不同请求共享同一份全局代码。"""
    session_id = _current_session_id.get()
    if not session_id:
        return ""
    return _code_by_session.get(session_id, "")


def _set_current_code(code: str) -> None:
    """写入当前会话的编辑器代码。"""
    session_id = _current_session_id.get()
    if session_id:
        _code_by_session[session_id] = code


def _begin_code_session(session_id: str, code: str):
    """绑定当前 async 上下文的会话 ID，并初始化该会话代码。"""
    token = _current_session_id.set(session_id)
    _code_by_session[session_id] = code
    return token


def _end_code_session(token) -> None:
    """恢复 contextvar，保留会话代码供本次请求返回。"""
    _current_session_id.reset(token)


CONTEXT_WINDOW_TOKENS = 131_072
CONTEXT_COMPACT_THRESHOLD = 0.80
RECENT_MESSAGES_TO_KEEP = 12
LARGE_TEXT_CHARS = 2_000
_context_usage_by_session: dict[str, dict[str, int | bool]] = {}


def _estimate_tokens(text: str) -> int:
    """轻量估算 token 数：中文/代码场景下按字符粗略折算，避免引入额外 tokenizer 依赖。"""
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, ascii_chars // 4 + non_ascii_chars)


def _message_text_for_budget(message) -> str:
    parts = [str(getattr(message, "content", "") or "")]
    for tc in getattr(message, "tool_calls", []) or []:
        parts.append(str(tc))
    return "\n".join(parts)


def _compact_tool_calls(message):
    tool_calls = getattr(message, "tool_calls", None)
    if not tool_calls:
        return message
    compacted = []
    changed = False
    for tc in tool_calls:
        item = dict(tc)
        args = item.get("args")
        if isinstance(args, dict):
            args = dict(args)
            tool_name = item.get("name")
            if tool_name == "write_game_code" and len(str(args.get("code", ""))) > LARGE_TEXT_CHARS:
                args["code"] = "[已省略：历史 write_game_code 的完整 HTML 已写入右侧编辑器，不再注入模型上下文]"
                changed = True
            elif tool_name == "append_game_code_chunk" and len(str(args.get("chunk", ""))) > LARGE_TEXT_CHARS:
                args["chunk"] = "[已省略：历史代码分块内容不再注入模型上下文]"
                changed = True
            item["args"] = args
        compacted.append(item)
    return message.model_copy(update={"tool_calls": compacted}) if changed else message


def _compact_tool_result(message):
    name = getattr(message, "name", "")
    content = str(getattr(message, "content", "") or "")
    if name == "view_code" and len(content) > LARGE_TEXT_CHARS:
        return message.model_copy(update={"content": "[已省略：历史 view_code 大段代码结果不再注入模型上下文]"})
    return message


@before_model
def compact_messages_before_model(state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
    """上下文达到 80% 时，裁剪旧消息并脱敏历史大代码，防止 checkpoint 撑爆上下文。"""
    messages = state["messages"]
    used_tokens = sum(_estimate_tokens(_message_text_for_budget(m)) for m in messages)
    compacted = used_tokens >= int(CONTEXT_WINDOW_TOKENS * CONTEXT_COMPACT_THRESHOLD)
    session_id = ""
    try:
        session_id = runtime.config.get("configurable", {}).get("thread_id", "")
    except Exception:
        session_id = _current_session_id.get()
    if session_id:
        _context_usage_by_session[session_id] = {
            "used_tokens": used_tokens,
            "max_tokens": CONTEXT_WINDOW_TOKENS,
            "percent": round(used_tokens / CONTEXT_WINDOW_TOKENS * 100),
            "compacted": compacted,
        }
    if not compacted:
        return None

    head = messages[:1]
    recent = messages[-RECENT_MESSAGES_TO_KEEP:]
    middle = messages[1:-RECENT_MESSAGES_TO_KEEP]
    summary = (
        f"[系统上下文压缩] 已省略 {len(middle)} 条较早消息。"
        "当前右侧编辑器保存最新游戏代码；如需修改代码，请使用 search_code/view_code 查看。"
    )
    sanitized_recent = [_compact_tool_result(_compact_tool_calls(m)) for m in recent]
    return {
        "messages": [
            RemoveMessage(id=REMOVE_ALL_MESSAGES),
            *head,
            {"role": "system", "content": summary},
            *sanitized_recent,
        ]
    }


def _get_context_usage(session_id: str) -> dict[str, int | bool]:
    return _context_usage_by_session.get(session_id, {
        "used_tokens": 0,
        "max_tokens": CONTEXT_WINDOW_TOKENS,
        "percent": 0,
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
    result = CodeEditor.str_replace(_get_current_code(), old_str, new_str)
    if result["success"]:
        _set_current_code(result["code"])
    return result["message"]


@tool
def insert_code(after_line: int, new_str: str) -> str:
    """在当前游戏代码的指定行号之后插入新代码。

    Args:
        after_line: 在此行之后插入（0表示插到最前面）
        new_str: 要插入的代码内容
    """
    result = CodeEditor.insert_after(_get_current_code(), after_line, new_str)
    if result["success"]:
        _set_current_code(result["code"])
    return result["message"]


@tool
def delete_code(start_line: int, end_line: int) -> str:
    """删除当前游戏代码中指定行范围的代码。

    Args:
        start_line: 起始行号（从1开始）
        end_line: 结束行号（包含此行）
    """
    result = CodeEditor.delete_lines(_get_current_code(), start_line, end_line)
    if result["success"]:
        _set_current_code(result["code"])
    return result["message"]


def _detect_startup_order_issues(code: str) -> list[str]:
    """检测常见黑屏启动顺序问题，尤其是 let/const 变量声明前被 resize/init 调用访问。"""
    issues = []
    startup_calls = ["resize();", "init();", "resetGame();", "gameLoop();", "loop();", "requestAnimationFrame("]
    guarded_vars = ["player", "state", "canvas", "ctx"]

    for var_name in guarded_vars:
        declarations = [
            pos for pos in (
                code.find(f"let {var_name}"),
                code.find(f"const {var_name}"),
                code.find(f"var {var_name}"),
            ) if pos >= 0
        ]
        if not declarations:
            continue
        declared_at = min(declarations)
        for call in startup_calls:
            called_at = code.find(call)
            if 0 <= called_at < declared_at:
                issues.append(f"{call} 出现在 {var_name} 声明之前，可能触发 Cannot access '{var_name}' before initialization")
                break

    if "ctx.scale(" in code and "ctx.setTransform(" not in code:
        issues.append("resize 中使用 ctx.scale() 可能重复叠加缩放，请改用 ctx.setTransform(dpr,0,0,dpr,0,0)")
    return issues



@tool
def write_game_code(code: str = "", reason: str = "") -> str:
    """将完整 H5 游戏代码写入当前会话的右侧代码编辑器。新建游戏或需要整体重写时必须使用此工具。

    Args:
        code: 完整 HTML 代码，必须可直接在浏览器运行。不可为空。
        reason: 本次写入代码的原因或摘要
    """
    clean_code = code.strip()
    if not clean_code:
        return (
            "写入失败：缺少 code 参数。请重新调用 write_game_code，"
            "并在 code 参数中传入完整 HTML 字符串；不要使用空对象 {} 调用此工具。"
        )
    startup_issues = _detect_startup_order_issues(clean_code)
    if startup_issues:
        return (
            "写入失败：生成的 HTML 可能黑屏，必须先修复启动顺序问题后重新调用 write_game_code：\n"
            + "\n".join(f"- {issue}" for issue in startup_issues)
        )
    _set_current_code(clean_code)
    lines = clean_code.count("\n") + 1
    suffix = f"\n原因：{reason}" if reason else ""
    return f"已写入右侧编辑器 game.html，共 {lines} 行。{suffix}"




@tool
def view_code(start_line: int = 1, end_line: int = -1) -> str:
    """查看当前游戏代码的指定行范围，返回带行号代码。修改前应先调用此工具确认上下文。

    Args:
        start_line: 起始行号（从1开始）
        end_line: 结束行号（包含此行，-1表示到末尾）
    """
    result = CodeEditor.view_lines(_get_current_code(), start_line, end_line)
    return f"共 {result['total_lines']} 行，当前显示 {result['range'][0]}-{result['range'][1]} 行：\n{result['content']}"


@tool
def replace_code_lines(start_line: int, end_line: int, new_str: str) -> str:
    """按行号替换当前游戏代码中的一段内容。适合 str_replace_code 精确匹配失败后的局部修改。

    Args:
        start_line: 起始行号（从1开始）
        end_line: 结束行号（包含此行）
        new_str: 替换后的新代码（空字符串表示删除）
    """
    result = CodeEditor.replace_lines(_get_current_code(), start_line, end_line, new_str)
    if result["success"]:
        _set_current_code(result["code"])
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
    result = CodeEditor.search(_get_current_code(), query)
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
4. 综合以上信息生成完整 HTML，但**必须调用 `write_game_code(code, reason)` 写入右侧编辑器**。
   - `write_game_code` 的 `code` 参数必须是完整 HTML 字符串，禁止用 `{}` 或空 `code` 调用。
   - 如果工具返回“写入失败：缺少 code 参数”，必须立刻重新调用 `write_game_code` 并补齐完整 HTML。
5. 最终回复只总结游戏玩法、操作方式和完成内容，**不要在聊天中输出完整 HTML 代码块**

### 修改/修 bug 时：
1. 调用 `search_code("关键字")` → 精确定位问题代码行号
2. 调用 `view_code(start_line, end_line)` → 查看要修改位置附近的上下文
3. 优先调用 `str_replace_code(old, new)` 精确替换；如果精确匹配失败，再调用 `replace_code_lines(start_line, end_line, new_str)` 按行替换
4. **绝不重新输出全部代码**

### 加新功能时：
1. 调用 `search_code("插入点关键字")` → 找到插入位置行号
2. 调用 `view_code(start_line, end_line)` → 确认插入点上下文
3. 调用 `insert_code(after_line, new_str)` → 插入新代码

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


### 9. 启动与初始化顺序（必须严格遵守，写入前必须自检）
```javascript
// 唯一安全顺序：
// 1) 先拿 DOM：const canvas = ...; const ctx = ...;
// 2) 立刻声明所有全局状态：let player=null; let state='start'; let enemies=[]; let lastTime=0; ...
// 3) 再定义函数：resize / createPlayer / resetGame / update / draw / loop / input handlers
// 4) 再注册事件：window.addEventListener / canvas.addEventListener
// 5) 最后执行启动调用：resize(); resetGame(); requestAnimationFrame(loop)
```

**绝对禁止以下黑屏写法：**
```javascript
function resize(){ if(player){ ... } }
resize();          // ❌ 这里执行时 player 还没声明
let player = null; // ❌ let/const 声明在调用之后，会触发 Temporal Dead Zone
```

**必须使用以下安全写法：**
```javascript
let player = null;
function resize() {
  const dpr = window.devicePixelRatio || 1;
  canvas.width = innerWidth * dpr;
  canvas.height = innerHeight * dpr;
  canvas.style.width = innerWidth + 'px';
  canvas.style.height = innerHeight + 'px';
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); // 禁止 ctx.scale() 重复叠加
  if (player) { /* 更新 player 尺寸 */ }
}
window.addEventListener('resize', resize);
resize();
```

写入 `write_game_code` 前必须逐项确认：
- `resize()` / `init()` / `resetGame()` / `requestAnimationFrame()` 之前，所有被这些函数读取的 `let/const` 全局变量都已经声明。
- 禁止在 `let player`、`let state`、`let enemies` 等声明之前调用任何会读它们的函数。
- `resize` 里必须用 `ctx.setTransform(dpr,0,0,dpr,0,0)`，禁止只用 `ctx.scale(dpr,dpr)`。
- 如果无法确认启动顺序安全，先重排代码，再调用 `write_game_code`。

### 10. 黑屏防护清单（生成前自检）
- 所有全局状态必须先初始化，再进入首帧绘制
- 所有 resize 逻辑必须空值保护，不能直接读未初始化对象
- 所有 requestAnimationFrame / setInterval 启动必须放在 init 完成之后
- 如果报错涉及 `Cannot access 'xxx' before initialization`，优先检查声明顺序，而不是先改样式或素材
- 生成完整 HTML 后，调用 `write_game_code` 前必须在脑内扫描全文：任何 `resize(); init(); resetGame(); loop(); requestAnimationFrame(...)` 都不能出现在其依赖的 `let/const` 变量声明之前
- 如果 `write_game_code` 返回“可能黑屏/启动顺序问题”，必须按工具提示重排声明顺序后重新调用，不要直接解释给用户

---

## 【素材使用规则】
- 生成游戏前**必须先调用 `list_all_assets()`**
- 有素材 → 用 `loadImages()` 预加载，用 `drawObj()` 绘制，绘制失败自动降级为图形
- 无素材 → 用 Canvas 图形代替（矩形/圆形/三角形），用颜色区分不同对象

---

## 【禁止事项】
- ❌ 新建游戏时禁止把完整 HTML 作为聊天正文输出，必须用 `write_game_code` 写入右侧编辑器
- ❌ 修改代码时禁止重新输出全部代码，必须用 view_code 后配合 str_replace_code/replace_code_lines/insert_code/delete_code 工具
- ❌ 禁止直接 `ctx.arc(x,y,r,...)` 而不保护 r 值
- ❌ 禁止 `arr.splice()` 在正向 for 循环中（用 filter 代替）
- ❌ 禁止忽略 deltaTime 直接写固定像素移动
- ❌ 禁止图片加载完成前启动游戏循环
- ❌ 禁止在声明 `let player` / `let state` / `let enemies` 等全局状态之前调用 `resize()`、`init()`、`resetGame()`、`requestAnimationFrame()`
- ❌ 禁止在 resize 中只用 `ctx.scale(dpr,dpr)`，必须用 `ctx.setTransform(dpr,0,0,dpr,0,0)` 防止缩放叠加"""

# ============ Agent 类 ============

ALL_TOOLS = [
    list_all_assets,
    search_assets,
    search_knowledge,
    str_replace_code,
    write_game_code,
    insert_code,
    delete_code,
    view_code,
    replace_code_lines,
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

        checkpoint_dir = Path(settings.chroma_persist_dir).parent
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path = checkpoint_dir / "langgraph_checkpoints.sqlite"
        self.model = model
        self.checkpoint_conn: aiosqlite.Connection | None = None
        self.checkpointer: AsyncSqliteSaver | None = None
        self.agent = None
        self._agent_lock: asyncio.Lock | None = None

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
            self.agent = create_agent(
                self.model,
                ALL_TOOLS,
                system_prompt=SYSTEM_PROMPT,
                middleware=[compact_messages_before_model],
                checkpointer=self.checkpointer,
            )


    async def chat(self, session_id: str, user_message: str, current_code: str = "") -> dict:
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

    async def chat_stream(self, session_id: str, user_message: str, current_code: str = ""):
        """流式对话 - 逐 token 返回"""
        token = _begin_code_session(session_id, current_code)
        await self._ensure_agent()

        config = {"configurable": {"thread_id": session_id}, "recursion_limit": 50}
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
