/* Durable application inbox UI. Loaded after console.js so it can reuse shared helpers. */
let notificationPollTimer = null;
let notificationInboxItems = [];
let notificationHistoryItems = [];
let notificationCurrentSignature = '';
let notificationHistoryFullscreen = false;
let notificationHistoryFilters = { query: '', kind: '', status: '', unread: '', channel: '' };

const notificationText = (zh, en) => currentLang === 'en' ? en : zh;
const notificationKindIcon = { approval: 'fa-shield-halved', automation_result: 'fa-robot', system: 'fa-circle-info', system_notice: 'fa-triangle-exclamation' };

function notificationKindLabel(item) {
    if (item.kind === 'approval') return notificationText('审批消息', 'Approval');
    if (item.kind === 'automation_result') return notificationText('任务结果', 'Task result');
    return notificationText('系统消息', 'System message');
}

function notificationTone(item) {
    if (item.status === 'failed' || item.status === 'warning') return 'text-orange-500';
    if (item.kind === 'approval') return 'text-amber-600';
    return 'text-primary-500';
}

function notificationItemChannel(item) {
    const payload = item?.payload || {}, target = item?.target || {};
    return String(payload.channel || target.channel || payload.target || target.target || '').trim();
}

function notificationStatusLabel(item) {
    if (item.status === 'failed' || item.status === 'error') return notificationText('失败', 'Failed');
    if (item.status === 'warning') return notificationText('需关注', 'Warning');
    if (item.status === 'success') return notificationText('成功', 'Success');
    if (item.status === 'pending') return notificationText('待处理', 'Pending');
    return notificationText('信息', 'Info');
}

function renderNotificationItem(item, options = {}) {
    const history = !!options.history;
    const unreadDot = item.unread ? '<span class="inline-block w-2 h-2 rounded-full bg-primary-500"></span>' : '';
    const groupCount = Number(item.group_count || 0);
    const unreadCount = Number(item.group_unread_count || 0);
    const groupBadge = groupCount > 1 ? `<span class="shrink-0 rounded-full bg-orange-50 text-orange-600 dark:bg-orange-500/10 px-2 py-0.5 text-[10px]">${notificationText('24小时聚合', '24h merged')} ${groupCount}${unreadCount ? ` · ${notificationText('未读', 'unread')} ${unreadCount}` : ''}</span>` : '';
    const buttonClass = history
        ? 'w-full text-left px-4 py-3 rounded-xl border border-slate-100 dark:border-white/10 hover:border-primary-300 hover:bg-slate-50 dark:hover:bg-white/5'
        : `w-full text-left px-4 py-3 border-b border-slate-100 dark:border-white/5 hover:bg-slate-50 dark:hover:bg-white/5 ${item.unread ? 'bg-primary-50/50 dark:bg-primary-500/5' : ''}`;
    return `<button onclick="openInboxItem('${escapeHtml(item.id || '')}', ${history ? 'true' : 'false'})" class="${buttonClass}">
        <div class="flex justify-between gap-3">
            <span class="text-xs ${notificationTone(item)}"><i class="fas ${notificationKindIcon[item.kind] || 'fa-bell'} mr-1"></i>${notificationKindLabel(item)}</span>
            <span class="text-[10px] text-slate-400">${escapeHtml(item.updated_at || item.created_at || '')}</span>
        </div>
        <div class="mt-1 flex items-center gap-2">
            ${unreadDot}
            <b class="block text-sm truncate">${escapeHtml(item.title || notificationText('消息', 'Message'))}</b>
            ${groupBadge}
        </div>
        ${history ? `<div class="mt-1 flex flex-wrap gap-1.5 text-[10px] text-slate-400"><span class="rounded-full bg-slate-100 dark:bg-white/10 px-2 py-0.5">${escapeHtml(notificationStatusLabel(item))}</span>${notificationItemChannel(item) ? `<span class="rounded-full bg-slate-100 dark:bg-white/10 px-2 py-0.5">${escapeHtml(notificationItemChannel(item))}</span>` : ''}${item.unread ? `<span class="rounded-full bg-primary-50 dark:bg-primary-500/10 text-primary-600 px-2 py-0.5">${notificationText('未读', 'Unread')}</span>` : ''}</div>` : ''}
        <span class="block text-xs text-slate-400 mt-1 line-clamp-2">${escapeHtml(item.summary || notificationText('点击查看详情', 'Open details'))}</span>
    </button>`;
}

