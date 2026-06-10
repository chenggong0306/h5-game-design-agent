/**
 * AI 游戏设计工坊 - 前端主逻辑
 */

// ============ 企业级错误处理模块 ============
class ErrorHandler {
    constructor() {
        this.errorLog = []; // 错误日志
        this.maxLogSize = 100; // 最多保留 100 条
    }

    /**
     * 解析后端结构化错误
     * @param {string} message - 错误消息
     * @returns {{code: string, message: string, isError: boolean}}
     */
    parse(message) {
        const match = String(message || '').match(/\[ERROR:(\w+)\]\s*(.+)/);
        if (match) {
            return {
                code: match[1],
                message: match[2],
                isError: true
            };
        }
        return { code: null, message, isError: false };
    }

    /**
     * 记录错误到日志
     */
    log(error) {
        this.errorLog.push({
            timestamp: new Date().toISOString(),
            code: error.code,
            message: error.message,
        });
        if (this.errorLog.length > this.maxLogSize) {
            this.errorLog.shift();
        }
    }

    /**
     * 显示错误消息（根据错误码分类处理）
     */
    show(error) {
        this.log(error);

        const handlers = {
            'RATE_LIMIT_EXCEEDED': () => this.showRateLimitError(error),
            'INPUT_TOO_LARGE': () => this.showInputTooLargeError(error),
            'CODE_SIZE_EXCEEDED': () => this.showCodeSizeError(error),
            'INVALID_PARAMS': () => this.showParamError(error),
            'APPEND_TO_EMPTY': () => this.showAppendError(error),
        };

        const handler = handlers[error.code] || (() => this.showDefaultError(error));
        handler();
    }

    showRateLimitError(error) {
        this.showErrorBubble('⏱️ 操作频率过快', error.message + '\n\n请稍等片刻再试。', 'warning');
    }

    showInputTooLargeError(error) {
        this.showErrorBubble('📦 内容过大', error.message + '\n\n建议：\n• 分段输入代码\n• 优化代码长度', 'warning');
    }

    showCodeSizeError(error) {
        this.showErrorBubble('💾 代码文件过大', error.message + '\n\n建议：\n• 删除不必要的注释\n• 精简代码逻辑', 'warning');
    }

    showParamError(error) {
        this.showErrorBubble('⚠️ 参数错误', error.message, 'error');
    }

    showAppendError(error) {
        this.showErrorBubble('📝 续写失败', error.message, 'error');
    }

    showDefaultError(error) {
        this.showErrorBubble('❌ 错误', error.message, 'error');
    }

    /**
     * 在聊天界面显示错误气泡
     */
    showErrorBubble(title, message, level = 'error') {
        const div = document.createElement('div');
        div.className = `msg error error-${level}`;
        div.innerHTML = `
            <div class="msg-content">
                <div class="error-title">${escapeHtml(title)}</div>
                <div class="error-message">${escapeHtml(message).replace(/\n/g, '<br>')}</div>
            </div>
        `;
        chatMessages.appendChild(div);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    /**
     * 导出错误日志
     */
    export() {
        return JSON.stringify(this.errorLog, null, 2);
    }
}

const errorHandler = new ErrorHandler();

/**
 * 校验 fetch 响应状态：非 2xx 时抛出带后端 detail 的错误。
 * 修复"错误响应被当成功 JSON 解析、误报成功/列表错乱"的问题。
 */
async function ensureOk(res) {
    if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
            const d = await res.json();
            if (d && d.detail) detail = Array.isArray(d.detail) ? JSON.stringify(d.detail) : d.detail;
        } catch (_) { /* 非 JSON 错误体，沿用状态码 */ }
        throw new Error(detail);
    }
    return res;
}

let editor = null;       // Monaco Editor 实例
let sessionId = localStorage.getItem('gameDesignSessionId') || '';       // 当前会话 ID
let currentProjectId = ''; // 当前项目 ID
let pendingLatestCode = null; // Monaco 尚未初始化时，临时保存要恢复的历史代码
let codeDirty = false;        // 用户是否在编辑器里手动改过代码（随请求上报，服务端据此决定是否采用 current_code）
let isApplyingCode = false;   // 程序化写入编辑器时置位，避免把它误判为用户编辑

// 程序化写入编辑器（服务器代码 / 模板 / 加载项目），写入后清除 dirty 标记
function applyEditorCode(code) {
    if (!editor) { pendingLatestCode = code; return; }
    isApplyingCode = true;
    editor.setValue(code || '');
    isApplyingCode = false;
    codeDirty = false;
}


// ============ 初始化 Monaco Editor ============
require.config({ paths: { vs: 'https://cdn.jsdelivr.net/npm/monaco-editor@0.45.0/min/vs' } });
require(['vs/editor/editor.main'], function () {
    editor = monaco.editor.create(document.getElementById('editor-container'), {
        value: '<!-- 在左侧对话框中描述你想要的游戏，AI 将在此生成代码 -->\n',
        language: 'html',
        theme: 'vs-dark',
        fontSize: 13,
        minimap: { enabled: false },
        wordWrap: 'on',
        automaticLayout: true,
        tabSize: 2,
    });

    // 区分用户手改与程序化写入：只有非程序化写入才标记为脏
    editor.onDidChangeModelContent(() => {
        if (!isApplyingCode) codeDirty = true;
    });

    if (pendingLatestCode !== null) {
        restoreEditorCode(pendingLatestCode);
        pendingLatestCode = null;
    }
});


// ============ 对话功能 ============
const chatMessages = document.getElementById('chat-messages');
const chatInput = document.getElementById('chat-input');
const contextMeter = document.getElementById('context-meter');
const contextPercent = document.getElementById('context-percent');
const contextRing = document.getElementById('context-ring');
let selectedImages = [];
const imageAttachments = document.getElementById('image-attachments');
const MAX_CHAT_IMAGE_BYTES = 10 * 1024 * 1024;

let activeStreamState = null;


let currentStreamController = null;
let isStreaming = false;

