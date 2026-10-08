/* Linux进程管理工具（潍鲸 - weijing.co） - 系统监控面板 / 清理面板
 *
 * 依赖 app.js 提供的全局函数：el / api / toast / state / confirmAction /
 * DEFAULT_SORT / buildFilterOptions / renderHead / renderBody。
 * 本文件必须在 app.js 之后加载（同为经典脚本，共享全局作用域）。
 */

'use strict';

const PANEL_TABS = { monitor: 1, cleanup: 1, search: 1 };

const mon = { data: null, timer: null };
const clean = {
  targets: [],
  files: [],
  busyTargets: false,
  busyFiles: false,
  wx: { rows: [], sort: 'size', order: 'desc', kind: 'all',
        busy: false, installed: false, loaded: false },
  // 「软件清理」：把系统已安装软件 + AI 编程工具缓存合并成一张清单
  sgroup: { items: [], rows: [], checked: {}, counts: {},
            target: '', hint: '', busy: false, loaded: false,
            sort: 'size', order: 'desc', filter: '', source: 'all',
            fileSort: 'size', fileOrder: 'desc', scope: 'cache',
            fileBusy: false },
  // 顶层「文件查找」：按名称查找文件与文件夹
  fsearch: { rows: [], sort: 'size', order: 'desc', busy: false, loaded: false },
};

/* ---------------- 页签切换 ---------------- */

function switchTab(tabEl) {
  document.querySelectorAll('#tabs .tab').forEach(t => t.classList.remove('active'));
  tabEl.classList.add('active');

  const key = tabEl.dataset.tab;
  state.tab = key;
  state.search = '';
  const search = document.getElementById('search');
  search.value = '';
  document.getElementById('clearSearch').hidden = true;

  const isPanel = !!PANEL_TABS[key];
  document.getElementById('toolbar').hidden = isPanel;
  document.getElementById('tableCard').hidden = isPanel;
  document.getElementById('mainTip').hidden = isPanel;
  document.getElementById('monitorPanel').hidden = key !== 'monitor';
  document.getElementById('cleanupPanel').hidden = key !== 'cleanup';
  document.getElementById('searchPanel').hidden = key !== 'search';
  if (key === 'search') loadSearchDrives();

  // 切页时收起列设置下拉，避免残留
  const colMenu = document.getElementById('colMenu');
  if (colMenu) colMenu.hidden = true;

  stopMonitorTimer();

  if (key === 'monitor') {
    loadMonitor();
    startMonitorTimer();
  } else if (key === 'cleanup') {
    loadCleanupMeta();
    if (!clean.targets.length) scanCleanTargets();
  } else {
    state.sort = { ...DEFAULT_SORT[key] };
    buildFilterOptions();
    renderHead();
    renderBody();
  }
}

/* ========================================================================== #
#  系统监控
#  ========================================================================== */

function startMonitorTimer() {
  stopMonitorTimer();
  // 后端采样间隔 2 秒，这里同频刷新
  mon.timer = setInterval(() => loadMonitor(true), 2000);
}

function stopMonitorTimer() {
  if (mon.timer) { clearInterval(mon.timer); mon.timer = null; }
}

async function loadMonitor(silent) {
  try {
    const json = await api('/api/monitor?history=1');
    mon.data = json.data;
    if (state.tab === 'monitor') renderMonitor();
  } catch (err) {
    if (!silent) toast(`读取监控数据失败：${err.message}`, 'err');
  }
}

function barRow(name, valueText, pct, cls) {
  const row = el('div', 'bar-item');
  row.appendChild(el('div', 'bn', name));
  row.appendChild(el('div', 'bv', valueText));
  const track = el('div', 'bar-track');
  const fill = document.createElement('span');
  fill.className = cls;
  fill.style.width = `${Math.max(1, Math.min(100, pct))}%`;
  track.appendChild(fill);
  row.appendChild(track);
  return row;
}

function renderMonitor() {
  const d = mon.data;
  if (!d) return;
  const cpu = d.cpu || {};
  const mem = d.memory || {};
  const disk = d.disk || {};
  const net = d.network || {};

  document.getElementById('mCpu').textContent = `${cpu.percent}%`;
  document.getElementById('mCpuFoot').textContent =
    `${cpu.cores} 逻辑核心 / ${cpu.physical_cores} 物理核心` +
    (cpu.freq_mhz ? ` · ${cpu.freq_mhz} MHz` : '');

  document.getElementById('mMem').textContent = `${mem.percent}%`;
  document.getElementById('mMemFoot').textContent =
    `${mem.used_text} / ${mem.total_text}（可用 ${mem.available_text}）`;

  document.getElementById('mDisk').textContent = `${disk.read_speed_text || '-'}`;
  document.getElementById('mDiskFoot').textContent =
    `写入 ${disk.write_speed_text || '-'} · 累计读 ${disk.read_total_text} / 写 ${disk.write_total_text}`;

  document.getElementById('mNet').textContent = `↓ ${net.recv_speed_text || '-'}`;
  document.getElementById('mNetFoot').textContent =
    `上传 ↑ ${net.sent_speed_text || '-'} · 累计下行 ${net.recv_total_text} / 上行 ${net.sent_total_text}`;

  // 逻辑核心
  const cores = document.getElementById('mCores');
  cores.textContent = '';
  (cpu.per_core || []).forEach((v, i) => {
    const item = el('div', 'core-item');
    item.appendChild(el('div', 'cn', `核心 ${i}`));
    item.appendChild(el('div', 'cv', `${v}%`));
    const bar = el('div', 'core-bar');
    const span = document.createElement('span');
    span.style.width = `${Math.min(100, v)}%`;
    bar.appendChild(span);
    item.appendChild(bar);
    cores.appendChild(item);
  });

  // 磁盘分区
  const disks = document.getElementById('mDisks');
  disks.textContent = '';
  (disk.partitions || []).forEach(p => {
    const row = barRow(
      `${p.mountpoint}  （${p.fstype || '-'}）`,
      `${p.used_text} / ${p.total_text}`,
      p.percent,
      p.percent >= 90 ? 'bar-risk-high' : 'bar-disk'
    );
    row.title = `可用 ${p.free_text}，已用 ${p.percent}%`;
    disks.appendChild(row);
  });
  if (!(disk.partitions || []).length) disks.appendChild(el('div', 'cell-sub', '未检测到磁盘分区'));

  // Top 进程
  const totalMem = mem.total || 1;
  const topCpu = document.getElementById('mTopCpu');
  topCpu.textContent = '';
  (d.top_cpu || []).forEach(p => {
    const row = barRow(p.name, `${p.cpu}% · PID ${p.pid}`, p.cpu, 'bar-cpu');
    row.title = `${p.name}  PID ${p.pid}  ${p.cpu}%`;
    topCpu.appendChild(row);
  });
  if (!(d.top_cpu || []).length) topCpu.appendChild(el('div', 'cell-sub', '正在采集…'));

  const topMem = document.getElementById('mTopMem');
  topMem.textContent = '';
  (d.top_mem || []).forEach(p => {
    const pct = (p.memory / totalMem) * 100;
    const row = barRow(p.name, p.memory_text, pct, 'bar-mem');
    row.title = `${p.name}  PID ${p.pid}  ${p.memory_text}`;
    topMem.appendChild(row);
  });
  if (!(d.top_mem || []).length) topMem.appendChild(el('div', 'cell-sub', '正在采集…'));

  // 网络适配器
  const ifaces = document.getElementById('mIfaces');
  ifaces.textContent = '';
  (net.interfaces || []).forEach(i => {
    const box = el('div', 'iface');
    const head = el('div', 'in');
    head.appendChild(el('b', '', i.name));
    head.appendChild(el('span', 'tag tag-' + (i.up ? 'running' : 'stopped'), i.up ? '已连接' : '未连接'));
    box.appendChild(head);
    box.appendChild(el('div', 'ip', i.ipv4 || '无 IPv4 地址'));
    box.appendChild(el('div', 'io', `↓ ${i.recv_text}   ↑ ${i.sent_text}` +
      (i.speed_mbps ? `   ·   ${i.speed_mbps} Mbps` : '')));
    ifaces.appendChild(box);
  });

  document.getElementById('mFoot').textContent =
    `主机名 ${net.hostname || '-'} · 开机时长 ${d.uptime_text || '-'} · 开机时间 ${d.boot_time || '-'} · ` +
    `CPU ${cpu.name || '-'} · 数据每 2 秒刷新`;

  drawChart(d.history || []);
}

