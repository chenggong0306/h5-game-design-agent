# 同类项目调研：AI 游戏生成平台可借鉴做法

> 2026-07-28 · 深度研究工作流产出：5 路检索角度 → 23 个来源 → 108 条候选结论 → 25 条经 3 票对抗验证（25 存活 / 0 被推翻）→ 合成 12 条发现，按投入产出比排序。

## 总结

调研确认：你的"生成→无头浏览器运行→自检→自动修复→质量探针"主链路已与行业最先进形态同构（OpenGame、阿里 V-GameGym、threejs-game-skills 均为同一模式），真正可借鉴的增量集中在四处——(1) 把修复成功经验持久化为可复用知识库（OpenGame Debug Skill 的 "living protocol of verified fixes"，你的修复循环唯一缺的一环）；(2) 直接收割生态中现成 SKILL.md 格式游戏技能内容（awesome-gamedev-agent-skills 66 技能、github/awesome-copilot 官方 HTML5 Canvas game-engine 技能、gstack-game 评审/QA 技能），格式与你的 MD 导入+语义召回天然兼容；(3) 用 ZzFX 系零素材程序化音频与 game-feel（juice）技能文档补齐单文件游戏的手感短板；(4) 用时序多帧截图+脚本化输入驱动+三轴评分（可运行/可玩/意图对齐）把质量探针形式化。商业产品中仅 Rosebud 的机制经受住三票验证（按职能拆多文件绕过约 2500 行生成上限+逐文件上下文开关），可内化为单文件平台的"虚拟模块化生成+修复时上下文掩码"。按投入产出比排序：修复知识库持久化与技能收割为最高杠杆（改动小、复利大）；程序化音频与 juice 技能化次之；评测三轴形式化、确定性测试钩子、分层技能装配为中期项；draw manifest 中间表示与"经验生长模板库"证据较弱，建议小流量 A/B 验证。findings 已按此优先级排列。

## 发现（按优先级）

### 1. P1（最高性价比）修复知识库持久化

**置信度**：high ｜ **验证票**：3-0（OpenGame Debug Skill）；3-0（Validator 重试循环）

P1（最高性价比）修复知识库持久化：OpenGame 的 Debug Skill 在沙箱运行游戏、捕获集成/console/交互错误并迭代修复后，把每次成功修复以结构化条目（错误签名+根因+已验证修复）写入 'living protocol' 知识库供后续任务复用，而非每次重新推导；sachiniiits/AI_Game_Engine 展示了同类循环的最简实现（Playwright 无头 Chromium 运行时校验：canvas 非黑像素、console 错误捕获、键鼠模拟，MAX_RETRIES=3 重试上限）。你的平台已有无头自检+自动修复+重试，唯一缺口就是持久化层。落地：在修复成功路径上追加一步，把 (错误签名→根因→修复策略/diff) 写入 MD/JSON 库并纳入技能召回，下次自检报错先查库命中再走 LLM 修复——对现有 verifier 是增量改动，收益随运行次数复利。

**证据**：OpenGame README 逐字：'Debug Skill that maintains a living protocol of verified fixes... runs the game in a sandbox, catches integration errors, console errors, and broken interactions'；arXiv 2604.18394 确认持久化机制：'Each time a failure occurs, the agent records a structured entry containing an error signature, a root cause, and a verified fix. These entries are added and reused in future tasks'。AI_Game_Engine 侧为代码级验证（pipeline.py 的 MAX_RETRIES=3 循环 + validator_agent.py 的 headless chromium 启动），但该仓库为 0-star 演示，仅作模式确认。

**来源**：<https://github.com/leigest519/OpenGame> ｜ <https://arxiv.org/abs/2604.18394> ｜ <https://github.com/sachiniiits/AI_Game_Engine>

### 2. P2 直接收割现成 MD 技能内容

**置信度**：high ｜ **验证票**：5 项 claim 全部 3-0