function formatBytes(bytes) {
    const size = Number(bytes || 0);
    if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)}MB`;
    if (size >= 1024) return `${Math.round(size / 1024)}KB`;
    return `${size}B`;
}

function addMessage(content, role = 'ai') {
    const div = document.createElement('div');
    div.className = `msg ${role}`;
    // 先转义再套 markdown，杜绝聊天内容（含历史回放）里的 HTML/脚本注入（XSS）
    let html = escapeHtml(content || '')
        .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>')
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/\n/g, '<br>');
    div.innerHTML = `<div class="msg-content">${html}</div>`;
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
    return div;
}

function renderImageAttachments() {
    if (!imageAttachments) return;
    if (!selectedImages.length) {
        imageAttachments.classList.add('hidden');
        imageAttachments.innerHTML = '';
        return;
    }
    imageAttachments.classList.remove('hidden');
    imageAttachments.innerHTML = selectedImages.map((image, index) => `
        <div class="image-attachment-card">
            <img src="${escapeHtml(image.data_url)}" alt="${escapeHtml(image.name)}">
            <div class="image-attachment-info">
                <strong>${escapeHtml(image.name)}</strong>
                <span>${escapeHtml(formatBytes(image.size))}</span>
            </div>
            <button type="button" onclick="removeSelectedImage(${index})" title="移除图片">×</button>
        </div>
    `).join('');
}

function readImageFile(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve({
            name: file.name || '粘贴图片.png',
            type: file.type || 'image/png',
            size: file.size,
            data_url: reader.result,
        });
        reader.onerror = () => reject(new Error(`读取图片失败: ${file.name || '未知'}`));
        reader.readAsDataURL(file);
    });
}

window.removeSelectedImage = function(index) {
    selectedImages.splice(index, 1);
    renderImageAttachments();
};

async function addSelectedImageFiles(files) {
    for (const file of files) {
        if (!file.type.startsWith('image/')) {
            alert(`❌ ${file.name} 不是图片文件`);
            continue;
        }
        if (file.size > MAX_CHAT_IMAGE_BYTES) {
            alert(`❌ ${file.name} 超过 10MB 限制`);
            continue;
        }
        if (selectedImages.length >= 4) {
            alert('❌ 一次最多发送 4 张图片');
            break;
        }
        selectedImages.push(await readImageFile(file));
    }
    renderImageAttachments();
}

function renderMessageImages(images = []) {
    if (!images.length) return '';
    return `<div class="message-image-list">${images.map(image => `
        <div class="message-image-card">
            <img src="${escapeHtml(image.data_url || '')}" alt="${escapeHtml(image.name || 'image')}">
            <span>${escapeHtml(image.name || 'image')} · ${escapeHtml(formatBytes(image.size))}</span>
        </div>
    `).join('')}</div>`;
}

function addUserMessage(content, images = []) {
    const div = document.createElement('div');
    div.className = 'msg user';
    const html = escapeHtml(content || '请看这张图片')
        .replace(/\n/g, '<br>') + renderMessageImages(images);
    div.innerHTML = `<div class="msg-content">${html}</div>`;
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
    return div;
}

// 创建一个空的 AI 消息气泡（用于流式填充）
function createStreamBubble() {
    const div = document.createElement('div');
    div.className = 'msg ai';
    div.innerHTML = '<div class="msg-content"><span class="stream-cursor">▊</span></div>';
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
    return div.querySelector('.msg-content');
}

// 将原始文本渲染为 HTML（简单 markdown）。先转义，防止 XSS。
function renderMarkdown(text) {
    return escapeHtml(String(text ?? '').trim())
        .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>')
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/\n/g, '<br>');
}

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

// 在 HTML 属性内的 JS 字符串字面量中安全使用（先 JS 转义反斜杠/单引号，再 HTML 转义）
function jsStr(value) {
    return escapeHtml(String(value ?? '').replace(/\\/g, '\\\\').replace(/'/g, "\\'"));
}

function getToolMeta(tool) {
    const map = {
        search_assets: ['🔎', '搜索素材'],
        load_skill: ['📚', '加载技能'],
        search_code: ['⌕', '搜索代码'],
        view_code: ['👁', '查看代码'],
        write_game: ['✍️', '写入游戏'],
        append_game: ['➿', '续写游戏'],
        replace_code: ['✏️', '替换代码'],
        insert_code: ['➕', '插入代码'],
        delete_code: ['🗑️', '删除代码'],
    };
    return map[tool] || ['🔧', tool || '工具调用'];
}

function formatToolPayload(value) {
    if (!value) return '无';
    try {
        const parsed = typeof value === 'string' ? JSON.parse(value.replace(/'/g, '"')) : value;
        return JSON.stringify(parsed, null, 2);
    } catch (_) {
        return String(value);
    }
}

function isToolErrorResult(result) {
    const text = String(result || '');
    return text.includes('Error invoking tool')
        || text.includes('Field required')
        || text.includes('写入失败')
        || text.includes('调用失败');
}
function formatTokenCount(value) {
    const num = Number(value || 0);
    if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`;
    if (num >= 1000) return `${Math.round(num / 1000)}K`;
    return String(Math.round(num));
}

function updateContextMeter(usage = {}) {
    if (!contextMeter || !contextPercent || !contextRing) return;
    const used = Number(usage.used_tokens || 0);
    const max = Number(usage.max_tokens || 131072);
    const percent = Math.max(0, Math.min(100, Number(usage.percent || (used / max * 100))));
    const compacted = Boolean(usage.compacted);
    contextPercent.textContent = `${Math.round(percent)}%`;
    contextRing.style.strokeDasharray = `${percent} 100`;
    contextMeter.classList.toggle('warning', percent >= 65 && percent < 80);
    contextMeter.classList.toggle('danger', percent >= 80);
    contextMeter.classList.toggle('compacted', compacted);
    const compactText = compacted ? ' · 已自动压缩' : '';
    contextMeter.title = `${percent.toFixed(1)}% · ${formatTokenCount(used)} / ${formatTokenCount(max)} context used${compactText}`;
}

updateContextMeter();



function renderToolCard(item) {
    const [icon, title] = getToolMeta(item.tool);
    const statusText = item.status === 'done' ? '完成' : item.status === 'error' ? '失败' : '运行中';
    const resultText = item.result ? formatToolPayload(item.result) : '等待工具返回...';
    return `
        <div class="tool-card ${item.open ? 'open' : ''}" data-tool-id="${item.id}">
            <button class="tool-card-header" type="button" onclick="toggleToolCard('${item.id}')">
                <span class="tool-chevron">${item.open ? '⌄' : '›'}</span>
                <span class="tool-icon">${icon}</span>
                <span class="tool-title">${escapeHtml(title)}</span>
                <span class="tool-status ${item.status}">${statusText}</span>
            </button>
            ${item.open ? `
                <div class="tool-card-body">
                    <div class="tool-section">
                        <div class="tool-section-title">参数</div>
                        <pre>${escapeHtml(formatToolPayload(item.args))}</pre>
                    </div>
                    <div class="tool-section">
                        <div class="tool-section-title">结果</div>
                        <pre>${escapeHtml(resultText)}</pre>
                    </div>
                </div>
            ` : ''}
        </div>
    `;
}

