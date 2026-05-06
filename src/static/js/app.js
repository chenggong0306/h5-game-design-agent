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
    // 简单的 markdown 处理
    let html = content
        .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>')
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/\n/g, '<br>');
    div.innerHTML = `<div class="msg-content">${html}</div>`;
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

async function sendMessage() {
    const msg = chatInput.value.trim();
    if (!msg) return;

    addMessage(msg, 'user');
    chatInput.value = '';
    showLoading(true);

    try {
        const currentCode = editor ? editor.getValue() : '';
        const res = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: sessionId,
                message: msg,
                current_code: currentCode,
            }),
        });

        if (!res.ok) {
            // 尝试解析后端返回的错误详情
            let detail = `HTTP ${res.status}`;
            try {
                const errData = await res.json();
                detail = errData.detail || detail;
            } catch(_) {}
            throw new Error(detail);
        }
        const data = await res.json();
        sessionId = data.session_id;

        // 显示 AI 回复
        addMessage(data.reply, 'ai');

        // 如果有生成的代码，更新编辑器
        if (data.code) {
            editor.setValue(data.code);
            runGame(); // 自动运行
        }
    } catch (err) {
        addMessage(`❌ 错误: ${err.message}`, 'ai');
    } finally {
        showLoading(false);
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
        list.innerHTML = assets.map(a => `
            <div class="asset-item">
                <span class="name">${getTypeIcon(a.asset_type)} ${a.file_name}</span>
                <div class="actions">
                    <button onclick="deleteAsset('${a.id || a.asset_id}')" title="删除">🗑️</button>
                </div>
            </div>
        `).join('');
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
