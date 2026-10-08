/* bili-dl Web 界面逻辑 —— 纯原生 JS，无任何外部依赖 */

'use strict';

// ------------------------------------------------------------------ 基础 ---
const TOKEN = new URLSearchParams(location.search).get('token') || '';
const $ = (id) => document.getElementById(id);

let STATE = null;              // /api/state 的返回
let PREVIEW = null;            // 最近一次解析结果
const jobViews = new Map();    // jobId -> { el, logSeq, logsOpen, filesShown }

// ------------------------------------------------------------------ 工具 ---
function toast(msg, isError) {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast show' + (isError ? ' error' : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = 'toast'; }, isError ? 5200 : 2600);
}

async function api(path, body) {
  const opt = {
    headers: { 'X-Bili-Token': TOKEN },
    cache: 'no-store',
  };
  if (body !== undefined) {
    opt.method = 'POST';
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opt);
  let data = null;
  try { data = await resp.json(); } catch (e) { /* 非 JSON */ }
  if (!resp.ok || !data || data.ok === false) {
    const msg = (data && data.error) || `请求失败（HTTP ${resp.status}）`;
    throw new Error(msg);
  }
  return data.data;
}

function fmtSize(n) {
  if (!n || n < 0) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0; n = Number(n);
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n.toFixed(0) : n.toFixed(2)) + u[i];
}

function fmtSpeed(n) { return (!n || n <= 0) ? '' : fmtSize(n) + '/s'; }