function renderEditLogCard(block) {
    const logHtml = (block.logs || [])
        .map(l => `<span style="color:${l.includes('FAILED')?'#e74c3c':'#2ecc71'}">${escapeHtml(l)}</span>`)
        .join('<br>');
    return `<div class="edit-log-card">📝 执行了 ${block.editsCount || 0} 个编辑操作：<br>${logHtml}</div>`;
}

function ensureTextBlock(state) {
    const last = state.blocks[state.blocks.length - 1];
    if (last && last.type === 'text') return last;
    const block = { type: 'text', content: '' };
    state.blocks.push(block);
    return block;
}

function renderStreamMessage(state, showCursor = true) {
    if (!state) return;
    const html = state.blocks.map(block => {
        if (block.type === 'tool') {
            return `<div class="tool-timeline">${renderToolCard(block.item)}</div>`;
        }
        if (block.type === 'edit-log') {
            return renderEditLogCard(block);
        }
        const textHtml = renderMarkdown(block.content || '');
        return textHtml ? `<div class="assistant-text">${textHtml}</div>` : '';
    }).join('');
    state.bubble.innerHTML = html + (showCursor ? '<span class="stream-cursor">▊</span>' : '');
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

// rAF 节流：高频流式事件（token/tool_call/tool_result/self_check 等）每帧最多触发一次全量渲染，
// 渲染频率与 token 速率解耦，避免长回复 O(n²) 重绘卡顿。done/error 收尾仍走同步 renderStreamMessage。
// showCursor 在回调时按 !state.done 求值，保证收尾后迟到的帧不会把光标加回来。
function scheduleStreamRender(state) {
    if (!state || state._raf) return;
    state._raf = requestAnimationFrame(() => {
        state._raf = 0;
        renderStreamMessage(state, !state.done);
    });
}

// 收尾前取消挂起的节流帧并做最终同步渲染，确保最后的内容不丢、光标被移除
function flushStreamRender(state) {
    if (!state) return;
    if (state._raf) {
        cancelAnimationFrame(state._raf);
        state._raf = 0;
    }
    renderStreamMessage(state, false);
}

window.toggleToolCard = function(id) {
    if (!activeStreamState) return;
    const item = activeStreamState.toolEvents.find(t => t.id === id);
    if (!item) return;
    item.open = !item.open;
    renderStreamMessage(activeStreamState, !activeStreamState.done);
};

function restoreEditorCode(code) {
    if (!editor) {
        pendingLatestCode = code;
        return;
    }
    const value = code || '<!-- 这个历史会话暂时没有保存代码快照 -->\n';
    applyEditorCode(value);
    if (code) runGame();
    else {
        document.getElementById('game-preview').srcdoc = '';
        clearPreviewRuntimeError();
    }
}


function setSessionId(id) {
    sessionId = id || '';
    if (sessionId) localStorage.setItem('gameDesignSessionId', sessionId);
    else localStorage.removeItem('gameDesignSessionId');
}

async function loadChatHistory(session) {
    if (!session) return false;
    try {
        const res = await fetch(`/api/chat/${session}/history`);
        if (!res.ok) return false;
        const data = await res.json();
        const messages = Array.isArray(data.messages) ? data.messages : [];
        if (!messages.length) return false;
        chatMessages.innerHTML = '';
        messages.forEach(m => addMessage(m.content || '', m.role === 'user' ? 'user' : 'ai'));
        if (typeof data.latest_code === 'string') {
            restoreEditorCode(data.latest_code);
        }
        return true;
    } catch (_) {
        return false;
    }
}

async function loadHistorySessions() {
    const res = await fetch('/api/chat/history');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
}

async function openHistoryModal() {
    const list = document.getElementById('history-list');
    list.innerHTML = '<p style="color:#aaa;text-align:center">加载中...</p>';
    openModal('modal-history');
    try {
        const sessions = await loadHistorySessions();
        if (!sessions.length) {
            list.innerHTML = '<p style="color:#aaa;text-align:center">暂无历史记录</p>';
            return;
        }
        list.innerHTML = sessions.map(s => `
            <div class="history-item">
                <div class="top">
                    <div>
                        <div class="title">${escapeHtml(s.title || '未命名会话')}</div>
                        <div class="meta">${s.message_count || 0} 条消息${s.updated_at ? ' · ' + escapeHtml(s.updated_at) : ''}</div>
                    </div>
                    <div class="history-actions">
                        <button type="button" onclick="openChatSession('${s.session_id}')">打开</button>
                        <button type="button" class="danger" onclick="deleteChatSession('${s.session_id}')">删除</button>
                    </div>
                </div>
            </div>
        `).join('');
    } catch (err) {
        list.innerHTML = `<p style="color:#e74c3c">加载失败: ${err.message}</p>`;
    }
}

async function openChatSession(id) {
    setSessionId(id);
    await loadChatHistory(id);
    closeModal('modal-history');
}

async function deleteChatSession(id) {
    if (!confirm('确定删除这条历史对话？对应的 AI 上下文也会删除。')) return;
    try {
        const res = await fetch(`/api/chat/${id}`, { method: 'DELETE' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        if (id === sessionId) {
            setSessionId('');
            chatMessages.innerHTML = '';
            addMessage('👋 当前历史对话已删除。告诉我你想做什么游戏吧！', 'ai');
            restoreEditorCode('');
        }
        await openHistoryModal();
    } catch (err) {
        alert('❌ 删除失败: ' + err.message);
    }
}


window.openHistoryModal = openHistoryModal;
window.openChatSession = openChatSession;
window.setSessionId = setSessionId;

function setStreamingUI(streaming) {
    const sendButton = document.getElementById('btn-send');
    isStreaming = streaming;
    sendButton.disabled = false;
    sendButton.textContent = streaming ? '停止 ■' : '发送 ⏎';
    sendButton.classList.toggle('stop-mode', streaming);
    chatInput.disabled = streaming;
}

function stopCurrentStream() {
    if (!isStreaming || !currentStreamController) return;
    currentStreamController.abort();
}

window.deleteChatSession = deleteChatSession;



async function sendMessage(messageOverride = null, options = {}) {
    if (messageOverride && typeof messageOverride !== 'string') {
        messageOverride = null;
        options = {};
    }
    if (isStreaming) {
        stopCurrentStream();
        return;
    }

    const hasOverride = typeof messageOverride === 'string';
    const msg = hasOverride ? messageOverride.trim() : chatInput.value.trim();
    const imagesToSend = hasOverride ? [] : [...selectedImages];
    if (!msg && !imagesToSend.length) return;

    addUserMessage(options.displayText || msg, imagesToSend);
    if (!hasOverride) {
        chatInput.value = '';
        selectedImages = [];
        renderImageAttachments();
    }
    currentStreamController = new AbortController();
    setStreamingUI(true);

    // 创建流式消息气泡
    const bubble = createStreamBubble();
    activeStreamState = {
        bubble,
        blocks: [],
        toolEvents: [],
        toolSeq: 0,
        done: false,
        codeUpdated: false,
        _raf: 0,
    };

    try {
        const currentCode = editor ? editor.getValue() : '';
        const res = await fetch('/api/chat/stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: sessionId,
                message: msg,
                current_code: currentCode,
                code_dirty: codeDirty,
                images: imagesToSend,

            }),
            signal: currentStreamController.signal,
        });

        if (!res.ok) {
            let detail = `HTTP ${res.status}`;
            try { const d = await res.json(); detail = d.detail || detail; } catch(_) {}
            throw new Error(detail);
        }

        // 读取 SSE 流
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop(); // 保留不完整的行

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                const data = line.slice(6).trim();
                if (data === '[DONE]') continue;

                try {
                    const event = JSON.parse(data);

                    if (event.type === 'session') {
                        setSessionId(event.session_id);
                    } else if (event.type === 'tool_call') {
                        activeStreamState.toolSeq += 1;
                        const toolId = event.id || `tool-${Date.now()}-${activeStreamState.toolSeq}`;
                        const item = {
                            id: toolId,
                            tool: event.tool,
                            args: event.args || '',
                            result: '',
                            status: 'running',
                            open: false,
                        };
                        activeStreamState.toolEvents.push(item);
                        activeStreamState.blocks.push({ type: 'tool', item });

                        scheduleStreamRender(activeStreamState);
                    } else if (event.type === 'tool_result') {
                        const target = event.id
                            ? activeStreamState.toolEvents.find(t => t.id === event.id)
                            : [...activeStreamState.toolEvents]
                                .reverse()
                                .find(t => t.tool === event.tool && t.status === 'running');
                        if (target) {
                            target.result = event.result || '';

                            // 企业级错误处理：解析后端错误码
                            const parsedError = errorHandler.parse(target.result);
                            if (parsedError.isError) {
                                errorHandler.show(parsedError);
                                target.status = 'error';
                            } else {
                                target.status = isToolErrorResult(target.result) ? 'error' : 'done';
                            }
                        } else {
                            activeStreamState.toolSeq += 1;
                            const toolId = event.id || `tool-${Date.now()}-${activeStreamState.toolSeq}`;

                            // 企业级错误处理：解析后端错误码
                            const parsedError = errorHandler.parse(event.result);
                            const status = parsedError.isError ? 'error' :
                                (isToolErrorResult(event.result) ? 'error' : 'done');

                            if (parsedError.isError) {
                                errorHandler.show(parsedError);
                            }

                            const item = {
                                id: toolId,
                                tool: event.tool,
                                args: '',
                                result: event.result || '',
                                status: status,
                                open: false,
                            };
                            activeStreamState.toolEvents.push(item);
                            activeStreamState.blocks.push({ type: 'tool', item });
                        }
                        scheduleStreamRender(activeStreamState);
                    } else if (event.type === 'token') {
                        ensureTextBlock(activeStreamState).content += event.content;
                        scheduleStreamRender(activeStreamState);
                    } else if (event.type === 'context_usage') {
                        updateContextMeter(event);

                    } else if (event.type === 'self_check') {
                        // 自检+自修闭环状态：在对话里展示一条系统提示（content 为原始文本，渲染时统一走 renderMarkdown 转义）
                        let txt;
                        if (event.status === 'repairing') {
                            txt = '🔧 自检发现问题，正在自动修复：\n' + (event.issues || []).map(s => '· ' + s).join('\n');
                        } else if (event.status === 'passed') {
                            txt = '✅ 自检通过';
                        } else {
                            txt = '⚠️ 自检仍有问题（已尽力修复）：\n' + (event.issues || []).map(s => '· ' + s).join('\n');
                        }
                        activeStreamState.blocks.push({ type: 'text', content: '\n' + txt + '\n' });
                        scheduleStreamRender(activeStreamState);

                    } else if (event.type === 'code_update') {
                        if (editor && event.code) {
                            // 流式期间只更新编辑器文本，不在此刷新预览（避免多段编辑反复重载/闪屏），
                            // 统一在 done 时跑一次
                            applyEditorCode(event.code);
                            activeStreamState.codeUpdated = true;
                        }
                        scheduleStreamRender(activeStreamState);

                    } else if (event.type === 'done') {
                        activeStreamState.done = true;
                        flushStreamRender(activeStreamState);

                        if (event.code && !activeStreamState.codeUpdated) {
                            // 没有收到过 code_update 时，done 保留兜底同步
                            applyEditorCode(event.code);
                        }
                        // 整轮结束后统一刷新一次预览（保证渲染的是完整代码，而非分段半成品）
                        if (activeStreamState.codeUpdated || event.code) runGame();
                        codeDirty = false;  // 本回合已完成，编辑器内容已随请求送达服务端，重置脏标记

                        // 显示编辑日志，不依赖 editor 是否需要兜底同步
                        if (event.action === 'edit' && event.edit_logs) {
                            activeStreamState.blocks.push({
                                type: 'edit-log',
                                editsCount: event.edits_count,
                                logs: event.edit_logs,
                            });
                            renderStreamMessage(activeStreamState, false);
                        }
                    } else if (event.type === 'error') {
                        activeStreamState.done = true;
                        // 带结构化错误码时走统一错误处理（限流/超时等有更友好的提示）
                        if (event.error_code) {
                            errorHandler.show({ code: event.error_code, message: event.content, isError: true });
                        } else {
                            ensureTextBlock(activeStreamState).content += `\n❌ ${event.content}`;
                        }
                        flushStreamRender(activeStreamState);
                    }
                } catch (e) {
                    // 忽略 JSON 解析错误
                }
            }
        }

        // 确保收尾：先冲掉挂起的节流帧（流结束但未收到 done 事件时兜底），再移除光标
        if (activeStreamState._raf) flushStreamRender(activeStreamState);
        const cursor = bubble.querySelector('.stream-cursor');
        if (cursor) cursor.remove();

    } catch (err) {
        activeStreamState.done = true;
        if (err.name === 'AbortError') {
            ensureTextBlock(activeStreamState).content += '\n⏹️ 已停止生成。';
        } else {
            ensureTextBlock(activeStreamState).content += `\n❌ 错误: ${err.message}`;
        }
        flushStreamRender(activeStreamState);
    } finally {
        currentStreamController = null;
        setStreamingUI(false);
    }
}