function drawChart(history) {
  const canvas = document.getElementById('mChart');
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 900;
  const cssH = 190;
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);

  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  const padL = 38, padR = 10, padT = 10, padB = 22;
  const w = cssW - padL - padR;
  const h = cssH - padT - padB;

  ctx.font = '11px system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif';
  ctx.lineWidth = 1;
  for (let p = 0; p <= 100; p += 25) {
    const y = padT + h - (h * p) / 100;
    ctx.strokeStyle = p === 0 ? '#dfe4ec' : '#eef1f6';
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(padL + w, y);
    ctx.stroke();
    ctx.fillStyle = '#9aa3af';
    ctx.fillText(`${p}%`, 8, y + 4);
  }

  if (!history || history.length < 2) {
    ctx.fillStyle = '#9aa3af';
    ctx.fillText('正在采集历史数据…', padL + 10, padT + h / 2);
    return;
  }

  const n = history.length;
  const px = i => padL + (w * i) / (n - 1);
  const py = v => padT + h - (h * Math.max(0, Math.min(100, v))) / 100;

  const line = (key, color) => {
    ctx.beginPath();
    history.forEach((p, i) => {
      const v = Number(p[key]) || 0;
      if (i === 0) ctx.moveTo(px(i), py(v));
      else ctx.lineTo(px(i), py(v));
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    ctx.stroke();

    ctx.lineTo(px(n - 1), padT + h);
    ctx.lineTo(px(0), padT + h);
    ctx.closePath();
    ctx.globalAlpha = 0.10;
    ctx.fillStyle = color;
    ctx.fill();
    ctx.globalAlpha = 1;
  };

  line('mem', '#ea580c');
  line('cpu', '#2563eb');

  ctx.fillStyle = '#9aa3af';
  const first = history[0].time || '';
  const last = history[n - 1].time || '';
  ctx.fillText(first, padL, cssH - 6);
  ctx.fillText(last, padL + w - ctx.measureText(last).width, cssH - 6);
  document.getElementById('mChartRange').textContent =
    `最近 ${n} 个采样点（每 2 秒采一次，约 ${Math.round((n * 2) / 60)} 分钟）`;
}

/* ========================================================================== #
#  清理面板
#  ========================================================================== */

async function loadCleanupMeta() {
  try {
    const json = await api('/api/cleanup/drives');
    const sel = document.getElementById('cDrive');
    sel.textContent = '';
    const all = el('option', '', '全部分区');
    all.value = '';
    sel.appendChild(all);
    json.data.forEach(d => {
      const o = el('option', '', `${d.mountpoint}   可用 ${d.free_text}`);
      o.value = d.mountpoint;
      sel.appendChild(o);
    });
  } catch (err) {
    /* 分区列表失败不影响其他功能 */
  }
  await updateMemCard();
}

async function updateMemCard() {
  let snap = mon.data;
  if (!snap) {
    try {
      snap = (await api('/api/monitor?history=0')).data;
    } catch (err) {
      return;
    }
  }
  const m = snap.memory || {};
  document.getElementById('cMemPct').textContent = `${m.percent}%`;
  document.getElementById('cMemText').textContent =
    `${m.used_text} / ${m.total_text}（可用 ${m.available_text}）`;
}

async function doCleanMemory() {
  const btn = document.getElementById('btnCleanMem');
  btn.disabled = true;
  btn.textContent = '清理中…';
  try {
    const json = await api('/api/cleanup/memory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        trim_working_set: true,
        clear_file_cache: document.getElementById('cOptFileCache').checked,
        purge_standby: document.getElementById('cOptStandby').checked,
      }),
    });
    const r = json.data;
    document.getElementById('cMemResult').textContent =
      `清理完成：${r.before_percent}% → ${r.after_percent}%（${r.before_text} → ${r.after_text}），` +
      `释放 ${r.freed_text}。` +
      (r.detail && r.detail.trim ? `裁剪了 ${r.detail.trim.trimmed} 个进程的工作集。` : '');
    toast(`内存清理完成，释放 ${r.freed_text}`, 'ok');
    await updateMemCard();
  } catch (err) {
    toast(`内存清理失败：${err.message}`, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '一键清理内存';
  }
}

/* ---------------- 磁盘清理项 ---------------- */

async function scanCleanTargets(force) {
  if (clean.busyTargets) return;
  clean.busyTargets = true;
  const box = document.getElementById('cTargets');
  box.textContent = '';
  box.appendChild(el('p', 'cell-sub',
    force ? '正在重新统计各项占用，请稍候…' : '正在统计各项占用（首次扫描可能较慢），请稍候…'));
  try {
    const json = await api(`/api/cleanup/targets${force ? '?refresh=1' : ''}`);
    clean.targets = json.data.rows;
    renderCleanTargets();
  } catch (err) {
    box.textContent = '';
    box.appendChild(el('p', 'cell-sub', `扫描失败：${err.message}`));
  } finally {
    clean.busyTargets = false;
  }
}

function riskTag(risk) {
  if (risk === 'high') return ['t-risk-high', '高风险'];
  if (risk === 'safe') return ['t-risk-safe', '安全'];
  return ['t-risk-medium', '一般'];
}

function renderCleanTargets() {
  const box = document.getElementById('cTargets');
  box.textContent = '';
  if (!clean.targets.length) {
    box.appendChild(el('p', 'cell-sub', '没有检测到可清理的项目'));
    updateTargetTotal();
    return;
  }
  clean.targets.forEach(t => {
    const row = el('div', 'target-row' + (t.available ? '' : ' unavailable'));
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !!t._checked;
    cb.disabled = !t.available || !t.size;
    cb.onchange = () => { t._checked = cb.checked; updateTargetTotal(); };
    row.appendChild(cb);

    const mid = document.createElement('div');
    const name = el('div', 'tn', t.name);
    const [cls, txt] = riskTag(t.risk);
    name.appendChild(el('span', `tag-sm ${cls}`, txt));
    if (t.admin) name.appendChild(el('span', 'tag-sm t-risk-medium', '需管理员'));
    if (t.group) name.appendChild(el('span', 'tag-sm', t.group));
    mid.appendChild(name);
    mid.appendChild(el('div', 'td', t.available ? t.desc : '该路径在本机不存在'));
    row.appendChild(mid);

    row.appendChild(el('div', 'ts', t.available ? t.size_text : '-'));
    box.appendChild(row);
  });
  updateTargetTotal();
}

