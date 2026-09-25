/* Windows进程管理工具（潍鲸 - weijing.co） - 前端逻辑 */

'use strict';

const MAX_ROWS = 500;

const state = {
  data: null,
  tab: 'running',
  search: '',
  filter: 'all',
  sort: { key: '', dir: 1 },
  loading: false,
  timer: null,
  colWidths: {},    // { tab: { colKey: px } } 用户拖拽后的列宽
  hiddenCols: {},   // { tab: [colKey, ...] } 被隐藏的列
};

/* ---------------- 工具函数 ---------------- */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function statusTag(text, status) {
  const cls = status === 'running' ? 'tag tag-running'
    : (status || '').includes('pending') ? 'tag tag-pending'
    : 'tag tag-stopped';
  return el('span', cls, text);
}

function portChips(ports) {
  if (!ports || !ports.length) return el('span', 'cell-sub', '-');
  const wrap = document.createElement('div');
  ports.slice(0, 8).forEach(p => wrap.appendChild(el('span', 'port-chip', p)));
  if (ports.length > 8) wrap.appendChild(el('span', 'cell-sub', `+${ports.length - 8}`));
  return wrap;
}

function memoryText(v) {
  if (v === null || v === undefined) return '-';
  return v >= 1024 ? `${(v / 1024).toFixed(1)} GB` : `${v} MB`;
}

function cpuText(v) {
  return (v === null || v === undefined) ? '-' : `${v}%`;
}

/* 估算文本显示宽度：中文按全角、英文按半角粗略加权 */
const CJK_RE = /[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef]/;
function textWidth(s, fontSize) {
  let w = 0;
  for (const ch of String(s)) w += CJK_RE.test(ch) ? fontSize : fontSize * 0.56;
  return w;
}

/* 列宽按当前数据内容自动调节，不超过列定义的上限（完整文本，不截断） */
function autoWidth(col, rows) {
  if (!col.w || col.key === '_actions' || col.render) return null;
  const size = (col.def || '').includes('cell-sub') || (col.def || '').includes('cell-path') ? 12 : 13;
  let max = textWidth(col.label, 12) + 26;            // 表头至少要放得下
  for (let i = 0; i < rows.length && i < 300; i++) {
    const v = rows[i][col.key];
    if (v === null || v === undefined || v === '') continue;
    const w = textWidth(String(v), size);
    if (w > max) max = w;
  }
  return Math.max(56, Math.min(Math.ceil(max + 26), col.w));
}

/* 当前页签可见列（剔除被隐藏的列） */
function visibleColumns(tab) {
  const cols = COLUMNS[tab] || [];
  const hidden = new Set(state.hiddenCols[tab] || []);
  return cols.filter(c => !hidden.has(c.key));
}

/* 某列当前生效宽度：优先用户拖拽值，否则按内容自动、再否则列定义宽度 */
function colWidth(tab, col) {
  const stored = (state.colWidths[tab] || {})[col.key];
  if (stored) return stored;
  return autoWidth(col, currentRows()) || col.w;
}

/* ---------------- 列定义 ---------------- */