async function loadNotificationCenter() {
    const list = document.getElementById('notification-list');
    const badge = document.getElementById('notification-badge');
    if (!list || !badge) return;
    try {
        const data = await intelligenceRequest('/api/intelligence/inbox?unread=1&limit=30&grouped=1');
        const items = data.items || [];
        const unreadCount = Math.max(Number(data.unread || 0), items.length);
        notificationInboxItems = items;
        badge.textContent = unreadCount > 99 ? '99+' : unreadCount;
        badge.classList.toggle('hidden', !unreadCount);
        list.innerHTML = items.length
            ? items.map(item => renderNotificationItem(item)).join('')
            : `<div class="py-12 text-center text-slate-400 text-sm"><i class="fas fa-bell-slash text-2xl block mb-3"></i>${notificationText('暂无新消息', 'No new messages')}</div>`;
        notificationCurrentSignature = items.map(x => x.id).sort().join('|');
        const acknowledged = sessionStorage.getItem('cow_ack_inbox_signature') || '';
        const button = document.getElementById('notification-toggle');
        const shouldBlink = !!notificationCurrentSignature && notificationCurrentSignature !== acknowledged;
        button?.classList.toggle('animate-pulse', shouldBlink);
        button?.classList.toggle('text-amber-500', shouldBlink);
        button?.classList.toggle('ring-2', shouldBlink);
        button?.classList.toggle('ring-amber-300', shouldBlink);
    } catch (error) {
        list.innerHTML = `<div class="p-4 text-xs text-red-500">${escapeHtml(error.message)}</div>`;
    }
}

function acknowledgeNotificationBlink() {
    sessionStorage.setItem('cow_ack_inbox_signature', notificationCurrentSignature);
    const button = document.getElementById('notification-toggle');
    button?.classList.remove('animate-pulse', 'text-amber-500', 'ring-2', 'ring-amber-300');
}

function toggleNotificationCenter(event) {
    event?.stopPropagation();
    acknowledgeNotificationBlink();
    const menu = document.getElementById('notification-menu');
    menu.classList.toggle('hidden');
    if (!menu.classList.contains('hidden')) loadNotificationCenter();
}

async function openApprovalFromNotification(id) {
    document.getElementById('notification-menu')?.classList.add('hidden');
    closeInboxHistory();
    navigateTo('intelligence');
    switchAutomationTab('approvals');
    await loadAutomationApprovals();
    const card = document.querySelector(`[data-approval-id="${CSS.escape(id || '')}"]`);
    if (card) {
        card.scrollIntoView({ behavior: 'smooth', block: 'center' });
        card.classList.add('ring-2', 'ring-amber-400');
        setTimeout(() => card.classList.remove('ring-2', 'ring-amber-400'), 2200);
    }
}

async function openAutomationRunFromNotification(target, payload, item) {
    const runId = target?.id || target?.run_id || payload?.run_id || '';
    if (runId) {
        closeInboxHistory();
        navigateTo('intelligence');
        switchAutomationTab('runs');
        await loadAutomationRuns();
        await showAutomationRun(runId);
        return true;
    }
    return openAutomationRecordFromNotification(target, payload, item);
}

async function openAutomationRecordFromNotification(target, payload, item) {
    if (payload?.artifact_id && typeof openArtifactDetail === 'function') {
        closeInboxHistory();
        navigateTo('artifacts');
        setTimeout(() => openArtifactDetail(payload.artifact_id), 120);
        return true;
    }
    closeInboxHistory();
    navigateTo('intelligence');
    switchAutomationTab('records');
    const taskId = target?.task_id || payload?.task_id || '';
    const reportPath = payload?.report_path || target?.report_path || '';
    const title = item?.title || payload?.task_name || '';
    const query = taskId || title.replace(/·.*$/, '').trim();
    const input = document.getElementById('automation-record-query');
    const date = document.getElementById('automation-record-date');
    if (input) input.value = query || '';
    if (date) date.value = '';
    await loadAutomationRecords(true);
    const normalize = value => String(value || '').replace(/\\/g, '/').toLowerCase();
    let index = -1;
    if (reportPath) {
        const pathNeedle = normalize(reportPath).split('/').pop()?.replace(/\.md$/, '') || '';
        index = automationRecordItems.findIndex(record => normalize(record.id) === pathNeedle || normalize(record.content).includes(pathNeedle));
    }
    if (index < 0 && taskId) index = automationRecordItems.findIndex(record => record.task_id === taskId || String(record.id || '').includes(taskId));
    if (index < 0 && title) index = automationRecordItems.findIndex(record => String(record.title || '').includes(title.replace(/·.*$/, '').trim()));
    if (index >= 0) {
        showAutomationRecord(index);
        return true;
    }
    _wsToast?.(notificationText('已打开任务结果中心，请查看最新记录', 'Opened task results. Please check the latest record.'));
    return false;
}