P2 直接收割现成 MD 技能内容：生态已有大量 SKILL.md（Markdown+YAML frontmatter）格式游戏技能可近零成本导入你的技能库——(a) gamedev-skills/awesome-gamedev-agent-skills：66 技能+master router，Apache-2.0，357 stars，分 5 引擎类（含 Web 引擎 6 个）+13 学科+9 品类模板+4 工作流；(b) github 官方 awesome-copilot 的 game-engine 技能：明确面向 HTML5 Canvas/WebGL/JS（游戏循环、碰撞检测、精灵、触控、tilemap、发布）；(c) majidmanzarpour/threejs-game-skills：9 技能，1.1k stars；(d) fagemx/gstack-game：27-29 个评审/QA 工作流技能（/game-review、/feel-pass、/game-qa），明确非代码生成器，价值在可移植的评审文档内容；(e) anthropics/skills：无游戏专项但有 algorithmic-art、webapp-testing、theme-factory 等相邻技能。Anthropic 官方格式规范仅要求 frontmatter 两字段 name+description，description 字段（做什么+何时用）天然适配你的描述语义召回。落地：写一个收割脚本解析 SKILL.md frontmatter 转平台技能格式；注意整技能文件夹（含 references/）一起搬而非只拿单文件；Godot/Three.js 味示例需轻量翻译成 Canvas JS。

**证据**：全部主源逐字核实：awesome-gamedev README '66 original, version-pinned skills (plus a master router) in the portable SKILL.md format'，LICENSE 实测 Apache-2.0，8 个技能目录树实测存在；awesome-copilot game-engine frontmatter 逐字 'building web-based game engines and games using HTML5, Canvas, WebGL, and JavaScript'（github 官方 org）；threejs-game-skills 经 GitHub API git/trees 逐文件核实 9/9 含 SKILL.md；gstack-game 经 GitHub API+README+生成器源码三路核实（27 vs 29 数字在其自述中即不一致，故取区间）；anthropics/skills 实测 17 技能、grep 全部 description 零 'game' 命中。Anthropic 规范 'The frontmatter requires only two fields: name...description'。

**来源**：<https://github.com/gamedev-skills/awesome-gamedev-agent-skills> ｜ <https://github.com/github/awesome-copilot/blob/main/skills/game-engine/SKILL.md> ｜ <https://github.com/majidmanzarpour/threejs-game-skills> ｜ <https://github.com/fagemx/gstack-game> ｜ <https://github.com/anthropics/skills>

### 3. P3 零素材程序化音频固化进音频引擎技能

**置信度**：high ｜ **验证票**：3-0

P3 零素材程序化音频固化进音频引擎技能：js13kGames 官方资源清单固化了'代码即音频'方案——ZzFX（音效合成，一行调用如 zzfx(...[,,,,.1,,,,9])，压缩后 <1KB，'No sound asset files'）、ZzFXM（音乐渲染器，播放器 <500B gzip，歌曲即嵌入 JS 的嵌套数组）、jsfxr、TinyMusic（Web Audio 合成/音序器），全部 MIT、零外部音频文件，与你的单文件 H5 约束完美匹配。落地：把 ZzFX 内联源码+常用音效参数速查表（跳跃/爆炸/拾取/受击/胜利）+ZzFXM 简单曲式模板写进现有音频引擎技能，生成时直接内联到游戏文件——这是所有发现中改动最小、玩家可感知提升最大的一项。

**证据**：js13kGames 官方 org 仓库 Sound-and-music 节逐字列出四工具及描述；ZzFX 自家 README 确认一行调用、<1KB、MIT、无音频资产文件；ZzFXM 作者文档确认播放器 <500B gzip、歌曲为直接嵌入代码的 JS 数组。唯一细微点：ZzFXM 依赖 zzfx.js，但那是可内联的 JS 而非音频资产，单文件构建下'零外部素材'成立。

**来源**：<https://github.com/js13kGames/resources> ｜ <https://github.com/KilledByAPixel/ZzFX> ｜ <https://github.com/keithclark/ZzFXM>

### 4. P4 game juice 方法论技能化

**置信度**：high ｜ **验证票**：3-0

P4 game juice 方法论技能化：awesome-gamedev-agent-skills 的 game-feel 学科技能已把 juice 方法论写成可抄的 SKILL.md——衰减 trauma 值的屏幕震动、真实时间计时器实现的 hit-stop/冻帧、tween/easing 过冲、squash & stretch、小/中/大三档反馈捆绑（feedback tiers）；配套 audio-design 技能覆盖总线/混音架构（dB 增益）、ducking（侧链）、分层与重排序的自适应音乐、SFX 变化、节拍同步。这正是研究问题第 4 项想固化的内容，且已是 MD 格式。落地：两技能整体导入后，把 Godot 味 API（TRANS_BACK、linear_to_db、bus tree）翻译为 Canvas rAF + WebAudio JS 片段，挂在'打击感/手感/juice'类语义触发词上做强注入；与 P3 的 ZzFX 组合即三档反馈的音频层。

