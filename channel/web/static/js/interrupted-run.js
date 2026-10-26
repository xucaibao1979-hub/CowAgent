(() => {
  const original = window.showAutomationRun;
  let request = 0;
  window.showAutomationRun = async function(id) {
    const token = ++request;
    await original(id);
    try {
      const {run} = await intelligenceRequest(`/api/intelligence/runs?id=${encodeURIComponent(id)}`);
      if (token !== request || !run || (run.status !== 'running' && !run.restart_hold)) return;
      const host = document.getElementById('run-viewer-content');
      const panel = document.createElement('section');
      panel.className = 'mb-5 rounded-xl border border-orange-300 bg-orange-50 dark:bg-orange-900/20 p-4 text-sm';
      const message = document.createElement('p');
      message.textContent = run.restart_hold ? '已结束中断记录，定时重跑暂缓。重新执行可能重复之前的写入或发送，请核对下方产物、通知与日志。' : '若该执行已中断，请先核对下方产物、通知与日志。通知无记录不代表没有发送；后端仍在执行时，系统会拒绝结束记录。';
      panel.append(message);
      const button = document.createElement('button');
      button.className = 'mt-3 rounded-lg bg-orange-600 text-white px-3 py-2';
      button.textContent = run.restart_hold ? '核对后手动重试…' : '核对并结束中断记录…';
      button.onclick = async () => {
        if (run.restart_hold) {
          if (confirm('重新执行可能再次抓取、写入和发送。确认已核对上次结果，并授权重新执行？')) await retryAutomationRun(id, button);
          return;
        }
        const note = prompt('请填写产物与通知核对结果。此操作只结束中断记录，不重新执行、不补发；定时重跑将暂缓。');
        if (!note?.trim()) return;
        button.disabled = true;
        try {
          const result = await intelligenceRequest('/api/intelligence/runs', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'resolve_interrupted', run_id:id, confirmed:true, note})});
          alert(result.message);
          await window.showAutomationRun(id);
        } catch (error) { alert(error.message); }
        finally { button.disabled = false; }
      };
      panel.append(button);
      host.prepend(panel);
    } catch (_) { /* Existing detail viewer already reports load errors. */ }
  };
})();