function setTargetChecked(ids, checked) {
  clean.targets.forEach(t => { t._checked = checked && ids.includes(t.id); });
  renderCleanTargets();
}

function checkedTargetIds() {
  return clean.targets.filter(t => t._checked && t.available && t.size).map(t => t.id);
}

function updateTargetTotal() {
  const ids = new Set(checkedTargetIds());
  const total = clean.targets.reduce((s, t) => s + (ids.has(t.id) ? t.size : 0), 0);
  const count = clean.targets.filter(t => ids.has(t.id)).length;
  document.getElementById('cTargetTotal').textContent =
    `已选中 ${count} 项 · 预计释放 ${formatBytes(total)}`;
}

function formatBytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = n, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

async function doCleanDisk() {
  const ids = checkedTargetIds();
  if (!ids.length) { toast('请先勾选要清理的项目', 'err'); return; }
  const picked = clean.targets.filter(t => ids.includes(t.id));
  const total = picked.reduce((s, t) => s + t.size, 0);
  const hasHigh = picked.some(t => t.risk === 'high');

  confirmAction(
    '清理磁盘空间',
    `即将清理 ${picked.length} 项：\n${picked.map(t => '· ' + t.name + '（' + t.size_text + '）').join('\n')}\n\n` +
    `预计释放 ${formatBytes(total)}。` +
    (hasHigh ? '\n\n⚠ 其中包含高风险项目，清理后可能无法恢复，请确认已了解后果。' : ''),
    async () => {
      const json = await api('/api/cleanup/disk', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids, dry_run: false }),
      });
      toast(json.message || '清理完成', json.ok ? 'ok' : 'err');
      clean.targets.forEach(t => { t._checked = false; });
      await scanCleanTargets(true);
    }
  );
}

async function doEmptyRecycleBin() {
  confirmAction('清空回收站', '确定要清空回收站吗？其中的文件将无法恢复。', async () => {
    try {
      const json = await api('/api/cleanup/empty-recycle-bin', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      toast(json.message, json.ok ? 'ok' : 'err');
    } catch (err) { toast(err.message, 'err'); }
  });
}

/* ---------------- 大文件 ---------------- */

async function scanLargeFiles() {
  if (clean.busyFiles) return;
  clean.busyFiles = true;
  const btn = document.getElementById('btnScanFiles');
  btn.disabled = true;
  btn.textContent = '扫描中…';
  const box = document.getElementById('cFiles');
  box.textContent = '';
  box.appendChild(el('p', 'cell-sub', '正在遍历磁盘查找大文件，请稍候…'));

  const drive = document.getElementById('cDrive').value;
  const minMb = document.getElementById('cMinMb').value || 200;
  try {
    const json = await api(`/api/cleanup/files?drive=${encodeURIComponent(drive)}` +
      `&min_mb=${encodeURIComponent(minMb)}&limit=300`);
    clean.files = json.data.rows;
    renderCleanFiles();
    const tail = json.data.timed_out ? '（已达时间上限，结果可能不完整）' : '';
    toast(`扫描完成，找到 ${json.data.total_found} 个大文件${tail}`, 'ok');
  } catch (err) {
    box.textContent = '';
    box.appendChild(el('p', 'cell-sub', `扫描失败：${err.message}`));
  } finally {
    clean.busyFiles = false;
    btn.disabled = false;
    btn.textContent = '扫描大文件';
  }
}

function renderCleanFiles() {
  const box = document.getElementById('cFiles');
  box.textContent = '';
  if (!clean.files.length) {
    box.appendChild(el('p', 'cell-sub', '没有找到符合条件的大文件'));
    updateFileTotal();
    return;
  }
  clean.files.forEach(f => {
    const row = el('div', 'file-row');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !!f._checked;
    // 系统保护目录内的文件不允许删除
    cb.disabled = f.risk === 'high' && f.category === '系统文件';
    cb.onchange = () => { f._checked = cb.checked; updateFileTotal(); };
    row.appendChild(cb);

    const mid = document.createElement('div');
    const p = el('div', 'fp', f.path);
    p.title = f.path;
    mid.appendChild(p);
    const meta = el('div', 'fm', `${f.category} · 修改于 ${f.mtime}（${f.age_days} 天前） · ${f.advice}`);
    mid.appendChild(meta);
    row.appendChild(mid);

    const [cls, txt] = riskTag(f.risk);
    row.appendChild(el('span', `tag-sm ${cls}`, txt));
    row.appendChild(el('div', 'fs', f.size_text));
    box.appendChild(row);
  });
  updateFileTotal();
}

function updateFileTotal() {
  const sel = clean.files.filter(f => f._checked);
  const total = sel.reduce((s, f) => s + f.size, 0);
  document.getElementById('cFilesTotal').textContent =
    `已选中 ${sel.length} 个文件 · 合计 ${formatBytes(total)}`;
}

async function doDeleteFiles() {
  const picked = clean.files.filter(f => f._checked);
  if (!picked.length) { toast('请先勾选要删除的文件', 'err'); return; }
  const total = picked.reduce((s, f) => s + f.size, 0);

  confirmAction(
    `删除 ${picked.length} 个大文件`,
    `即将把以下 ${picked.length} 个文件（合计 ${formatBytes(total)}）送入回收站：\n` +
    `${picked.slice(0, 8).map(f => '· ' + f.name + '（' + f.size_text + '）').join('\n')}` +
    (picked.length > 8 ? `\n…以及另外 ${picked.length - 8} 个文件` : '') +
    '\n\n删除后可从回收站还原。',
    async () => {
      const json = await api('/api/cleanup/delete-files', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths: picked.map(f => f.path), recycle: true }),
      });
      const d = json.data || {};
      toast(`${json.message || d.message || '已提交删除'}` +
        (d.skipped ? `，跳过 ${d.skipped} 个受保护文件` : ''), json.ok ? 'ok' : 'err');
      clean.files.forEach(f => { f._checked = false; });
      await scanLargeFiles();
    }
  );
}

/* ---------------- 微信缓存 / 聊天文件 ---------------- */

async function scanWechat() {
  const wx = clean.wx;
  if (wx.busy) return;
  wx.busy = true;
  const box = document.getElementById('cWxFiles');
  box.textContent = '';
  box.appendChild(el('p', 'cell-sub', '正在扫描微信缓存与聊天文件…'));
  try {
    const sum = await api('/api/cleanup/wechat');
    wx.installed = !!(sum.data && sum.data.installed);
    renderWxSummary((sum.data && sum.data.groups) || []);

    const json = await api(
      `/api/cleanup/wechat-files?kind=${encodeURIComponent(wx.kind)}` +
      `&sort=${encodeURIComponent(wx.sort)}&order=${encodeURIComponent(wx.order)}`);
    wx.rows = (json.data && json.data.rows) || [];
    wx.loaded = true;
    renderWxFiles();
  } catch (err) {
    box.textContent = '';
    box.appendChild(el('p', 'cell-sub', `扫描失败：${err.message}`));
  } finally {
    wx.busy = false;
  }
}