async function openSettingsFromNotification(target) {
    document.getElementById('notification-menu')?.classList.add('hidden');
    closeInboxHistory();
    if (typeof openSettingsModal === 'function') openSettingsModal(target?.tab || 'channels');
    else if (typeof openSettingsCenter === 'function') {
        openSettingsCenter();
        if (target?.tab) setTimeout(() => showSettingsView?.(target.tab, target.tab), 80);
    } else if (typeof navigateTo === 'function') navigateTo('settings');
}

async function openRemoteBridgeFromNotification(target) {
    document.getElementById('notification-menu')?.classList.add('hidden');
    closeInboxHistory();
    if (typeof openSettingsCenter === 'function') {
        openSettingsCenter();
        setTimeout(() => showSettingsView?.('remote-bridge', '远程 Bridge'), 80);
    } else navigateTo('remote-bridge');
    await loadRemoteBridge?.(true);
    const commandId = target?.id || '';
    if (commandId) {
        const card = document.querySelector(`[data-remote-command-id="${CSS.escape(commandId)}"]`);
        if (card) {
            card.scrollIntoView({ behavior: 'smooth', block: 'center' });
            card.classList.add('ring-2', 'ring-emerald-400');
            setTimeout(() => card.classList.remove('ring-2', 'ring-emerald-400'), 2200);
        }
    }
}

async function openMaintenanceFromNotification(target) {
    document.getElementById('notification-menu')?.classList.add('hidden');
    closeInboxHistory();
    if (typeof openSettingsCenter === 'function') {
        openSettingsCenter();
        setTimeout(() => showSettingsView?.('maintenance', '维护看板'), 80);
    } else navigateTo('maintenance');
    setTimeout(async () => {
        await loadMaintenanceDashboard?.(true);
        if (target?.action === 'backend_instances') setTimeout(() => maintenanceDashboardCheckBackend?.(), 250);
    }, 120);
}

function ensureInboxResultModal() {
    let modal = document.getElementById('inbox-result-modal');
    if (modal) return modal;
    modal = document.createElement('div');
    modal.id = 'inbox-result-modal';
    modal.className = 'hidden fixed inset-0 z-[220] bg-black/55 p-3 sm:p-6 items-center justify-center';
    modal.innerHTML = `<div class="w-full max-w-4xl max-h-[90vh] rounded-2xl bg-white dark:bg-[#181818] shadow-2xl flex flex-col overflow-hidden"><header class="flex items-center justify-between gap-3 px-5 py-4 border-b border-slate-200 dark:border-white/10"><div class="min-w-0"><h3 id="inbox-result-title" class="font-bold truncate"></h3><p id="inbox-result-meta" class="text-xs text-slate-400 mt-1"></p></div><div class="flex items-center gap-1"><button onclick="copyInboxResult()" class="px-3 py-2 rounded-lg hover:bg-slate-100 dark:hover:bg-white/10 text-sm"><i class="far fa-copy mr-1"></i>${notificationText('复制', 'Copy')}</button><button onclick="closeInboxResult()" class="w-9 h-9 rounded-lg hover:bg-slate-100 dark:hover:bg-white/10"><i class="fas fa-times"></i></button></div></header><div id="inbox-result-content" class="flex-1 overflow-y-auto p-5 sm:p-7 whitespace-pre-wrap text-sm leading-7 select-text"></div></div>`;
    modal.addEventListener('click', event => { if (event.target === modal) closeInboxResult(); });
    document.body.appendChild(modal);
    return modal;
}