const COLUMNS = {
  running: [
    { key: 'display_name', label: '显示名称', def: 'cell-main', w: 250 },
    { key: 'name', label: '服务名', def: 'mono cell-sub', w: 170 },
    { key: 'status_text', label: '状态', render: r => statusTag(r.status_text || r.status, r.status), w: 90 },
    { key: 'pid', label: 'PID', def: 'mono', w: 80 },
    { key: 'process_name', label: '进程', def: 'mono', w: 120 },
    { key: 'ports', label: '端口', render: r => portChips(r.ports), w: 150 },
    { key: 'start_type_text', label: '启动类型', w: 100 },
    { key: 'memory_mb', label: '内存', render: r => el('span', '', memoryText(r.memory_mb)), w: 90 },
    { key: '_actions', label: '操作', sortable: false, w: 150 },
  ],
  services: [
    { key: 'display_name', label: '显示名称', def: 'cell-main', w: 250 },
    { key: 'name', label: '服务名', def: 'mono cell-sub', w: 170 },
    { key: 'status_text', label: '状态', render: r => statusTag(r.status_text || r.status, r.status), w: 90 },
    { key: 'pid', label: 'PID', def: 'mono', w: 80 },
    { key: 'ports', label: '端口', render: r => portChips(r.ports), w: 150 },
    { key: 'start_type', label: '启动类型', render: startTypeSelect, w: 110 },
    { key: 'username', label: '登录身份', w: 130 },
    { key: '_actions', label: '操作', sortable: false, w: 150 },
  ],
  processes: [
    { key: 'name', label: '进程名', def: 'cell-main', w: 170 },
    { key: 'pid', label: 'PID', def: 'mono', w: 80 },
    { key: 'ppid', label: '父 PID', def: 'mono', w: 80 },
    { key: 'cpu', label: 'CPU', render: r => el('span', '', cpuText(r.cpu)), w: 70 },
    { key: 'memory_mb', label: '内存', render: r => el('span', '', memoryText(r.memory_mb)), w: 90 },
    { key: 'ports', label: '端口', render: r => portChips(r.ports), w: 150 },
    { key: 'create_time', label: '启动时间', def: 'mono', w: 150 },
    { key: 'username', label: '用户', w: 120 },
    { key: 'exe', label: '路径 / 命令行', def: 'cell-path', tip: true, w: 360 },
    { key: '_actions', label: '操作', sortable: false, w: 190 },
  ],
  ports: [
    { key: 'local_port', label: '端口', def: 'mono cell-main', w: 90 },
    { key: 'proto', label: '协议', w: 80 },
    { key: 'local_ip', label: '本地地址', def: 'mono', w: 170 },
    { key: 'state_text', label: '状态', render: r => statusTag(r.state_text, r.state === 'LISTEN' || r.state === 'LISTENING' ? 'running' : 'stopped'), w: 90 },
    { key: 'pid', label: 'PID', def: 'mono', w: 80 },
    { key: 'process_name', label: '进程', def: 'mono', w: 150 },
    { key: 'remote_ip', label: '远程地址', def: 'mono', w: 170 },
    { key: '_actions', label: '操作', sortable: false, w: 190 },
  ],
};

const DEFAULT_SORT = {
  running: { key: 'display_name', dir: 1 },
  services: { key: 'display_name', dir: 1 },
  processes: { key: 'memory_mb', dir: -1 },
  ports: { key: 'local_port', dir: 1 },
};

const FILTERS = {
  running: [['all', '全部状态'], ['running', '运行中'], ['start_pending', '启动中'], ['stop_pending', '停止中']],
  services: [['all', '全部状态'], ['running', '运行中'], ['stopped', '已停止'], ['disabled', '已禁用（启动类型）']],
  processes: [['all', '全部进程'], ['with_port', '占用端口的进程'], ['high_mem', '内存 > 100MB']],
  ports: [['all', '全部连接'], ['listen', '仅监听端口'], ['tcp', '仅 TCP'], ['udp', '仅 UDP']],
};

/* ---------------- 数据获取 ---------------- */

async function api(path, options) {
  const res = await fetch(path, options);
  const json = await res.json().catch(() => ({ ok: false, error: '响应解析失败' }));
  if (!json.ok && json.error) throw new Error(json.error);
  return json;
}

async function scan(force, silent, quick) {
  if (state.loading) return;
  state.loading = true;
  if (!silent) setLoading(true);
  try {
    const url = `/api/scan?force=${force ? 1 : 0}&quick=${quick ? 1 : 0}`;
    const json = await api(url);
    state.data = json.data;
    renderAll();
  } catch (err) {
    toast(`扫描失败：${err.message}`, 'err');
  } finally {
    state.loading = false;
    setLoading(false);
  }
}

async function loadStatus() {
  try {
    const json = await api('/api/status');
    const d = json.data;
    document.getElementById('envInfo').textContent =
      `${d.hostname} · 本机服务端口 ${d.port} · 进程 ${d.pid} · 扫描于 ${d.time}`;

    const badge = document.getElementById('adminBadge');
    badge.className = `badge ${d.admin ? 'badge-green' : 'badge-orange'}`;
    badge.textContent = d.admin ? '✓ 管理员权限' : '⚠ 非管理员权限';
    document.getElementById('btnElevate').hidden = d.admin;
  } catch (e) { /* 忽略 */ }
}

/* ---------------- 渲染 ---------------- */