function renderWxSummary(groups) {
  const box = document.getElementById('cWxSummary');
  box.textContent = '';
  if (!clean.wx.installed) {
    box.appendChild(el('p', 'cell-sub',
      '未检测到微信数据目录（本机可能未安装微信，或聊天文件保存在其他位置）。'));
    return;
  }
  if (!groups.length) {
    box.appendChild(el('p', 'cell-sub', '未发现可清理的微信文件。'));
    return;
  }
  const grid = el('div', 'wx-cat-grid');
  groups.forEach(g => {
    const card = el('div', 'wx-cat' + (clean.wx.kind === g.kind ? ' on' : ''));
    card.appendChild(el('div', 'wc-name', g.name));
    card.appendChild(el('div', 'wc-size', g.size_text));
    card.appendChild(el('div', 'wc-meta', `${g.count} 个文件`));
    if (g.desc) {
      const d = el('div', 'wc-desc', g.desc);
      d.title = g.desc;
      card.appendChild(d);
    }
    card.onclick = () => {
      clean.wx.kind = (clean.wx.kind === g.kind) ? 'all' : g.kind;
      const sel = document.getElementById('cWxKind');
      if (sel) sel.value = clean.wx.kind;
      scanWechat();
    };
    grid.appendChild(card);
  });
  box.appendChild(grid);
}

function selectWxKind() {
  const wx = clean.wx;
  if (!wx.rows.length) { toast('当前分类没有可勾选的文件', 'err'); return; }
  const turnOn = wx.rows.some(f => !f._checked);
  wx.rows.forEach(f => { f._checked = turnOn; });
  renderWxFiles();
}

/* ---------------- 软件清理（已安装软件 + AI 工具缓存，合并展示） ---------------- */

async function scanSoftAll() {
  const sg = clean.sgroup;
  if (sg.busy) return;
  sg.busy = true;
  const sumEl = document.getElementById('cSoftAllSummary');
  if (sumEl) sumEl.textContent = '正在扫描本机全部软件（已安装软件 + AI 工具缓存），请稍候…';
  try {
    const json = await api('/api/cleanup/software-list');
    const d = json.data || {};
    sg.items = d.items || [];
    sg.counts = d.counts || {};
    sg.checked = {};
    sg.loaded = true;
    if (sumEl) {
      sumEl.textContent =
        `${sg.items.length} 项软件（已安装 ${sg.counts.installed || 0} · ` +
        `AI 工具缓存 ${sg.counts.agents || 0}` +
        (sg.counts.discovered ? ` · 扫描发现 ${sg.counts.discovered}` : '') + '）' +
        ` · 可清理合计 ${d.total_cache_text || '0 B'}` +
        ` · 用时 ${d.elapsed || 0}s` +
        (d.timed_out ? ' · 已触发时间上限，未测算项显示「—」' : '');
    }
    renderSoftAll();
    if (sg.target) await loadSoftFiles(sg.target, true);
  } catch (err) {
    if (sumEl) sumEl.textContent = '扫描失败：' + err.message;
  } finally {
    sg.busy = false;
  }
}

function softFilteredItems() {
  const sg = clean.sgroup;
  const kw = (sg.filter || '').trim().toLowerCase();
  let items = (sg.items || []).slice();
  if (sg.source && sg.source !== 'all') {
    items = items.filter(i => i.source_key === sg.source);
  }
  if (kw) {
    items = items.filter(i =>
      String(i.name || '').toLowerCase().includes(kw) ||
      String(i.publisher || '').toLowerCase().includes(kw) ||
      String(i.location || '').toLowerCase().includes(kw));
  }
  const dir = sg.order === 'asc' ? 1 : -1;
  if (sg.sort === 'name') {
    items.sort((a, b) => String(a.name).localeCompare(String(b.name), 'zh-Hans-CN') * dir);
  } else {
    const val = (i) => (i.cache_size > 0 ? i.cache_size : (i.size || 0));
    items.sort((a, b) => (val(a) - val(b)) * dir);
  }
  return items;
}

function renderSoftAll() {
  const sg = clean.sgroup;
  const body = document.getElementById('cSoftAllBody');
  if (!body) return;
  body.textContent = '';
  const items = softFilteredItems();
  if (!items.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 7;
    td.className = 'cell-sub';
    td.textContent = sg.loaded ? '没有匹配的软件。' : '点击「扫描全部软件」开始…';
    tr.appendChild(td);
    body.appendChild(tr);
    updateSoftAllTotal();
    return;
  }
  items.forEach(i => {
    const tr = document.createElement('tr');
    tr.className = 'soft-row' + (sg.target === i.key ? ' on' : '');

    const tdChk = document.createElement('td');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !!sg.checked[i.key];
    cb.onchange = () => { sg.checked[i.key] = cb.checked; updateSoftAllTotal(); };
    tdChk.appendChild(cb);
    tr.appendChild(tdChk);

    const tdName = document.createElement('td');
    tdName.appendChild(el('div', 'fp', i.name));
    tdName.title = i.name;
    tr.appendChild(tdName);

    const tdSrc = document.createElement('td');
    tdSrc.appendChild(el('span', 'tag-sm ' +
      (i.source_key === 'installed' ? 't-risk-low' : 't-risk-medium'), i.source));
    tr.appendChild(tdSrc);

    const tdPub = document.createElement('td');
    tdPub.appendChild(el('div', 'fm', i.publisher || '—'));
    tdPub.appendChild(el('div', 'fm', i.version && i.version !== '—'
      ? ('版本 ' + i.version) : (i.cache_count ? (i.cache_count + ' 个缓存文件') : '')));
    tr.appendChild(tdPub);

    tr.appendChild(el('td', 'num', i.size > 0 ? i.size_text : '—'));
    tr.appendChild(el('td', 'num', i.cache_size > 0 ? i.cache_text : '—'));

    const tdLoc = document.createElement('td');
    tdLoc.appendChild(el('div', 'fm', i.location || '（未提供安装位置）'));
    tdLoc.title = i.location || '';
    tr.appendChild(tdLoc);

    tr.onclick = (ev) => {
      if (ev.target && ev.target.tagName === 'INPUT') return;
      selectSoftRow(i.key);
    };
    body.appendChild(tr);
  });
  updateSoftAllTotal();
}

function selectSoftRow(key) {
  const sg = clean.sgroup;
  sg.target = (sg.target === key) ? '' : key;
  renderSoftAll();
  const item = (sg.items || []).find(i => i.key === key);
  const title = document.getElementById('cSoftFileTitle');
  if (title) {
    title.textContent = (sg.target && item)
      ? ('可清理文件 · ' + item.name) : '可清理文件';
  }
  if (sg.target) {
    loadSoftFiles(sg.target);
  } else {
    sg.rows = [];
    renderSoftFiles();
  }
}