**证据**：两个 SKILL.md 原始文件直接抓取核实：game-feel description 'screen shake, hit-stop/freeze frames, tweened/eased motion, squash & stretch, knockback, and layered audio-visual feedback'，正文含衰减 trauma 震动与三档反馈捆绑；audio-design description 'bus/mixer architecture and gain in decibels, ducking (sidechain), adaptive/dynamic music via layering and re-sequencing, SFX variation'。示例为 Godot 风味是唯一需注意点，技术概念本身引擎无关。

**来源**：<https://github.com/gamedev-skills/awesome-gamedev-agent-skills> ｜ <https://raw.githubusercontent.com/gamedev-skills/awesome-gamedev-agent-skills/main/skills/disciplines/game-feel/SKILL.md> ｜ <https://raw.githubusercontent.com/gamedev-skills/awesome-gamedev-agent-skills/main/skills/disciplines/audio-design/SKILL.md>

### 5. P5 执行级评测形式化（时序视觉证据+三轴评分）

**置信度**：high ｜ **验证票**：3-0（OpenGame-Bench）；3-0（V-GameGym 流水线）；2-1（三轴 rubric）；3-0（视觉证据采集）

P5 执行级评测形式化（时序视觉证据+三轴评分）：两个独立项目证明行业共识是运行时评测而非静态分析——OpenGame-Bench 无头浏览器启动游戏→脚本化交互驱动→VLM 评审，按 Build Health（编译/加载/运行稳定）、Visual Usability（像素启发式+VLM）、Intent Alignment（逐需求 VLM 判定）三轴打分，150 prompt 自称 SOTA；阿里 V-GameGym（ACL 2026 Findings，2219 样本）四阶段全自动流水线：生成→沙箱执行→评分（functionality/playability/execution 三轴）→连续截图合成 gameplay 视频作证据。对你的质量探针的可借鉴增量：(a) 时序多帧截图 diff 检测冻结游戏（你现有 _is_blank_png 只测单帧空白，测不出'画面正常但死机'）；(b) 脚本化输入驱动后再截图（点击/按键→状态应变化）；(c) 把探针输出形式化为三轴分数，意图对齐轴可用 DeepSeek 文本判审+代码/截图摘要近似替代 VLM。落地顺序：先加帧 diff（几行代码），再加输入驱动，最后上评分轴。

**证据**：OpenGame README 逐字 'dynamically launches generated games, drives them with scripted interactions'+三轴名；arXiv 给出各轴定义。V-GameGym README 逐字 'complete pipeline for automatic game generation, execution, evaluation, and gameplay recording' 与 'Built-in scoring metrics for functionality, playability, and execution'，game_evaluator.py/screenshot_recorder.py 代码可查，ACL 2026 Findings 收录。注意：V-GameGym 三轴 claim 为 2-1 分歧票——轴名 README 逐字属实，但无公开逐轴评分标准表（'具体 rubric'指轴名+可审查评分代码）；且其为 Pygame 基准而非 H5 产品，借鉴的是模式而非代码。SOTA 为 OpenGame 自称。

**来源**：<https://github.com/leigest519/OpenGame> ｜ <https://arxiv.org/abs/2604.18394> ｜ <https://github.com/alibaba/SKYLENAGE-GameCodeGym> ｜ <https://arxiv.org/abs/2509.20136>

### 6. P6 生成的游戏自带确定性测试钩子+种子 RNG

**置信度**：high ｜ **验证票**：3-0

P6 生成的游戏自带确定性测试钩子+种子 RNG：threejs-game-skills（1.1k stars，2026-06 发布仍活跃维护）让生成产物自带可测性——确定性测试钩子（__THREE_GAME_TEST_HOOKS__ 配 --seed n，使 active-play/fail/stress 等命名状态可确定性复现测量）、种子 RNG、Playwright 冒烟/视觉回归模板，其 QA 技能执行非空 canvas 像素检查+桌面/移动双视口截图+触控目标检查。验证者实测确认：你的 verifier.py 已有像素探针（同类机制），但你的 src 中无种子 RNG——这是真实缺口。落地：在生成 prompt 中约定输出 window.__GAME_TEST_HOOKS__ = {seed(n), setState(name), getState()} 接口，自检脚本据此把游戏驱动到指定状态再做像素/逻辑断言，修复后回归可精确复现（当前随机性会让'修复验证'不可靠）。

