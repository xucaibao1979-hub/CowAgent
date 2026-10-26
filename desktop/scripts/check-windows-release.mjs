import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const strict = process.argv.includes('--strict');
const shouldWriteReport = process.argv.includes('--report');
const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

const sourceFiles = [
  {
    label: '前端入口页面',
    rel: ['channel', 'web', 'chat.html'],
    markers: ['P3 交付验收', 'automation-template-detail-modal'],
  },
  {
    label: '前端交互脚本',
    rel: ['channel', 'web', 'static', 'js', 'console.js'],
    markers: ['p3_windows_release_check', 'showAutomationTemplateDetail'],
  },
  {
    label: '自动化模板数据',
    rel: ['plugins', 'intelligence_center', 'templates.py'],
    markers: ['p3_windows_release_check', 'Windows 安装包验收'],
  },
  {
    label: '模板市场元数据',
    rel: ['plugins', 'intelligence_center', 'platform_features.py'],
    markers: ['TEMPLATE_MARKET_META', 'p3_windows_release_check'],
  },
  {
    label: '后端接口路由',
    rel: ['channel', 'web', 'web_channel.py'],
    markers: ['WindowsReleaseCheckHandler', '/api/release/windows-check'],
  },
];

const packageRoots = [
  ['PyInstaller 后端 dist', ['dist', 'cowagent-backend', '_internal']],
  ['桌面构建后端 dist', ['desktop', 'build', 'dist', 'cowagent-backend', '_internal']],
  ['Electron win-unpacked 后端', ['desktop', 'release', 'win-unpacked', 'resources', 'backend', 'cowagent-backend', '_internal']],
  ['Electron resources 后端', ['desktop', 'dist', 'win-unpacked', 'resources', 'backend', 'cowagent-backend', '_internal']],
].map(([label, parts]) => [label, path.join(sourceRoot, ...parts)]);

function fileInfo(filePath, markers = []) {
  const exists = fs.existsSync(filePath) && fs.statSync(filePath).isFile();
  if (!exists) {
    return { exists: false, path: filePath, size: 0, mtimeMs: 0, sha256: '', markers: markers.map(text => ({ text, found: false })) };
  }
  const buffer = fs.readFileSync(filePath);
  const stat = fs.statSync(filePath);
  const text = buffer.toString('utf8');
  return {
    exists: true,
    path: filePath,
    size: stat.size,
    mtimeMs: stat.mtimeMs,
    mtime: stat.mtime.toISOString(),
    sha256: crypto.createHash('sha256').update(buffer).digest('hex'),
    markers: markers.map(marker => ({ text: marker, found: text.includes(marker) })),
  };
}

function latestInstaller() {
  const releaseDir = path.join(sourceRoot, 'desktop', 'release');
  if (!fs.existsSync(releaseDir)) return null;
  const installers = fs.readdirSync(releaseDir)
    .filter(name => /^CowAgent-Setup-.*\.exe$/i.test(name))
    .map(name => {
      const full = path.join(releaseDir, name);
      const stat = fs.statSync(full);
      return { name, path: full, size: stat.size, mtimeMs: stat.mtimeMs, mtime: stat.mtime.toISOString() };
    })
    .sort((a, b) => b.mtimeMs - a.mtimeMs);
  return installers[0] || null;
}

function judge(source, targets) {
  const existing = targets.filter(item => item.info.exists);
  if (!existing.length) {
    return strict
      ? ['error', '未发现 Windows 打包输出目录，无法确认安装包是否包含二开版本。']
      : ['warning', '暂未发现 Windows 打包输出目录，请先完成打包后再验收。'];
  }
  if (existing.some(item => item.info.markers.some(marker => !marker.found))) {
    return ['error', '打包文件存在，但缺少二开特征点，可能仍是旧版本。'];
  }
  if (existing.some(item => item.info.mtimeMs + 1000 < source.mtimeMs) && !existing.some(item => item.info.sha256 === source.sha256)) {
    return ['warning', '打包文件时间早于源码，建议重新打包后复验。'];
  }
  if (existing.some(item => item.info.sha256 === source.sha256)) {
    return ['ok', '已找到与当前源码完全一致的打包文件。'];
  }
  return ['ok', '打包文件包含二开特征点，内容可能经过构建处理。'];
}