function showInboxResult(item) {
    closeInboxHistory();
    const modal = ensureInboxResultModal();
    document.getElementById('inbox-result-title').textContent = item.title || notificationText('任务结果', 'Task result');
    document.getElementById('inbox-result-meta').textContent = item.updated_at || item.created_at || '';
    document.getElementById('inbox-result-content').textContent = item.summary || notificationText('暂无结果内容', 'No result content');
    modal.classList.remove('hidden');
    modal.classList.add('flex');
}

function closeInboxResult() {
    const modal = document.getElementById('inbox-result-modal');
    modal?.classList.add('hidden');
    modal?.classList.remove('flex');
}

async function copyInboxResult() {
    const text = document.getElementById('inbox-result-content')?.innerText || '';
    if (!text) return;
    try {
        await navigator.clipboard.writeText(text);
    } catch (_error) {
        const selection = window.getSelection(), range = document.createRange();
        range.selectNodeContents(document.getElementById('inbox-result-content'));
        selection.removeAllRanges();
        selection.addRange(range);
    }
}

function findInboxItem(id, history = false) {
    return (history ? notificationHistoryItems : notificationInboxItems).find(x => x.id === id)
        || notificationInboxItems.find(x => x.id === id)
        || notificationHistoryItems.find(x => x.id === id);
}

async function openInboxItem(id, fromHistory = false) {
    const item = findInboxItem(id, fromHistory);
    if (!item) return;
    const groupIds = Array.isArray(item.group_ids) ? item.group_ids.filter(Boolean) : [];
    const readPayload = groupIds.length ? { action: 'read_many', ids: groupIds } : { action: 'read', id };
    await intelligenceRequest('/api/intelligence/inbox', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(readPayload) });
    item.unread = false;
    document.getElementById('notification-menu')?.classList.add('hidden');
    loadNotificationCenter();
    if (fromHistory) loadInboxHistory();
    const target = item.target || {}, payload = item.payload || {};
    if (target.view === 'automation' || target.tab) {
        if (target.tab === 'approvals' || item.kind === 'approval') await openApprovalFromNotification(target.id || payload.approval_id || item.source_id || '');
        else if (target.tab === 'runs') await openAutomationRunFromNotification(target, payload, item);
        else if (target.tab === 'records' || item.kind === 'automation_result') await openAutomationRecordFromNotification(target, payload, item);
        else showInboxResult(item);
    } else if (target.view === 'settings') await openSettingsFromNotification(target);
    else if (target.view === 'remote-bridge') await openRemoteBridgeFromNotification(target);
    else if (target.view === 'maintenance') await openMaintenanceFromNotification(target);
    else showInboxResult(item);
}

async function markAllInboxRead(event) {
    event?.stopPropagation();
    await intelligenceRequest('/api/intelligence/inbox', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'read_all' }) });
    notificationInboxItems = [];
    await loadNotificationCenter();
    if (!document.getElementById('inbox-history-modal')?.classList.contains('hidden')) await loadInboxHistory();
    acknowledgeNotificationBlink();
}