function currentRows() {
  if (!state.data) return [];
  const { services = [], processes = [], ports = [] } = state.data;
  let rows;
  switch (state.tab) {
    case 'running': rows = services.filter(s => s.status === 'running' || s.status.includes('pending')); break;
    case 'services': rows = services.slice(); break;
    case 'processes': rows = processes.slice(); break;
    case 'ports': rows = ports.slice(); break;
    default: rows = [];
  }

  const f = state.filter;
  if (f !== 'all') {
    rows = rows.filter(r => {
      if (state.tab === 'services' && f === 'disabled') return r.start_type === 'disabled';
      if (state.tab === 'processes' && f === 'with_port') return r.ports && r.ports.length;
      if (state.tab === 'processes' && f === 'high_mem') return (r.memory_mb || 0) > 100;
      if (state.tab === 'ports' && f === 'listen') return r.state === 'LISTEN' || r.state === 'LISTENING';
      if (state.tab === 'ports' && f === 'tcp') return r.proto.startsWith('TCP');
      if (state.tab === 'ports' && f === 'udp') return r.proto.startsWith('UDP');
      return r.status === f;
    });
  }

  const kw = state.search.trim().toLowerCase();
  if (kw) {
    rows = rows.filter(r => haystack(r).includes(kw));
  }

  const { key, dir } = state.sort;
  if (key) {
    rows.sort((a, b) => {
      const va = sortValue(a, key), vb = sortValue(b, key);
      if (va === vb) return 0;
      if (va === null || va === undefined) return 1;
      if (vb === null || vb === undefined) return -1;
      return (va > vb ? 1 : -1) * dir;
    });
  }
  return rows;
}

function haystack(r) {
  if (r._hay) return r._hay;
  const parts = [];
  for (const k in r) {
    const v = r[k];
    if (Array.isArray(v)) parts.push(v.join(' '));
    else if (v !== null && typeof v !== 'object') parts.push(String(v));
  }
  r._hay = parts.join(' ').toLowerCase();
  return r._hay;
}

function sortValue(r, key) {
  const v = r[key];
  if (Array.isArray(v)) return v.length ? v[0] : -1;
  return v;
}

function renderHead() {
  const head = document.getElementById('tableHead');
  head.textContent = '';
  const tr = document.createElement('tr');
  visibleColumns(state.tab).forEach(col => {
    const sortable = col.sortable !== false;
    const th = el('th', sortable ? '' : 'no-sort');
    if (col.key === '_actions') th.classList.add('col-actions');
    const w = colWidth(state.tab, col);
    if (w) th.style.width = `${w}px`;
    th.appendChild(document.createTextNode(col.label));
    if (sortable) {
      const arrow = el('span', 'arrow', state.sort.key === col.key ? (state.sort.dir === 1 ? '▲' : '▼') : '▲');
      th.appendChild(arrow);
      if (state.sort.key === col.key) th.classList.add('sorted');
      th.onclick = () => {
        if (state.sort.key === col.key) state.sort.dir *= -1;
        else state.sort = { key: col.key, dir: 1 };
        renderHead();
        renderBody();
      };
    }
    // 列宽拖拽手柄
    const handle = el('span', 'col-resizer');
    handle.addEventListener('mousedown', (e) => startResize(e, th, col));
    handle.addEventListener('click', (e) => e.stopPropagation());
    th.appendChild(handle);
    tr.appendChild(th);
  });
  head.appendChild(tr);
}

/* 列宽拖拽：记录起点，移动时实时更新表头与该列所有单元格宽度 */
function startResize(e, th, col) {
  e.stopPropagation();
  e.preventDefault();
  const tab = state.tab;
  const startX = e.clientX;
  const startW = th.getBoundingClientRect().width;
  const handle = e.currentTarget;
  handle.classList.add('dragging');
  document.body.style.cursor = 'col-resize';

  function onMove(ev) {
    const w = Math.max(48, Math.round(startW + (ev.clientX - startX)));
    state.colWidths[tab] = state.colWidths[tab] || {};
    state.colWidths[tab][col.key] = w;
    th.style.width = w + 'px';
    const cols = visibleColumns(tab);
    const vi = cols.findIndex(c => c.key === col.key);
    document.querySelectorAll('#tableBody tr').forEach(tr => {
      const td = tr.children[vi];
      if (td) { td.style.width = w + 'px'; td.style.maxWidth = w + 'px'; }
    });
  }
  function onUp() {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    handle.classList.remove('dragging');
    document.body.style.cursor = '';
  }
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}