// 发送按钮 & 回车键
document.getElementById('btn-send').addEventListener('click', sendMessage);
chatInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

// ---------- 粘贴图片上传 ----------
// 通用粘贴提取：从 clipboardData 中揪出所有图片 File
function _extractImageFilesFromClipboard(e) {
    const items = e.clipboardData?.items;
    if (!items || !items.length) return [];
    const imageFiles = [];
    for (let i = 0; i < items.length; i++) {
        const item = items[i];
        if (item.kind === 'file' && item.type.startsWith('image/')) {
            const file = item.getAsFile();
            if (file) imageFiles.push(file);
        }
    }
    return imageFiles;
}

// ① 正文输入框粘贴（聚焦在此）
chatInput.addEventListener('paste', (e) => {
    const imageFiles = _extractImageFilesFromClipboard(e);
    if (imageFiles.length) {
        e.preventDefault();
        addSelectedImageFiles(imageFiles).catch(err => console.warn('粘贴图片失败:', err));
    }
});

// ② 兜底：焦点不在 textarea 时（如点击了聊天历史）也能接收 Ctrl+V
document.addEventListener('paste', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return; // ① 已处理
    if (e.target.closest('#modal-assets') || e.target.closest('#modal-skills') || e.target.closest('#modal-projects')) return;
    const imageFiles = _extractImageFilesFromClipboard(e);
    if (imageFiles.length) {
        addSelectedImageFiles(imageFiles).catch(err => console.warn('粘贴图片失败:', err));
    }
});

