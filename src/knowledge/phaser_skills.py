"""H5 页面游戏开发技能文档 - 预加载到知识库"""

H5_GAME_SKILLS = [
    {
        "title": "H5 Canvas 游戏基础结构",
        "category": "base",
        "tags": ["canvas", "gameloop", "h5"],
        "content": """H5游戏核心: Canvas + requestAnimationFrame 游戏循环 + 事件监听。
模板: viewport meta适配移动端, touch-action:none 禁止缩放, canvas自适应屏幕尺寸。
resize时重设 canvas.width=innerWidth, canvas.height=innerHeight。"""
    },
    {
        "title": "H5 触摸与键盘输入",
        "category": "input",
        "tags": ["touch", "keyboard", "mobile"],
        "content": """同时支持触摸和键盘:
touchstart/touchmove/touchend + e.preventDefault()
mousedown/mousemove/mouseup
keydown/keyup 用 input.keys[e.key] 记录按键状态"""
    },
    {
        "title": "H5 Canvas 绘图技巧",
        "category": "drawing",
        "tags": ["canvas", "fillRect", "arc", "gradient"],
        "content": """Canvas 2D API:
fillRect画矩形, arc画圆, fillText写字, createLinearGradient渐变。
font设置字体, textAlign居中, 圆角矩形用arcTo实现。"""
    },
    {
        "title": "H5 碰撞检测",
        "category": "physics",
        "tags": ["collision", "AABB", "circle"],
        "content": """矩形碰撞AABB: a.x<b.x+b.w && a.x+a.w>b.x && a.y<b.y+b.h && a.y+a.h>b.y
圆形碰撞: sqrt(dx*dx+dy*dy) < a.r+b.r
点在矩形内: px>=rect.x && px<=rect.x+rect.w && py>=rect.y && py<=rect.y+rect.h"""
    },
    {
        "title": "H5 游戏开始/结束界面",
        "category": "ui",
        "tags": ["ui", "state", "start", "gameover"],
        "content": """state管理: 'start'|'playing'|'over'
开始界面: 半透明遮罩 + 游戏标题 + '点击开始'
结束界面: 半透明遮罩 + '游戏结束' + 分数 + '点击重来'
canvas.addEventListener('click') 切换状态"""
    },
    {
        "title": "H5 游戏类型参考",
        "category": "template",
        "tags": ["flappy", "2048", "snake", "shooter"],
        "content": """常见H5游戏类型:
跳跃(Flappy), 跑酷, 消除(2048/三消), 射击, 弹球,
接东西, 打地鼠, 贪吃蛇, 俄罗斯方块, 答题, 抽奖转盘, 刮刮卡, 记忆翻牌"""
    },
    {
        "title": "H5 移动端适配要点",
        "category": "mobile",
        "tags": ["viewport", "responsive", "dpr"],
        "content": """viewport meta: width=device-width,initial-scale=1.0,user-scalable=no
touch-action:none 防止浏览器手势
用百分比/vw/vh或canvas自适应做响应式
考虑安全区域(env safe-area-inset)"""
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
