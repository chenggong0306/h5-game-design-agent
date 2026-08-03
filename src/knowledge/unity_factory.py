"""Unity 素材工厂：无头 batchmode 把带动画的 3D 模型渲染成 2D 序列帧图集，喂给 H5 线。

与编辑器画布工程完全隔离（独立 asset_factory 工程），不抢项目锁。
产物：横向 grid 图集 sheet.png + 元信息（帧数/帧尺寸），经 kb.upload_asset 入素材库，
H5 游戏用现成的 drawImage 裁帧套路播放——补上"精灵动画帧"这个已知素材短板。

模型库：把带动画的 .fbx/.glb 放进 {unity_factory_path}/Assets/Models/ 后按文件名渲染；
内置 "demo-cube"（程序化旋转立方体）用于零素材验证管线。
"""

import re
import subprocess
import time
from pathlib import Path

from src.config import settings

_RENDER_TIMEOUT_S = 900
_FACTORY_SCRIPT = r'''
using System;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEngine;

public static class SpriteFactory
{
    static string Arg(string name, string fallback)
    {
        var args = Environment.GetCommandLineArgs();
        for (int i = 0; i < args.Length - 1; i++)
            if (args[i] == name) return args[i + 1];
        return fallback;
    }

    public static void Render()
    {
        var model  = Arg("-sfModel", "demo-cube");
        var frames = int.Parse(Arg("-sfFrames", "8"));
        var size   = int.Parse(Arg("-sfSize", "256"));
        var outDir = Arg("-sfOut", "Output");
        Directory.CreateDirectory(outDir);

        GameObject subject;
        AnimationClip clip = null;
        if (model == "demo-cube")
        {
            subject = GameObject.CreatePrimitive(PrimitiveType.Cube);
            var mr = subject.GetComponent<MeshRenderer>();
            mr.sharedMaterial = new Material(Shader.Find("Standard")) { color = new Color(0.9f, 0.35f, 0.2f) };
        }
        else
        {
            var path = "Assets/Models/" + model;
            var prefab = AssetDatabase.LoadAssetAtPath<GameObject>(path);
            if (prefab == null) { File.WriteAllText(Path.Combine(outDir, "error.txt"), "MODEL_NOT_FOUND:" + path); EditorApplication.Exit(2); return; }
            subject = UnityEngine.Object.Instantiate(prefab);
            clip = AssetDatabase.LoadAllAssetsAtPath(path).OfType<AnimationClip>().FirstOrDefault();
        }

        var bounds = new Bounds(subject.transform.position, Vector3.one);
        foreach (var r in subject.GetComponentsInChildren<Renderer>()) bounds.Encapsulate(r.bounds);

        var camGo = new GameObject("SFCam");
        var cam = camGo.AddComponent<Camera>();
        cam.orthographic = true;
        cam.orthographicSize = Mathf.Max(bounds.extents.x, bounds.extents.y) * 1.35f;
        cam.transform.position = bounds.center + new Vector3(0, 0, -(bounds.extents.z + 5f));
        cam.transform.LookAt(bounds.center);
        cam.clearFlags = CameraClearFlags.SolidColor;
        cam.backgroundColor = new Color(0, 0, 0, 0);   // 透明底

        var light = new GameObject("SFLight").AddComponent<Light>();
        light.type = LightType.Directional;
        light.transform.rotation = Quaternion.Euler(45, -30, 0);

        var rt = new RenderTexture(size, size, 24, RenderTextureFormat.ARGB32);
        var sheet = new Texture2D(size * frames, size, TextureFormat.RGBA32, false);
        var frame = new Texture2D(size, size, TextureFormat.RGBA32, false);

        for (int i = 0; i < frames; i++)
        {
            float t = frames <= 1 ? 0f : (float)i / frames;
            if (clip != null) clip.SampleAnimation(subject, t * clip.length);
            else subject.transform.rotation = Quaternion.Euler(20f, t * 360f, 0);

            cam.targetTexture = rt;
            cam.Render();
            RenderTexture.active = rt;
            frame.ReadPixels(new Rect(0, 0, size, size), 0, 0);
            frame.Apply();
            sheet.SetPixels(i * size, 0, size, size, frame.GetPixels());
            RenderTexture.active = null;
            cam.targetTexture = null;
        }
        sheet.Apply();
        File.WriteAllBytes(Path.Combine(outDir, "sheet.png"), sheet.EncodeToPNG());
        File.WriteAllText(Path.Combine(outDir, "meta.json"),
            "{\"frames\":" + frames + ",\"frameWidth\":" + size + ",\"frameHeight\":" + size + "}");
        EditorApplication.Exit(0);
    }
}
'''


def factory_ready() -> bool:
    return (Path(settings.unity_factory_path) / "Assets" / "Editor" / "SpriteFactory.cs").exists()


def ensure_factory(log_path: str | None = None) -> str:
    """首次创建无头工厂工程并注入渲染脚本（幂等；创建约 2-5 分钟）。"""
    root = Path(settings.unity_factory_path)
    script = root / "Assets" / "Editor" / "SpriteFactory.cs"
    if not (root / "Assets").exists():
        proc = subprocess.run(
            [settings.unity_editor_exe, "-batchmode", "-quit",
             "-createProject", str(root),
             "-logFile", log_path or str(root.parent / "factory_create.log")],
            capture_output=True, text=True, timeout=900,
        )
        if not (root / "Assets").exists():
            return f"工厂工程创建失败（exit={proc.returncode}），看日志 {log_path}"
    script.parent.mkdir(parents=True, exist_ok=True)
    if not script.exists():
        script.write_text(_FACTORY_SCRIPT, encoding="utf-8")
    (root / "Assets" / "Models").mkdir(exist_ok=True)
    return "ok"


def render(model: str = "demo-cube", frames: int = 8, size: int = 256) -> dict:
    """渲染序列帧图集。返回 {ok, sheet_path, frames, frame_w, frame_h} 或 {ok:False, error}。"""
    model = re.sub(r"[^\w.\-]", "", model or "demo-cube") or "demo-cube"
    frames = max(1, min(int(frames), 32))
    size = max(64, min(int(size), 512))
    status = ensure_factory()
    if status != "ok":
        return {"ok": False, "error": status}
    out_dir = Path(settings.unity_factory_path) / "Output" / f"{model.split('.')[0]}-{int(time.time())}"
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / "render.log"
    try:
        subprocess.run(
            [settings.unity_editor_exe, "-batchmode", "-quit",
             "-projectPath", settings.unity_factory_path,
             "-executeMethod", "SpriteFactory.Render",
             "-sfModel", model, "-sfFrames", str(frames), "-sfSize", str(size),
             "-sfOut", str(out_dir), "-logFile", str(log)],
            capture_output=True, text=True, timeout=_RENDER_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"渲染超时（{_RENDER_TIMEOUT_S}s）"}
    err = out_dir / "error.txt"
    if err.exists():
        return {"ok": False, "error": err.read_text(encoding="utf-8")[:200]}
    sheet = out_dir / "sheet.png"
    if not sheet.exists():
        tail = log.read_text(encoding="utf-8", errors="replace")[-800:] if log.exists() else "(无日志)"
        return {"ok": False, "error": f"未产出图集，日志尾部：{tail}"}
    return {"ok": True, "sheet_path": str(sheet), "frames": frames, "frame_w": size, "frame_h": size}
