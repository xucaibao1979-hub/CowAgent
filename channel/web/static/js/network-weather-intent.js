/* Weather requests are not news. Keep the user's request as the source of truth. */
function weatherIntentDraft(text) {
  text = String(text || '').trim();
  if (!/天气|气温|降雨概率/.test(text)) return null;
  const time = text.match(/(?:(早上|上午|下午|晚上|中午)\s*)?(\d{1,2})(?:[:：](\d{2})|点(?:半|(\d{1,2})分?)?)/);
  let hour = time ? Number(time[2]) : 8;
  const minute = time ? Number(time[3] || time[4] || (/半/.test(time[0]) ? 30 : 0)) : 0;
  if (time && /下午|晚上|中午/.test(time[1] || '') && hour < 12) hour += 12;
  if (hour > 23 || minute > 59) throw new Error('执行时间无效，请填写有效的小时和分钟');
  const query = text.replace(/每天|每日|每个工作日|工作日/g, '').replace(/(?:早上|上午|下午|晚上|中午)?\s*\d{1,2}(?:[:：]\d{2}|点(?:半|\d{1,2}分?)?)/g, '')
    .replace(/发送|提醒|帮我|请|查询|查看|告诉我|发给我|当日|今天|情况/g, '').trim();
  return {name: query || '天气提醒', original: text, scheduled: /每天|每日|工作日|定时|每周|每隔/.test(text),
    frequency: /工作日/.test(text) ? 'weekday' : 'daily', runTime: `${String(hour).padStart(2,'0')}:${String(minute).padStart(2,'0')}`,
    content: `查询天气并生成天气提醒。用户原始要求：${text}\n以任务实际执行当天、目标地点的当地时间为准，识别用户指定的城市和区县，不得替换成其他地点。优先使用可用天气工具或气象机构数据，注明预报日期、地点、数据更新时间和真实来源。\n简短汇报天气、最高/最低气温、降雨概率、风力和出行提醒；未取得的字段明确标注，无法获取数据时说明原因，不编造。不是新闻检索，不生成个人早间简报或新闻列表。不输出检索过程，不生成占位链接；有真实来源时使用“链接”超链接。`};
}
if (typeof module !== 'undefined') module.exports = {weatherIntentDraft};
if (typeof window !== 'undefined') (() => {
  const previousParse = window.parseNetworkNaturalPrompt;
  let active = null;
  window.parseNetworkNaturalPrompt = async function() {
    const text = document.getElementById('network-natural-prompt')?.value || '';
    const status = document.getElementById('network-natural-status');
    try { active = weatherIntentDraft(text); } catch (error) { status.textContent = error.message; return; }
    if (!active) return previousParse();
    networkCompositeBriefingDraft = null;
    const f = networkSearchAgentFields();
    f.query.value = active.name; f.rules.value = ''; f.delivery.value = '当前 Web 会话';
    f.frequency.value = active.frequency === 'weekday' ? '工作日' : '每天'; f.runTime.value = active.runTime;
    networkSearchTags = {include:[], exclude:[], sites:[]};
    ['include','exclude','sites'].forEach(renderNetworkTags);
    setNetworkSourceMode('all'); setNetworkSearchMode(active.scheduled ? 'schedule' : 'once');
    saveNetworkSearchAgent();
    const host = document.getElementById('network-natural-draft');
    host.classList.remove('hidden'); host.replaceChildren();
    const title = document.createElement('h4'); title.className = 'font-semibold'; title.textContent = active.name;
    const summary = document.createElement('p'); summary.className = 'my-3 text-sm';
    summary.textContent = `${active.scheduled ? (active.frequency === 'weekday' ? '工作日' : '每天') + ' ' + active.runTime : '单次查询'} · 天气提醒 · 发送目标需在保存前确认`;
    const preview = document.createElement('pre'); preview.className = 'whitespace-pre-wrap text-xs'; preview.textContent = active.content;
    const once = document.createElement('button'); once.className = 'mt-3 mr-3 px-3 py-2 rounded-lg border'; once.textContent = '带入会话';
    once.onclick = () => {navigateTo('chat'); chatInput.value = active.content; chatInput.dispatchEvent(new Event('input',{bubbles:true})); chatInput.focus();};
    const schedule = document.createElement('button'); schedule.className = 'mt-3 px-3 py-2 rounded-lg bg-blue-600 text-white'; schedule.textContent = '检查定时配置';
    schedule.onclick = () => window.openWeatherSchedule();
    host.append(title, summary, preview, once, schedule);
    status.className = 'text-xs text-emerald-600'; status.textContent = '已识别为天气请求，未创建任务；不会使用新闻简报模板。';
  };
  window.openWeatherSchedule = async function() {
    if (!active) return;
    networkCompositeBriefingDraft = {...active};
    await openCompositeBriefingEditor();
    const modal = document.getElementById('network-composite-editor');
    if (!modal) return;
    modal.querySelector('header b').textContent = active.name;
    document.getElementById('composite-editor-frequency').value = active.frequency;
    modal.querySelector('details')?.remove();
    const banner = modal.querySelector('main .border-blue-200');
    if (banner) banner.textContent = '保存后按设置时间查询天气。请核对地点、频率及接收通道；程序需保持运行。';
    if (/每周|每隔/.test(active.original)) document.getElementById('composite-editor-status').textContent = '原请求包含周或间隔频率，请手动调整执行频率后保存。';
  };
  const oldBuild = window.buildNetworkSearchAgentPrompt;
  window.buildNetworkSearchAgentPrompt = data => weatherIntentDraft(data.query)?.content || oldBuild(data);
  const oldRouting = window.buildNetworkSearchPromptWithRouting;
  window.buildNetworkSearchPromptWithRouting = async data => weatherIntentDraft(data.query)?.content || oldRouting(data);
  const oldSave = window.saveCompositeBriefingEditor;
  window.saveCompositeBriefingEditor = async function() {
    const weather = !!weatherIntentDraft(document.getElementById('composite-editor-content')?.value);
    await oldSave();
    if (weather && !document.getElementById('network-composite-editor')) {
      document.getElementById('network-natural-status').textContent = '天气任务已创建，可在“定时任务”中查看和编辑。';
    }
  };
  const oldSchedule = window.createScheduledNetworkSearch;
  window.createScheduledNetworkSearch = function() {
    const f = networkSearchAgentFields();
    if ((!active || f.query.value !== active.name) && weatherIntentDraft(f.query.value)) {
      active = weatherIntentDraft(`${f.frequency.value} ${f.runTime.value} 查询${f.query.value}`);
    }
    if (active && networkSearchAgentFields().query.value === active.name) return window.openWeatherSchedule();
    return oldSchedule();
  };
})();
