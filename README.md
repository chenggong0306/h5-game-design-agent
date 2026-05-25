# 🎮 H5 游戏设计工坊

> 基于 AI 智能体 + 知识库的 H5 页面游戏生成器
> 通过自然语言对话，引导用户设计并生成可在手机浏览器中直接运行的 H5 小游戏

![Python](https://img.shields.io/badge/Python-3.14-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.136-green)
![LangChain](https://img.shields.io/badge/LangChain-1.3-orange)
![LangGraph](https://img.shields.io/badge/LangGraph-1.2-red)
![License](https://img.shields.io/badge/License-MIT-yellow)

## ✨ 核心功能

| 功能 | 说明 |
|------|------|
| 💬 **AI 对话设计** | 自然语言描述需求 → AI 自动生成完整 H5 游戏代码 |
| ⚡ **流式输出** | SSE 逐字输出，打字机效果实时显示 AI 回复 |
| 📝 **代码编辑器** | Monaco Editor（VS Code 同款），语法高亮 + 自动补全 |
| 🎮 **实时预览** | iframe 沙盒预览，代码改完即时运行 |
| 🎨 **素材管理** | 上传图片/音频素材，AI 自动在生成的代码中引用 |
| 📚 **知识库** | ChromaDB 向量检索，内置 H5 游戏开发技能文档 |
| 💾 **项目管理** | 保存 / 加载 / 删除游戏项目 |
| 🧠 **上下文压缩** | 对话历史自动总结，长对话不丢失上下文 |

## 🏗️ 架构

```
用户对话 ──→ FastAPI ──→ LangGraph Agent ──→ DeepSeek / Qwen / OpenAI
                │              │
                │         Middleware 层
                │          ├── track_context_usage（上下文监控）
                │          ├── ContextEditingMiddleware（清理旧工具结果）
                │          └── SummarizationMiddleware（历史自动总结）
                │
                │        ChromaDB 知识库
                │         ├── 游戏素材（图片/音频）
                │         └── H5 开发技能文档
                │
           SSE 流式输出 ──→ 前端实时渲染
                              ├── 对话面板（打字机效果）
                              ├── 上下文使用率圆环
                              ├── 工具调用卡片
                              ├── Monaco 代码编辑器
                              └── iframe 游戏预览
```

## 🤖 Agent 工具

| 工具 | 说明 |
|------|------|
| `search_assets` | 搜索知识库中的游戏素材 |
| `search_knowledge` | 搜索 H5 游戏开发知识库 |
| `write_game_code` | 一次性写入完整游戏代码（<500行） |
| `start_code_write` | 分块写入：初始化缓冲区（长代码用） |
| `append_code_chunk` | 分块写入：追加代码块 |
| `finish_code_write` | 分块写入：完成并写入编辑器 |
| `view_code` | 查看代码（每次最多100行） |
| `str_replace_code` | 替换代码片段（支持空白归一化匹配） |

## 🚀 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/chenggong0306/h5-game-design-agent.git
cd h5-game-design-agent
```

### 2. 安装依赖

```bash
# 需要先安装 uv: https://docs.astral.sh/uv/
uv sync
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env` 文件，填入你的 API Key：

```env
# DeepSeek（推荐，便宜好用）
OPENAI_API_KEY=sk-your-deepseek-key
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-v4-flash

# 或 OpenAI
# OPENAI_API_KEY=sk-your-openai-key
# OPENAI_BASE_URL=https://api.openai.com/v1
# OPENAI_MODEL=gpt-4o

# 或本地 Ollama（免费）
# OPENAI_API_KEY=ollama
# OPENAI_BASE_URL=http://localhost:11434/v1
# OPENAI_MODEL=llama3
```

### 4. 启动

```bash
uv run python main.py
```

打开浏览器访问 **http://localhost:8000**

## 📖 使用指南

### 对话生成游戏

在左侧对话框输入你的需求，AI 会生成完整的 H5 游戏代码：

```
👤 做一个贪吃蛇游戏
👤 做一个 Flappy Bird
👤 做一个 2048 数字合并游戏
👤 做一个接水果的小游戏
```

生成的代码会自动填入编辑器并在右侧预览运行。

### 编辑修改

生成后可以继续对话修改：

```
👤 把蛇的速度加快
👤 添加一个最高分记录
👤 把背景改成深蓝色
👤 加一个开始界面
```

### 上传素材

1. 点击顶部 **🎨 素材** 按钮
2. 拖入或选择图片/音频文件
3. AI 会自动在生成的代码中使用 `new Image()` 加载你的素材

```
素材引用流程：
上传 player.png → 存储为 /assets/image/{uuid}.png
                → AI 生成代码时自动引用:
                   const img = new Image();
                   img.src = '/assets/image/{uuid}.png';
                   ctx.drawImage(img, x, y, w, h);
```

## 📁 项目结构

```
h5-game-design-agent/
├── main.py                          # 启动入口
├── pyproject.toml                   # 项目配置 & 依赖
├── .env.example                     # 环境变量模板
│
├── src/
│   ├── main.py                      # FastAPI 应用
│   ├── config.py                    # 全局配置
│   │
│   ├── agent/
│   │   └── game_agent.py            # AI 智能体（对话 + 流式输出 + 代码提取）
│   │
│   ├── knowledge/
│   │   ├── knowledge_base.py        # ChromaDB 知识库（素材/技能/项目 CRUD）
│   │   └── phaser_skills.py         # 7 个 H5 游戏开发技能文档
│   │
│   ├── api/
│   │   └── routes.py                # REST API + SSE 流式端点
│   │
│   ├── templates/
│   │   ├── index.html               # 主页（三栏布局）
│   │   └── preview.html             # 游戏预览页
│   │
│   └── static/
│       ├── js/app.js                # 前端逻辑（流式渲染 + 编辑器 + 预览）
│       └── css/style.css            # 暗色主题 UI
│
└── data/
    ├── assets/                      # 用户上传的游戏素材
    ├── chroma_db/                   # 向量数据库（运行时生成）
    ├── skills/                      # 扩展技能文档
    └── templates/                   # 扩展模板
```


## 🔌 API 接口

### 对话

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/api/chat` | 非流式对话（兼容） |
| POST | `/api/chat/stream` | **流式对话（SSE）** |
| DELETE | `/api/chat/{session_id}` | 清除对话历史 |

**流式对话 SSE 事件格式：**

```text
data: {"type":"session","session_id":"uuid"}     ← 会话ID
data: {"type":"token","content":"你"}             ← 逐字输出
data: {"type":"token","content":"好"}
data: {"type":"done","code":"...","action":"generate"}  ← 完成+代码
data: [DONE]                                      ← 结束
```

### 素材

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/api/assets/upload` | 上传素材（multipart） |
| GET | `/api/assets` | 列出所有素材 |
| GET | `/api/assets/search?q=xxx` | 搜索素材 |
| DELETE | `/api/assets/{id}` | 删除素材 |
| GET | `/assets/{type}/{file}` | 访问素材文件（游戏代码引用） |

### 项目

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/api/projects` | 保存项目 |
| GET | `/api/projects` | 列出所有项目 |
| GET | `/api/projects/{id}` | 获取项目详情 |
| DELETE | `/api/projects/{id}` | 删除项目 |

### 知识库

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET | `/api/knowledge/stats` | 知识库统计 |
| GET | `/api/knowledge/search?q=xxx` | 搜索知识库 |

## 🛠️ 技术栈

| 层 | 技术 | 用途 |
| -- | ---- | ---- |
| 后端 | **FastAPI** + uvicorn | REST API + SSE 流式 |
| 智能体 | **LangChain 1.3** + **LangGraph 1.2** | Agent 框架 + 持久化 checkpoint |
| 上下文管理 | **SummarizationMiddleware** + **ContextEditingMiddleware** | 自动总结历史 + 清理旧工具结果 |
| 知识库 | **ChromaDB** | 向量检索（素材/技能） |
| LLM | **DeepSeek** / **Qwen** / OpenAI | 代码生成（OpenAI 兼容接口） |
| 前端 | **Monaco Editor** | 代码编辑（VS Code 同款） |
| 预览 | **iframe sandbox** | 安全的游戏实时预览 |
| 包管理 | **uv** | Python 依赖管理 |

## 📝 内置知识库

启动时自动加载 7 个 H5 游戏开发技能文档：

1. **Canvas 游戏基础结构** — 游戏循环、画布自适应
2. **触摸与键盘输入** — 移动端触摸 + PC 键盘兼容
3. **Canvas 绘图技巧** — 矩形、圆形、渐变、文字
4. **碰撞检测** — AABB 矩形碰撞、圆形碰撞
5. **游戏开始/结束界面** — 状态管理、UI 绘制
6. **游戏类型参考** — Flappy/2048/贪吃蛇/射击等
7. **移动端适配要点** — viewport、touch-action、安全区域

## 🤝 贡献

欢迎提 Issue 和 Pull Request！

## 📄 License

MIT