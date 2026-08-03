"""Unity 3D 生成线：平台经 unity-mcp 桥驱动本机 Unity 编辑器。

架构（全部走编辑器内桥，无 batchmode 项目锁问题）：
- run_tool()      → unity-mcp-cli run-tool <name> --input <json>（77 个编辑器工具）
- build_webgl C#  → 经 script-execute 在编辑器内调 BuildPipeline，产物落
                    data/projects/{session}/build/（压缩关闭，树服务免 Content-Encoding）
- 桥不在线一律清晰降级提示，绝不阻断 H5 主线。
"""

import json
import os
import re
import subprocess
import tempfile

import httpx

from src.config import settings

_TOOL_TIMEOUT_S = 180
_OUTPUT_CAP = 6000


def bridge_alive() -> bool:
    try:
        httpx.get(settings.unity_bridge_url, timeout=2)
        return True
    except Exception:
        return False


def list_tool_names() -> list[str]:
    """真实工具名清单：读画布工程 .claude/skills/ 目录（技能名==工具名）。"""
    from pathlib import Path
    skills = Path(settings.unity_project_path) / ".claude" / "skills"
    try:
        names = sorted(p.name for p in skills.iterdir() if p.is_dir())
        if names:
            return names
    except OSError:
        pass
    return []


def tool_doc(name: str, cap: int = 2600) -> str:
    """读取某工具的官方 SKILL.md（含参数表和调用示例）；不存在返回空串。"""
    from pathlib import Path
    safe = re.sub(r"[^a-z0-9\-]", "", (name or "").lower())
    doc = Path(settings.unity_project_path) / ".claude" / "skills" / safe / "SKILL.md"
    try:
        text = doc.read_text(encoding="utf-8")
    except OSError:
        return ""
    # 去掉与调用无关的 CLI 安装排障样板，省 token
    text = re.sub(r"### Troubleshooting.*", "", text, flags=re.S).strip()
    return text[:cap]


def _offline_hint() -> str:
    return (
        "Unity 桥不在线（编辑器未打开画布工程或 MCP server 未 Start）。"
        f"需要先在 Unity 中打开 {settings.unity_project_path} 并确认 AI Game Developer "
        "面板 MCP server 为绿色。本次请求可改走 H5/Phaser 线，或提示用户开启 Unity。"
    )


def run_tool(name: str, input_json: str = "{}") -> str:
    """执行一个 unity-mcp 工具，返回其输出（截断到安全长度）。"""
    if not bridge_alive():
        return _offline_hint()
    safe_name = re.sub(r"[^a-z0-9\-]", "", (name or "").lower())
    if not safe_name:
        return "工具名非法（只允许小写字母/数字/连字符，如 gameobject-create）"
    try:
        json.loads(input_json or "{}")
    except json.JSONDecodeError as e:
        return f"input 不是合法 JSON：{e}"
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    try:
        tmp.write(input_json or "{}")
        tmp.close()
        # --input-file 而非 --input：Windows shell=True 会把命令行 JSON 的引号嚼碎（实测大 payload 必 500）
        proc = subprocess.run(
            ["unity-mcp-cli", "run-tool", safe_name, "--path", settings.unity_project_path,
             "--timeout", str(_TOOL_TIMEOUT_S * 1000), "--input-file", tmp.name],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_TOOL_TIMEOUT_S + 30, shell=True,
        )
    except subprocess.TimeoutExpired:
        return f"工具 {safe_name} 执行超时（{_TOOL_TIMEOUT_S}s）——检查编辑器是否弹了模态窗口卡住"
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    out = (proc.stdout or "") + (("\n[stderr] " + proc.stderr) if proc.stderr.strip() else "")
    out = out.strip() or "(无输出)"
    # 桥半死状态（HTTP 活着但 server↔编辑器 hub 断了，长构建后常见）：给出精确自救指引，
    # 并明确告诉模型停止重试——重试只会烧时间
    if re.search(r"Failed to invoke|after \d+ retries|504", out, re.I):
        return (
            "Unity 桥内部连接已断（HTTP 在但编辑器 hub 失联，长构建后常见）。"
            "【不要重试】请告知用户：在 Unity 的 AI Game Developer 面板把 MCP server "
            "Stop 再 Start（或重启 Unity 编辑器）后再让我继续。本轮可先降级或结束。"
        )
    # 工具名打错：直接把真实清单喂回去，禁止模型自己"发现 API"式盲试
    if re.search(r"Tool with Name .* not found", out, re.I):
        names = list_tool_names()
        return (f"工具 '{safe_name}' 不存在。【不要猜名、不要探 API】真实工具清单如下，选对再调：\n"
                + ", ".join(names))
    # 参数错误：自动附上该工具的官方文档（参数表+示例），禁止盲试参数形状
    if re.search(r"Parameter validation failed|cannot be null|Unable to convert|error CS", out, re.I):
        doc = tool_doc(safe_name)
        if doc:
            return (out[:1200] + f"\n\n【'{safe_name}' 官方文档——按此纠正参数，不要再盲试】\n" + doc)
    if len(out) > _OUTPUT_CAP:
        out = out[:_OUTPUT_CAP] + f"\n…（截断，共 {len(out)} 字符）"
    return out


