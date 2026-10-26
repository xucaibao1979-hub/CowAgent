const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'channel/web/static/js/console.js'), 'utf8');
const save = source.split(/\r?\n/).find(x => x.startsWith('async function saveCompositeBriefingEditor('));
const backend = fs.readFileSync(path.join(root, 'channel/web/web_channel.py'), 'utf8');
const allowed = backend.match(/if action_type not in \(([^)]+)\):raise ValueError\("不支持的任务内容类型"\)/)[1];
async function check(mode, targetValue, frequency='daily') {
  const elements = {
    'network-composite-editor': {dataset:{scheduleMode:mode}},
    'composite-editor-name': {value:'上海浦东新区天气'},
    'composite-editor-content': {value:'每天早上8:00发送上海浦东新区天气情况'},
    'composite-editor-target': {value:targetValue},
    'composite-editor-time': {value:'08:00'},
    'composite-editor-frequency': {value:frequency},
    'composite-editor-interval': {value:'2'},
    'composite-editor-interval-unit': {value:'3600'},
    'composite-editor-once': {value:'2030-01-01T08:00'},
    'composite-editor-save': {}, 'composite-editor-status': {}, 'network-natural-status': {}
  };
  let payload, closed=false;
  const ctx = {document:{getElementById:id=>elements[id]}, sessionId:'test-web', tasksLoaded:true,
    networkCompositeTargets:[{channel:'weixin', receiver:'test-recipient', receiver_name:'隔离测试'}],
    closeCompositeBriefingEditor:()=>{closed=true;},
    fetch:async (url, options)=>{assert.equal(url,'/api/scheduler'); payload=JSON.parse(options.body); return {json:async()=>({status:'success'})};}};
  vm.createContext(ctx); vm.runInContext(save,ctx); await ctx.saveCompositeBriefingEditor();
  assert.equal(payload.action_type, 'agent_task');
  assert.ok(allowed.includes('"'+payload.action_type+'"'));
  assert.equal(payload.content,elements['composite-editor-content'].value);
  assert.equal(payload.channel_type,targetValue==='web'?'web':'weixin');
  assert.equal(payload.receiver,targetValue==='web'?'test-web':'test-recipient');
  if(mode==='daily') assert.equal(payload.cron,frequency==='weekday'?'0 8 * * 1-5':'0 8 * * *');
  if(mode==='interval') assert.equal(payload.interval_seconds,7200);
  if(mode==='once') assert.equal(payload.run_at,'2030-01-01T08:00');
  assert.ok(closed);
}
(async()=>{await check('daily','target:0'); await check('daily','web','weekday'); await check('interval','web'); await check('once','web'); console.log('4 scheduler payload cases passed; no real requests sent');})().catch(e=>{console.error(e);process.exitCode=1;});
