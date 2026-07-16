# 🎮 H5 游戏设计工坊 — API 接口文档

> 面向 uniapp 等前端客户端开发，BASE_URL 替换为实际部署地址（本地默认 `http://localhost:8000`）。

---

## 目录

1. [对话 / 聊天](#1-对话--聊天)
2. [素材管理](#2-素材管理)
3. [项目管理](#3-项目管理)
4. [技能管理](#4-技能管理)
5. [真机预览](#5-真机预览)
6. [通用说明](#6-通用说明)

---

## 1. 对话 / 聊天

### 1.1 非流式对话

```
POST /api/chat
```

**请求体** (JSON)：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `session_id` | string | 否 | 会话 ID，传空则自动创建新的。字母数字下划线连字符，1-128 位 |
| `message` | string | 是 | 用户消息文本 |
| `current_code` | string | 否 | 当前编辑器中的代码 |
| `code_dirty` | boolean | 否 | 用户是否手动改过代码（`true` 时以客户端代码为准） |
| `images` | array | 否 | 图片列表，每项见下方 `ChatImage` |

**ChatImage**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 文件名，如 `"screenshot.png"` |
| `type` | string | MIME 类型，如 `"image/png"` |
| `size` | integer | 文件字节数 |
| `data_url` | string | base64 data URL，格式 `data:image/png;base64,...` |

限制：最多 4 张，单张 ≤ 10MB。

**响应** (JSON)：

```json
{
  "session_id": "uuid-string",
  "reply": "AI 的回复文本",
  "code": "<!DOCTYPE html>... 或 null",
  "action": "generate" | "edit" | "chat",
  "reference_summary": {
    "skills": [
      { "name": "卡牌塔防", "web_bundle": true, "source_reads": 3, "assets": 1 }
    ]
  }
}
```

- `action` = `"generate"`：新建了游戏代码
- `action` = `"edit"`：修改了已有代码
- `action` = `"chat"`：纯聊天，未改代码
- `reference_summary`：本回合参考摘要。`web_bundle` 是否加载过组合视图，`source_reads`
  源码读取/搜索次数，`assets` 素材查询次数；本回合未用任何技能工具时为 `null`

---

### 1.2 流式对话 (SSE)

```
POST /api/chat/stream
```

**请求体**：同 [1.1 非流式对话](#11-非流式对话) 的 `ChatRequest`。

**响应**：`text/event-stream`，每条 `data:` 后跟 JSON。

**SSE 事件类型**：

| type | 说明 | 额外字段 |
|------|------|----------|
| `session` | 首个事件，告知会话 ID | `session_id` |
| `token` | AI 逐字输出的文本片段 | `content` |
| `tool_call` | AI 调用工具 | `id`, `tool`, `args` |
| `tool_result` | 工具返回结果 | `id`, `tool`, `result` |
| `code_update` | 代码有更新（去抖 ~1s） | `code`, `source` |
| `context_usage` | 上下文使用率变化 | `used_tokens`, `max_tokens`, `percent`, `compacted` |
| `self_check` | 自检反馈 | `status` (`"passed"` / `"repairing"` / `"issues_remain"`), `issues` |
| `done` | 本轮对话结束 | `code` (可能为 null), `action`, `reference_summary` (同 1.1，未用技能工具时为 null) |
| `error` | 出错 | `content`, `error_code` |

**结束标记**：最后一行 `data: [DONE]`。

**示例**：

```
data: {"type":"session","session_id":"abc-123"}
data: {"type":"token","content":"好的"}
data: {"type":"token","content":"，我来"}
data: {"type":"code_update","code":"<!DOCTYPE html>...","source":"write_game"}
data: {"type":"context_usage","used_tokens":12345,"max_tokens":114000,"percent":11}
data: {"type":"done","code":null,"action":"generate"}
data: [DONE]
```

---

### 1.3 聊天历史

**列出所有会话**：

```
GET /api/chat/history
```

响应：会话摘要数组，按更新时间倒序。

```json
[
  {
    "session_id": "abc-123",
    "title": "做一个贪吃蛇",
    "message_count": 12,
    "updated_at": "2026-06-25T10:30:00+00:00"
  }
]
```

**获取某个会话的消息记录 + 最新代码**：

```
GET /api/chat/{session_id}/history
```

响应：

```json
{
  "session_id": "abc-123",
  "messages": [
    { "role": "user", "content": "做一个贪吃蛇", "ts": "..." },
    { "role": "ai", "content": "好的，我来生成...", "ts": "..." }
  ],
  "latest_code": "<!DOCTYPE html>..."
}
```

**清除会话**（同时清空对话历史 + LangGraph checkpoint + 编辑器代码 + 版本历史）：

```
DELETE /api/chat/{session_id}
```

响应：`{"ok": true}`

---

### 1.4 代码版本历史

每次代码被覆盖前，旧版本自动归档（内容相同不产生新版本），每会话最多保留 10 个最新版本。

**列出版本**（新→旧）：

```
GET /api/chat/{session_id}/versions
```

响应（无版本返回空数组）：

```json
{
  "versions": [
    { "id": "v-20260716T083000-000042", "time": "2026-07-16T08:30:00+00:00", "size": 18234, "lines": 412 }
  ]
}
```

**恢复到指定版本**：

```
POST /api/chat/{session_id}/versions/{version_id}/restore
```

响应：`200 {"code": "<恢复后的完整HTML>"}`；版本不存在（或 `version_id` 非法）→ `404`。

- 恢复前会先把当前代码归档为新版本，恢复操作本身可回退
- 服务端会话内存代码同步更新，下一轮对话基于恢复后的代码

---

## 2. 素材管理

### 2.1 单文件上传

```
POST /api/assets/upload
```

**请求**：`multipart/form-data`

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file` | file | 是 | 素材文件 |
| `asset_type` | string | 是 | 素材类型：`image` / `audio` / `spritesheet` / `tilemap` / `font` |
| `description` | string | 否 | 描述 |
| `tags` | string | 否 | 逗号分隔的标签 |

**支持的格式**：

| 类型 | 扩展名 |
|------|--------|
| image / spritesheet | `.png` `.jpg` `.jpeg` `.gif` `.webp` `.bmp` `.ico` `.avif` `.tif` `.tiff` |
| audio | `.mp3` `.wav` `.ogg` `.m4a` `.flac` `.aac` `.opus` |
| tilemap | `.json` `.tmx` |
| font | `.ttf` `.otf` `.woff` `.woff2` |

文件上限：20MB（由服务端 `max_upload_bytes` 控制）。

**响应**：

```json
{
  "asset_id": "uuid",
  "file_name": "click2.ogg",
  "asset_type": "audio",
  "extension": ".ogg",
  "file_path": "/data/assets/audio/xxx.ogg",
  "tags": "[\"功能音效\"]",
  "url": "/assets/audio/uuid.ogg"
}
```

---

### 2.2 列出素材

```
GET /api/assets
GET /api/assets?asset_type=audio
```

**响应**：

```json
[
  {
    "id": "uuid",
    "asset_id": "uuid",
    "file_name": "click2.ogg",
    "asset_type": "audio",
    "file_path": "...",
    "extension": ".ogg",
    "tags": "[\"功能音效\"]",
    "document": "[audio] click2.ogg - 极短的静音或空白声音片段... | 标签: 功能音效",
    "url": "/assets/audio/uuid.ogg"
  }
]
```

| 字段 | 说明 |
|------|------|
| `id` / `asset_id` | 用于删除的素材唯一 ID |
| `tags` | JSON 字符串数组，如 `"[\"功能音效\",\"UI\"]"` |
| `document` | 检索文本（含类型、描述、标签），前端可从中提取描述展示 |
| `url` | 游戏代码中可直接引用的路径 |

---

### 2.3 搜索素材

```
GET /api/assets/search?q=音效&asset_type=audio&top_k=5
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `q` | 是 | 搜索关键词 |
| `asset_type` | 否 | 按类型过滤 |
| `top_k` | 否 | 返回条数，默认 5 |

**响应**：同 [2.2 列出素材](#22-列出素材)，每条多一个 `score`（相似度 0-1）。

---

### 2.4 删除素材

```
DELETE /api/assets/{asset_id}
```

响应：`{"ok": true}` 或 404。

---

### 2.5 CSV 标注批量导入

```
POST /api/assets/import/batch
```

**请求**：`multipart/form-data`

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `csv_file` | file | 是 | CSV 标注文件（UTF-8 BOM / UTF-8 / GBK 均可） |
| `files` | file[] | 是 | 多个音频文件，最多 500 个，总额 ≤ 1GB |

**CSV 格式**（无表头也行，按位置读取）：

| 列 | 说明 |
|----|------|
| 第 2 列 | **文件名**（如 `click2.ogg`），用于匹配上传文件 |
| 第 4 列 | **标签**（如 `功能音效`） |
| 第 5 列 | **描述**（如 `极短的静音或空白声音片段...`） |

**响应**：

```json
{
  "ok": true,
  "success": 150,
  "total": 181,
  "failed": [],
  "not_found": ["missing_file.mp3", "..."],
  "not_found_count": 31,
  "csv_errors": []
}
```

| 字段 | 说明 |
|------|------|
| `success` | 成功入库数量 |
| `total` | 上传的文件总数 |
| `failed` | 失败列表（含错误信息） |
| `not_found` | CSV 有标注但没传对应文件的文件名（最多 50 个） |
| `not_found_count` | 实际未匹配总数 |
| `csv_errors` | CSV 解析中的问题行 |

---

### 2.6 CSV 事后补标注

> 如果先上传了文件但忘记带 CSV，或者分批上传后有遗漏，用这个接口单独上传 CSV 补全标签和描述。

```
POST /api/assets/annotate/batch
```

**请求**：`multipart/form-data`

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `csv_file` | file | 是 | CSV 标注文件 |

**响应**：

```json
{
  "ok": true,
  "updated": 120,
  "csv_entries": 500,
  "not_found": ["xxx.mp3"],
  "not_found_count": 319
}
```

---

### 2.7 素材文件访问

```
GET /api/assets/file/{asset_type}/{filename}
GET /assets/{asset_type}/{filename}
```

第二条短路径供游戏代码 `<audio src="/assets/audio/uuid.ogg">` 直接引用。

所有素材访问带 `X-Content-Type-Options: nosniff` 安全头。

---

## 3. 项目管理

### 3.1 保存项目

```
POST /api/projects
```

**请求体** (JSON)：

```json
{
  "project_id": "",
  "name": "我的贪吃蛇",
  "code": "<!DOCTYPE html>...",
  "config": { "difficulty": "hard" }
}
```

`project_id` 留空则新建，否则覆盖已有项目。

**响应**：

```json
{
  "project_id": "uuid",
  "name": "我的贪吃蛇",
  "code": "<!DOCTYPE html>...",
  "config": { "difficulty": "hard" }
}
```

---

### 3.2 列出 / 获取 / 删除

```
GET    /api/projects              # 列出所有项目
GET    /api/projects/{project_id} # 获取单个项目详情
DELETE /api/projects/{project_id} # 删除项目
```

---

## 4. 技能管理

技能是注入给 AI 的领域知识文档，让 AI 生成更专业的游戏代码。

### 4.1 列出技能

```
GET /api/skills
```

响应：

```json
[
  { "name": "Canvas基础", "description": "Canvas 游戏基础结构" },
  { "name": "genre_platformer", "description": "平台跳跃品类模板" }
]
```

---

### 4.2 获取技能完整内容

```
GET /api/skills/{skill_name}
```

响应：

```json
{
  "name": "Canvas基础",
  "description": "Canvas 游戏基础结构",
  "content": "## Canvas 游戏基础结构\n\n...(完整文档)..."
}
```

---

### 4.3 添加 / 删除技能

```
POST   /api/skills              # 添加自定义技能
DELETE /api/skills/{skill_name} # 删除技能
```

**添加请求体**：

```json
{
  "name": "rpg_combat",
  "description": "RPG 战斗系统模板",
  "content": "## RPG 战斗\n...(Markdown 文档)..."
}
```

---

### 4.4 ZIP 批量导入技能

```
POST /api/skills/import
```

**请求**：`multipart/form-data`，上传 `.zip` 文件。

ZIP 内支持：
- `.md` 文件 → 文件名作为技能名，`# 标题` 作为描述，正文为内容
- `.json` 文件 → 一个对象或数组，需含 `name` + `content` + `description`

上限 20MB。

**响应**：

```json
{
  "ok": true,
  "added": 5,
  "errors": ["bad.json: JSON 解析失败"],
  "warnings": ["检测到源码文件已忽略，如需源码参考请用 源码 ZIP 入口"]
}
```

- `warnings`：仅当 ZIP 同时含技能文档和源码文件（html/js/css）时出现

**误传源码项目的防呆**：ZIP 含 ≥1 个 `.html` 且 ≥1 个 `.js`/`.css`、且没有任何
`.md`/`.json` 技能文档时，返回 `422`：

```json
{
  "detail": {
    "code": "SOURCE_PROJECT_DETECTED",
    "message": "检测到这是一个源码项目 ZIP……请使用「源码 ZIP」导入入口。",
    "stats": { "html": 1, "js": 3, "css": 2 }
  }
}
```

---

### 4.6 源码参考技能导入

```
POST /api/skills/source          # 新建（multipart：name/description/content + files）
PUT  /api/skills/{name}/source   # 替换现有技能的源码参考
```

成功响应除原有摘要字段（`source_file_count`、`skipped` 等）外，新增结构化跳过明细：

```json
{
  "skipped_details": [
    {
      "path": "js/three.min.js",
      "reason": "third_party_lib",
      "hint": "AI 无法直接使用该库，只能用原生 Canvas 重实现相关效果，还原度会打折"
    }
  ]
}
```

`reason` 取值：`third_party_lib`（第三方库/压缩库/锁文件）、`unsupported_type`（不支持
的类型/不可读文本）、`too_large`（超出数量或大小上限）、`unsafe`（不安全路径）。
`hint` 是一句话中文后果说明。

---

### 4.5 扫描本地文件夹导入

```
POST /api/skills/scan
```

**请求体**：

```json
{ "path": "C:/skills/my_game_skills" }
```

安全限制：仅允许扫描项目 `skills_dir` 或用户家目录下的路径。

**响应**：

```json
{
  "ok": true,
  "added": 3,
  "skipped": ["Canvas基础"],
  "total_found": 4
}
```

---

## 5. 真机预览

**会话代码直出**（手机扫码用）：

```
GET /play/{session_id}
```

直接以 `text/html` 返回该会话最新的磁盘代码。`session_id` 经安全正则校验（防路径
穿越），会话不存在返回 `404`。与 `/assets/*` 同理豁免鉴权（裸浏览器 GET 无法携带
自定义头；session_id 为不可猜的 UUID）。

**服务器信息**（生成局域网扫码地址用）：

```
GET /api/server-info
```

响应：

```json
{ "lan_ip": "192.168.1.23", "port": 8010 }
```

`lan_ip` 通过 UDP connect 技巧探测（不真正发包），探测失败为 `null`。

---

## 6. 通用说明

### 鉴权

可选，`.env` 中配置 `API_TOKEN` 后，所有 `/api/*` 写操作需带 `X-API-Token` 头。

素材文件读取（`/assets/*`、`/api/assets/file/*`）与真机预览（`/play/*`）豁免鉴权，
供 `null-origin` iframe 预览与手机裸浏览器加载。

### 限流

对话端点（`/api/chat`、`/api/chat/stream`）：每会话每 60 秒最多 30 次请求。

### 错误码

HTTP 层：

| 状态码 | 说明 |
|--------|------|
| 400 | 参数无效 |
| 401 | API Token 缺失或无效 |
| 404 | 资源不存在 |
| 413 | 文件过大 |
| 429 | 请求过于频繁 |
| 500 | 服务端错误 |

SSE 流内 `error_code`：

| 错误码 | 说明 |
|--------|------|
| `RATE_LIMIT_EXCEEDED` | 限流 |
| `TIMEOUT` | 单回合超时 |
| `SYSTEM_ERROR` | 系统错误 |

工具调用返回中（以 `[ERROR:CODE]` 开头）：

| 错误码 | 说明 |
|--------|------|
| `INPUT_TOO_LARGE` | 输入超过限制 |
| `CODE_SIZE_EXCEEDED` | 代码总大小超限 |
| `INVALID_PARAMS` | 参数无效 |
| `NOT_FOUND` | 找不到 |
| `APPEND_TO_EMPTY` | 没有正在进行的写入 |
| `SYSTEM_ERROR` | 系统错误 |

### 素材访问 URL 构建

```
/assets/{asset_type}/{asset_id}{extension}
```

例如：`/assets/audio/a1b2c3d4.ogg`

### uniapp 开发建议

1. **流式对话**：用 `uni.request` + `responseType: 'text'`，收到 `data:` 行后分割 JSON，最后一个 token 是 `[DONE]`
2. **文件上传**：`uni.uploadFile` + `formData`
3. **素材图片/音频**：直接拼接完整 URL 引用（如 `http://your-server:8000/assets/audio/xxx.ogg`）
4. **会话管理**：首次对话不传 `session_id`，从响应中提取后持久化存储，后续带上同一 ID
5. **代码获取**：从 `done` 事件或 `ChatResponse.code` 获取，或者从 `code_update` SSE 事件增量获取
