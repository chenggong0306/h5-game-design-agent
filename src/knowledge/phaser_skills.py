"""H5 页面游戏开发技能文档"""

H5_GAME_SKILLS = [
    {
        "title": "如何在游戏代码中使用知识库素材",
        "category": "assets",
        "tags": ["assets", "image", "audio", "preload", "knowledge_base"],
        "content": """通过 search_assets 工具搜索到的素材，URL 格式为 /assets/{类型}/{asset_id}{扩展名}，直接填入代码即可。

## 图片素材

```javascript
const imgs = {};
function loadImages(urlMap, onDone) {
  const keys = Object.keys(urlMap);
  if (!keys.length) { onDone(); return; }
  let n = 0;
  keys.forEach(key => {
    const img = new Image();
    img.onload  = () => { if (++n === keys.length) onDone(); };
    img.onerror = () => { console.warn('加载失败:', urlMap[key]); if (++n === keys.length) onDone(); };
    img.src = urlMap[key];
    imgs[key] = img;
  });
}

// search_assets 返回的 URL 直接用，例如：
// - [image] player.png → URL: /assets/image/abc123.png
// - [image] bg.jpg    → URL: /assets/image/def456.jpg
loadImages({
  player: '/assets/image/abc123.png',
  bg:     '/assets/image/def456.jpg',
}, () => {
  state = 'start';
  requestAnimationFrame(loop);
});

// 绘制时判断是否加载完成，失败降级为图形
function drawSprite(key, x, y, w, h) {
  const img = imgs[key];
  if (img && img.complete && img.naturalWidth > 0) {
    ctx.drawImage(img, x, y, w, h);
  } else {
    ctx.fillStyle = '#4af';
    ctx.fillRect(x, y, w, h);
  }
}
```

## 音频素材

```javascript
const sounds = {};
function loadSounds(urlMap, onDone) {
  const keys = Object.keys(urlMap);
  if (!keys.length) { if (onDone) onDone(); return; }
  let loaded = 0;
  keys.forEach(key => {
    const audio = new Audio(urlMap[key]);
    audio.preload = 'auto';
    audio.addEventListener('canplaythrough', () => {
      if (++loaded === keys.length && onDone) onDone();
    }, { once: true });
    audio.addEventListener('error', () => {
      console.warn('音频加载失败:', urlMap[key]);
      if (++loaded === keys.length && onDone) onDone();
    }, { once: true });
    sounds[key] = audio;
  });
}

// search_assets 返回的 URL 直接用，例如：
// - [audio] hit.wav → URL: /assets/audio/ghi789.wav
// 音频也需要等待加载完成，在回调里启动游戏循环或解除 loading 状态
loadSounds({
  bgm: '/assets/audio/xxx.mp3',
  hit: '/assets/audio/ghi789.wav',
}, () => {
  console.log('音频加载完成');
  // 如果图片和音频都有，在图片的 loadImages 回调里再调用 loadSounds
});

function playSound(key) {
  const s = sounds[key];
  if (!s) return;
  s.currentTime = 0;
  s.play().catch(() => {});   // 忽略浏览器自动播放限制
}

function playBgm() {
  if (sounds.bgm) { sounds.bgm.loop = true; sounds.bgm.play().catch(() => {}); }
}
```"""
    },
    {
        "title": "H5 Canvas 游戏完整架构（gameloop）",
        "category": "gameloop",
        "tags": ["canvas", "gameloop", "state", "deltaTime", "resize", "dpr"],
        "content": """高质量 H5 游戏必须严格遵守以下代码结构与顺序，任何偏差都可能导致黑屏或 bug。

## 标准文件结构（从上到下顺序不可变）

```javascript
// ① canvas / ctx 获取
const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d');

// ② resize：DPR 适配 + 保存【逻辑尺寸 W/H】。此后一切定位都用 W、H，不要用 canvas.width/height
let W = 0, H = 0, groundY = 0;
function resize() {
  const dpr = window.devicePixelRatio || 1;
  W = window.innerWidth; H = window.innerHeight;     // 逻辑尺寸(CSS像素)，用于布局/坐标/相机/UI
  canvas.width = W * dpr; canvas.height = H * dpr;   // 物理后备缓冲(=逻辑×DPR)，别拿来定位
  canvas.style.width = W + 'px'; canvas.style.height = H + 'px'; // ⚠️ 必写！漏了高分屏画面放大≈2倍、人物/地面跑屏外看不到
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); // 此后画布坐标==逻辑像素；禁止 ctx.scale（会叠加）
  groundY = H - 80;                        // 依赖 H 的布局量(地面线等)必须在 resize 里重算
}
window.addEventListener('resize', resize);
resize(); // 必须在全局变量声明之前调用

// ③ 全局状态变量（在 resize 调用之后声明）
let state = 'loading'; // loading | start | playing | over
let score = 0, hiScore = 0, lastTime = 0;
// 游戏对象也在这里声明，例如：let player, enemies = [], bullets = [];

// ④ 工具函数（碰撞、随机数、缓动等）

// ⑤ update / draw 函数定义

// ⑥ resetGame 定义
function resetGame() {
  score = 0;
  state = 'playing';
  // 重置所有游戏对象
}

// ⑦ 输入事件绑定（在 resetGame 定义之后）
//    用 Pointer Events 一次性覆盖【鼠标(桌面)+触摸(手机)】，桌面预览才点得动
function getPointer(e) {                 // 统一取逻辑坐标
  const r = canvas.getBoundingClientRect();
  return { x: (e.clientX - r.left) * (W / r.width),    // ✅ 用 r.width(CSS宽)，不是 canvas.width
           y: (e.clientY - r.top)  * (H / r.height) };
}
canvas.addEventListener('pointerdown', e => {
  e.preventDefault();
  if (state === 'start' || state === 'over') { resetGame(); return; }
  const p = getPointer(e);
  // ...用 p.x / p.y 处理点击或分区操作（左半移动 / 右半攻击等）...
}, { passive: false });
canvas.addEventListener('pointermove', e => { e.preventDefault(); /* 按住拖动 */ }, { passive: false });
window.addEventListener('pointerup', () => { /* 松手：清除移动/格挡状态 */ });
// 键盘(桌面常用)：window.addEventListener('keydown'/'keyup', ...) 处理移动/攻击/切换

// ⑧ 主循环
function loop(ts) {
  const dt = Math.min((ts - lastTime) / 1000, 0.05); // 上限 0.05s 防止跳帧
  lastTime = ts;
  ctx.clearRect(0, 0, W, H); // 用逻辑尺寸清屏（已 setTransform(dpr)，不要用 canvas.width/height）
  if      (state === 'start')   drawStart();
  else if (state === 'playing') { update(dt); draw(); }
  else if (state === 'over')    drawOver();
  requestAnimationFrame(loop);
}

// ⑨ 启动（最后一行，图片加载完成才进 start 状态）
loadImages({ /* ... */ }, () => { state = 'start'; requestAnimationFrame(loop); });
```

## 关键规则

- **坐标系（最易出 bug！）**：定位/地面/相机/UI 一律用逻辑尺寸 `W`、`H`（resize 里存 `W=innerWidth, H=innerHeight`）。**绝不用 `canvas.width`/`canvas.height` 做坐标**——它们是物理像素(=CSS×DPR)，已 `setTransform(dpr)` 后拿它们定位会让角色/地面/UI 跑到屏幕外、游戏没法玩。地面线、屏幕中心、相机夹紧都用 `W`/`H`/`groundY`。
- 实体要有明确的 y：角色/敌人统一站在 `groundY`（如 `player.y = groundY`），别让 y 留在 0 或用物理高度。
- 所有移动必须乘以 dt：`obj.x += speed * dt`，禁止 `obj.x += 5`
- `ctx.arc` / `ctx.ellipse` 半径必须 `Math.max(1, r)`，防止负值报错
- 数组删除用 `filter`，禁止在 `for` 循环中 `splice`
- **输入必须同时支持鼠标和触摸**：游戏会在桌面预览里用**鼠标**测试、在手机上用**触摸**。优先用 **Pointer Events**（`pointerdown/pointermove/pointerup`，一次覆盖鼠标+触摸），或鼠标(`mousedown/move/up`)和触摸(`touchstart/move/end`)各绑一套。**只绑 `touchstart` 会导致桌面点击毫无反应、连开始界面都进不去**。开始/结束界面也要能用鼠标点击进入。
- 坐标转换到**逻辑像素**：`const r = canvas.getBoundingClientRect(); x = (e.clientX - r.left) * (W / r.width)`——**用 `r.width`(CSS宽)，不是 `canvas.width`(物理宽，会多乘 DPR、坐标翻倍)**。
- `pointer*` / `touchstart` / `touchmove` 必须加 `e.preventDefault()` + `{ passive: false }`"""
    },
    {
        "title": "游戏视觉与手感打磨（polish）",
        "category": "polish",
        "tags": ["particles", "screenshake", "easing", "glow", "gradient", "juice"],
        "content": """高质量游戏必须有视觉反馈和手感，以下是可直接复用的完整实现。

## 粒子系统

```javascript
let particles = [];
function spawnParticles(x, y, count, color) {
  for (let i = 0; i < count; i++) {
    const angle = Math.random() * Math.PI * 2;
    const speed = 60 + Math.random() * 120;
    particles.push({
      x, y, color,
      vx: Math.cos(angle) * speed,
      vy: Math.sin(angle) * speed,
      life: 1, decay: 0.8 + Math.random() * 0.8, r: 3 + Math.random() * 4,
    });
  }
}
function updateParticles(dt) {
  particles = particles.filter(p => {
    p.x += p.vx * dt; p.y += p.vy * dt;
    p.vy += 200 * dt; // 重力
    p.life -= p.decay * dt;
    return p.life > 0;
  });
}
function drawParticles() {
  particles.forEach(p => {
    ctx.globalAlpha = Math.max(0, p.life);
    ctx.fillStyle = p.color;
    ctx.beginPath();
    ctx.arc(p.x, p.y, Math.max(0.5, p.r * p.life), 0, Math.PI * 2);
    ctx.fill();
  });
  ctx.globalAlpha = 1;
}
```

## 屏幕震动

```javascript
let shakeTime = 0, shakeMag = 0;
function screenShake(magnitude, duration) { shakeMag = magnitude; shakeTime = duration; }
function applyShake(dt) {
  if (shakeTime <= 0) return;
  shakeTime -= dt;
  const s = shakeMag * (shakeTime / 0.3);
  ctx.save();
  ctx.translate((Math.random() - 0.5) * s, (Math.random() - 0.5) * s);
}
function restoreShake() { if (shakeMag > 0) ctx.restore(); }
// 用法：drawStart 里 applyShake(dt) ... 绘制所有内容 ... restoreShake()
// 触发：screenShake(8, 0.3); // 击中时调用
```

## 缓动函数

```javascript
const ease = {
  outQuad:  t => 1 - (1-t)*(1-t),
  outBack:  t => 1 + 2.7*(t-1)**3 + 1.7*(t-1)**2,
  outBounce: t => {
    if (t < 1/2.75) return 7.5625*t*t;
    if (t < 2/2.75) return 7.5625*(t-=1.5/2.75)*t+0.75;
    if (t < 2.5/2.75) return 7.5625*(t-=2.25/2.75)*t+0.9375;
    return 7.5625*(t-=2.625/2.75)*t+0.984375;
  },
};
// 用法：obj.y = startY + (targetY - startY) * ease.outBack(Math.min(t, 1));
```

## 光效与渐变

```javascript
// 发光文字 / 对象
ctx.shadowColor = '#0ff'; ctx.shadowBlur = 20;
ctx.fillText('SCORE', x, y);
ctx.shadowBlur = 0; // 画完记得重置

// 渐变背景
const grad = ctx.createLinearGradient(0, 0, 0, canvas.height);
grad.addColorStop(0, '#0a0a1a');
grad.addColorStop(1, '#1a0a2e');
ctx.fillStyle = grad; ctx.fillRect(0, 0, canvas.width, canvas.height);

// 径向渐变（爆炸/光晕）
const radGrad = ctx.createRadialGradient(x, y, 0, x, y, r);
radGrad.addColorStop(0, 'rgba(255,200,0,0.8)');
radGrad.addColorStop(1, 'rgba(255,0,0,0)');
ctx.fillStyle = radGrad; ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI*2); ctx.fill();
```

## 数字平滑滚动（显示分数时用）

```javascript
let displayScore = 0;
// 在 update 里：
displayScore += (score - displayScore) * Math.min(1, dt * 10);
// 绘制：ctx.fillText(Math.round(displayScore), x, y);
```"""
    },
    {
        "title": "游戏设计规范（gamedesign）",
        "category": "gamedesign",
        "tags": ["difficulty", "feedback", "scoring", "ui", "ux", "progression"],
        "content": """高质量游戏必须有清晰的设计规范，以下是必须实现的要素。

## 难度曲线（必须实现）

```javascript
// 游戏时长驱动难度，而非直接用分数（更平滑）
let gameTime = 0;
function getDifficulty() {
  // 前10秒宽松，之后每30秒增加一档，最高5档
  return Math.min(5, 1 + Math.floor(gameTime / 30));
}
function update(dt) {
  gameTime += dt;
  const diff = getDifficulty();
  enemySpeed  = 80  + diff * 30;   // 速度随难度增加
  spawnRate   = 2.0 - diff * 0.25; // 生成间隔缩短
  // 分数加成：高难度得分更多
  // if (killed) score += diff * 10;
}
```

## 玩家反馈（击中/得分/死亡必须有视觉反馈）

```javascript
// 浮动文字（得分弹出）
let floatTexts = [];
function addFloatText(x, y, text, color = '#ff0') {
  floatTexts.push({ x, y, text, color, life: 1, vy: -60 });
}
function updateFloatTexts(dt) {
  floatTexts = floatTexts.filter(t => {
    t.y += t.vy * dt; t.life -= dt * 1.5; return t.life > 0;
  });
}
function drawFloatTexts() {
  floatTexts.forEach(t => {
    ctx.globalAlpha = t.life;
    ctx.fillStyle = t.color;
    ctx.font = `bold ${canvas.width * 0.04}px Arial`;
    ctx.textAlign = 'center';
    ctx.fillText(t.text, t.x, t.y);
  });
  ctx.globalAlpha = 1;
}
// 触发示例：addFloatText(enemy.x, enemy.y, '+10', '#ff0');
//           screenShake(5, 0.15); spawnParticles(x, y, 8, '#f80');
```

## 开始 / 结束界面规范

```javascript
function drawStart() {
  // 1. 半透明背景
  ctx.fillStyle = 'rgba(0,0,0,0.75)'; ctx.fillRect(0, 0, canvas.width, canvas.height);
  // 2. 游戏标题（大字，有光效）
  ctx.shadowColor = '#0ff'; ctx.shadowBlur = 30;
  ctx.fillStyle = '#fff'; ctx.textAlign = 'center';
  ctx.font = `bold ${canvas.width * 0.1}px Arial`;
  ctx.fillText('游戏名称', canvas.width / 2, canvas.height * 0.35);
  ctx.shadowBlur = 0;
  // 3. 操作说明
  ctx.font = `${canvas.width * 0.04}px Arial`; ctx.fillStyle = '#aaa';
  ctx.fillText('← → 移动  空格跳跃', canvas.width / 2, canvas.height * 0.52);
  // 4. 开始提示（闪烁）
  if (Math.floor(Date.now() / 500) % 2) {
    ctx.fillStyle = '#ff0';
    ctx.font = `${canvas.width * 0.05}px Arial`;
    ctx.fillText('点击开始', canvas.width / 2, canvas.height * 0.68);
  }
}

function drawOver() {
  ctx.fillStyle = 'rgba(0,0,0,0.8)'; ctx.fillRect(0, 0, canvas.width, canvas.height);
  // 1. 结束标题
  ctx.fillStyle = '#f44'; ctx.textAlign = 'center';
  ctx.font = `bold ${canvas.width * 0.1}px Arial`;
  ctx.fillText('GAME OVER', canvas.width / 2, canvas.height * 0.32);
  // 2. 本局得分
  ctx.fillStyle = '#fff'; ctx.font = `${canvas.width * 0.06}px Arial`;
  ctx.fillText(`得分  ${score}`, canvas.width / 2, canvas.height * 0.48);
  // 3. 最高分（不同颜色区分）
  ctx.fillStyle = score >= hiScore ? '#ff0' : '#888';
  ctx.font = `${canvas.width * 0.04}px Arial`;
  ctx.fillText(`最高分  ${hiScore}`, canvas.width / 2, canvas.height * 0.60);
  // 4. 重玩提示
  ctx.fillStyle = '#aaa'; ctx.font = `${canvas.width * 0.04}px Arial`;
  ctx.fillText('点击重玩', canvas.width / 2, canvas.height * 0.76);
}
```

## UI 层次与分数显示

```javascript
function drawHUD() {
  // 分数左上角，字体相对画布宽度
  ctx.fillStyle = '#fff'; ctx.textAlign = 'left'; ctx.shadowBlur = 0;
  ctx.font = `bold ${canvas.width * 0.05}px Arial`;
  ctx.fillText(`${Math.round(displayScore)}`, canvas.width * 0.04, canvas.height * 0.07);
  // 最高分右上角
  ctx.textAlign = 'right'; ctx.fillStyle = '#aaa';
  ctx.font = `${canvas.width * 0.035}px Arial`;
  ctx.fillText(`最高 ${hiScore}`, canvas.width * 0.96, canvas.height * 0.06);
  // 难度档位（可选）
  ctx.fillStyle = `hsl(${120 - getDifficulty() * 20}, 80%, 60%)`;
  ctx.textAlign = 'center';
  ctx.font = `${canvas.width * 0.03}px Arial`;
  ctx.fillText(`Lv.${getDifficulty()}`, canvas.width / 2, canvas.height * 0.05);
}
```"""
    },
    {
        "title": "Canvas 2D 模拟 3D 渲染 — 魔方/立方体等旋转物体",
        "category": "3d-rendering",
        "tags": ["3d", "cube", "rubik", "透视投影", "深度排序", "背面剔除", "向量"],
        "content": """# Canvas 2D 模拟 3D 渲染的正确姿势

## ⚠️ 最容易出错的点（反复踩坑总结）

| 陷阱 | 错误做法 | 正确做法 |
|------|---------|---------|
| 投影返回值类型 | 返回普通 `{x,y,z}` 对象 | 返回普通对象即可，**不要**在后续对投影结果调用 V3 方法 |
| 背面剔除 | 在世界空间判 `z>0` | 法线变换到**视角空间**后判 `vn.z <= 0` 跳过（背向相机） |
| 深度排序 | 按投影后 `z` 排序 | 先 project 拿 z，排序后再 drawFace（远→近） |
| 面色判定 | 复杂条件 `x===0||z===0` | 用 `orig`（初始局部坐标）判断该面是否在魔方外层 |
| 旋转后量化 | 不量化，浮点累积 | `Math.round(orig)` 到 -1/0/1（防止漂移） |
| DPR 适配 | `ctx.scale(dpr)` | `ctx.setTransform(dpr,0,0,dpr,0,0)`（一劳永逸，不叠加） |
| 顶点生成 | 手动加减坐标 | 用法线 `n` 动态计算切线 `u = n×temp`，再 `center + u*h + v*h` |

## 完整可运行的 3D 魔方参考代码

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>3D 魔方</title>
<style>
    body { margin:0; overflow:hidden; background:#1a1a2e; font-family:'Segoe UI',sans-serif; user-select:none; touch-action:none; }
    canvas { display:block; width:100vw; height:100vh; }
    #ui { position:absolute; top:20px; left:20px; color:#fff; pointer-events:none; text-shadow:0 2px 4px rgba(0,0,0,.5); }
    #ui h1 { font-size:24px; margin:0 0 8px; letter-spacing:2px; }
    .hint { position:absolute; bottom:24px; width:100%; text-align:center; color:#888; font-size:13px; pointer-events:none; }
</style>
</head>
<body>
<div id="ui"><h1>3D 魔方</h1><div id="mc" style="font-size:16px;opacity:.85;">步数: 0</div></div>
<div class="hint">拖拽旋转视角 | 键盘 U D L R F B 旋转层 | 空格 重置</div>
<canvas id="c"></canvas>
<script>
const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d');
const $mc = document.getElementById('mc');
let W, H, dpr;
function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = window.innerWidth; H = window.innerHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
window.addEventListener('resize', resize); resize();

const C = { U:'#FFF', D:'#FF0', L:'#F80', R:'#F00', F:'#0A0', B:'#00F', INNER:'#1a1a1a' };

// ---- V3: 不可变向量（所有操作返回新实例）----
class V3 {
    constructor(x=0,y=0,z=0) { this.x=x;this.y=y;this.z=z; }
    add(b) { return new V3(this.x+b.x, this.y+b.y, this.z+b.z); }
    sub(b) { return new V3(this.x-b.x, this.y-b.y, this.z-b.z); }
    mul(s) { return new V3(this.x*s, this.y*s, this.z*s); }
    dot(b) { return this.x*b.x + this.y*b.y + this.z*b.z; }
    cross(b) { return new V3(this.y*b.z-this.z*b.y, this.z*b.x-this.x*b.z, this.x*b.y-this.y*b.x); }
    len() { return Math.sqrt(this.x*this.x+this.y*this.y+this.z*this.z); }
    norm() { const l=this.len(); return l<1e-9?new V3():new V3(this.x/l,this.y/l,this.z/l); }
}
function rotX(v,a) { const c=Math.cos(a),s=Math.sin(a); return new V3(v.x, v.y*c-v.z*s, v.y*s+v.z*c); }
function rotY(v,a) { const c=Math.cos(a),s=Math.sin(a); return new V3(v.x*c+v.z*s, v.y, -v.x*s+v.z*c); }
function rotZ(v,a) { const c=Math.cos(a),s=Math.sin(a); return new V3(v.x*c-v.y*s, v.x*s+v.y*c, v.z); }

// ---- Cubelet ----
class Cubelet {
    constructor(x, y, z) {
        this.p = new V3(x, y, z);       // 当前位置
        this.orig = new V3(x, y, z);    // 初始位置（判断面色归属）
    }
    getFaces() {
        const faces = [];
        const h = 0.46;
        const dirs = [
            { n: new V3(0,-1,0), k:'U' }, { n: new V3(0,1,0), k:'D' },
            { n: new V3(-1,0,0), k:'L' }, { n: new V3(1,0,0), k:'R' },
            { n: new V3(0,0,1), k:'F' },  { n: new V3(0,0,-1), k:'B' },
        ];
        for (const d of dirs) {
            const o = this.orig, n = d.n;
            const outer =
                (n.x===-1&&o.x===-1)||(n.x===1&&o.x===1)||
                (n.y===-1&&o.y===-1)||(n.y===1&&o.y===1)||
                (n.z===1&&o.z===1)||(n.z===-1&&o.z===-1);
            faces.push({ n:d.n, c:outer?C[d.k]:C.INNER });
        }
        return faces;
    }
    rotate(axis, angle) {
        const fn = axis==='x'?rotX:axis==='y'?rotY:rotZ;
        this.p = fn(this.p, angle);
        this.orig = fn(this.orig, angle);
        this.orig.x = Math.round(this.orig.x);
        this.orig.y = Math.round(this.orig.y);
        this.orig.z = Math.round(this.orig.z);
    }
}

// ---- 全局 ----
let cubes = [], moves = 0;
let vRY = 0.7, vRX = -0.5;
function init() {
    cubes = [];
    for (let x=-1; x<=1; x++) for (let y=-1; y<=1; y++) for (let z=-1; z<=1; z++)
        cubes.push(new Cubelet(x, y, z));
    moves = 0; $mc.textContent = '步数: 0';
}

function rotLayer(axis, layer, angle) {
    for (const c of cubes) {
        const v = axis==='x'?c.orig.x:axis==='y'?c.orig.y:c.orig.z;
        if (Math.abs(v - layer) < 0.1) c.rotate(axis, angle);
    }
    moves++; $mc.textContent = '步数: ' + moves;
}

// ---- 渲染 ----
function proj(w) {
    let v = rotX(w, vRX);
    v = rotY(v, vRY);
    // ★ 弱透视：透视系数 1 - v.z*0.04，保证远近面尺寸一致（正方体不变形）
    // 对比错误写法：v.x * 100 * 400/(6+v.z) → 叠加因子导致坐标飞出屏幕
    const unit = Math.min(W, H) * 0.18;       // 1 个 cubelet ≈ 屏幕 18%
    const perspective = 1 - v.z * 0.04;        // z ∈ [-2,2] → factor ∈ [1.08, 0.92]
    const f = unit * perspective;
    return { x: W/2 + v.x*f, y: H/2 - v.y*f, z: v.z };
}

function faceVerts(c, n) {
    const h = 0.45;
    const t = Math.abs(n.x)<0.9 ? new V3(1,0,0) : new V3(0,1,0);
    const u = n.cross(t).norm();
    const v = n.cross(u).norm();
    const cen = c.p.add(n.mul(h));
    return [cen.add(u.mul(h)).add(v.mul(h)), cen.add(u.mul(-h)).add(v.mul(h)),
            cen.add(u.mul(-h)).add(v.mul(-h)), cen.add(u.mul(h)).add(v.mul(-h))];
}

function drawFace(pts3, color) {
    const pts = pts3.map(proj);
    // ★ 背面剔除已由 drawC 的视角空间法线完成（vn.z <= 0），
    // 这里不需要再做 2D 叉积——投影后顶点顺序可能因旋转而反转。
    ctx.fillStyle = color; ctx.strokeStyle = 'rgba(0,0,0,.5)'; ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(pts[0].x,pts[0].y); ctx.lineTo(pts[1].x,pts[1].y);
    ctx.lineTo(pts[2].x,pts[2].y); ctx.lineTo(pts[3].x,pts[3].y);
    ctx.closePath(); ctx.fill(); ctx.stroke();
}

function drawC(c) {
    for (const f of c.getFaces()) {
        let vn = rotX(f.n, vRX); vn = rotY(vn, vRY);
        if (vn.z >= 0) continue; // 相机看 +z 方向，法线 z≥0 → 背向相机 → 剔除
        drawFace(faceVerts(c, f.n), f.c);
    }
}

function loop() {
    ctx.clearRect(0, 0, W, H);
    const projCubes = cubes.map(c => ({ c, z: proj(c.p).z }));
    projCubes.sort((a, b) => a.z - b.z);
    for (const { c } of projCubes) drawC(c);
    requestAnimationFrame(loop);
}

// ---- 交互 ----
let dragging = false, lx = 0, ly = 0;
canvas.addEventListener('pointerdown', e => { dragging=true; lx=e.clientX; ly=e.clientY; });
window.addEventListener('pointermove', e => {
    if (!dragging) return;
    vRY += (e.clientX-lx)*0.008; vRX += (e.clientY-ly)*0.008;
    lx=e.clientX; ly=e.clientY;
});
window.addEventListener('pointerup', () => dragging=false);
window.addEventListener('keydown', e => {
    const k = e.key.toUpperCase(), T = Math.PI/2;
    if (k==='U') rotLayer('y',-1,-T); if (k==='D') rotLayer('y',1,T);
    if (k==='L') rotLayer('x',-1,-T); if (k==='R') rotLayer('x',1,T);
    if (k==='F') rotLayer('z',1,T);  if (k==='B') rotLayer('z',-1,-T);
    if (k===' ') init();
});
init(); loop();
</script>
</body>
</html>
```

## 关键渲染流程（严格照此顺序）

1. **resize**: `setTransform(dpr,0,0,dpr,0,0)` + `canvas.style.width = W + 'px'`（禁止 ctx.scale）
2. **每帧**: `clearRect` → 所有 cubelet 投影拿 z → **按 z 排序（远→近）** → 逐个 drawC
3. **drawC**: 6 个面 → 法线变到视角空间 → `vn.z >= 0` 跳过（从视角空间看，z 正半轴指向屏幕深处，法线 z≥0 = 背向相机 = 剔除）
4. **drawFace**: 投影 4 个顶点 → fill + stroke（背面剔除已在 drawC 完成）
5. **旋转层**: 改 cubelet 的 `p` 和 `orig` → `Math.round(orig)` 量化到 -1/0/1 防止浮点漂移

## 投影公式（最容易出错的点）

```
错误: v.x * 100 * 400/(6+v.z) → 坐标 ≈ 10000，飞到屏幕外（叠加两个缩放因子）
错误: v.x * scale/(6+v.z)    → scale=350,z∈[-2,2], f 从 350/4=88 到 350/8=44
                                 近处尺寸是远处的 2 倍 → 正方体变梯形/扭曲
正确: v.x * unit * (1 - z*0.04) → unit = min(W,H)*0.18
                                   z=-2→factor=1.08, z=+2→factor=0.92
                                   仅 ~15% 差异 → 正方体看起来方正不变形
```

不要叠加两个系数！不要用除法做透视（分母变化太大）。用减法/线性缩放，
factor 在 0.9~1.1 之间波动 → 有 3D 立体感但不变形。

## 如果生成 3D 游戏，请严格参考上述代码的结构和数学逻辑，避免：
- ❌ `v.x * 数值 * f` 叠加两个缩放因子（投影到屏幕外）
- ❌ 在世界空间判背面（方向随视角变化会反）
- ❌ 背面剔除用 `vn.z <= 0` 而不用 `vn.z >= 0`（符号方向反了，导致所有面被裁掉）
- ❌ 对 project() 返回的普通对象调 V3 方法
- ❌ 旋转后不量化 orig（浮点累积漂移）
- ❌ 忘记 canvas.style.width/height（高分屏放大）
- ❌ 使用 ctx.scale(dpr)（叠加缩放）
- ❌ drawFace 里再加 2D 叉积背面剔除（与 drawC 重复，可能误杀面）"""
    },
]