**证据**：README 逐字两处：'Generated games ship with deterministic test hooks, a seeded RNG, and Playwright templates' 与 'canvas-pixel checks, mobile viewport checks'；qa-release SKILL.md 本体指示 'Verify nonblank canvas pixels'、驱动 __THREE_GAME_TEST_HOOKS__ 配 --seed 使命名状态可确定性测量；最新 commit（2026-07-16）为修复 headless Playwright SwiftShader 渲染，证明是活代码。验证者对照了 C:\xiangmu\game_design\src\agent\verifier.py（_is_blank_png 等已存在）并 grep 确认种子 RNG 缺失。脚手架为 Three.js/Vite/TS，模板需适配单文件 2D Canvas。

**来源**：<https://github.com/majidmanzarpour/threejs-game-skills> ｜ <https://raw.githubusercontent.com/majidmanzarpour/threejs-game-skills/main/skills/threejs-qa-release/SKILL.md>

### 7. P7 分层技能装配顺序（在语义召回之上加确定性分层）

**置信度**：high ｜ **验证票**：3-0；3-0

P7 分层技能装配顺序（在语义召回之上加确定性分层）：两个技能包展示同一路由模式——awesome-gamedev-agent-skills 的 master router 本身是技能，每次请求做三件事：从项目文件指纹检测引擎（project.godot→Godot，package.json deps→Phaser/three.js）→解析任务→按固定分层顺序只加载匹配技能（引擎基础→学科概念→品类粘合→工作流）；threejs-game-skills 用 director 编排技能把完整构建自动路由给 9 个专家技能而无需用户手选。你已有语义召回+强注入三层，缺的是召回命中后的确定性装配顺序。落地：给每个技能加 layer 元数据（base 引擎基础/mechanic 机制/genre 品类/workflow 流程），召回结果先按层排序再按语义分注入 prompt，保证引擎基础永远先于品类模板出现——低成本改动，直接提升多技能命中时的注入质量。

**证据**：README 逐字 'Loads only the matching skills, in order: engine basics first, then the concept, then any genre glue'；router/SKILL.md 实测存在，文档化引擎指纹表与完整四层顺序 'engine fundamentals → discipline concept → genre orchestration → workflow'。threejs 包 README 逐字 'the director routes gameplay, graphics, UI, asset generation, audio, debugging, and release verification without requiring users to choose every specialist skill manually'。注意：这些 router 是确定性指纹/表分派而非语义召回，借鉴的是'分层顺序'而非检索方式。

**来源**：<https://github.com/gamedev-skills/awesome-gamedev-agent-skills> ｜ <https://raw.githubusercontent.com/gamedev-skills/awesome-gamedev-agent-skills/main/router/SKILL.md> ｜ <https://github.com/majidmanzarpour/threejs-game-skills>

### 8. P8 渐进披露技能结构防上下文膨胀

**置信度**：high ｜ **验证票**：3-0；3-0

P8 渐进披露技能结构防上下文膨胀：github 官方 awesome-copilot 的 game-engine 技能示范了紧凑 SKILL.md 路由（约 350-400 行）+9 个主题参考文件（basics/web-apis/techniques/algorithms/game-control-mechanisms/3d-web-games/game-publishing/terminology/core-principles）的结构，射线投射、碰撞数学、tilemap 等深内容仅在需要时按需加载；Anthropic 官方 Agent Skills 规范（frontmatter 仅 name+description 必填）即为此渐进披露设计。落地：把你的内置大技能拆为'短正文（召回时注入）+references/ 子文档（正文内列索引，agent 需要时二跳读取）'——直接对治你此前审计中确认过的 agent 上下文膨胀痛点，与内置技能单存储分层改造同方向。

**证据**：raw 文件核实 SKILL.md 含参考索引表 'Detailed reference material is available in the references/ folder. Consult these files for in-depth coverage of specific topics'，目录列表实测 9 个参考文件恰好齐全（algorithms.md=Raycasting/collision/physics/vector math 等）；anthropics/skills README 逐字确认两字段规范。github 官方 org 仓库，2026-07-28 抓取 main 分支。

**来源**：<https://github.com/github/awesome-copilot/blob/main/skills/game-engine/SKILL.md> ｜ <https://github.com/anthropics/skills>

### 9. P9（建议 A/B 试验）生成前中间表示——draw manifest

**置信度**：medium ｜ **验证票**：3-0（但单一弱来源）