// ---------- 拖拽图片到聊天区上传 ----------
const chatPanel = document.querySelector('.chat-panel');
chatPanel.addEventListener('dragover', (e) => {
    // 只处理文件拖拽（忽略从素材/技能弹窗拖来的文本链接）
    if (e.dataTransfer.types.includes('Files')) {
        e.preventDefault();
        chatPanel.classList.add('drag-over');
    }
});
chatPanel.addEventListener('dragleave', () => chatPanel.classList.remove('drag-over'));
chatPanel.addEventListener('drop', (e) => {
    e.preventDefault();
    chatPanel.classList.remove('drag-over');
    const imageFiles = Array.from(e.dataTransfer.files || []).filter(f => f.type.startsWith('image/'));
    if (imageFiles.length) addSelectedImageFiles(imageFiles);
});

async function resetCurrentWorkspace(message = '👋 已创建新项目。告诉我你想做什么游戏吧！') {
    if (isStreaming) {
        stopCurrentStream();
    }
    if (sessionId) {
        try {
            await fetch(`/api/chat/${sessionId}`, { method: 'DELETE' });
        } catch (_) {}
    }
    setSessionId('');
    currentProjectId = '';
    chatMessages.innerHTML = '';
    addMessage(message, 'ai');
    updateContextMeter();  // 重置上下文圆环
    const starterCode = '<!-- 新项目 - 开始设计你的游戏 -->\n';
    if (editor) {
        applyEditorCode(starterCode);
        document.getElementById('game-preview').srcdoc = '';
    } else {
        pendingLatestCode = starterCode;
    }
}


// 清除对话
document.getElementById('btn-clear-chat').addEventListener('click', async () => {
    if (isStreaming) {
        stopCurrentStream();
    }
    if (sessionId) {
        // 网络错误不应阻断本地 UI 清理（与 resetCurrentWorkspace 行为一致）
        try {
            await fetch(`/api/chat/${sessionId}`, { method: 'DELETE' });
        } catch (_) {}
    }
    setSessionId('');
    chatMessages.innerHTML = '';
    addMessage('👋 对话已清除。告诉我你想做什么游戏吧！', 'ai');
});

// 对话历史
const btnHistory = document.getElementById('btn-history');
if (btnHistory) {
    btnHistory.addEventListener('click', openHistoryModal);
}

(async () => {
    if (sessionId) {
        await loadChatHistory(sessionId);
    }
})();


// ============ 游戏预览与运行时错误捕获 ============
let lastPreviewRuntimeError = null;
let runtimeErrorCard = null;

function getRuntimeErrorDetailText() {
    if (!lastPreviewRuntimeError) return '';
    const location = lastPreviewRuntimeError.line
        ? `第 ${lastPreviewRuntimeError.line} 行，第 ${lastPreviewRuntimeError.column || 0} 列`
        : '未知位置';
    return [
        lastPreviewRuntimeError.message,
        location,
        lastPreviewRuntimeError.stack,
    ].filter(Boolean).join('\n');
}

function clearPreviewRuntimeError() {
    lastPreviewRuntimeError = null;
    const badge = document.getElementById('preview-error-badge');
    const detail = document.getElementById('runtime-error-modal-detail');
    if (badge) badge.classList.add('hidden');
    if (detail) detail.textContent = '';
    closeModal('modal-runtime-error');
    if (runtimeErrorCard) {
        runtimeErrorCard.remove();
        runtimeErrorCard = null;
    }
}