function ensureInboxHistoryModal() {
    let modal = document.getElementById('inbox-history-modal');
    if (modal) return modal;
    modal = document.createElement('div');
    modal.id = 'inbox-history-modal';
    modal.className = 'hidden fixed inset-0 z-[210] bg-black/55 p-3 sm:p-6 items-center justify-center';
    modal.innerHTML = `<div id="inbox-history-panel" class="w-full max-w-6xl max-h-[90vh] rounded-2xl bg-white dark:bg-[#181818] shadow-2xl flex flex-col overflow-hidden">
        <header class="flex items-center justify-between gap-3 px-5 py-4 border-b border-slate-200 dark:border-white/10">
            <div class="min-w-0"><h3 class="font-bold truncate">${notificationText('历史消息', 'Message history')}</h3><p id="inbox-history-meta" class="text-xs text-slate-400 mt-1"></p></div>
            <div class="flex items-center gap-1">
                <button onclick="loadInboxHistory()" class="px-3 py-2 rounded-lg hover:bg-slate-100 dark:hover:bg-white/10 text-sm"><i class="fas fa-rotate mr-1"></i>${notificationText('刷新', 'Refresh')}</button>
                <button onclick="toggleInboxHistoryFullscreen()" class="px-3 py-2 rounded-lg hover:bg-slate-100 dark:hover:bg-white/10 text-sm"><i id="inbox-history-fullscreen-icon" class="fas fa-expand mr-1"></i>${notificationText('全屏', 'Fullscreen')}</button>
                <button onclick="closeInboxHistory()" class="w-9 h-9 rounded-lg hover:bg-slate-100 dark:hover:bg-white/10"><i class="fas fa-times"></i></button>
            </div>
        </header>
        <div class="grid md:grid-cols-5 gap-2 px-4 py-3 border-b border-slate-100 dark:border-white/10 bg-slate-50/70 dark:bg-white/[0.03]">
            <label class="md:col-span-2 relative"><i class="fas fa-search absolute left-3 top-3 text-xs text-slate-400"></i><input id="inbox-history-query" oninput="applyInboxHistoryFilters()" class="w-full rounded-lg border border-slate-200 dark:border-white/10 bg-white dark:bg-[#202020] py-2 pl-9 pr-3 text-sm" placeholder="${notificationText('按任务/标题/内容搜索', 'Search task/title/content')}"></label>
            <select id="inbox-history-kind" onchange="applyInboxHistoryFilters()" class="rounded-lg border border-slate-200 dark:border-white/10 bg-white dark:bg-[#202020] px-3 py-2 text-sm"><option value="">${notificationText('全部类型', 'All types')}</option><option value="automation_result">${notificationText('任务结果', 'Task results')}</option><option value="system_notice">${notificationText('系统消息', 'System notices')}</option><option value="approval">${notificationText('审批消息', 'Approvals')}</option></select>
            <select id="inbox-history-status" onchange="applyInboxHistoryFilters()" class="rounded-lg border border-slate-200 dark:border-white/10 bg-white dark:bg-[#202020] px-3 py-2 text-sm"><option value="">${notificationText('全部状态', 'All status')}</option><option value="success">${notificationText('成功', 'Success')}</option><option value="failed">${notificationText('失败', 'Failed')}</option><option value="warning">${notificationText('需关注', 'Warning')}</option><option value="pending">${notificationText('待处理', 'Pending')}</option></select>
            <select id="inbox-history-unread" onchange="applyInboxHistoryFilters()" class="rounded-lg border border-slate-200 dark:border-white/10 bg-white dark:bg-[#202020] px-3 py-2 text-sm"><option value="">${notificationText('全部已读状态', 'All read states')}</option><option value="1">${notificationText('未读', 'Unread')}</option><option value="0">${notificationText('已读', 'Read')}</option></select>
            <select id="inbox-history-channel" onchange="applyInboxHistoryFilters()" class="md:col-span-5 rounded-lg border border-slate-200 dark:border-white/10 bg-white dark:bg-[#202020] px-3 py-2 text-sm"><option value="">${notificationText('全部通道', 'All channels')}</option></select>
        </div>
        <div id="inbox-history-list" class="flex-1 overflow-y-auto p-4 space-y-2"></div>
    </div>`;
    modal.addEventListener('click', event => { if (event.target === modal) closeInboxHistory(); });
    document.body.appendChild(modal);
    return modal;
}

function inboxHistoryFilterValues() {
    return {
        query: (document.getElementById('inbox-history-query')?.value || '').trim().toLowerCase(),
        kind: document.getElementById('inbox-history-kind')?.value || '',
        status: document.getElementById('inbox-history-status')?.value || '',
        unread: document.getElementById('inbox-history-unread')?.value || '',
        channel: document.getElementById('inbox-history-channel')?.value || '',
    };
}

function updateInboxHistoryChannelOptions(items) {
    const select = document.getElementById('inbox-history-channel');
    if (!select) return;
    const previous = select.value;
    const channels = [...new Set((items || []).map(notificationItemChannel).filter(Boolean))].sort();
    select.innerHTML = `<option value="">${notificationText('全部通道', 'All channels')}</option>` + channels.map(x => `<option value="${escapeHtml(x)}">${escapeHtml(x)}</option>`).join('');
    if (channels.includes(previous)) select.value = previous;
}