function renderBody() {
  const body = document.getElementById('tableBody');
  body.textContent = '';

  const rows = currentRows();
  const shown = rows.slice(0, MAX_ROWS);
  const cols = visibleColumns(state.tab);
  const widths = {};
  cols.forEach(col => { widths[col.key] = colWidth(state.tab, col); });

  shown.forEach(r => {
    const tr = document.createElement('tr');
    cols.forEach(col => {
      const td = document.createElement('td');
      const cls = col.def || col.cell || '';
      if (cls) td.className = cls;
      if (widths[col.key]) {
        td.style.width = `${widths[col.key]}px`;
        td.style.maxWidth = `${widths[col.key]}px`;
      }

      if (col.key === '_actions') {
        td.className = 'actions';
        buildActions(td, r);
      } else if (col.render) {
        td.appendChild(col.render(r));
      } else {
        const v = r[col.key];
        if (v === null || v === undefined || v === '') {
          td.textContent = '-';
        } else {
          // 完整显示，不再截断
          td.textContent = String(v);
          if (col.tip) td.title = String(v);
        }
      }
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });

  document.getElementById('emptyState').hidden = rows.length !== 0;
  document.getElementById('loadingState').hidden = true;

  const info = document.getElementById('countInfo');
  info.textContent = rows.length > MAX_ROWS
    ? `共 ${rows.length} 条，显示前 ${MAX_ROWS} 条（可用搜索缩小范围）`
    : `共 ${rows.length} 条`;
}

function startTypeSelect(r) {
  if (r.protected) {
    const span = el('span', '', r.start_type_text || '-');
    span.appendChild(el('span', 'lock', '🔒'));
    span.title = '系统核心服务，已保护';
    return span;
  }
  const sel = el('select', 'select-xs');
  [['automatic', '自动'], ['manual', '手动'], ['disabled', '禁用']].forEach(([v, t]) => {
    const op = el('option', '', t);
    op.value = v;
    if (r.start_type === v) op.selected = true;
    sel.appendChild(op);
  });
  sel.onchange = async () => {
    const val = sel.value;
    sel.disabled = true;
    try {
      const json = await api('/api/service/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: r.name, start_type: val }),
      });
      toast(json.message, json.ok ? 'ok' : 'err');
      r.start_type = val;
      r.start_type_text = { automatic: '自动', manual: '手动', disabled: '禁用' }[val];
    } catch (e) { toast(e.message, 'err'); }
    sel.disabled = false;
    setTimeout(() => scan(true, true), 800);
  };
  return sel;
}

function buildActions(td, r) {
  if (state.tab === 'processes' || state.tab === 'ports') {
    const pid = r.pid;
    if (!pid) { td.appendChild(el('span', 'cell-sub', '-')); return; }
    const pname = String(r.name || r.process_name || pid);
    if (r.protected || pname.toLowerCase().match(/^(system|csrss|wininit|winlogon|services|lsass|smss|registry|memory compression)/)) {
      td.appendChild(el('span', 'cell-sub', '已保护'));
      return;
    }
    // 该进程承载了 Windows 服务时，额外提供「停止服务 / 重启服务」
    const svc = serviceOfPid(pid);
    if (svc && !svc.protected) {
      td.appendChild(makeBtn('停止服务', 'btn btn-xs btn-warn', () => confirmAction(
        '停止服务',
        `确定停止服务「${svc.display_name || svc.name}」吗？它正由进程 ${pname} (PID ${pid}) 承载。`,
        () => doService(svc.name, 'stop')
      )));
      td.appendChild(makeBtn('重启服务', 'btn btn-xs btn-ghost', () => confirmAction(
        '重启服务',
        `确定重启服务「${svc.display_name || svc.name}」吗？`,
        () => doService(svc.name, 'restart')
      )));
      return;
    }
    td.appendChild(makeBtn('结束', 'btn btn-xs btn-danger', () => confirmAction(
      '结束进程',
      `确定结束进程「${pname}」(PID ${pid}) 吗？未保存的数据可能丢失。`,
      () => doKill(pid, false)
    )));
    td.appendChild(makeBtn('结束树', 'btn btn-xs btn-ghost', () => confirmAction(
      '结束进程树',
      `确定结束「${pname}」(PID ${pid}) 及其全部子进程吗？`,
      () => doKill(pid, true)
    )));
    return;
  }

  // 服务操作
  const st = r.status;
  if (r.protected) {
    td.appendChild(el('span', 'cell-sub', '已保护'));
    return;
  }
  if (st === 'stopped') {
    td.appendChild(makeBtn('启动', 'btn btn-xs btn-success', () => doService(r.name, 'start')));
  } else if (st === 'running') {
    td.appendChild(makeBtn('停止', 'btn btn-xs btn-danger', () => confirmAction(
      '停止服务', `确定停止服务「${r.display_name || r.name}」吗？`, () => doService(r.name, 'stop'))));
    td.appendChild(makeBtn('重启', 'btn btn-xs btn-ghost', () => confirmAction(
      '重启服务', `确定重启服务「${r.display_name || r.name}」吗？`, () => doService(r.name, 'restart'))));
    return;
  }

  // 启动中 / 停止中 / 暂停等中间状态：仍允许停止与重启
  td.appendChild(makeBtn('停止', 'btn btn-xs btn-danger', () => confirmAction(
    '停止服务', `确定停止服务「${r.display_name || r.name}」吗？`, () => doService(r.name, 'stop'))));
  td.appendChild(makeBtn('重启', 'btn btn-xs btn-ghost', () => confirmAction(
    '重启服务', `确定重启服务「${r.display_name || r.name}」吗？`, () => doService(r.name, 'restart'))));
}

/* 找出由某个 PID 承载的 Windows 服务（用于在进程 / 端口视图里提供启停服务） */
function serviceOfPid(pid) {
  if (!state.data || !pid) return null;
  const list = state.data.services || [];
  for (const s of list) if (s.pid === pid && s.status === 'running') return s;
  return null;
}

function makeBtn(text, cls, onClick) {
  const b = el('button', cls, text);
  b.onclick = onClick;
  return b;
}

/* ---------------- 操作 ---------------- */

async function doService(name, action) {
  const label = { start: '启动', stop: '停止', restart: '重启' }[action];
  toast(`正在${label}服务 ${name}…`);
  try {
    const json = await api('/api/service/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, action }),
    });
    toast(json.message, json.ok ? 'ok' : 'err');
  } catch (e) { toast(e.message, 'err'); }
  setTimeout(() => scan(true, true), 1200);
}