async function loadSoftFiles(key, silent) {
  const sg = clean.sgroup;
  if (sg.fileBusy) return;
  sg.fileBusy = true;
  const box = document.getElementById('cSoftFiles');
  if (box && !silent) {
    box.textContent = '';
    box.appendChild(el('p', 'cell-sub', '正在读取可清理文件…'));
  }
  try {
    const json = await api('/api/cleanup/software-cache-files?key=' +
      encodeURIComponent(key || '') +
      '&scope=' + encodeURIComponent(sg.scope) +
      '&sort=' + encodeURIComponent(sg.fileSort) +
      '&order=' + encodeURIComponent(sg.fileOrder) + '&limit=400');
    const d = json.data || {};
    sg.rows = d.rows || [];
    sg.hint = d.hint || '';
    sg.fileLabel = d.label || '';
    sg.fileDirText = d.dir_size_text || '0 B';
    sg.fileDirCount = d.dir_count || 0;
    renderSoftFiles();
  } catch (err) {
    sg.rows = [];
    sg.hint = '读取失败：' + err.message;
    renderSoftFiles();
  } finally {
    sg.fileBusy = false;
  }
}

function renderSoftFiles() {
  const sg = clean.sgroup;
  const box = document.getElementById('cSoftFiles');
  if (!box) return;
  box.textContent = '';
  if (!sg.target) {
    box.appendChild(el('p', 'cell-sub', '点击上方软件列表中的一项，查看它的可清理文件。'));
    updateSoftFileTotal();
    return;
  }
  if (sg.hint) box.appendChild(el('p', 'cell-sub', sg.hint));
  if (sg.rows.length) {
    box.appendChild(el('p', 'cell-sub',
      `${sg.fileLabel || ''} · 当前范围合计 ${sg.fileDirText} · ${sg.fileDirCount} 个文件`));
  }
  if (!sg.rows.length) {
    box.appendChild(el('p', 'cell-sub', '没有可列出的文件。'));
    updateSoftFileTotal();
    return;
  }
  sg.rows.forEach(f => {
    const row = el('div', 'file-row');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !!f._checked;
    cb.onchange = () => { f._checked = cb.checked; updateSoftFileTotal(); };
    row.appendChild(cb);

    const mid = document.createElement('div');
    const p = el('div', 'fp', f.path);
    p.title = f.path;
    mid.appendChild(p);
    mid.appendChild(el('div', 'fm', `修改于 ${f.mtime}（${f.age_days} 天前）`));
    row.appendChild(mid);

    row.appendChild(el('div', 'fs', f.size_text));
    box.appendChild(row);
  });
  updateSoftFileTotal();
}

function softCheckedItems() {
  const sg = clean.sgroup;
  return (sg.items || []).filter(i => sg.checked[i.key]);
}

function updateSoftAllTotal() {
  const picked = softCheckedItems();
  const cache = picked.reduce((s, i) => s + (i.cache_size || 0), 0);
  const canUn = picked.filter(i => i.can_uninstall).length;
  document.getElementById('cSoftAllTotal').textContent =
    `已选中 ${picked.length} 项软件 · 可清理 ${formatBytes(cache)} · 可卸载 ${canUn} 项`;
}

function updateSoftFileTotal() {
  const sel = (clean.sgroup.rows || []).filter(f => f._checked);
  const total = sel.reduce((s, f) => s + f.size, 0);
  const elTotal = document.getElementById('cSoftFileTotal');
  if (elTotal) elTotal.textContent = `已选中 ${sel.length} 个文件 · 合计 ${formatBytes(total)}`;
}

function selectAllSoftRows() {
  const sg = clean.sgroup;
  const items = softFilteredItems();
  if (!items.length) { toast('当前列表没有可选的项', 'err'); return; }
  const turnOn = items.some(i => !sg.checked[i.key]);
  items.forEach(i => { sg.checked[i.key] = turnOn; });
  renderSoftAll();
}

function toggleSoftFilesAll() {
  const rows = clean.sgroup.rows || [];
  if (!rows.length) { toast('当前没有可勾选的文件', 'err'); return; }
  const turnOn = rows.some(f => !f._checked);
  rows.forEach(f => { f._checked = turnOn; });
  renderSoftFiles();
}

function setSoftAllSort(by) {
  const sg = clean.sgroup;
  if (sg.sort === by) {
    sg.order = sg.order === 'desc' ? 'asc' : 'desc';
  } else {
    sg.sort = by;
    sg.order = by === 'name' ? 'asc' : 'desc';
  }
  updateSoftAllSortButtons();
  renderSoftAll();
}

function updateSoftAllSortButtons() {
  const sg = clean.sgroup;
  const bs = document.getElementById('btnSoftAllSortSize');
  const bn = document.getElementById('btnSoftAllSortName');
  if (bs) bs.textContent = '按大小' + (sg.sort === 'size' ? (sg.order === 'desc' ? ' ▼' : ' ▲') : '');
  if (bn) bn.textContent = '按名称' + (sg.sort === 'name' ? (sg.order === 'desc' ? ' ▼' : ' ▲') : '');
}

function setSoftFileSort(by) {
  const sg = clean.sgroup;
  if (sg.fileSort === by) {
    sg.fileOrder = sg.fileOrder === 'desc' ? 'asc' : 'desc';
  } else {
    sg.fileSort = by;
    sg.fileOrder = 'desc';
  }
  updateSoftFileSortButtons();
  if (sg.target) loadSoftFiles(sg.target);
}

function updateSoftFileSortButtons() {
  const sg = clean.sgroup;
  const bs = document.getElementById('btnSoftFileSortSize');
  const bt = document.getElementById('btnSoftFileSortTime');
  const arrow = (on, ascIsUp) => on ? (sg.fileOrder === 'desc' ? ' ▼' : ' ▲') : '';
  if (bs) bs.textContent = '按大小' + arrow(sg.fileSort === 'size');
  if (bt) bt.textContent = '按时间' + arrow(sg.fileSort === 'time');
}

async function doCleanSoftSelected() {
  const picked = softCheckedItems();
  if (!picked.length) { toast('请先勾选要清理的软件', 'err'); return; }
  const total = picked.reduce((s, i) => s + (i.cache_size || 0), 0);
  confirmAction(
    `清理 ${picked.length} 项软件的缓存`,
    `即将清理：${picked.slice(0, 8).map(i => '· ' + i.name +
      (i.cache_size ? '（' + i.cache_text + '）' : '')).join('\n')}` +
    (picked.length > 8 ? `\n…以及另外 ${picked.length - 8} 项` : '') +
    `\n\n合计约 ${formatBytes(total)}。只会清理缓存 / 日志 / 会话历史等可再生数据，` +
    '账号凭证、用户配置与程序本体不会被删除，文件送入回收站可还原。',
    async () => {
      for (const item of picked) {
        if (!item.cache_size) continue;
        try {
          await api('/api/cleanup/delete-files', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ paths: item.paths || [], recycle: true }),
          });
        } catch (err) {
          toast(`清理 ${item.name} 失败：${err.message}`, 'err');
        }
      }
      toast('选中软件的缓存已清理完成，可回收站还原', 'ok');
      await scanSoftAll();
    }
  );
}

