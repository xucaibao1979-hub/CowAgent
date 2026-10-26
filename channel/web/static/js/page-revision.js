(() => {
  const initial = document.querySelector('meta[name="cow-page-revision"]')?.content;
  if (!initial || initial.includes('{{')) return;
  let shown = false;
  async function check() {
    if (document.hidden || shown) return;
    try {
      const response = await fetch('/api/health', {cache: 'no-store'});
      if (!response.ok) return;
      const state = await response.json();
      if (!state.page_revision || state.page_revision === initial) return;
      shown = true;
      const english = (localStorage.getItem('cow_lang') || document.documentElement.lang || '').startsWith('en');
      const notice = document.createElement('aside');
      notice.setAttribute('role', 'status');
      notice.style.cssText = 'position:fixed;right:20px;bottom:24px;max-width:360px;padding:14px;border-radius:12px;background:#1e3a8a;color:#fff;z-index:10000;box-shadow:0 4px 18px #0004;font-size:14px';
      const label = document.createElement('p');
      label.textContent = english ? 'A local update is available. Finish your work and save drafts before refreshing. This page will not refresh automatically.' : '本地页面已更新。请完成当前操作、保存草稿后刷新；不会自动刷新或中断会话。';
      const button = document.createElement('button');
      button.textContent = english ? 'Refresh…' : '刷新页面…';
      button.style.cssText = 'margin-right:12px;text-decoration:underline;color:inherit';
      button.onclick = () => {
        if (confirm(english ? 'Refreshing may disconnect an active response and discard unsaved drafts. Refresh now?' : '刷新可能中断当前页面接收回复，并丢失未保存草稿。确定现在刷新？')) location.reload();
      };
      const dismiss = document.createElement('button');
      dismiss.textContent = english ? 'Later' : '稍后处理';
      dismiss.onclick = () => notice.remove();
      notice.append(label, button, dismiss);
      document.body.append(notice);
    } catch (_) { /* Offline or restarting: never interrupt the conversation. */ }
  }
  setInterval(check, 60000);
  document.addEventListener('visibilitychange', check);
  check();
})();