async function doKill(pid, tree) {
  try {
    const json = await api('/api/process/kill', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pid, tree }),
    });
    toast(json.message, json.ok ? 'ok' : 'err');
  } catch (e) { toast(e.message, 'err'); }
  setTimeout(() => scan(true, true), 1000);
}

/* ---------------- UI 辅助 ---------------- */

let toastTimer = null;
function toast(msg, type) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = `toast ${type === 'ok' ? 'ok' : type === 'err' ? 'err' : ''}`;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, type === 'ok' || type === 'err' ? 3200 : 1800);
}

let modalResolve = null;
function confirmAction(title, body, onOk) {
  document.getElementById('modalTitle').textContent = title;
  document.getElementById('modalBody').textContent = body;
  document.getElementById('modal').hidden = false;
  modalResolve = onOk;
}

function setLoading(v) {
  document.getElementById('loadingState').hidden = !v;
  const btn = document.getElementById('btnRefresh');
  btn.disabled = v;
  document.getElementById('refreshText').textContent = v ? '扫描中…' : '刷新扫描';
}

function renderStats() {
  if (!state.data) return;
  const s = state.data.summary;
  document.getElementById('statRunning').textContent = s.services_running;
  document.getElementById('statServiceTotal').textContent = `服务总数 ${s.services_total}`;
  document.getElementById('statProc').textContent = s.processes_total;
  document.getElementById('statProcFoot').textContent = `CPU ${s.cpu_percent}%`;
  document.getElementById('statPorts').textContent = s.ports_listen;
  document.getElementById('statPortsTotal').textContent = `连接总数 ${s.ports_total}`;
  document.getElementById('statCpu').textContent = `${s.cpu_percent}% / ${s.memory_percent}%`;
  document.getElementById('statFoot').textContent =
    `扫描耗时 ${state.data.elapsed}s · ${state.data.scan_time}`;

  document.querySelectorAll('#tabs .tab').forEach(tab => {
    const key = tab.dataset.tab;
    const cntEl = tab.querySelector('.cnt');
    if (!cntEl) return;
    const d = state.data;
    const n = key === 'running' ? d.summary.services_running
      : key === 'services' ? d.summary.services_total
      : key === 'processes' ? d.summary.processes_total
      : d.summary.ports_total;
    cntEl.textContent = n;
  });
}

function buildFilterOptions() {
  const sel = document.getElementById('statusFilter');
  sel.textContent = '';
  FILTERS[state.tab].forEach(([v, t]) => {
    const op = el('option', '', t);
    op.value = v;
    sel.appendChild(op);
  });
  sel.value = 'all';
  state.filter = 'all';
}