P9（建议 A/B 试验）生成前中间表示——draw manifest：sachiniiits/AI_Game_Engine 在设计与编码之间插入 Logic Agent 阶段，先产出伪代码+visual draw manifest（逐元素 Name/Draw method/Shape/Colours/Size/Position/Animation，强制'无外部图片、全部用图形 API 绘制'），Coding Agent 再对着清单写 JS——机制在 pipeline.py 中真实实现（pseudocode, draw_manifest = logic_agent.run(...) 先于任何 JS 存在并注入编码 prompt）。对你的 Canvas 单文件生成高度适配：清单既约束 LLM 的空间/视觉推理，又可作为质量探针的可比对规格（探针检查'清单里的元素是否真的画出来了'）。落地：LangGraph 设计节点后加'绘制清单'节点，产物注入编码节点并存档供自检比对；因源仓库无社区效果证据，建议小流量 A/B 对比有无该阶段的一次通过率。

**证据**：三层核实：README 管线顺序 Research→Script→Logic→Coding→Validator；pipeline.py 代码确认 IR 在 JS 之前产出并被 coding_agent 消费；logic_agent.py prompt 要求逐元素属性且 'NO external images — everything is drawn with Phaser Graphics API'。降为 medium 的原因：0-star/0-fork/8-commit 单一来源演示仓库，机制属实但零生产/社区效果证据，验证者明确建议按'模式确认'而非'已证实践'对待；且其目标是 Phaser 而非裸 Canvas（图形 API 绘制清单可直接映射）。

**来源**：<https://github.com/sachiniiits/AI_Game_Engine> ｜ <https://raw.githubusercontent.com/sachiniiits/AI_Game_Engine/main/pipeline.py>

### 10. P10（中期方向）模板库随经验生长

**置信度**：medium ｜ **验证票**：3-0（框架）；2-1（经验生长机制）

P10（中期方向）模板库随经验生长：OpenGame（CUHK MMLab，Apache-2.0，2.8k stars，扩展 qwen-code 运行时，目标 canvas/Phaser/three.js，prompt→端到端可玩网页游戏）的 Template Skill 按请求选引擎/模板并脚手架稳定项目结构，且模板库非静态——'grows a library of project skeletons from experience'，从跨运行积累中生长新骨架。落地：当一次生成通过全部自检且用户确认满意时，自动把该局的骨架（品类+机制组合+结构）蒸馏为新品类模板技能并自动生成 description 供语义召回——与你记忆中'导入时自动生成描述'的既定方向同构，等于把 faithful_port 的导入路径扩展成'自产游戏也回流成模板'。

**证据**：框架存在与 Template Skill 选型/脚手架为 3-0（README 逐字 'picks an appropriate engine/template (canvas, Phaser, three.js, etc.) and scaffolds a stable, conventional project structure'）；'经验生长'部分为 2-1 分歧票——README 与 arXiv 摘要两份主文档独立确认该设计（'grows a library of project skeletons from experience'），仓库为真实 TypeScript 实现而非论文占位，但生长机制的具体代码路径未被逐行追踪，故按规则降为 medium。

**来源**：<https://github.com/leigest519/OpenGame> ｜ <https://arxiv.org/abs/2604.18394>

### 11. P11（游戏变大后再做）虚拟模块化生成+修复时上下文掩码

**置信度**：high ｜ **验证票**：3-0；3-0

P11（游戏变大后再做）虚拟模块化生成+修复时上下文掩码：商业产品 Rosebud AI 用两个机制绕过单次约 2500 行的 LLM 代码生成上限——按职能自动拆多文件（主循环/UI/环境效果/关卡/载具，'like a real dev would'），以及用户可逐文件切出 AI 上下文（紫色图标 toggle）为 prompt/更新腾容量。你坚持单文件交付与其表面冲突，但可内化：生成时按模块（loop/render/input/audio/levels）分段生成再拼装为单文件；修复迭代时只把出错模块源码+其余模块的接口摘要放入上下文（即 Rosebud 文件开关的自动化版）。当前你的游戏若普遍 <1500 行则不急，属于规模到达后的必需品。

**证据**：官方博客（2025-04-14）逐字：'Most AI tools cap out around 2,500 lines of code'、'Rosebud now automatically splits your project into multiple files — like a real dev would'、'Click the purple icon next to those files, to hide them from Rosie's view. That means more room for prompts, updates, and new features'；多文件能力另有 3 篇官方配套文档佐证非一次性营销。注意：2500 行是厂商对 token/上下文限制的营销化表述而非行业常数；来源为厂商自述（对'厂商自己怎么做'这类 claim 是恰当主源）。websim.ai/Bitmagic 无 claim 存活，商业产品部分仅此一家有效。

**来源**：<https://lab.rosebud.ai/blog/how-to-overcome-ai-code-limitations>