const existingRoots = packageRoots.filter(([, root]) => fs.existsSync(root) && fs.statSync(root).isDirectory());
const rootsToCheck = existingRoots.length ? existingRoots : packageRoots;
const checks = sourceFiles.map(spec => {
  const source = fileInfo(path.join(sourceRoot, ...spec.rel), spec.markers);
  const targets = rootsToCheck.map(([label, root]) => ({
    label,
    info: fileInfo(path.join(root, ...spec.rel), spec.markers),
  }));
  if (!source.exists) {
    return { ...spec, status: 'error', message: '源码文件不存在。', source, targets };
  }
  const [status, message] = judge(source, targets);
  return { ...spec, status, message, source, targets };
});

const installer = latestInstaller();
const summary = {
  ok: checks.filter(item => item.status === 'ok').length,
  warning: checks.filter(item => item.status === 'warning').length,
  error: checks.filter(item => item.status === 'error').length,
  total: checks.length,
};

function summaryText() {
  return `通过 ${summary.ok}/${summary.total}，警告 ${summary.warning}，错误 ${summary.error}`;
}

function writeReport() {
  const releaseDir = path.join(sourceRoot, 'desktop', 'release');
  fs.mkdirSync(releaseDir, { recursive: true });
  const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\..*/, '').replace('T', '-');
  const filePath = path.join(releaseDir, `windows-release-check-${stamp}.md`);
  const lines = [
    '# Windows 安装包验收报告',
    '',
    `生成时间：${new Date().toISOString()}`,
    `源码目录：${sourceRoot}`,
    `安装包：${installer ? installer.path : '未发现 CowAgent-Setup-*.exe'}`,
    '',
    '## 汇总',
    '',
    summaryText(),
    '',
    '## 检查项',
    '',
  ];
  const icons = { ok: '✅', warning: '⚠️', error: '❌' };
  for (const check of checks) {
    lines.push(`### ${icons[check.status] || '•'} ${check.label}`);
    lines.push('');
    lines.push(`状态：${check.message}`);
    lines.push(`源码：\`${check.source.path}\``);
    const existing = check.targets.filter(item => item.info.exists);
    if (existing.length) {
      lines.push('');
      lines.push('目标文件：');
      for (const target of existing) {
        const missing = target.info.markers.filter(marker => !marker.found).map(marker => marker.text);
        lines.push(`- ${target.label}：\`${target.info.path}\`${missing.length ? `；缺少特征点：${missing.join('、')}` : ''}`);
      }
    }
    lines.push('');
  }
  fs.writeFileSync(filePath, `${lines.join('\n').trim()}\n`, 'utf8');
  return filePath;
}

console.log('Windows 安装包验收');
console.log(`源码目录：${sourceRoot}`);
console.log(`安装包：${installer ? `${installer.name}（${Math.round(installer.size / 1024 / 1024)} MB，${installer.mtime}）` : '未发现 CowAgent-Setup-*.exe'}`);
console.log(`结果：${summaryText()}`);
for (const check of checks) {
  const icon = check.status === 'ok' ? '✅' : check.status === 'warning' ? '⚠️' : '❌';
  console.log(`${icon} ${check.label}：${check.message}`);
  for (const target of check.targets.filter(item => item.info.exists)) {
    const missing = target.info.markers.filter(marker => !marker.found).map(marker => marker.text);
    if (missing.length) console.log(`   - ${target.label} 缺少：${missing.join('、')}`);
  }
}

if (shouldWriteReport) {
  console.log(`报告：${writeReport()}`);
}

if (strict && (summary.error > 0 || !installer || summary.warning > 0)) {
  process.exitCode = 1;
}