function fmtDur(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  const p = (x) => String(x).padStart(2, '0');
  return h ? `${h}:${p(m)}:${p(s)}` : `${m}:${p(s)}`;
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// -------------------------------------------------------------- 初始化 ---
if (!TOKEN) {
  document.body.innerHTML =
    '<div style="padding:60px;text-align:center;font-size:16px;">' +
    '缺少访问令牌。<br><br>请使用启动时终端里打印的完整地址打开本页面' +
    '（形如 <code>http://127.0.0.1:8765/?token=xxxx</code>）。</div>';
  throw new Error('no token');
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $('btn-theme').textContent = theme === 'dark' ? '🌙' : '☀️';
  localStorage.setItem('bili-theme', theme);
}
applyTheme(localStorage.getItem('bili-theme') ||
  (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'));

$('btn-theme').onclick = () =>
  applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');

// ---------------------------------------------------------------- 状态 ---
async function loadState() {
  STATE = await api('/api/state');
  $('ver').textContent = 'v' + STATE.version;

  const login = $('pill-login');
  if (STATE.logged_in) {
    login.className = 'pill ok';
    login.innerHTML = `<span class="dot"></span>已登录 <b>${escapeHtml(STATE.uname || '')}</b>`;
  } else {
    login.className = 'pill warn';
    login.innerHTML = '<span class="dot"></span>未登录 · 最高 720P';
    login.title = '想要 1080P 以上 / 收藏夹 / 投稿列表 / 字幕，需要配置 SESSDATA Cookie';
  }

  const ff = $('pill-ffmpeg');
  if (STATE.ffmpeg_ok) {
    ff.className = 'pill ok';
    ff.innerHTML = '<span class="dot"></span>ffmpeg 就绪';
    ff.title = STATE.ffmpeg;
  } else {
    ff.className = 'pill warn';
    ff.innerHTML = '<span class="dot"></span>无 ffmpeg';
    ff.title = '未找到 ffmpeg，将使用 MP4 合流（拿不到 1080P60 / 4K / HDR）';
  }

  // 画质下拉
  const sel = $('opt-quality');
  sel.innerHTML = '';
  (STATE.qualities || []).forEach((q) => {
    const o = document.createElement('option');
    o.value = q.value; o.textContent = q.label;
    if (q.value === STATE.settings.quality) o.selected = true;
    sel.appendChild(o);
  });

  const s = STATE.settings;
  $('opt-codec').value = s.codec || 'avc';
  $('opt-cover').checked = !!s.cover;
  $('opt-danmaku').checked = !!s.danmaku;
  $('opt-subtitle').checked = !!s.subtitle;
  $('opt-audio').checked = !!s.audio_only;
  $('opt-video').checked = !!s.video_only;
  $('opt-maxitems').value = s.max_items || 0;
  $('set-outdir').value = s.output_dir || '';
  $('set-workers').value = s.workers || 8;
  $('set-maxitems').value = s.max_items || 0;
  $('pv-outdir').textContent = '保存到：' + (s.output_dir || '');
}

// ---------------------------------------------------------------- 解析 ---
let parsing = false;

async function doParse() {
  const input = $('input-url').value.trim();
  if (!input) { toast('请先输入链接或关键字', true); return; }
  if (parsing) return;
  parsing = true;
  const btn = $('btn-parse');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 解析中';

  try {
    const data = await api('/api/parse', { input });
    PREVIEW = data;
    renderPreview(data);
  } catch (err) {
    toast(err.message, true);
    $('preview-card').hidden = true;
  } finally {
    parsing = false;
    btn.disabled = false;
    btn.textContent = '解析';
  }
}

function renderPreview(data) {
  const card = $('preview-card');
  card.hidden = false;

  const isCollection = data.kind === 'collection';
  const cover = (data.cover || '').replace(/^http:/, 'https:');
  const img = $('pv-cover');
  if (cover) { img.src = cover; img.style.display = ''; }
  else { img.removeAttribute('src'); img.style.display = isCollection ? 'none' : ''; }

  $('pv-title').textContent = data.title || data.describe || data.input;

  const bits = [];
  if (data.owner) bits.push('UP主：' + data.owner);
  if (data.duration) bits.push('时长 ' + fmtDur(data.duration));
  if (data.page_count > 1) bits.push(data.page_count + ' 个分P');
  if (isCollection) bits.push((data.source || '列表') + '：共 ' + (data.total || 0) + ' 个视频');
  $('pv-sub').innerHTML = bits.map((b) => escapeHtml(b)).join(' &nbsp;·&nbsp; ');

  $('pv-desc').textContent = data.desc || '';
  $('pv-desc').style.display = data.desc ? '' : 'none';

  // 分P / 条目数
  $('wrap-pages').hidden = !(data.page_count > 1);
  $('wrap-maxitems').hidden = !isCollection;
  if (isCollection && !$('opt-maxitems').value) {
    $('opt-maxitems').value = STATE.settings.max_items || 0;
  }

  // 条目预览列表
  const box = $('pv-items');
  if (isCollection && (data.items || []).length) {
    box.hidden = false;
    box.innerHTML = data.items.map((it, i) =>
      `<div class="row"><span class="idx">${i + 1}</span>` +
      `<span class="ttl">${escapeHtml(it.title || it.bvid)}</span></div>`).join('') +
      (data.preview_limited
        ? '<div class="row"><span class="idx">…</span><span class="ttl">仅预览前若干条，实际会下载全部</span></div>'
        : '');
  } else {
    box.hidden = true;
    box.innerHTML = '';
  }

  // 画质提示
  renderQualityNote(data);
  $('pv-outdir').textContent = '保存到：' + (STATE.settings.output_dir || '');
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderQualityNote(data) {
  const note = $('pv-quality-note');
  const q = data.qualities;
  if (!q) {
    note.innerHTML = data.qualities_error
      ? '未能探测可用画质：' + escapeHtml(data.qualities_error)
      : '';
    return;
  }
  const parts = [];
  const chosen = q.auto || {};
  if (chosen.label) {
    parts.push(`自动选择：<b>${escapeHtml(chosen.label)}</b>` +
      `（${chosen.mode === 'progressive' ? 'MP4 合流' : 'DASH 分离流'}）`);
  }
  const dashList = (q.dash || []).map((d) => d.label);
  if (dashList.length) parts.push('DASH 可用：' + dashList.map(escapeHtml).join(' / '));
  if (q.progressive) parts.push('合流可用：' + escapeHtml(q.progressive.label));
  if (!q.ffmpeg) parts.push('⚠️ 未装 ffmpeg，1080P 以上需要它');
  note.innerHTML = parts.join('<br>');
}

$('btn-parse').onclick = doParse;
$('input-url').addEventListener('keydown', (e) => { if (e.key === 'Enter') doParse(); });
$('btn-cancel-parse').onclick = () => { $('preview-card').hidden = true; PREVIEW = null; };

// ---------------------------------------------------------------- 下载 ---
$('btn-download').onclick = async () => {
  if (!PREVIEW) return;
  const btn = $('btn-download');
  btn.disabled = true;
  try {
    const options = {
      quality: parseInt($('opt-quality').value, 10) || 127,
      codec: $('opt-codec').value,
      cover: $('opt-cover').checked,
      danmaku: $('opt-danmaku').checked,
      subtitle: $('opt-subtitle').checked,
      audio_only: $('opt-audio').checked,
      video_only: $('opt-video').checked,
      output_dir: STATE.settings.output_dir,
      pages: parsePages($('opt-pages').value),
      max_items: parseInt($('opt-maxitems').value, 10) || 0,
    };
    await api('/api/jobs', { input: PREVIEW.input, options });
    toast('已加入下载队列');
    $('preview-card').hidden = true;
    PREVIEW = null;
    pollJobs();
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
};

function parsePages(text) {
  text = (text || '').trim();
  if (!text) return null;
  const out = [];
  text.split(',').forEach((chunk) => {
    chunk = chunk.trim();
    if (!chunk) return;
    const m = chunk.match(/^(\d+)\s*-\s*(\d+)$/);
    if (m) {
      const a = +m[1], b = +m[2];
      for (let i = Math.min(a, b); i <= Math.max(a, b); i++) out.push(i);
    } else if (/^\d+$/.test(chunk)) {
      out.push(+chunk);
    }
  });
  return out.length ? [...new Set(out)].sort((a, b) => a - b) : null;
}

// ---------------------------------------------------------------- 任务 ---
async function pollJobs() {
  if (document.hidden) return;
  let jobs;
  try {
    jobs = await api('/api/jobs');
  } catch (err) {
    return;
  }

  const box = $('jobs');
  const seen = new Set();

  jobs.forEach((job) => {
    seen.add(job.id);
    let view = jobViews.get(job.id);
    if (!view) {
      const node = $('tpl-job').content.firstElementChild.cloneNode(true);
      box.appendChild(node);
      view = { el: node, logSeq: 0, logsOpen: false, filesShown: false };
      jobViews.set(job.id, view);
      node.querySelector('.logs').hidden = true;
    }
    updateJobView(view, job);
  });

  // 清掉已被移除的任务节点
  jobViews.forEach((view, id) => {
    if (!seen.has(id)) { view.el.remove(); jobViews.delete(id); }
  });

  const empty = box.querySelector('.empty');
  if (jobs.length && empty) empty.remove();
  if (!jobs.length && !box.querySelector('.empty')) {
    box.innerHTML = '<div class="empty">还没有任务，在上面粘贴一个链接试试</div>';
  }
  $('jobs-count').textContent = jobs.length + ' 个';

  // 抓取运行中 / 展开日志的任务的新日志
  for (const job of jobs) {
    const view = jobViews.get(job.id);
    if (!view) continue;
    if (job.status === 'running' || view.logsOpen) {
      try {
        const full = await api(`/api/jobs/${job.id}?since=${view.logSeq}`);
        if (full.logs && full.logs.length) {
          appendLogs(view, full.logs);
          view.logSeq = full.logs[full.logs.length - 1].seq;
        }
      } catch (e) { /* 忽略 */ }
    }
  }
}

function updateJobView(view, job) {
  const el = view.el;
  el.className = 'job ' + job.status;

  el.querySelector('.title').textContent = job.title || job.input;
  const badge = el.querySelector('.badge');
  badge.className = 'badge ' + job.status;
  badge.textContent = job.status_text;

  el.querySelector('.src').textContent = job.input +
    '  ·  ' + job.quality_label + '  ·  ' + fmtTime(job.created_at);

  const p = job.progress || {};
  const pct = p.percent || 0;
  el.querySelector('.bar > i').style.width = pct.toFixed(1) + '%';

  // 统计行
  const stats = el.querySelector('.job-stats');
  const bits = [];
  if (job.status === 'running') {
    if (p.total) {
      bits.push(`<b>${pct.toFixed(1)}%</b>`);
      bits.push(`${fmtSize(p.done)} / ${fmtSize(p.total)}`);
      if (p.speed) {
        bits.push(fmtSpeed(p.speed));
        const left = (p.total - p.done) / p.speed;
        if (isFinite(left) && left > 0) bits.push('剩余 ' + fmtDur(left));
      }
    } else if (p.done) {
      bits.push('已下载 ' + fmtSize(p.done));
      if (p.speed) bits.push(fmtSpeed(p.speed));
    }
    if (p.label) bits.push(escapeHtml(p.label));
  } else if (job.status === 'queued') {
    bits.push('等待前面的任务…');
  } else {
    bits.push('用时 ' + fmtDur(job.elapsed));
    if (job.stats && (job.stats.ok || job.stats.skip || job.stats.fail)) {
      bits.push(`成功 <b>${job.stats.ok}</b> · 跳过 <b>${job.stats.skip}</b> · 失败 <b>${job.stats.fail}</b>`);
    }
    if (job.error) bits.push(`<span style="color:var(--err)">${escapeHtml(job.error)}</span>`);
  }
  stats.innerHTML = bits.join(' &nbsp;·&nbsp; ');

  // 按钮
  const actions = el.querySelector('.job-actions');
  const want = [];
  if (job.status === 'running' || job.status === 'queued') want.push('cancel');
  if (job.status === 'done' || job.status === 'error' || job.status === 'cancelled') want.push('reveal');
  want.push('logs');
  if (job.status !== 'running') want.push('remove');

  const sig = want.join(',') + '|' + (job.output_dir || '');
  if (actions.dataset.sig !== sig) {
    actions.dataset.sig = sig;
    actions.innerHTML = '';
    if (want.includes('cancel')) {
      actions.appendChild(mkBtn('取消', 'small danger', () => jobAction(job.id, 'cancel', '已请求取消')));
    }
    if (want.includes('reveal')) {
      actions.appendChild(mkBtn('打开所在文件夹', 'small', () => reveal(job.output_dir)));
    }
    actions.appendChild(mkBtn('日志', 'small ghost', () => toggleLogs(view)));
    if (want.includes('remove')) {
      actions.appendChild(mkBtn('移除', 'small ghost', () => jobAction(job.id, 'remove', '已移除')));
    }
  }

  // 产出文件
  const filesBox = el.querySelector('.files');
  if (job.files && job.files.length) {
    filesBox.hidden = false;
    filesBox.innerHTML = job.files.map((f) =>
      `<div class="f"><span>📄 ${escapeHtml(f.split('/').pop())}</span></div>`).join('');
  } else if (job.status !== 'running') {
    filesBox.hidden = true;
  }
}

function mkBtn(text, cls, fn) {
  const b = document.createElement('button');
  b.className = cls;
  b.textContent = text;
  b.onclick = fn;
  return b;
}

async function jobAction(id, action, okMsg) {
  try {
    await api(`/api/jobs/${id}/${action}`, {});
    toast(okMsg);
    pollJobs();
  } catch (err) { toast(err.message, true); }
}

function toggleLogs(view) {
  const box = view.el.querySelector('.logs');
  view.logsOpen = !view.logsOpen;
  box.hidden = !view.logsOpen;
  if (view.logsOpen) box.scrollTop = box.scrollHeight;
}

function appendLogs(view, logs) {
  const box = view.el.querySelector('.logs');
  if (!box.dataset.ready) {
    box.innerHTML = '';
    box.dataset.ready = '1';
  }
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  logs.forEach((l) => {
    const row = document.createElement('div');
    row.className = 'l ' + (l.level || 'info');
    row.innerHTML = `<span class="t">${fmtTime(l.ts)}</span><span class="m">${escapeHtml(l.message)}</span>`;
    box.appendChild(row);
  });
  while (box.children.length > 400) box.removeChild(box.firstChild);
  if (atBottom || view.logsOpen) box.scrollTop = box.scrollHeight;
}

// ---------------------------------------------------------------- 历史 ---
async function loadHistory() {
  let rows;
  try { rows = await api('/api/history'); } catch (e) { return; }
  const box = $('history');
  if (!rows.length) {
    box.innerHTML = '<div class="empty">暂无记录</div>';
    return;
  }
  box.innerHTML = '';
  rows.slice(0, 60).forEach((r) => {
    const div = document.createElement('div');
    div.className = 'hist-row';
    const statusColor = r.status === 'done' ? 'var(--ok)'
      : r.status === 'cancelled' ? 'var(--warn)' : 'var(--err)';
    div.innerHTML =
      `<span class="t" title="${escapeHtml(r.input)}">${escapeHtml(r.title)}</span>` +
      `<span class="d" style="color:${statusColor}">${escapeHtml(r.quality_label || '')}</span>` +
      `<span class="d">${fmtTime(r.finished_at)}</span>`;
    const b = mkBtn('打开', 'small ghost', () => reveal(r.output_dir));
    div.appendChild(b);
    box.appendChild(div);
  });
}

$('btn-clear-history').onclick = async () => {
  if (!confirm('确定清空历史记录吗？（不会删除已下载的文件）')) return;
  try { await api('/api/history/clear', {}); loadHistory(); toast('已清空'); }
  catch (err) { toast(err.message, true); }
};

// -------------------------------------------------------------- 打开目录 ---
async function reveal(path) {
  try { await api('/api/reveal', { path: path || STATE.settings.output_dir }); }
  catch (err) { toast(err.message, true); }
}
$('btn-open-folder').onclick = () => reveal(STATE.settings.output_dir);

// ---------------------------------------------------------------- 设置 ---
$('btn-settings').onclick = () => {
  $('set-outdir').value = STATE.settings.output_dir || '';
  $('set-workers').value = STATE.settings.workers || 8;
  $('set-maxitems').value = STATE.settings.max_items || 0;
  $('dlg-settings').showModal();
};
$('btn-settings-cancel').onclick = () => $('dlg-settings').close();
$('btn-settings-save').onclick = async () => {
  try {
    await api('/api/settings', {
      output_dir: $('set-outdir').value.trim(),
      workers: parseInt($('set-workers').value, 10) || 8,
      max_items: parseInt($('set-maxitems').value, 10) || 0,
    });
    $('dlg-settings').close();
    await loadState();
    toast('设置已保存');
  } catch (err) { toast(err.message, true); }
};

// -------------------------------------------------------------- 轮询循环 ---
let polling = false;
async function tick() {
  if (polling) return;
  polling = true;
  try { await pollJobs(); } finally { polling = false; }
}

(async function init() {
  try {
    await loadState();
  } catch (err) {
    toast('无法连接后端：' + err.message, true);
  }
  await pollJobs();
  await loadHistory();
  setInterval(tick, 900);
  setInterval(loadHistory, 15000);
})();
