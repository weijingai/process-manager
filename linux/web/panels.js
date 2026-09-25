/* Linux进程管理工具（潍鲸 - weijing.co） - 系统监控面板 / 清理面板
 *
 * 依赖 app.js 提供的全局函数：el / api / toast / state / confirmAction /
 * DEFAULT_SORT / buildFilterOptions / renderHead / renderBody。
 * 本文件必须在 app.js 之后加载（同为经典脚本，共享全局作用域）。
 */

'use strict';

const PANEL_TABS = { monitor: 1, cleanup: 1 };

const mon = { data: null, timer: null };
const clean = {
  targets: [],
  files: [],
  busyTargets: false,
  busyFiles: false,
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
    if (t.admin) name.appendChild(el('span', 'tag-sm t-risk-medium', '需 root'));
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