async function doUninstallSoft() {
  const picked = softCheckedItems().filter(i => i.can_uninstall);
  if (!picked.length) {
    toast('请勾选支持卸载的已安装软件（软件名称右侧「来源」为已安装）', 'err');
    return;
  }
  const skipped = softCheckedItems().filter(i => !i.can_uninstall);
  confirmAction(
    `卸载 ${picked.length} 个软件`,
    `即将调用软件自带的卸载程序：\n` +
    `${picked.map(i => '· ' + i.name + (i.version && i.version !== '—' ? ' ' + i.version : '')).join('\n')}` +
    (skipped.length ? `\n\n另有 ${skipped.length} 项未提供卸载命令，已跳过。` : '') +
    '\n\n卸载命令来自系统登记的卸载信息，实际进度请在弹出的卸载窗口中完成。',
    async () => {
      for (const item of picked) {
        try {
          const json = await api('/api/cleanup/uninstall', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ident: item.ident, quiet: false }),
          });
          toast(json.message || (json.ok ? '已启动卸载程序' : '卸载失败'), json.ok ? 'ok' : 'err');
        } catch (err) {
          toast(`卸载 ${item.name} 失败：${err.message}`, 'err');
        }
      }
    }
  );
}

async function doDeleteSoftFiles() {
  const picked = (clean.sgroup.rows || []).filter(f => f._checked);
  if (!picked.length) { toast('请先勾选要删除的文件', 'err'); return; }
  const total = picked.reduce((s, f) => s + f.size, 0);
  confirmAction(
    `删除 ${picked.length} 个文件`,
    `即将把以下 ${picked.length} 个文件（合计 ${formatBytes(total)}）送入回收站：\n` +
    `${picked.slice(0, 8).map(f => '· ' + f.name + '（' + f.size_text + '）').join('\n')}` +
    (picked.length > 8 ? `\n…以及另外 ${picked.length - 8} 个文件` : '') +
    '\n\n这些都是缓存 / 日志类可再生数据，删除后软件会自动重建，可从回收站还原。',
    async () => {
      const json = await api('/api/cleanup/delete-files', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths: picked.map(f => f.path), recycle: true }),
      });
      const d = json.data || {};
      toast(`${json.message || d.message || '已提交删除'}` +
        (d.skipped ? `，跳过 ${d.skipped} 个受保护文件` : ''), json.ok ? 'ok' : 'err');
      clean.sgroup.rows.forEach(f => { f._checked = false; });
      await loadSoftFiles(clean.sgroup.target);
    }
  );
}

function renderWxFiles() {
  const wx = clean.wx;
  const box = document.getElementById('cWxFiles');
  box.textContent = '';
  if (!wx.rows.length) {
    box.appendChild(el('p', 'cell-sub',
      wx.installed ? '没有符合条件的微信文件' : '未检测到微信数据目录'));
    updateWxTotal();
    return;
  }
  wx.rows.forEach(f => {
    const row = el('div', 'file-row');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !!f._checked;
    cb.onchange = () => { f._checked = cb.checked; updateWxTotal(); };
    row.appendChild(cb);

    const mid = document.createElement('div');
    const p = el('div', 'fp', f.path);
    p.title = f.path;
    mid.appendChild(p);
    mid.appendChild(el('div', 'fm',
      `${f.kind_text} · 修改于 ${f.mtime}（${f.age_days} 天前）`));
    row.appendChild(mid);

    row.appendChild(el('span', 'tag-sm t-risk-medium', f.kind_text));
    row.appendChild(el('div', 'fs', f.size_text));
    box.appendChild(row);
  });
  updateWxTotal();
}

function updateWxTotal() {
  const sel = clean.wx.rows.filter(f => f._checked);
  const total = sel.reduce((s, f) => s + f.size, 0);
  document.getElementById('cWxTotal').textContent =
    `已选中 ${sel.length} 个文件 · 合计 ${formatBytes(total)}`;
}

function setWxSort(by) {
  const wx = clean.wx;
  if (wx.sort === by) {
    wx.order = wx.order === 'desc' ? 'asc' : 'desc';
  } else {
    wx.sort = by;
    wx.order = 'desc';
  }
  updateWxSortButtons();
  if (wx.loaded) scanWechat();
}

function updateWxSortButtons() {
  const wx = clean.wx;
  const bs = document.getElementById('btnWxSortSize');
  const bt = document.getElementById('btnWxSortTime');
  if (bs) bs.textContent = '按大小' + (wx.sort === 'size' ? (wx.order === 'desc' ? ' ▼' : ' ▲') : '');
  if (bt) bt.textContent = '按时间' + (wx.sort === 'time' ? (wx.order === 'desc' ? ' ▼' : ' ▲') : '');
}

async function doDeleteWechat() {
  const picked = clean.wx.rows.filter(f => f._checked);
  if (!picked.length) { toast('请先勾选要删除的文件', 'err'); return; }
  const total = picked.reduce((s, f) => s + f.size, 0);
  confirmAction(
    `删除 ${picked.length} 个微信文件`,
    `即将把以下 ${picked.length} 个文件（合计 ${formatBytes(total)}）送入回收站：\n` +
    `${picked.slice(0, 8).map(f => '· ' + f.name + '（' + f.size_text + '）').join('\n')}` +
    (picked.length > 8 ? `\n…以及另外 ${picked.length - 8} 个文件` : '') +
    '\n\n删除后聊天记录中的图片 / 视频将无法查看，可从回收站还原。',
    async () => {
      const json = await api('/api/cleanup/delete-files', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths: picked.map(f => f.path), recycle: true }),
      });
      const d = json.data || {};
      toast(`${json.message || d.message || '已提交删除'}` +
        (d.skipped ? `，跳过 ${d.skipped} 个受保护文件` : ''), json.ok ? 'ok' : 'err');
      clean.wx.rows.forEach(f => { f._checked = false; });
      await scanWechat();
    }
  );
}

/* ---------------- 顶层「文件查找」：查找文件与文件夹 ---------------- */

async function loadSearchDrives() {
  const sel = document.getElementById('fDrive');
  if (!sel || sel.options.length > 1) return;
  try {
    const json = await api('/api/cleanup/drives');
    const drives = (json.data || []);
    sel.textContent = '';
    // 默认即「全盘（所有分区）」，亦可任选某个指定分区
    const all = el('option', '', '全盘（所有分区）');
    all.value = '';
    sel.appendChild(all);
    drives.forEach(d => {
      const o = el('option', '', `${d.mountpoint}（可用 ${d.free_text}）`);
      o.value = d.mountpoint;
      sel.appendChild(o);
    });
    sel.value = '';
  } catch (err) {
    if (sel) sel.textContent = '';
  }
}

