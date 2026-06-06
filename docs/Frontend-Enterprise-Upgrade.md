# 前端企业级改造方案

## 现状分析

当前代码：
- HTML: 192 行
- CSS: 837 行  
- JS: 1073 行
- 总计: 2102 行

现有功能：
- ✅ AI 对话界面
- ✅ Monaco 代码编辑器
- ✅ 实时预览
- ✅ 素材管理
- ✅ 技能管理
- ✅ 项目保存/加载
- ✅ 对话历史

---

## 企业级改造目标

### 1. UI/UX 专业化 🎨

**配色方案**
- [ ] 实现深色/浅色主题切换
- [ ] 企业级配色（主色/辅色/语义色）
- [ ] 高对比度无障碍模式

**布局优化**
- [ ] 更专业的间距系统（8px grid）
- [ ] 响应式布局优化（移动端适配）
- [ ] 可调整面板尺寸（拖拽分割线）

**动画效果**
- [ ] 细腻的过渡动画（200ms ease-out）
- [ ] 加载状态骨架屏
- [ ] 微交互反馈（hover/active/focus）

---

### 2. 错误处理增强 🚨

**结构化错误显示**
```javascript
// 解析后端错误码 [ERROR:CODE] message
function parseError(message) {
    const match = message.match(/\[ERROR:(\w+)\]\s*(.+)/);
    return match ? { code: match[1], message: match[2] } : null;
}

// 不同错误类型不同处理
ERROR_HANDLERS = {
    'RATE_LIMIT_EXCEEDED': showRateLimitDialog,
    'INPUT_TOO_LARGE': showFileSizeTip,
    'CODE_SIZE_EXCEEDED': suggestOptimization,
}
```

**错误日志面板**
- [ ] 记录所有错误（时间、类型、消息、堆栈）
- [ ] 导出错误日志（支持）
- [ ] 错误统计（按类型分组）

---

### 3. 工具调用可视化 🔧

**进度指示器**
```javascript
// SSE 消息类型扩展
{
    "type": "tool_call",
    "tool": "str_replace_code",
    "args": "old_len=100, new_len=200"
}
→ 显示："正在修改代码..."

{
    "type": "tool_result",
    "tool": "str_replace_code",
    "result": "已替换第 10-15 行"
}
→ 显示："✅ 代码已更新（第 10-15 行）"
```

**工具调用时间线**
- [ ] 每次工具调用的时间轴
- [ ] 耗时统计（毫秒级）
- [ ] 成功/失败状态

---

### 4. 性能监控 📊

**Token 使用可视化**
- [x] 已有 context-meter（百分比）
- [ ] 添加数值显示（已用/总量）
- [ ] 历史趋势图（折线图）
- [ ] 成本估算（按 token 计费）

**响应时间监控**
- [ ] 每次对话的响应时间
- [ ] 工具调用耗时
- [ ] 平均响应时间

---

### 5. 会话管理增强 📁

**会话列表**
- [ ] 显示所有持久化会话
- [ ] 按修改时间排序
- [ ] 搜索/过滤会话
- [ ] 一键切换会话

**会话元数据**
- [ ] 会话名称（可编辑）
- [ ] 创建时间
- [ ] 代码行数
- [ ] 对话轮数

---

### 6. 代码编辑器增强 💻

**版本历史（Undo/Redo）**
- [ ] 版本列表（最近 10 个）
- [ ] 版本对比（diff 视图）
- [ ] 一键恢复到任意版本

**代码质量**
- [ ] 行数统计
- [ ] 文件大小显示
- [ ] 语法高亮优化

---

### 7. 快捷键支持 ⌨️

```
Ctrl+Enter    发送消息
Ctrl+N        新建项目
Ctrl+S        保存项目
Ctrl+Z        撤销代码修改
Ctrl+Y        重做代码修改
Ctrl+/        切换主题
Esc           关闭弹窗
```

---

### 8. 导出/分享 📤

**导出格式**
- [ ] 导出 HTML（单文件）
- [ ] 导出项目（ZIP：代码+素材+配置）
- [ ] 生成分享链接（临时访问）

**分享功能**
- [ ] 生成预览链接
- [ ] 二维码扫描手机预览
- [ ] 嵌入代码（iframe）

---

## 实施优先级

### 🔴 P0（立即实施，核心体验）

1. **错误处理可视化**（解析 ERROR:CODE，分类显示）
2. **工具调用进度**（显示"正在..."状态）
3. **深色主题**（企业应用标配）

### 🟠 P1（短期，1-2天）

4. **会话列表**（管理多个项目）
5. **响应时间显示**（性能感知）
6. **快捷键**（效率提升）

### 🟡 P2（中期，3-5天）

7. **版本历史/Undo**（需后端支持）
8. **主题切换**（浅色/深色/高对比度）
9. **导出/分享**

### 🟢 P3（长期优化）

10. **性能监控面板**
11. **错误日志导出**
12. **无障碍优化**

---

## 技术方案

### 深色主题实现
```css
:root {
    /* 企业级配色变量 */
    --color-primary: #3b82f6;
    --color-success: #10b981;
    --color-warning: #f59e0b;
    --color-error: #ef4444;
    
    /* 浅色主题 */
    --bg-primary: #ffffff;
    --bg-secondary: #f3f4f6;
    --text-primary: #111827;
}

[data-theme="dark"] {
    --bg-primary: #1f2937;
    --bg-secondary: #111827;
    --text-primary: #f9fafb;
}
```

### 错误处理
```javascript
class ErrorHandler {
    parse(message) {
        const match = message.match(/\[ERROR:(\w+)\]\s*(.+)/);
        if (!match) return { type: 'unknown', message };
        return { code: match[1], message: match[2] };
    }
    
    show(error) {
        const handler = this.handlers[error.code] || this.showDefault;
        handler(error);
    }
}
```

---

## 下一步行动

1. ✅ 完成改造方案
2. ⏳ 实施 P0（错误处理、工具进度、深色主题）
3. ⏳ 实施 P1（会话管理、快捷键）
4. ⏳ 根据反馈迭代 P2/P3

---

**预计工作量：P0 需要 2-3 小时，完整改造需要 1-2 周。**