function renderRuntimeErrorCard() {
    if (!lastPreviewRuntimeError) return;
    // 自愈：清对话/切历史/新建后旧卡片已从 DOM 移除，引用变成游离节点，
    // 否则会渲染到看不见的地方。检测断连就丢弃旧引用、重新挂载。
    if (runtimeErrorCard && !runtimeErrorCard.isConnected) runtimeErrorCard = null;
    if (!runtimeErrorCard) {
        runtimeErrorCard = document.createElement('div');
        runtimeErrorCard.className = 'msg ai runtime-error-chat-card';
        chatMessages.appendChild(runtimeErrorCard);
    }
    const line = lastPreviewRuntimeError.line || '未知';
    runtimeErrorCard.innerHTML = `
        <div class="msg-content runtime-error-content">
            <div class="runtime-error-kicker">⚠️ 预览运行错误</div>
            <div class="runtime-error-summary">${escapeHtml(lastPreviewRuntimeError.message)}</div>
            <div class="runtime-error-meta">位置：${escapeHtml(line)} 行</div>
            <div class="runtime-error-card-actions">
                <button type="button" onclick="fixPreviewRuntimeError()">让 AI 修复</button>
                <button type="button" onclick="openRuntimeErrorModal()">查看详情</button>
                <button type="button" onclick="ignorePreviewRuntimeError()">忽略</button>
            </div>
        </div>`;
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function showPreviewRuntimeError(error) {
    lastPreviewRuntimeError = {
        message: String(error.message || '未知运行错误'),
        source: String(error.source || ''),
        line: error.line || 0,
        column: error.column || 0,
        stack: String(error.stack || ''),
        ts: new Date().toISOString(),
    };

    const badge = document.getElementById('preview-error-badge');
    const detail = document.getElementById('runtime-error-modal-detail');
    if (badge) badge.classList.remove('hidden');
    if (detail) detail.textContent = getRuntimeErrorDetailText();
    renderRuntimeErrorCard();
}

function buildRuntimeErrorFixPrompt() {
    if (!lastPreviewRuntimeError) return '';
    const currentCode = editor ? editor.getValue() : '';
    return [
        '右侧 iframe 运行游戏时报错了，请直接修复当前代码。',
        '',
        '要求：',
        '1. 先定位报错根因，不要重写无关功能。',
        '2. 用代码编辑工具精准修改，最后写回可运行的完整 HTML。',
        '3. 修复后保证游戏能继续启动和游玩，避免黑屏。',
        '',
        '运行错误：',
        `message: ${lastPreviewRuntimeError.message}`,
        `line: ${lastPreviewRuntimeError.line || 'unknown'}`,
        `column: ${lastPreviewRuntimeError.column || 'unknown'}`,
        lastPreviewRuntimeError.stack ? `stack:\n${lastPreviewRuntimeError.stack}` : '',
        '',
        `当前代码长度：${currentCode.length} 字符，代码已随请求作为 current_code 传入。`,
    ].filter(Boolean).join('\n');
}

function injectPreviewErrorBridge(code) {
    const bridge = `
<script>
(function(){
  // 预览运行在 sandbox="allow-scripts"（null origin）下，原生访问 localStorage/sessionStorage
  // 会抛 SecurityError 直接令游戏崩溃。这里注入内存垫片：游戏照常存取（如最高分），
  // 数据仅在本次预览内有效、刷新即重置——既不破坏功能，又不让预览读到本站真实存储。
  function installStorageShim(name){
    try { var probe = window[name]; probe.getItem('__probe__'); return; } catch (_) {}
    var mem = {};
    var shim = {
      getItem: function(k){ k = String(k); return Object.prototype.hasOwnProperty.call(mem, k) ? mem[k] : null; },
      setItem: function(k, v){ mem[String(k)] = String(v); },
      removeItem: function(k){ delete mem[String(k)]; },
      clear: function(){ mem = {}; },
      key: function(i){ var ks = Object.keys(mem); return (i >= 0 && i < ks.length) ? ks[i] : null; }
    };
    Object.defineProperty(shim, 'length', { get: function(){ return Object.keys(mem).length; } });
    try { Object.defineProperty(window, name, { value: shim, configurable: true }); } catch (_) {}
  }
  installStorageShim('localStorage');
  installStorageShim('sessionStorage');

  function sendRuntimeError(payload){
    try {
      parent.postMessage(Object.assign({ channel: 'h5-game-preview-runtime-error' }, payload), '*');
    } catch (_) {}
  }
  window.addEventListener('error', function(event){
    var target = event.target || {};
    if (target !== window && (target.src || target.href)) {
      sendRuntimeError({
        message: '资源加载失败: ' + (target.src || target.href),
        source: target.src || target.href || '',
        line: 0,
        column: 0,
        stack: ''
      });
      return;
    }
    sendRuntimeError({
      message: event.message || 'Script error',
      source: event.filename || '',
      line: event.lineno || 0,
      column: event.colno || 0,
      stack: event.error && event.error.stack ? String(event.error.stack) : ''
    });
  }, true);
  window.addEventListener('unhandledrejection', function(event){
    var reason = event.reason || {};
    sendRuntimeError({
      message: reason.message || String(reason || 'Unhandled promise rejection'),
      source: '',
      line: 0,
      column: 0,
      stack: reason.stack ? String(reason.stack) : ''
    });
  });
})();
<\/script>`;
    if (/<head[^>]*>/i.test(code)) return code.replace(/<head([^>]*)>/i, `<head$1>\n${bridge}`);
    if (/<html[^>]*>/i.test(code)) return code.replace(/<html([^>]*)>/i, `<html$1>\n${bridge}`);
    return `${bridge}\n${code}`;
}

function runGame() {
    const code = editor ? editor.getValue() : '';
    if (!code.trim()) return;
    clearPreviewRuntimeError();
    const iframe = document.getElementById('game-preview');
    iframe.srcdoc = injectPreviewErrorBridge(code);
}

document.getElementById('btn-run').addEventListener('click', runGame);
document.getElementById('btn-fullscreen').addEventListener('click', () => {
    const iframe = document.getElementById('game-preview');
    if (iframe.requestFullscreen) iframe.requestFullscreen();
});

window.addEventListener('message', (event) => {
    // 只接受来自预览 iframe 本身的消息（iframe 为 sandbox=allow-scripts，origin 为 "null"，
    // 故用 source 身份校验而非 origin），防止其它窗口/嵌入方伪造运行错误消息。
    const iframe = document.getElementById('game-preview');
    if (!iframe || event.source !== iframe.contentWindow) return;
    if (!event.data || event.data.channel !== 'h5-game-preview-runtime-error') return;
    showPreviewRuntimeError(event.data);
});

function openRuntimeErrorModal() {
    const detail = document.getElementById('runtime-error-modal-detail');
    if (detail) detail.textContent = getRuntimeErrorDetailText();
    openModal('modal-runtime-error');
}

function ignorePreviewRuntimeError() {
    clearPreviewRuntimeError();
}

function fixPreviewRuntimeError() {
    const prompt = buildRuntimeErrorFixPrompt();
    if (!prompt) return;
    if (isStreaming) {
        alert('AI 正在生成中，请先停止或等待完成后再修复。');
        return;
    }
    closeModal('modal-runtime-error');
    sendMessage(prompt, {
        displayText: `🔧 请修复右侧预览运行错误：${lastPreviewRuntimeError.message}`,
    });
}

window.openRuntimeErrorModal = openRuntimeErrorModal;
window.ignorePreviewRuntimeError = ignorePreviewRuntimeError;
window.fixPreviewRuntimeError = fixPreviewRuntimeError;

document.getElementById('preview-error-badge')?.addEventListener('click', openRuntimeErrorModal);
document.getElementById('btn-ai-fix-runtime-error')?.addEventListener('click', fixPreviewRuntimeError);
document.getElementById('btn-ignore-runtime-error')?.addEventListener('click', ignorePreviewRuntimeError);


// ============ 项目管理 ============
document.getElementById('btn-new').addEventListener('click', async () => {
    if (confirm('创建新项目？当前未保存的代码和当前对话上下文都会清空。')) {
        await resetCurrentWorkspace();
    }
});

document.getElementById('btn-save').addEventListener('click', async () => {
    const name = prompt('项目名称:', '我的游戏');
    if (!name) return;
    const code = editor.getValue();
    try {
        const res = await fetch('/api/projects', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project_id: currentProjectId, name, code }),
        });
        await ensureOk(res);
        const data = await res.json();
        currentProjectId = data.project_id;
        alert('✅ 保存成功！');
    } catch (err) {
        alert('❌ 保存失败: ' + err.message);
    }
});