# script-execute 在编辑器内执行的 WebGL 构建方法体（schema 实测：csharpCode/className/methodName；
# 输出路径直接烤进代码、零参数调用——绕开参数序列化的类型描述要求）。
# 关闭压缩：产物只有 .html/.js/.wasm/.data，树服务无需 Content-Encoding 协商。
# ⚠️ 传输链路会把 => 等运算符转义嚼碎（实测 lambda 编译炸 CS1525/CS0103）——
# 模板必须是零 lambda/零泛型/零比较运算符的"白开水 C#"；画布是单场景工作流，直接取活动场景
_BUILD_CSHARP_TEMPLATE = r'''
using UnityEditor;
using UnityEditor.Build.Reporting;

public static class ForgeWebGLBuild
{
    public static string Build()
    {
        var outputPath = @"__OUT__";
        PlayerSettings.WebGL.compressionFormat = WebGLCompressionFormat.Disabled;
        var scenes = new string[1];
        scenes[0] = UnityEngine.SceneManagement.SceneManager.GetActiveScene().path;
        var report = BuildPipeline.BuildPlayer(scenes, outputPath, BuildTarget.WebGL, BuildOptions.None);
        var summary = report.summary;
        return "result=" + summary.result + " size=" + summary.totalSize + " errors=" + summary.totalErrors;
    }
}
'''


def build_webgl(session_id: str) -> str:
    """在编辑器内构建 WebGL 到 data/projects/{session}/build/。返回构建摘要或错误。"""
    if not bridge_alive():
        return _offline_hint()
    from src.agent import project_store
    out_dir = (project_store.project_dir(session_id) / "build").resolve()
    payload = json.dumps({
        "csharpCode": _BUILD_CSHARP_TEMPLATE.replace("__OUT__", str(out_dir).replace("\\", "/")),
        "className": "ForgeWebGLBuild",
        "methodName": "Build",
    }, ensure_ascii=False)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    try:
        tmp.write(payload)
        tmp.close()
        proc = subprocess.run(
            ["unity-mcp-cli", "run-tool", "script-execute", "--path", settings.unity_project_path,
             "--timeout", "1150000", "--input-file", tmp.name],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=1200, shell=True,  # WebGL 首次构建可达 10+ 分钟
        )
    except subprocess.TimeoutExpired:
        return "WebGL 构建超时（20 分钟）——首次构建慢属正常，可重试一次"
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if "Succeeded" in out:
        return _build_success_message(out_dir)
    if re.search(r"WebGL.*(module|support|not installed|无法|不支持)", out, re.I) or "invalid target" in out.lower():
        return ("构建失败：本机 Unity 未安装 WebGL Build Support 模块。"
                "请在 Unity Hub → 安装 → 6000.5.6f1 齿轮 → 添加模块 → 勾选 WebGL Build Support。")
    # 网关等不完长构建会 504/重试耗尽，但编辑器里 BuildPipeline 继续跑——
    # 这不是失败，转入产物轮询（实测 504 时 build.data 已在增长）
    if re.search(r"504|gateway|timed out|retries", out, re.I):
        return _poll_build_output(out_dir)
    return f"构建失败：{out[-1500:] or '(无输出)'}"


def _build_output_ready(out_dir) -> bool:
    build_sub = out_dir / "Build"
    if not (out_dir / "index.html").exists() or not build_sub.is_dir():
        return False
    exts = {p.suffix.lower() for p in build_sub.iterdir() if p.is_file()}
    return ".wasm" in exts and ".data" in exts


def _build_success_message(out_dir) -> str:
    files = sorted(str(p.relative_to(out_dir)) for p in out_dir.rglob("*") if p.is_file())[:12]
    return (f"构建成功 → build/ 目录（{', '.join(files)}）。"
            "接下来用 write_game 写包装页 iframe 指向 build/index.html。")


def _poll_build_output(out_dir, max_wait_s: int = 1080) -> str:
    """同步调用超时后盯产物：index.html + Build/*.wasm/*.data 齐且 45s 稳定 = 构建完成。"""
    import time as _time
    deadline = _time.time() + max_wait_s
    stable, prev = 0, None
    while _time.time() < deadline:
        _time.sleep(15)
        snapshot = tuple(sorted(
            (str(p), p.stat().st_size) for p in out_dir.rglob("*") if p.is_file()
        ))
        if snapshot and snapshot == prev:
            stable += 1
            if stable >= 3 and _build_output_ready(out_dir):
                return _build_success_message(out_dir)
        else:
            stable, prev = 0, snapshot
    return ("构建仍未完成（网关超时后轮询 18 分钟）。Unity 编辑器可能弹了窗或构建极慢——"
            "检查编辑器界面后可再调一次 unity_build_webgl（会重新发起）。")