async function doSearchFiles() {
  const fs = clean.fsearch;
  if (fs.busy) return;
  const kwEl = document.getElementById('fKey');
  const kw = (kwEl.value || '').trim();
  const sumEl = document.getElementById('fSummary');
  const box = document.getElementById('fResults');
  if (!kw) { toast('请输入要查找的名称关键字', 'err'); return; }
  fs.busy = true;
  fs.loaded = true;
  if (box) {
    box.textContent = '';
    box.appendChild(el('p', 'cell-sub', '正在搜索，请稍候…'));
  }
  try {
    const q = [
      'q=' + encodeURIComponent(kw),
      'drive=' + encodeURIComponent((document.getElementById('fDrive') || {}).value || ''),
      'root=' + encodeURIComponent((document.getElementById('fRoot') || {}).value || ''),
      'mode=' + encodeURIComponent((document.getElementById('fMode') || {}).value || 'all'),
      'ext=' + encodeURIComponent((document.getElementById('fExt') || {}).value || ''),
      'min_mb=' + encodeURIComponent((document.getElementById('fMinMb') || {}).value || '0'),
      'depth=' + encodeURIComponent((document.getElementById('fDepth') || {}).value || '7'),
      'max_seconds=' + encodeURIComponent((document.getElementById('fSecs') || {}).value || '20'),
      'dir_size=' + (((document.getElementById('fDirSize') || {}).checked) ? '1' : '0'),
      'sort=' + encodeURIComponent(fs.sort),
      'order=' + encodeURIComponent(fs.order),
      'limit=500',
    ].join('&');
    const json = await api('/api/search/files?' + q);
    const d = json.data || {};
    fs.rows = d.rows || [];
    fs.foundText = d.total_found || 0;
    fs.measuredText = d.measured_text || '0 B';
    fs.hint = d.hint || '';
    fs.scannedDirs = d.scanned_dirs || 0;
    fs.scannedFiles = d.scanned_files || 0;
    fs.elapsed = d.elapsed || 0;
    renderSearchResults();
    if (sumEl) {
      sumEl.textContent =
        `关键字「${d.keyword || kw}」：找到 ${d.total_found || 0} 项` +
        `（已显示 ${d.shown || 0} 项） · 合计 ${d.measured_text || '0 B'}` +
        ` · 遍历 ${d.scanned_dirs || 0} 个目录 / ${d.scanned_files || 0} 个文件` +
        ` · 用时 ${d.elapsed || 0}s` +
        (d.timed_out ? ' · 已触发时间上限' : '');
    }
  } catch (err) {
    fs.rows = [];
    fs.hint = '搜索失败：' + err.message;
    renderSearchResults();
    if (sumEl) sumEl.textContent = '搜索失败：' + err.message;
  } finally {
    fs.busy = false;
  }
}

function renderSearchResults() {
  const fs = clean.fsearch;
  const box = document.getElementById('fResults');
  if (!box) return;
  box.textContent = '';
  if (fs.hint) box.appendChild(el('p', 'cell-sub', fs.hint));
  if (!fs.rows.length) {
    box.appendChild(el('p', 'cell-sub', '没有匹配的结果。'));
    updateSearchTotal();
    return;
  }
  fs.rows.forEach(r => {
    const row = el('div', 'file-row');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !!r._checked;
    cb.onchange = () => { r._checked = cb.checked; updateSearchTotal(); };
    row.appendChild(cb);

    row.appendChild(el('span', 'tag-sm ' + (r.is_dir ? 't-risk-low' : 't-risk-medium'),
      r.is_dir ? '文件夹' : '文件'));

    const mid = document.createElement('div');
    const name = el('div', 'fp', r.name);
    name.title = r.path;
    mid.appendChild(name);
    mid.appendChild(el('div', 'fm', r.parent + ' · 修改于 ' + r.mtime +
      '（' + r.age_days + ' 天前）'));
    mid.onclick = () => revealSearchRow(r.path);
    mid.style.cursor = 'pointer';
    row.appendChild(mid);

    row.appendChild(el('div', 'fs', r.size_text));
    box.appendChild(row);
  });
  updateSearchTotal();
}

function searchCheckedRows() {
  return (clean.fsearch.rows || []).filter(r => r._checked);
}

function updateSearchTotal() {
  const sel = searchCheckedRows();
  const dirs = sel.filter(r => r.is_dir).length;
  const total = sel.reduce((s, r) => s + (r.size > 0 ? r.size : 0), 0);
  const elTotal = document.getElementById('fTotal');
  if (elTotal) {
    elTotal.textContent =
      `已选中 ${sel.length} 项（文件夹 ${dirs} · 文件 ${sel.length - dirs}） · ` +
      `合计 ${formatBytes(total)}`;
  }
}

function toggleSearchAll() {
  const rows = clean.fsearch.rows || [];
  if (!rows.length) { toast('当前没有可勾选的结果', 'err'); return; }
  const turnOn = rows.some(r => !r._checked);
  rows.forEach(r => { r._checked = turnOn; });
  renderSearchResults();
}

function setSearchSort(by) {
  const fs = clean.fsearch;
  if (fs.sort === by) {
    fs.order = fs.order === 'desc' ? 'asc' : 'desc';
  } else {
    fs.sort = by;
    fs.order = by === 'name' ? 'asc' : 'desc';
  }
  updateSearchSortButtons();
  const kwEl = document.getElementById('fKey');
  if (kwEl && (kwEl.value || '').trim()) doSearchFiles();
}

function updateSearchSortButtons() {
  const fs = clean.fsearch;
  const arr = (on) => on ? (fs.order === 'desc' ? ' ▼' : ' ▲') : '';
  const bs = document.getElementById('btnFSortSize');
  const bt = document.getElementById('btnFSortTime');
  const bn = document.getElementById('btnFSortName');
  if (bs) bs.textContent = '按大小' + arr(fs.sort === 'size');
  if (bt) bt.textContent = '按时间' + arr(fs.sort === 'time');
  if (bn) bn.textContent = '按名称' + arr(fs.sort === 'name');
}

async function measureSearchSelected() {
  const picked = searchCheckedRows();
  if (!picked.length) { toast('请先勾选要测算的结果行', 'err'); return; }
  const withSize = picked.filter(r => !(r.is_dir && r.size < 0));
  let targets = withSize.length ? withSize : picked;
  targets = targets.slice(0, 40);
  try {
    const json = await api('/api/search/measure', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths: targets.map(r => r.path), max_seconds: 25 }),
    });
    const map = {};
    ((json.data || {}).items || []).forEach(i => { map[i.path] = i; });
    clean.fsearch.rows.forEach(r => {
      const d = map[r.path];
      if (d && d.size >= 0) {
        r.size = d.size;
        r.size_text = d.size_text;
      }
    });
    renderSearchResults();
    toast(`已测算 ${targets.length} 项占用`, 'ok');
  } catch (err) {
    toast('测算失败：' + err.message, 'err');
  }
}

async function revealSearchRow(path) {
  if (!path) {
    const picked = searchCheckedRows();
    if (!picked.length) { toast('请先勾选一行', 'err'); return; }
    path = picked[0].path;
  }
  try {
    const json = await api('/api/search/reveal', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: path }),
    });
    toast(json.message || (json.ok ? '已打开' : '打开失败'), json.ok ? 'ok' : 'err');
  } catch (err) {
    toast('打开失败：' + err.message, 'err');
  }
}