function renderAll() {
  renderStats();
  // 监控 / 磁盘清理 为面板页签，不渲染数据表格，避免 COLUMNS[tab] 为 undefined 触发 .filter 报错
  if (typeof PANEL_TABS !== 'undefined' && PANEL_TABS[state.tab]) return;
  renderHead();
  renderBody();
}

/* 显示列卡片：每列一张可勾选的卡片，点击卡片即切换显示 / 隐藏 */
function buildColMenu() {
  const menu = document.getElementById('colMenu');
  menu.textContent = '';
  menu.appendChild(el('div', 'col-card-title', '勾选要显示的列：'));
  const grid = el('div', 'col-card-grid');
  COLUMNS[state.tab].forEach(col => {
    const hidden = new Set(state.hiddenCols[state.tab] || []);
    const on = !hidden.has(col.key);
    const chip = el('label', 'col-chip' + (on ? ' on' : ''));
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = on;
    cb.addEventListener('change', () => {
      chip.classList.toggle('on', cb.checked);
      state.hiddenCols[state.tab] = state.hiddenCols[state.tab] || [];
      const h = new Set(state.hiddenCols[state.tab]);
      if (cb.checked) h.delete(col.key); else h.add(col.key);
      state.hiddenCols[state.tab] = Array.from(h);
      renderHead();
      renderBody();
    });
    chip.appendChild(cb);
    chip.appendChild(el('span', 'col-chip-label', col.label));
    grid.appendChild(chip);
  });
  menu.appendChild(grid);
}

/* ---------------- 事件绑定 ---------------- */

function bind() {
  document.querySelectorAll('#tabs .tab').forEach(tab => {
    tab.onclick = () => switchTab(tab);
  });

  let searchTimer = null;
  const searchInput = document.getElementById('search');
  searchInput.oninput = () => {
    document.getElementById('clearSearch').hidden = !searchInput.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.search = searchInput.value; renderBody(); }, 200);
  };

  document.getElementById('clearSearch').onclick = () => {
    searchInput.value = '';
    state.search = '';
    document.getElementById('clearSearch').hidden = true;
    renderBody();
  };

  document.getElementById('statusFilter').onchange = (e) => {
    state.filter = e.target.value;
    renderBody();
  };

  document.getElementById('btnRefresh').onclick = () => scan(true);

  // 列设置：点击展开 / 收起下拉
  const btnCol = document.getElementById('btnColSettings');
  btnCol.onclick = (e) => {
    e.stopPropagation();
    const menu = document.getElementById('colMenu');
    if (menu.hidden) { buildColMenu(); menu.hidden = false; }
    else menu.hidden = true;
  };
  // 点击下拉以外区域自动收起
  document.addEventListener('click', (e) => {
    const menu = document.getElementById('colMenu');
    const wrap = document.querySelector('.col-menu-wrap');
    if (!menu.hidden && !menu.contains(e.target) && !wrap.contains(e.target)) menu.hidden = true;
  });

  const auto = document.getElementById('autoRefresh');
  const interval = document.getElementById('refreshInterval');
  const applyAuto = () => {
    clearInterval(state.timer);
    state.timer = null;
    if (auto.checked) {
      state.timer = setInterval(() => scan(true, true), parseInt(interval.value, 10) * 1000);
    }
  };
  auto.onchange = applyAuto;
  interval.onchange = applyAuto;

  document.getElementById('btnElevate').onclick = async () => {
    try {
      const json = await api('/api/elevate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      toast(json.message, json.ok ? 'ok' : 'err');
    } catch (e) { toast(e.message, 'err'); }
  };

  const modal = document.getElementById('modal');
  const closeModal = () => {
    modal.hidden = true;
    modal.style.display = '';
    modalResolve = null;
  };

  document.getElementById('modalCancel').onclick = closeModal;
  document.getElementById('modalOk').onclick = () => {
    const fn = modalResolve;
    closeModal();
    if (fn) fn();
  };
  // 点击遮罩空白处 / 按 ESC 也能关闭
  modal.onclick = (e) => { if (e.target === modal) closeModal(); };
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !modal.hidden) closeModal();
  });
}

/* ---------------- 启动 ---------------- */

(async function init() {
  document.getElementById('modal').hidden = true;
  bind();
  bindPanels();
  buildFilterOptions();
  state.sort = { ...DEFAULT_SORT[state.tab] };
  await loadStatus();
  await scan(true);
})();
