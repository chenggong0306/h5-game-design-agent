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

// ② resize 定义 + 立即调用（DPR 适配，防模糊）
function resize() {
  const dpr = window.devicePixelRatio || 1;
  const w = window.innerWidth, h = window.innerHeight;
  canvas.width = w * dpr; canvas.height = h * dpr;
  canvas.style.width = w + 'px'; canvas.style.height = h + 'px';
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); // ✅ 禁止用 ctx.scale（会叠加）
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
canvas.addEventListener('click', () => {
  if (state === 'start' || state === 'over') resetGame();
});

// ⑧ 主循环
function loop(ts) {
  const dt = Math.min((ts - lastTime) / 1000, 0.05); // 上限 0.05s 防止跳帧
  lastTime = ts;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if      (state === 'start')   drawStart();
  else if (state === 'playing') { update(dt); draw(); }
  else if (state === 'over')    drawOver();
  requestAnimationFrame(loop);
}

// ⑨ 启动（最后一行，图片加载完成才进 start 状态）
loadImages({ /* ... */ }, () => { state = 'start'; requestAnimationFrame(loop); });
```

## 关键规则

- 所有移动必须乘以 dt：`obj.x += speed * dt`，禁止 `obj.x += 5`
- `ctx.arc` / `ctx.ellipse` 半径必须 `Math.max(1, r)`，防止负值报错
- 数组删除用 `filter`，禁止在 `for` 循环中 `splice`
- 触摸坐标必须转换：`(e.touches[0].clientX - rect.left) * (canvas.width / rect.width)`
- `touchstart` / `touchmove` 必须加 `e.preventDefault()` + `{ passive: false }`"""
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
]