async function deleteSearchSelected() {
  const picked = searchCheckedRows();
  if (!picked.length) { toast('请先勾选要删除的结果行', 'err'); return; }
  const total = picked.reduce((s, r) => s + (r.size > 0 ? r.size : 0), 0);
  confirmAction(
    `删除 ${picked.length} 项`,
    `即将把以下 ${picked.length} 项送入回收站：\n` +
    `${picked.slice(0, 8).map(r => '· ' + r.name +
      (r.size > 0 ? '（' + r.size_text + '）' : '（文件夹）')).join('\n')}` +
    (picked.length > 8 ? `\n…以及另外 ${picked.length - 8} 项` : '') +
    (total > 0 ? `\n\n合计 ${formatBytes(total)}。` : '\n\n文件夹体积未测算，实际释放空间以回收站为准。') +
    '\n\n删除的文件送入回收站，可随时还原；位于系统保护目录的文件会被自动跳过。',
    async () => {
      const json = await api('/api/cleanup/delete-files', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths: picked.map(r => r.path), recycle: true }),
      });
      const d = json.data || {};
      toast(`${json.message || d.message || '已提交删除'}` +
        (d.skipped ? `，跳过 ${d.skipped} 个受保护文件` : ''), json.ok ? 'ok' : 'err');
      clean.fsearch.rows.forEach(r => { r._checked = false; });
      await doSearchFiles();
    }
  );
}

/* ---------------- 面板事件绑定 ---------------- */

function bindPanels() {
  document.getElementById('btnCleanMem').onclick = doCleanMemory;
  document.getElementById('btnScanTargets').onclick = () => scanCleanTargets(true);
  document.getElementById('btnCleanDisk').onclick = doCleanDisk;
  document.getElementById('btnEmptyBin').onclick = doEmptyRecycleBin;
  document.getElementById('btnScanFiles').onclick = scanLargeFiles;
  document.getElementById('btnCleanFiles').onclick = doDeleteFiles;

  document.getElementById('btnSelectSafe').onclick = () => {
    setTargetChecked(
      clean.targets.filter(t => t.risk === 'safe' && t.available && t.size).map(t => t.id),
      true
    );
  };
  document.getElementById('btnSelectNone').onclick = () => setTargetChecked([], false);

  // --- 微信清理 ---
  document.getElementById('btnScanWechat').onclick = scanWechat;
  document.getElementById('btnCleanWechat').onclick = doDeleteWechat;
  document.getElementById('btnWxSortSize').onclick = () => setWxSort('size');
  document.getElementById('btnWxSortTime').onclick = () => setWxSort('time');
  const cWxKind = document.getElementById('cWxKind');
  if (cWxKind) {
    cWxKind.onchange = () => {
      clean.wx.kind = cWxKind.value;
      if (clean.wx.loaded) scanWechat();
    };
  }
  const btnWxSelKind = document.getElementById('btnWxSelectKind');
  if (btnWxSelKind) btnWxSelKind.onclick = selectWxKind;

  // --- 软件清理（已安装软件 + AI 工具缓存，合并展示） ---
  document.getElementById('btnScanSoftAll').onclick = scanSoftAll;
  document.getElementById('btnCleanSoftSelected').onclick = doCleanSoftSelected;
  document.getElementById('btnUninstallSoft').onclick = doUninstallSoft;
  document.getElementById('btnSelectAllSoft').onclick = selectAllSoftRows;
  document.getElementById('btnSoftAllSortSize').onclick = () => setSoftAllSort('size');
  document.getElementById('btnSoftAllSortName').onclick = () => setSoftAllSort('name');
  const cSoftFilter = document.getElementById('cSoftFilter');
  if (cSoftFilter) {
    let filterTimer = null;
    cSoftFilter.oninput = () => {
      clearTimeout(filterTimer);
      filterTimer = setTimeout(() => {
        clean.sgroup.filter = cSoftFilter.value || '';
        renderSoftAll();
      }, 200);
    };
  }
  const cSoftSource = document.getElementById('cSoftSource');
  if (cSoftSource) {
    cSoftSource.onchange = () => {
      clean.sgroup.source = cSoftSource.value || 'all';
      renderSoftAll();
    };
  }
  document.getElementById('btnSoftFileSortSize').onclick = () => setSoftFileSort('size');
  document.getElementById('btnSoftFileSortTime').onclick = () => setSoftFileSort('time');
  document.getElementById('btnSoftFileSelectAll').onclick = toggleSoftFilesAll;
  document.getElementById('btnSoftFileDelete').onclick = doDeleteSoftFiles;
  const cSoftFileScope = document.getElementById('cSoftFileScope');
  if (cSoftFileScope) {
    cSoftFileScope.onchange = () => {
      clean.sgroup.scope = cSoftFileScope.value || 'cache';
      if (clean.sgroup.target) loadSoftFiles(clean.sgroup.target);
    };
  }

  // --- 顶层文件查找 ---
  document.getElementById('btnSearchFiles').onclick = doSearchFiles;
  document.getElementById('btnFSelectAll').onclick = toggleSearchAll;
  document.getElementById('btnFMeasure').onclick = measureSearchSelected;
  document.getElementById('btnFReveal').onclick = () => revealSearchRow('');
  document.getElementById('btnFDelete').onclick = deleteSearchSelected;
  document.getElementById('btnFSortSize').onclick = () => setSearchSort('size');
  document.getElementById('btnFSortTime').onclick = () => setSearchSort('time');
  document.getElementById('btnFSortName').onclick = () => setSearchSort('name');
  const fKey = document.getElementById('fKey');
  if (fKey) fKey.onkeydown = (e) => { if (e.key === 'Enter') doSearchFiles(); };
  updateSearchSortButtons();

  // 首次切到微信页 / 软件清理页时自动扫描一次
  const clnTreeEl = document.getElementById('clnTree');
  if (clnTreeEl) {
    clnTreeEl.addEventListener('click', (e) => {
      const item = e.target.closest('.tree-item');
      if (!item) return;
      if (item.dataset.page === 'clnWechat' && !clean.wx.loaded) scanWechat();
      // 软件清理：默认打开就扫描本机全部软件（已安装软件 + AI 工具缓存）
      if (item.dataset.page === 'clnAgent' && !clean.sgroup.loaded) scanSoftAll();
    });
  }

  bindTreeNav('monTree');
  bindTreeNav('clnTree');

  // 窗口尺寸变化时重画趋势图，避免拉伸模糊
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    if (state.tab !== 'monitor') return;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => drawChart((mon.data && mon.data.history) || []), 200);
  });
}

/* ---------------- 树状图导航 ---------------- */

function bindTreeNav(treeId) {
  const tree = document.getElementById(treeId);
  if (!tree) return;
  tree.addEventListener('click', (e) => {
    const item = e.target.closest('.tree-item');
    if (!item) return;
    const panel = tree.closest('.panel');
    if (!panel) return;
    panel.querySelectorAll('.tree-item').forEach(t => t.classList.toggle('active', t === item));
    const page = item.dataset.page;
    panel.querySelectorAll('.tree-page').forEach(p => { p.hidden = p.dataset.page !== page; });
    // 概览页含 canvas，切换显示后需按真实宽度重画趋势图
    if (state.tab === 'monitor' && page === 'monOverview') {
      requestAnimationFrame(() => drawChart((mon.data && mon.data.history) || []));
    }
  });
}