### 12. P12（可选）微引擎架构嵌入/模仿

**置信度**：high ｜ **验证票**：3-0

P12（可选）微引擎架构嵌入/模仿：js13k 社区有 MIT 微引擎提供已验证的紧凑架构——Kontra.js（官方描述'optimized for js13kGames'，文档化 API 面恰好是 GameLoop/Sprite/Pool（对象池）/Keyboard/Pointer/Gamepad）与 LittleJS（4.2k stars、2847 commits 活跃维护，WebGL2+Canvas2D 混合渲染，'Great for size coding competitions'）。落地两种姿势：(a) 低成本——把 Kontra 的对象池模式、循环结构、输入抽象提炼成'引擎骨架'技能文档让 DeepSeek 模仿生成（比自由发挥结构更稳，与你现有粒子技能互补）；(b) 高成本——对性能敏感品类内联精简版 Kontra（MIT 允许）。与 P2 收割和现有技能有部分重叠，故列末位。

**证据**：js13kGames 官方 org 资源清单逐字描述两引擎；Kontra 自家仓库与 API 文档确认模块清单（含 'a fast and memory efficient object pool for sprite reuse'）；LittleJS 仓库确认 WebGL2+Canvas2D 混合渲染与尺寸竞赛定位。两者均 MIT，嵌入/模仿法律与技术上均可行。

**来源**：<https://github.com/js13kGames/resources> ｜ <https://github.com/straker/kontra> ｜ <https://github.com/KilledByAPixel/LittleJS>

## 注意事项与已知空白

1) 商业产品覆盖严重不完整：研究问题第 1 项点名的产品中仅 Rosebud 的机制通过三票验证，websim.ai、Bitmagic 及其他 prompt-to-game 产品没有任何 claim 存活——商业管线本就少公开，且 Rosebud 数据全部来自其官方博客（自述性质，"2500 行"是营销化表述而非实测行业常数）。2) 两处 2-1 分歧票已在对应 finding 降级或标注：OpenGame"模板库经验生长"仅有 README+arXiv 设计文档支撑、代码路径未逐行追踪；V-GameGym"三轴 rubric"实为轴名+可审查评分代码，无公开逐轴评分标准表。3) draw manifest 机制来自 0-star/0-fork 演示仓库，代码属实但零社区/生产效果证据，只能当模式确认，落地前应 A/B。4) 引擎错配普遍存在：game-feel/audio 技能示例为 Godot 风味、threejs-game-skills 为 Three.js/Vite/TS、V-GameGym 为 Pygame——技术概念引擎无关，但代码示例导入前都需翻译成 Canvas/WebAudio 单文件 JS。5) 时效性：OpenGame（2026-04）与 threejs-game-skills（2026-06）都非常新，仓库结构可能快速变动；OpenGame-Bench 评测代码"即将发布"尚未公开；全部来源核查时间为 2026-07-28。6) 许可合规：可收割内容均为 Apache-2.0/MIT，导入平台内置技能库需保留许可与出处声明。7) 明显空白：AI 生图/2D 精灵素材方案（研究问题第 1 项的"素材/美术方案"半边）几乎没有存活 claim，本报告在该维度回答不足。

## 待研究问题

- websim.ai、Bitmagic 等其余商业产品的生成管线与素材方案到底怎么做的？本轮无 claim 存活，值得换方法二次调研（实际注册试用+浏览器抓包观察其生成/修复行为，而非搜公开资料）。
- OpenGame '经验生长模板库'的具体实现细节（骨架如何蒸馏、去重、检索命中）——需等其代码可深挖或 OpenGame-Bench 发布后复核，这直接决定 P10 的落地设计。
- 单文件 H5 的美术上限问题未解：程序化图形 API 绘制（draw manifest 路线）vs 内联 base64 AI 生图 vs 程序化像素画算法，三条路线的质量/体积/生成稳定性对比缺乏证据，值得专项调研。
- VLM 评审的本机可行性：你的栈是 DeepSeek 文本模型，Intent Alignment/Visual Usability 判审改用 DeepSeek-VL 本地部署、或截图转文本描述后由文本模型判审，成本与精度均未验证。

## 验证统计

```json
{
  "angles": 5,
  "sourcesFetched": 23,
  "claimsExtracted": 108,
  "claimsVerified": 25,
  "confirmed": 25,
  "killed": 0,
  "unverified": 0,
  "afterSynthesis": 12,
  "urlDupes": 0,
  "budgetDropped": 7,
  "agentCalls": 105
}
```