document.getElementById('btn-load').addEventListener('click', async () => {
    await loadProjects();
    openModal('modal-projects');
});

async function loadProjects() {
    try {
        const res = await fetch('/api/projects');
        await ensureOk(res);
        const projects = await res.json();
        const list = document.getElementById('projects-list');
        if (projects.length === 0) {
            list.innerHTML = '<p style="color:#aaa;text-align:center">暂无项目</p>';
            return;
        }
        list.innerHTML = projects.map(p => `
            <div class="project-item">
                <span class="name">📁 ${escapeHtml(p.name || '未命名项目')}</span>
                <div class="actions">
                    <button onclick="loadProject('${p.project_id}')" title="加载">📂</button>
                    <button onclick="deleteProject('${p.project_id}')" title="删除">🗑️</button>
                </div>
            </div>
        `).join('');
    } catch (err) {
        console.error('加载项目失败:', err);
    }
}

async function loadProject(id) {
    try {
        const res = await fetch(`/api/projects/${id}`);
        await ensureOk(res);
        const data = await res.json();
        currentProjectId = id;
        applyEditorCode(data.code || '');
        closeModal('modal-projects');
        runGame();
    } catch (err) {
        alert('❌ 加载失败: ' + err.message);
    }
}

async function deleteProject(id) {
    if (!confirm('确定删除此项目？')) return;
    try {
        await ensureOk(await fetch(`/api/projects/${id}`, { method: 'DELETE' }));
        await loadProjects();
    } catch (err) {
        alert('❌ 删除失败: ' + err.message);
    }
}

// ============ 素材管理 ============
document.getElementById('btn-assets').addEventListener('click', async () => {
    await loadAssets();
    openModal('modal-assets');
});

document.getElementById('btn-upload').addEventListener('click', async () => {
    const fileInput = document.getElementById('file-input');
    if (!fileInput.files.length) { alert('请选择文件'); return; }

    const failed = [];
    for (const file of fileInput.files) {
        const formData = new FormData();
        formData.append('file', file);
        formData.append('asset_type', document.getElementById('asset-type').value);
        formData.append('description', document.getElementById('asset-desc').value);
        formData.append('tags', document.getElementById('asset-tags').value);
        try {
            await ensureOk(await fetch('/api/assets/upload', { method: 'POST', body: formData }));
        } catch (err) {
            failed.push(`${file.name}: ${err.message}`);
        }
    }
    fileInput.value = '';
    document.getElementById('asset-desc').value = '';
    document.getElementById('asset-tags').value = '';
    await loadAssets();
    // 别再无条件报成功：有失败就如实告知
    alert(failed.length ? '❌ 部分上传失败：\n' + failed.join('\n') : '✅ 上传成功！');
});

async function loadAssets() {
    try {
        const res = await fetch('/api/assets');
        await ensureOk(res);
        const assets = await res.json();
        const list = document.getElementById('assets-list');
        if (assets.length === 0) {
            list.innerHTML = '<p style="color:#aaa;text-align:center">暂无素材，上传一些吧！</p>';
            return;
        }
        list.innerHTML = assets.map(a => {
            const aid = a.id || a.asset_id;
            const url = `/assets/${a.asset_type}/${aid}${a.extension || ''}`;
            return `
            <div class="asset-item">
                <span class="name">${getTypeIcon(a.asset_type)} ${escapeHtml(a.file_name || '')}</span>
                <code style="font-size:11px;color:#888;margin:0 8px">${escapeHtml(url)}</code>
                <div class="actions">
                    <button onclick="deleteAsset('${aid}')" title="删除">🗑️</button>
                </div>
            </div>`;
        }).join('');
    } catch (err) {
        console.error('加载素材失败:', err);
    }
}

function getTypeIcon(type) {
    const icons = { image: '🖼️', spritesheet: '🎞️', audio: '🔊', tilemap: '🗺️', font: '🔤' };
    return icons[type] || '📄';
}

async function deleteAsset(id) {
    if (!confirm('确定删除此素材？')) return;
    try {
        await ensureOk(await fetch(`/api/assets/${id}`, { method: 'DELETE' }));
        await loadAssets();
    } catch (err) {
        alert('❌ 删除失败: ' + err.message);
    }
}

// 拖拽上传
const uploadZone = document.getElementById('upload-zone');
uploadZone.addEventListener('dragover', (e) => { e.preventDefault(); uploadZone.classList.add('drag-over'); });
uploadZone.addEventListener('dragleave', () => uploadZone.classList.remove('drag-over'));
uploadZone.addEventListener('drop', (e) => {
    e.preventDefault();
    uploadZone.classList.remove('drag-over');
    const fileInput = document.getElementById('file-input');
    const dt = new DataTransfer();
    for (const file of e.dataTransfer.files) {
        dt.items.add(file);
    }
    fileInput.files = dt.files;
    fileInput.dispatchEvent(new Event('change'));
});

// ============ 工具函数 ============
function openModal(id) { document.getElementById(id).classList.remove('hidden'); }
function closeModal(id) { document.getElementById(id).classList.add('hidden'); }

// 点击弹窗背景关闭
document.querySelectorAll('.modal').forEach(m => {
    m.addEventListener('click', (e) => { if (e.target === m) m.classList.add('hidden'); });
});

// ============ 技能管理 ============
document.getElementById('btn-skills').addEventListener('click', () => {
    openModal('modal-skills');
    loadSkills();
});

