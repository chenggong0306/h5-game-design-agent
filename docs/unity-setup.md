# Unity 3D 生成线·接入指南（新机器从零到可用）

> 平台不装 Unity 也**完全正常**：H5/Phaser 线不受影响，用户点名 "用 unity" 时工具会明确提示
> 桥不在线并自动降级 Canvas 拟 3D。本文只在你想启用真 Unity 线时需要。
>
> 启用后的效果：聊天说"用 unity 做个 3D 滚球收集金币"→ 平台 agent 驱动本机 Unity 编辑器
> 施工 → Play Mode 自测 → WebGL 构建 → 浏览器/手机扫码即玩（端到端已实测）。

## 一、装机（一次性，约 40 分钟，多为下载等待）

1. **Unity Hub + 编辑器**
   - 装 Unity Hub（winget: `winget install Unity.UnityHub`），登录 Unity 账号（免费 Personal 许可自动激活；
     激活失败先关代理重试——许可服务器对代理 TUN 敏感）
   - Hub → 安装 → **Unity 6.x**（6000.5+），确认 **Web Build Support 已含**（Unity 6 里 WebGL 改叫 Web；
     没有就在版本齿轮 → 添加模块里补）
2. **unity-mcp-cli**（需 Node）：
   ```bash
   npm install -g unity-mcp-cli
   ```
3. **建画布工程**：Hub → 新建项目 → **Universal 3D** 模板 → 任意路径（如 `D:\unity_games\forge_canvas`）
4. **给画布工程装 AI 插件**：
   ```bash
   unity-mcp-cli install-plugin <画布工程路径>
   ```
   打开工程让它导入插件；AI Game Developer 面板里 Connection 选 **Custom**（本地 27099），不要用 Cloud。
   > 坑：MCP 服务器二进制下载 checksum 失败（代理环境常见）。修法——手动下载校验后放进
   > `<工程>/Library/mcp-server/win-x64/` 再重启 Unity：
   > ```bash
   > curl -L -o s.zip https://github.com/IvanMurzak/GameDev-MCP-Server/releases/download/v9.2.4/gamedev-mcp-server-win-x64.zip
   > # 对照同 release 的 SHA256SUMS 校验后解压到上述目录
   > ```
5. **生成技能目录**（平台依赖它！）：
   ```bash
   npx unity-mcp-cli setup-skills claude-code <画布工程路径>
   ```
   会在 `<工程>/.claude/skills/` 生成 77 个工具文档——平台的 `unity_list_tools` /
   `unity_tool_help` / 出错自动喂文档，读的全是这个目录。**没有它，agent 会因为拿不到
   工具清单和参数文档而寸步难行。**

## 二、平台侧配置（.env 三行）

```bash
UNITY_PROJECT_PATH=D:\unity_games\forge_canvas
UNITY_EDITOR_EXE=C:\Program Files\Unity\Hub\Editor\6000.5.6f1\Editor\Unity.exe
# UNITY_BRIDGE_URL=http://localhost:27099   # 默认即此，改端口才需要
# UNITY_FACTORY_PATH=D:\unity_games\asset_factory  # 素材工厂工程（首次用时自动创建）
```
改完重启平台（`uv run python main.py`）。

## 三、日常使用

1. Unity 打开画布工程，确认面板 MCP server **绿色**（编辑器必须保持开着）
2. 平台聊天框：`用 unity 做一个 ……`（显式点名 unity/真 3D 才走此线；平台会自动
   unity_list_tools → 施工 → Play Mode 自测 → unity_build_webgl → 包装页交付）
3. Unity 线单回合时限 40 分钟（`UNITY_TURN_DEADLINE_SECONDS` 可调）；WebGL 构建
   3-10 分钟属正常，编辑器会弹进度条

## 四、排障速查（全部实战验证过）

| 症状 | 处置 |
|---|---|
| Hub 点不开工程 | 僵尸进程/孤儿锁：`taskkill /F /IM Unity.exe` + 删 `<工程>/Temp/UnityLockfile` |
| 工具返回"桥内部连接已断" | 长构建后常见：面板 MCP server Stop→Start，或重启编辑器 |
| 构建报 504/Gateway Timeout | **不是失败**——编辑器还在构建，平台会自动轮询产物直到完成 |
| WebGL 游戏满屏 Input 异常、键盘无效 | 旧 Input API vs 新 Input System：平台会引导设 activeInputHandler=Both，**改完必须重启编辑器再重新构建** |
| 许可激活失败 | 关代理重试；不行则 Hub 退出重新登录 |

## 五、素材工厂（可选加餐）

把带动画的 `.fbx` 放进 `<UNITY_FACTORY_PATH>/Assets/Models/`，聊天里就能让 AI 用
`render_sprite_sheet` 把模型渲染成透明底序列帧图集入素材库（无头运行，不占编辑器）。
不放模型也有内置 `demo-cube` 可验证管线。
