/**
 * AI 游戏设计工坊 - 前端主逻辑
 */

let editor = null;       // Monaco Editor 实例
let sessionId = localStorage.getItem('gameDesignSessionId') || '';       // 当前会话 ID
let currentProjectId = ''; // 当前项目 ID
let pendingLatestCode = null; // Monaco 尚未初始化时，临时保存要恢复的历史代码


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

let activeStreamState = null;


let currentStreamController = null;
let isStreaming = false;

function addMessage(content, role = 'ai') {
    const div = document.createElement('div');
    div.className = `msg ${role}`;
    let html = content
        .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>')
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/\n/g, '<br>');
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

// 将原始文本渲染为 HTML（简单 markdown）
function renderMarkdown(text) {
    return text
        .trim()  // 去掉开头和结尾的空白
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

function getToolMeta(tool) {
    const map = {
        list_all_assets: ['🧩', '列出素材'],
        search_assets: ['🔎', '搜索素材'],
        search_knowledge: ['📚', '检索知识库'],
        search_code: ['⌕', '搜索代码'],
        view_code: ['👁', '查看代码'],
        str_replace_code: ['✏️', '替换代码'],
        replace_code_lines: ['🧵', '按行替换'],
        write_game_code: ['✍️', '写入 game.html'],
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
    editor.setValue(value);
    if (code) runGame();
    else document.getElementById('game-preview').srcdoc = '';
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
                        <div class="title">${s.title || '未命名会话'}</div>
                        <div class="meta">${s.message_count || 0} 条消息${s.updated_at ? ' · ' + s.updated_at : ''}</div>
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



async function sendMessage() {
    if (isStreaming) {
        stopCurrentStream();
        return;
    }

    const msg = chatInput.value.trim();
    if (!msg) return;

    addMessage(msg, 'user');
    chatInput.value = '';
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
                        renderStreamMessage(activeStreamState, true);
                    } else if (event.type === 'tool_result') {
                        const target = event.id
                            ? activeStreamState.toolEvents.find(t => t.id === event.id)
                            : [...activeStreamState.toolEvents]
                                .reverse()
                                .find(t => t.tool === event.tool && t.status === 'running');
                        if (target) {
                            target.result = event.result || '';
                            target.status = isToolErrorResult(target.result) ? 'error' : 'done';
                        } else {
                            activeStreamState.toolSeq += 1;
                            const toolId = event.id || `tool-${Date.now()}-${activeStreamState.toolSeq}`;
                            const item = {
                                id: toolId,
                                tool: event.tool,
                                args: '',
                                result: event.result || '',
                                status: isToolErrorResult(event.result) ? 'error' : 'done',
                                open: false,
                            };
                            activeStreamState.toolEvents.push(item);
                            activeStreamState.blocks.push({ type: 'tool', item });
                        }
                        renderStreamMessage(activeStreamState, true);
                    } else if (event.type === 'token') {
                        ensureTextBlock(activeStreamState).content += event.content;
                        renderStreamMessage(activeStreamState, true);
                    } else if (event.type === 'context_usage') {
                        updateContextMeter(event);

                    } else if (event.type === 'code_update') {
                        if (editor && event.code) {
                            editor.setValue(event.code);
                            runGame();
                            activeStreamState.codeUpdated = true;
                        }
                        renderStreamMessage(activeStreamState, true);

                    } else if (event.type === 'done') {
                        activeStreamState.done = true;
                        renderStreamMessage(activeStreamState, false);

                        if (event.code && !activeStreamState.codeUpdated) {
                            // 没有收到过 code_update 时，done 保留兜底同步
                            editor.setValue(event.code);
                            runGame();
                        }

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
                        ensureTextBlock(activeStreamState).content += `\n❌ ${event.content}`;
                        renderStreamMessage(activeStreamState, false);
                    }
                } catch (e) {
                    // 忽略 JSON 解析错误
                }
            }
        }

        // 确保光标被移除
        const cursor = bubble.querySelector('.stream-cursor');
        if (cursor) cursor.remove();

    } catch (err) {
        activeStreamState.done = true;
        if (err.name === 'AbortError') {
            ensureTextBlock(activeStreamState).content += '\n⏹️ 已停止生成。';
        } else {
            ensureTextBlock(activeStreamState).content += `\n❌ 错误: ${err.message}`;
        }
        renderStreamMessage(activeStreamState, false);
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
    const starterCode = '<!-- 新项目 - 开始设计你的游戏 -->\n';
    if (editor) {
        editor.setValue(starterCode);
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
        await fetch(`/api/chat/${sessionId}`, { method: 'DELETE' });
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


// ============ 游戏预览 ============
function runGame() {
    const code = editor ? editor.getValue() : '';
    if (!code.trim()) return;
    const iframe = document.getElementById('game-preview');
    iframe.srcdoc = code;
}

document.getElementById('btn-run').addEventListener('click', runGame);
document.getElementById('btn-fullscreen').addEventListener('click', () => {
    const iframe = document.getElementById('game-preview');
    if (iframe.requestFullscreen) iframe.requestFullscreen();
});

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
        const projects = await res.json();
        const list = document.getElementById('projects-list');
        if (projects.length === 0) {
            list.innerHTML = '<p style="color:#aaa;text-align:center">暂无项目</p>';
            return;
        }
        list.innerHTML = projects.map(p => `
            <div class="project-item">
                <span class="name">📁 ${p.name || '未命名项目'}</span>
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
        const data = await res.json();
        currentProjectId = id;
        editor.setValue(data.code || '');
        closeModal('modal-projects');
        runGame();
    } catch (err) {
        alert('❌ 加载失败: ' + err.message);
    }
}

async function deleteProject(id) {
    if (!confirm('确定删除此项目？')) return;
    try {
        await fetch(`/api/projects/${id}`, { method: 'DELETE' });
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

    for (const file of fileInput.files) {
        const formData = new FormData();
        formData.append('file', file);
        formData.append('asset_type', document.getElementById('asset-type').value);
        formData.append('description', document.getElementById('asset-desc').value);
        formData.append('tags', document.getElementById('asset-tags').value);
        try {
            await fetch('/api/assets/upload', { method: 'POST', body: formData });
        } catch (err) {
            alert('❌ 上传失败: ' + err.message);
        }
    }
    fileInput.value = '';
    document.getElementById('asset-desc').value = '';
    document.getElementById('asset-tags').value = '';
    await loadAssets();
    alert('✅ 上传成功！');
});

async function loadAssets() {
    try {
        const res = await fetch('/api/assets');
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
                <span class="name">${getTypeIcon(a.asset_type)} ${a.file_name}</span>
                <code style="font-size:11px;color:#888;margin:0 8px">${url}</code>
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
        await fetch(`/api/assets/${id}`, { method: 'DELETE' });
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
    document.getElementById('file-input').files = e.dataTransfer.files;
});

// ============ 工具函数 ============
function openModal(id) { document.getElementById(id).classList.remove('hidden'); }
function closeModal(id) { document.getElementById(id).classList.add('hidden'); }
function showLoading(show) { document.getElementById('loading').classList.toggle('hidden', !show); }

// 点击弹窗背景关闭
document.querySelectorAll('.modal').forEach(m => {
    m.addEventListener('click', (e) => { if (e.target === m) m.classList.add('hidden'); });
});