async function loadSkills() {
    const list = document.getElementById('skills-list');
    list.innerHTML = '<p style="color:#888;text-align:center">加载中...</p>';
    try {
        const res = await fetch('/api/skills');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const skills = await res.json();
        if (!skills.length) {
            list.innerHTML = '<p style="color:#888;text-align:center">暂无技能</p>';
            return;
        }
        list.innerHTML = skills.map(s => `
            <div class="skill-item">
                <div class="skill-info">
                    <strong>${escapeHtml(s.name || '')}</strong>
                    <span>${escapeHtml(s.description || '')}</span>
                </div>
                <div class="skill-actions">
                    <button onclick="viewSkill('${jsStr(s.name)}')" title="查看">👁</button>
                    <button onclick="deleteSkill('${jsStr(s.name)}')" title="删除">🗑️</button>
                </div>
            </div>
        `).join('');
    } catch (err) {
        list.innerHTML = `<p style="color:#f66;text-align:center">加载失败: ${err.message}</p>`;
        console.error('加载技能失败:', err);
    }
}

async function viewSkill(name) {
    try {
        const res = await fetch(`/api/skills/${encodeURIComponent(name)}`);
        await ensureOk(res);
        const skill = await res.json();
        document.getElementById('skill-name').value = skill.name;
        document.getElementById('skill-name').disabled = true;  // 查看时禁止改名
        document.getElementById('skill-desc').value = skill.description;
        document.getElementById('skill-content').value = skill.content;
        // 切换按钮文字
        document.getElementById('btn-add-skill').textContent = '✏️ 更新技能';
        document.getElementById('btn-add-skill').dataset.mode = 'edit';
    } catch (err) {
        alert('加载失败: ' + err.message);
    }
}

async function deleteSkill(name) {
    if (!confirm(`确定删除技能 "${name}"？`)) return;
    try {
        await ensureOk(await fetch(`/api/skills/${encodeURIComponent(name)}`, { method: 'DELETE' }));
        await loadSkills();
    } catch (err) {
        alert('删除失败: ' + err.message);
    }
}

document.getElementById('btn-add-skill').addEventListener('click', async () => {
    const btn = document.getElementById('btn-add-skill');
    const nameInput = document.getElementById('skill-name');
    const name = nameInput.value.trim();
    const desc = document.getElementById('skill-desc').value.trim();
    const content = document.getElementById('skill-content').value.trim();
    if (!name || !desc || !content) { alert('请填写完整'); return; }
    // 编辑模式：后端 POST 对重名返回 400 且无更新端点，只能"先删旧再建新"。
    // 为防 DELETE 成功后 POST 失败导致旧技能永久丢失：删除前先取出旧技能备份，失败时回滚恢复。
    let oldSkill = null;
    try {
        if (btn.dataset.mode === 'edit') {
            const oldRes = await fetch(`/api/skills/${encodeURIComponent(name)}`);
            await ensureOk(oldRes);
            const backup = await oldRes.json();
            await ensureOk(await fetch(`/api/skills/${encodeURIComponent(name)}`, { method: 'DELETE' }));
            oldSkill = backup; // 删除确实成功后才需要回滚
        }
        const res = await fetch('/api/skills', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, description: desc, content }),
        });
        await ensureOk(res);
        // 重置表单
        nameInput.value = '';
        nameInput.disabled = false;
        document.getElementById('skill-desc').value = '';
        document.getElementById('skill-content').value = '';
        btn.textContent = '➕ 添加技能';
        btn.dataset.mode = 'add';
        await loadSkills();
    } catch (err) {
        if (oldSkill) {
            // 旧技能已删但新内容写入失败：重新 POST 旧技能恢复
            try {
                await ensureOk(await fetch('/api/skills', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        name: oldSkill.name,
                        description: oldSkill.description,
                        content: oldSkill.content,
                    }),
                }));
                alert('❌ 更新失败: ' + err.message + '\n已恢复原技能内容，表单中的修改尚未保存，可重试。');
            } catch (restoreErr) {
                alert('❌ 更新失败: ' + err.message
                    + '\n且自动恢复原技能也失败: ' + restoreErr.message
                    + '\n请不要关闭页面，表单中仍保留当前内容，请重试保存。');
            }
            await loadSkills();
        } else {
            alert('操作失败: ' + err.message);
        }
    }
});

// 文件导入（支持 .json / .md / .zip）
document.getElementById('skill-import-file').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const ext = file.name.split('.').pop().toLowerCase();
    try {
        let added = 0;
        if (ext === 'json') {
            // JSON：单个或数组格式
            const text = await file.text();
            const data = JSON.parse(text);
            const skills = Array.isArray(data) ? data : [data];
            for (const s of skills) {
                if (!s.name || !s.description || !s.content) continue;
                const res = await fetch('/api/skills', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(s),
                });
                if (res.ok) added++;
            }
        } else if (ext === 'md') {
            // Markdown：文件名作为 name，首行作为 description，其余作为 content
            const text = await file.text();
            const name = file.name.replace(/\.md$/, '').replace(/[^a-zA-Z0-9_-]/g, '_');
            const lines = text.split('\n');
            let description = name;
            let content = text;
            // 如果首行是 # 标题，用作描述
            if (lines[0] && lines[0].startsWith('#')) {
                description = lines[0].replace(/^#+\s*/, '').trim();
                content = lines.slice(1).join('\n').trim();
            }
            const res = await fetch('/api/skills', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, description, content }),
            });
            if (res.ok) added++;
        } else if (ext === 'zip') {
            // ZIP：上传到后端解析
            const formData = new FormData();
            formData.append('file', file);
            const res = await fetch('/api/skills/import', { method: 'POST', body: formData });
            if (!res.ok) { const d = await res.json(); throw new Error(d.detail); }
            const result = await res.json();
            added = result.added || 0;
        } else {
            alert('不支持的文件格式，请使用 .json / .md / .zip');
            e.target.value = '';
            return;
        }
        alert(`✅ 成功导入 ${added} 个技能`);
        await loadSkills();
    } catch (err) {
        alert('导入失败: ' + err.message);
    }
    e.target.value = '';
});


// 扫描本地文件夹导入
document.getElementById('btn-scan-skills').addEventListener('click', async () => {
    const scanPath = document.getElementById('skill-scan-path').value.trim();
    if (!scanPath) { alert('请输入文件夹路径'); return; }
    try {
        const res = await fetch('/api/skills/scan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: scanPath }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail);
        let msg = `✅ 扫描完成！找到 ${data.total_found} 个技能，成功导入 ${data.added} 个`;
        if (data.skipped && data.skipped.length) {
            msg += `\n跳过（已存在）: ${data.skipped.join(', ')}`;
        }
        alert(msg);
        await loadSkills();
    } catch (err) {
        alert('扫描失败: ' + err.message);
    }
});