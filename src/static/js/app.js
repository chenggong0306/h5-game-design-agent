/**
 * AI 游戏设计工坊 - 前端主逻辑
 */

let editor = null;       // Monaco Editor 实例
let sessionId = '';       // 当前会话 ID
let currentProjectId = ''; // 当前项目 ID

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
});

// ============ 对话功能 ============
const chatMessages = document.getElementById('chat-messages');
const chatInput = document.getElementById('chat-input');

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

async function sendMessage() {
    const msg = chatInput.value.trim();
    if (!msg) return;

    addMessage(msg, 'user');
    chatInput.value = '';
    document.getElementById('btn-send').disabled = true;

    // 创建流式消息气泡
    const bubble = createStreamBubble();
    let fullText = '';
    let toolCalls = [];
    let toolResults = [];

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
                        sessionId = event.session_id;
                    } else if (event.type === 'tool_call') {
                        // 工具调用 - 立即显示
                        toolCalls.push({ tool: event.tool, args: event.args });

                        // 立即渲染工具调用
                        let toolHtml = '';
                        toolCalls.forEach(tc => {
                            toolHtml += `<div style="margin:4px 0;padding:6px 10px;background:#2a2a3e;border-radius:4px;font-size:11px;">
                                🔧 <strong>调用工具:</strong> <code>${tc.tool}</code>
                            </div>`;
                        });
                        bubble.innerHTML = toolHtml + '<span class="stream-cursor">▊</span>';
                        chatMessages.scrollTop = chatMessages.scrollHeight;
                    } else if (event.type === 'tool_result') {
                        // 工具执行结果 - 立即显示
                        toolResults.push({ tool: event.tool, result: event.result });

                        // 立即渲染工具调用和结果
                        let toolHtml = '';
                        toolCalls.forEach(tc => {
                            toolHtml += `<div style="margin:4px 0;padding:6px 10px;background:#2a2a3e;border-radius:4px;font-size:11px;">
                                🔧 <strong>调用工具:</strong> <code>${tc.tool}</code>
                            </div>`;
                        });
                        toolResults.forEach(tr => {
                            toolHtml += `<div style="margin:4px 0;padding:6px 10px;background:#1a3a2e;border-radius:4px;font-size:11px;">
                                ✅ <strong>${tr.tool} 结果:</strong> <code style="color:#2ecc71;">${tr.result}</code>
                            </div>`;
                        });
                        bubble.innerHTML = toolHtml + '<span class="stream-cursor">▊</span>';
                        chatMessages.scrollTop = chatMessages.scrollHeight;
                    } else if (event.type === 'token') {
                        fullText += event.content;
                        // 实时渲染
                        let html = renderMarkdown(fullText);

                        // 添加工具调用和结果
                        if (toolCalls.length > 0 || toolResults.length > 0) {
                            let toolHtml = '';

                            // 显示工具调用
                            toolCalls.forEach(tc => {
                                toolHtml += `<div style="margin:4px 0;padding:6px 10px;background:#2a2a3e;border-radius:4px;font-size:11px;">
                                    🔧 <strong>调用工具:</strong> <code>${tc.tool}</code>
                                </div>`;
                            });

                            // 显示工具结果
                            toolResults.forEach(tr => {
                                toolHtml += `<div style="margin:4px 0;padding:6px 10px;background:#1a3a2e;border-radius:4px;font-size:11px;">
                                    ✅ <strong>${tr.tool} 结果:</strong> <code style="color:#2ecc71;">${tr.result}</code>
                                </div>`;
                            });

                            html = toolHtml + html;
                        }

                        bubble.innerHTML = html + '<span class="stream-cursor">▊</span>';
                        chatMessages.scrollTop = chatMessages.scrollHeight;
                    } else if (event.type === 'done') {
                        // 最终渲染
                        let html = renderMarkdown(fullText);

                        // 添加工具调用和结果
                        if (toolCalls.length > 0 || toolResults.length > 0) {
                            let toolHtml = '';

                            toolCalls.forEach(tc => {
                                toolHtml += `<div style="margin:4px 0;padding:6px 10px;background:#2a2a3e;border-radius:4px;font-size:11px;">
                                    🔧 <strong>调用工具:</strong> <code>${tc.tool}</code>
                                </div>`;
                            });

                            toolResults.forEach(tr => {
                                toolHtml += `<div style="margin:4px 0;padding:6px 10px;background:#1a3a2e;border-radius:4px;font-size:11px;">
                                    ✅ <strong>${tr.tool} 结果:</strong> <code style="color:#2ecc71;">${tr.result}</code>
                                </div>`;
                            });

                            html = toolHtml + html;
                        }

                        bubble.innerHTML = html;
                        if (event.code) {
                            // 有代码变更（新生成 / 编辑指令执行后的结果）
                            editor.setValue(event.code);
                            runGame();

                            // 显示编辑日志
                            if (event.action === 'edit' && event.edit_logs) {
                                const logHtml = event.edit_logs
                                    .map(l => `<span style="color:${l.includes('FAILED')?'#e74c3c':'#2ecc71'}">${l}</span>`)
                                    .join('<br>');
                                bubble.innerHTML += `<div style="margin-top:8px;padding:6px 10px;background:#111;border-radius:6px;font-size:12px;font-family:monospace">
                                    📝 执行了 ${event.edits_count} 个编辑操作：<br>${logHtml}</div>`;
                            }
                        }
                    } else if (event.type === 'error') {
                        bubble.innerHTML = `<span style="color:#e74c3c">❌ ${event.content}</span>`;
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
        bubble.innerHTML = `<span style="color:#e74c3c">❌ 错误: ${err.message}</span>`;
    } finally {
        document.getElementById('btn-send').disabled = false;
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

// 清除对话
document.getElementById('btn-clear-chat').addEventListener('click', async () => {
    if (sessionId) {
        await fetch(`/api/chat/${sessionId}`, { method: 'DELETE' });
    }
    sessionId = '';
    chatMessages.innerHTML = '';
    addMessage('👋 对话已清除。告诉我你想做什么游戏吧！', 'ai');
});

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
document.getElementById('btn-new').addEventListener('click', () => {
    if (confirm('创建新项目？当前未保存的代码将丢失。')) {
        currentProjectId = '';
        editor.setValue('<!-- 新项目 - 开始设计你的游戏 -->\n');
        document.getElementById('game-preview').srcdoc = '';
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