function filteredInboxHistoryItems() {
    const filters = inboxHistoryFilterValues();
    notificationHistoryFilters = filters;
    return (notificationHistoryItems || []).filter(item => {
        if (filters.kind && item.kind !== filters.kind) return false;
        if (filters.status) {
            const status = String(item.status || '');
            if (filters.status === 'failed' && !['failed', 'error'].includes(status)) return false;
            else if (filters.status !== 'failed' && status !== filters.status) return false;
        }
        if (filters.unread !== '') {
            const unread = !!item.unread || Number(item.group_unread_count || 0) > 0;
            if (filters.unread === '1' && !unread) return false;
            if (filters.unread === '0' && unread) return false;
        }
        if (filters.channel && notificationItemChannel(item) !== filters.channel) return false;
        if (filters.query) {
            const payload = item.payload || {}, target = item.target || {};
            const haystack = `${item.title || ''} ${item.summary || ''} ${payload.task_name || ''} ${payload.task_id || ''} ${target.task_id || ''}`.toLowerCase();
            if (!haystack.includes(filters.query)) return false;
        }
        return true;
    });
}

function renderInboxHistoryList(data = {}) {
    const list = document.getElementById('inbox-history-list');
    const meta = document.getElementById('inbox-history-meta');
    if (!list) return;
    updateInboxHistoryChannelOptions(notificationHistoryItems);
    const filtered = filteredInboxHistoryItems();
    const total = data.total || notificationHistoryItems.length;
    const unread = data.unread || 0;
    if (meta) meta.textContent = notificationText(`共 ${total} 组，当前筛选 ${filtered.length} 组，未读 ${unread} 组`, `${total} groups · ${filtered.length} shown · ${unread} unread`);
    list.innerHTML = filtered.length
        ? filtered.map(item => renderNotificationItem(item, { history: true })).join('')
        : `<div class="py-12 text-center text-slate-400 text-sm"><i class="fas fa-filter-circle-xmark text-2xl block mb-3"></i>${notificationText('没有符合筛选条件的历史消息', 'No messages match these filters')}</div>`;
}

function applyInboxHistoryFilters() {
    renderInboxHistoryList();
}

async function loadInboxHistory() {
    const modal = ensureInboxHistoryModal();
    const list = document.getElementById('inbox-history-list');
    const meta = document.getElementById('inbox-history-meta');
    if (!list) return;
    list.innerHTML = `<div class="py-12 text-center text-slate-400 text-sm"><i class="fas fa-spinner fa-spin text-2xl block mb-3"></i>${notificationText('正在读取历史消息…', 'Loading history...')}</div>`;
    try {
        const data = await intelligenceRequest('/api/intelligence/inbox?limit=100&grouped=1');
        notificationHistoryItems = data.items || [];
        renderInboxHistoryList(data);
    } catch (error) {
        list.innerHTML = `<div class="p-4 text-xs text-red-500">${escapeHtml(error.message || String(error))}</div>`;
    }
    modal.classList.remove('hidden');
    modal.classList.add('flex');
}

function openInboxHistory(event) {
    event?.stopPropagation();
    document.getElementById('notification-menu')?.classList.add('hidden');
    loadInboxHistory();
}

function closeInboxHistory() {
    const modal = document.getElementById('inbox-history-modal');
    modal?.classList.add('hidden');
    modal?.classList.remove('flex');
}

function toggleInboxHistoryFullscreen() {
    const panel = document.getElementById('inbox-history-panel');
    const icon = document.getElementById('inbox-history-fullscreen-icon');
    if (!panel) return;
    notificationHistoryFullscreen = !notificationHistoryFullscreen;
    panel.classList.toggle('max-w-6xl', !notificationHistoryFullscreen);
    panel.classList.toggle('max-h-[90vh]', !notificationHistoryFullscreen);
    panel.classList.toggle('h-[calc(100vh-1.5rem)]', notificationHistoryFullscreen);
    panel.classList.toggle('max-w-none', notificationHistoryFullscreen);
    icon?.classList.toggle('fa-expand', !notificationHistoryFullscreen);
    icon?.classList.toggle('fa-compress', notificationHistoryFullscreen);
}

document.addEventListener('click', event => {
    const center = document.getElementById('notification-center');
    if (center && !center.contains(event.target)) document.getElementById('notification-menu')?.classList.add('hidden');
});

document.addEventListener('DOMContentLoaded', () => {
    loadNotificationCenter();
    notificationPollTimer = setInterval(loadNotificationCenter, 5000);
});
