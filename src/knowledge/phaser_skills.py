"""H5 页面游戏开发技能文档 - 预加载到知识库（含完整代码示例）"""

H5_GAME_SKILLS = [
    {
        "title": "H5 Canvas 游戏完整基础模板",
        "category": "base",
        "tags": ["canvas", "gameloop", "h5", "template"],
        "content": """H5游戏必须使用以下完整结构，包含状态机、deltaTime、图片预加载：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0,user-scalable=no">
  <title>游戏名</title>
  <style>
    * { margin:0; padding:0; box-sizing:border-box; }
    body { background:#000; overflow:hidden; touch-action:none; }
    canvas { display:block; }
  </style>
</head>
<body>
<canvas id="c"></canvas>
<script>
const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d');

// 自适应屏幕
function resize() { canvas.width = window.innerWidth; canvas.height = window.innerHeight; }
window.addEventListener('resize', resize);
resize();

// ===== 游戏状态机（必须有这三种状态）=====
let state = 'loading'; // loading | start | playing | over
let score = 0, hiScore = 0, lastTime = 0;

// ===== 图片预加载（有素材时使用）=====
const imgs = {};
function loadImages(map, cb) {
  const keys = Object.keys(map);
  if (!keys.length) { cb(); return; }
  let loaded = 0;
  keys.forEach(k => {
    const img = new Image();
    img.onload = img.onerror = () => { if (++loaded === keys.length) cb(); };
    img.src = map[k];
    imgs[k] = img;
  });
}

// ===== 输入处理（触摸+键盘统一）=====
const keys = {};
const pointer = { x: 0, y: 0, down: false };
document.addEventListener('keydown', e => { keys[e.key] = true; });
document.addEventListener('keyup',   e => { keys[e.key] = false; });
canvas.addEventListener('touchstart', e => { e.preventDefault(); const t = e.touches[0]; const r = canvas.getBoundingClientRect(); pointer.x = t.clientX-r.left; pointer.y = t.clientY-r.top; pointer.down = true; }, {passive:false});
canvas.addEventListener('touchend',   e => { e.preventDefault(); pointer.down = false; }, {passive:false});
canvas.addEventListener('mousedown',  e => { const r = canvas.getBoundingClientRect(); pointer.x = e.clientX-r.left; pointer.y = e.clientY-r.top; pointer.down = true; });
canvas.addEventListener('mouseup',    () => { pointer.down = false; });

function resetGame() { score = 0; state = 'playing'; /* 初始化游戏对象 */ }

// ===== 主循环（用 deltaTime 保证帧率无关）=====
function loop(ts) {
  const dt = Math.min((ts - lastTime) / 1000, 0.05); // 最大0.05秒防卡顿
  lastTime = ts;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (state === 'start')   { drawStart(); }
  else if (state === 'playing') { update(dt); draw(); }
  else if (state === 'over')  { drawOver(); }
  requestAnimationFrame(loop);
}

// 点击/触摸切换状态
canvas.addEventListener('click', () => {
  if (state === 'start' || state === 'over') resetGame();
});

function drawStart() {
  ctx.fillStyle = 'rgba(0,0,0,0.7)'; ctx.fillRect(0,0,canvas.width,canvas.height);
  ctx.fillStyle='#fff'; ctx.font=`bold ${canvas.width*0.08}px Arial`; ctx.textAlign='center';
  ctx.fillText('游戏名', canvas.width/2, canvas.height*0.4);
  ctx.font=`${canvas.width*0.045}px Arial`;
  ctx.fillText('点击开始', canvas.width/2, canvas.height*0.6);
}
function drawOver() {
  ctx.fillStyle='rgba(0,0,0,0.75)'; ctx.fillRect(0,0,canvas.width,canvas.height);
  ctx.fillStyle='#f66'; ctx.font=`bold ${canvas.width*0.09}px Arial`; ctx.textAlign='center';
  ctx.fillText('游戏结束', canvas.width/2, canvas.height*0.38);
  ctx.fillStyle='#fff'; ctx.font=`${canvas.width*0.05}px Arial`;
  ctx.fillText('得分: '+score, canvas.width/2, canvas.height*0.52);
  ctx.fillText('点击重来', canvas.width/2, canvas.height*0.66);
}
function update(dt) { /* 用 dt 计算移动量，如 obj.x += speed * dt */ }
function draw()   { /* 绘制游戏对象 */ }

// 启动：先加载图片再进入 start 状态
loadImages({ /* player: '/assets/image/xxx.png' */ }, () => {
  state = 'start';
  requestAnimationFrame(loop);
});
</script>
</body>
</html>
```"""
    },
    {
        "title": "H5 触摸与键盘输入（含坐标转换）",
        "category": "input",
        "tags": ["touch", "keyboard", "mobile", "coordinate"],
        "content": """触摸坐标必须用 getBoundingClientRect() 转换，否则全屏时坐标会偏移：

```javascript
// 坐标转换工具函数
function getCanvasPos(clientX, clientY) {
  const r = canvas.getBoundingClientRect();
  return {
    x: (clientX - r.left) * (canvas.width / r.width),
    y: (clientY - r.top) * (canvas.height / r.height)
  };
}

// 统一输入状态
const pointer = { x: 0, y: 0, down: false, justDown: false };
canvas.addEventListener('touchstart', e => {
  e.preventDefault();
  const p = getCanvasPos(e.touches[0].clientX, e.touches[0].clientY);
  pointer.x = p.x; pointer.y = p.y; pointer.down = true; pointer.justDown = true;
}, {passive: false});
canvas.addEventListener('touchmove', e => {
  e.preventDefault();
  const p = getCanvasPos(e.touches[0].clientX, e.touches[0].clientY);
  pointer.x = p.x; pointer.y = p.y;
}, {passive: false});
canvas.addEventListener('touchend', e => { e.preventDefault(); pointer.down = false; }, {passive: false});
canvas.addEventListener('mousedown', e => {
  const p = getCanvasPos(e.clientX, e.clientY);
  pointer.x = p.x; pointer.y = p.y; pointer.down = true; pointer.justDown = true;
});
canvas.addEventListener('mousemove', e => {
  if (!pointer.down) return;
  const p = getCanvasPos(e.clientX, e.clientY);
  pointer.x = p.x; pointer.y = p.y;
});
canvas.addEventListener('mouseup', () => { pointer.down = false; });

// 每帧末尾重置 justDown
function update(dt) {
  // ... 游戏逻辑
  pointer.justDown = false; // 必须在帧末重置
}
```"""
    },
    {
        "title": "H5 碰撞检测（完整实现）",
        "category": "physics",
        "tags": ["collision", "AABB", "circle", "boundary"],
        "content": """完整碰撞检测实现，含边界检测和安全保护：

```javascript
// 矩形 AABB 碰撞（对象需有 x,y,w,h 属性）
function rectHit(a, b) {
  return a.x < b.x + b.w && a.x + a.w > b.x &&
         a.y < b.y + b.h && a.y + a.h > b.y;
}

// 圆形碰撞（对象需有 x,y,r 属性）
function circleHit(a, b) {
  const dx = a.x - b.x, dy = a.y - b.y;
  return dx*dx + dy*dy < (a.r + b.r) * (a.r + b.r);
}

// 点是否在矩形内（触摸检测按钮用）
function pointInRect(px, py, rect) {
  return px >= rect.x && px <= rect.x + rect.w &&
         py >= rect.y && py <= rect.y + rect.h;
}

// 边界约束（防止对象飞出屏幕）
function clampToBounds(obj) {
  obj.x = Math.max(obj.r || 0, Math.min(canvas.width  - (obj.r || obj.w || 0), obj.x));
  obj.y = Math.max(obj.r || 0, Math.min(canvas.height - (obj.r || obj.h || 0), obj.y));
}

// 移出屏幕的对象清理（防内存泄漏）
function cleanOffScreen(arr, margin) {
  return arr.filter(o =>
    o.x > -margin && o.x < canvas.width  + margin &&
    o.y > -margin && o.y < canvas.height + margin
  );
}
```"""
    },
    {
        "title": "H5 常见 Bug 预防规则",
        "category": "bugfix",
        "tags": ["bug", "safety", "protection", "nan", "ellipse"],
        "content": """生成代码时必须遵守的防 Bug 规则：

```javascript
// ❌ 错误：ellipse/arc 半径可能为负 → Uncaught IndexSizeError
ctx.ellipse(x, y, w/4 - i*5, h/3 - i*3, 0, 0, Math.PI*2);
// ✅ 正确：用 Math.max 保护最小值
ctx.ellipse(x, y, Math.max(1, w/4 - i*5), Math.max(1, h/3 - i*3), 0, 0, Math.PI*2);

// ❌ 错误：除法可能 NaN / Infinity
const speed = distance / time;
// ✅ 正确：除法前检查分母
const speed = time > 0 ? distance / time : 0;

// ❌ 错误：数组迭代中直接 splice 导致跳过元素
for (let i = 0; i < bullets.length; i++) {
  if (outOfBounds(bullets[i])) bullets.splice(i, 1);
}
// ✅ 正确：从后往前 splice，或用 filter
bullets = bullets.filter(b => !outOfBounds(b));

// ❌ 错误：图片未加载完就绘制 → 显示空白
const img = new Image(); img.src = 'xxx.png';
ctx.drawImage(img, x, y); // 可能还没加载好
// ✅ 正确：onload 回调或等全部加载完再启动
img.onload = () => { gameReady = true; };

// ❌ 错误：速度用固定像素，帧率不同速度不同
obj.x += 5; // 60fps=300px/s，30fps=150px/s
// ✅ 正确：用 deltaTime 乘以像素/秒速率
obj.x += speed * dt; // speed=300，任何帧率都是300px/s

// ❌ 错误：平台过短/间隙过大导致角色落空
// ✅ 正确：平台间隙 <= 角色最大跳跃距离，第一个平台紧接出发点

// ❌ 错误：随机数范围错误
const x = Math.random() * canvas.width + 100; // 可能超出右边界
// ✅ 正确
const x = Math.random() * (canvas.width - 100) + 50;
```"""
    },
    {
        "title": "H5 图片素材加载与使用",
        "category": "assets",
        "tags": ["image", "sprite", "drawImage", "preload"],
        "content": """有用户上传的素材时，必须使用图片而非绘制图形：

```javascript
// === 统一预加载所有图片 ===
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

// === 调用（URL 由 search_assets 工具返回）===
loadImages({
  player: '/assets/image/abc123.png',
  bg:     '/assets/image/def456.jpg',
}, () => {
  state = 'start';
  requestAnimationFrame(loop);
});

// === 绘制图片（3种方式）===
// 1. 原始尺寸
ctx.drawImage(imgs.player, x, y);
// 2. 指定宽高（拉伸/缩放）
ctx.drawImage(imgs.player, x, y, w, h);
// 3. 裁剪精灵图 (sx,sy=源裁剪起点, sw,sh=裁剪大小)
ctx.drawImage(imgs.sheet, sx, sy, sw, sh, x, y, w, h);

// === 绘制前检查图片是否加载完成 ===
if (imgs.player && imgs.player.complete) {
  ctx.drawImage(imgs.player, x, y, w, h);
} else {
  // 降级：用矩形代替
  ctx.fillStyle = '#4af'; ctx.fillRect(x, y, w, h);
}
```"""
    },
    {
        "title": "H5 游戏类型参考",
        "category": "template",
        "tags": ["flappy", "2048", "snake", "shooter", "runner"],
        "content": """常见H5游戏类型和关键设计要点：

跳跃(Flappy Bird): 重力+跳跃速度、管道从右向左移动、碰撞即死
跑酷: 角色原地跳跃、平台/障碍从右向左、难度随分数增加
贪吃蛇: 网格移动、方向键/滑动控制、吃食物增长
射击: 玩家上下移动、子弹向上飞、敌人从上落下、双端碰撞检测
接东西: 篮子左右移动、物品从上落下、接到得分失去漏掉
打地鼠: 随机位置弹出、限时点击、点击特效
2048: 4x4网格、方向键合并同值方块、生成新方块
俄罗斯方块: 7种形状、旋转、消行、加速

所有游戏共同要求:
- 必须有开始/游戏中/结束三个状态
- 难度随时间/分数动态增加
- 移动端触摸支持（滑动/点击/虚拟摇杆）
- 分数显示在左上角，最高分记录"""
    },
    {
        "title": "H5 移动端适配要点",
        "category": "mobile",
        "tags": ["viewport", "responsive", "dpr", "fullscreen"],
        "content": """移动端适配必须处理的关键点：

```javascript
// 1. viewport meta（HTML head 中必须有）
// <meta name="viewport" content="width=device-width,initial-scale=1.0,user-scalable=no">

// 2. 禁止默认手势（CSS）
// body { touch-action: none; overflow: hidden; }

// 3. 高分辨率屏幕适配（防止模糊）
function resize() {
  const dpr = window.devicePixelRatio || 1;
  const w = window.innerWidth, h = window.innerHeight;
  canvas.width  = w * dpr;
  canvas.height = h * dpr;
  canvas.style.width  = w + 'px';
  canvas.style.height = h + 'px';
  ctx.scale(dpr, dpr);
  // 重新计算游戏元素尺寸（基于 w/h，不是 canvas.width/height）
}

// 4. 虚拟按键（竖屏手机专用）
function drawJoystick() {
  const r = canvas.width * 0.08;
  const bx = canvas.width * 0.15, by = canvas.height * 0.85;
  ctx.globalAlpha = 0.4;
  ctx.fillStyle = '#fff';
  ctx.beginPath(); ctx.arc(bx, by, r, 0, Math.PI*2); ctx.fill();     // 左移
  ctx.beginPath(); ctx.arc(bx+r*2.5, by, r, 0, Math.PI*2); ctx.fill(); // 右移
  ctx.globalAlpha = 1;
}
```"""
    },
    {
        "title": "H5 启动顺序与黑屏防护",
        "category": "startup",
        "tags": ["startup", "init", "blackscreen", "order", "resize"],
        "content": """启动顺序是 H5 游戏黑屏的头号原因。必须严格遵守以下顺序：

```
正确顺序（从上到下）：
1. canvas/ctx 获取
2. resize() 定义 + window.addEventListener('resize', resize) + resize()
3. let/const 全局状态声明（player, state, enemies, score 等）
4. 工具函数定义（碰撞检测、绘图等）
5. update(dt) / draw() 定义
6. resetGame() 定义
7. 输入事件绑定（click/touch/keyboard）
8. loadImages({...}, () => { state='start'; requestAnimationFrame(loop); })
```

❌ 绝对禁止：
- resize() 调用在 let player 声明之前
- resetGame() 调用在 state 声明之前
- requestAnimationFrame(loop) 在图片加载完成之前
- init() 在全局变量声明之前

黑屏自检清单（生成代码后必须逐项检查）：
□ 有且仅有一个 requestAnimationFrame(loop)，在 loadImages 回调中
□ loop 函数调用了 ctx.clearRect(0,0,canvas.width,canvas.height)
□ state 初始值是 'loading' 或 'start'，不是 'playing'
□ drawStart() 函数存在且有可见绘制（fillText/fillRect）
□ canvas.addEventListener('click', ...) 在 resetGame 定义之后
□ resize 中使用 ctx.setTransform(dpr,0,0,dpr,0,0) 而非 ctx.scale(dpr,dpr)
□ 所有 let/const 变量在顶层 resize() 调用之后声明

resize 正确写法：
```javascript
function resize() {
  const dpr = window.devicePixelRatio || 1;
  const w = window.innerWidth, h = window.innerHeight;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  canvas.style.width = w + 'px';
  canvas.style.height = h + 'px';
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); // ✅ 不要用 ctx.scale
}
```"""
    },
    {
        "title": "H5 代码质量规则集",
        "category": "quality",
        "tags": ["quality", "rules", "safety", "validation", "platform"],
        "content": """生成 H5 游戏代码时必须遵守的完整质量规则：

## 状态机
- 必须有 state 变量，至少包含 'start' | 'playing' | 'over' 三种状态
- 主循环 loop(ts) 根据 state 分支：drawStart / update+draw / drawOver
- 点击/触摸事件切换状态，不要在 loop 里写 if(clicked)

## 帧率无关
- 所有移动必须用 deltaTime：obj.x += speed * dt
- dt = Math.min((ts - lastTime) / 1000, 0.05)，必须有 0.05 上限防卡顿跳帧
- 禁止 obj.x += 5 这种固定像素移动

## 数值安全
- ctx.ellipse / ctx.arc 的半径参数必须 Math.max(1, value)
- 除法前检查分母：time > 0 ? distance / time : 0
- Math.random 范围不要超出画布：Math.random() * (canvas.width - margin)

## 数组与内存
- 删除元素用 filter，不要在 for 循环中 splice
- 屏幕外对象及时清理：arr = arr.filter(o => o.y < canvas.height + margin)
- 粒子/子弹数组限制最大长度

## 触摸与输入
- 触摸坐标必须转换：getBoundingClientRect() + clientX/clientY 偏移
- 同时支持 touch + mouse + keyboard
- touchstart/touchmove 必须 e.preventDefault() + {passive: false}

## Canvas 绘制
- 每帧开头 ctx.clearRect(0, 0, canvas.width, canvas.height)
- 绘制顺序：背景 → 游戏对象 → UI/分数（后绘制的在上层）
- 文字用相对尺寸：ctx.font = `${canvas.width * 0.05}px Arial`

## 平台类游戏额外规则
- 重力加速度基于 dt：vy += gravity * dt; y += vy * dt
- 第一个平台必须在玩家脚下，间隙 ≤ 跳跃距离
- 落出屏幕底部 → game over
- 平台宽度 ≥ 玩家宽度 × 1.5"""
    },
]


def load_default_skills(kb):
   """将默认的 H5 游戏技能加载到知识库"""
   existing = kb.skills_collection.count()
   if existing > 0:
       return existing
   count = 0
   for skill in H5_GAME_SKILLS:
       kb.add_skill(
           title=skill["title"],
           content=skill["content"],
           category=skill["category"],
           tags=skill["tags"],
       )
       count += 1
   return count
