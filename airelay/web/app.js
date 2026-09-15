/* ==========================================================================
   本地 AI 中转站 · 控制台
   纯原生 JS：无构建步骤、无外链依赖，断网环境（NAS 内网）也能完整使用。
   结构：工具 → 图表 → 状态 → 视图 → 启动
   ========================================================================== */
(() => {
'use strict';

/* ======================================================================== */
/* 一、基础工具                                                              */
/* ======================================================================== */

const NS_SVG = 'http://www.w3.org/2000/svg';

function append(node, kids) {
  for (const kid of kids) {
    if (kid === null || kid === undefined || kid === false || kid === true) continue;
    if (Array.isArray(kid)) append(node, kid);
    else if (kid instanceof Node) node.appendChild(kid);
    else node.appendChild(document.createTextNode(String(kid)));
  }
}

function el(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'style' && typeof value === 'object') Object.assign(node.style, value);
      // input / textarea / select 的 value 必须走属性赋值，setAttribute 对 textarea 无效
      else if (key === 'value' && 'value' in node) node.value = value;
      else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else node.setAttribute(key, value === true ? '' : String(value));
    }
  }
  append(node, kids);
  return node;
}

function svg(tag, attrs, ...kids) {
  const node = document.createElementNS(NS_SVG, tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'text') node.textContent = value;
      else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, String(value));
    }
  }
  append(node, kids);
  return node;
}

const $ = (sel, root) => (root || document).querySelector(sel);

const ICONS = {
  dashboard: ['M3 3h7v7H3z', 'M14 3h7v5h-7z', 'M14 12h7v9h-7z', 'M3 14h7v7H3z'],
  channels: ['M3 5h18v5H3z', 'M3 14h18v5H3z', 'M6.5 7.5h.01', 'M6.5 16.5h.01'],
  keys: ['M14.5 9.5a4 4 0 1 1-1.2-2.8', 'M13.3 6.7 20 13.4', 'M17.5 10.9l1.8 1.8', 'M15.7 12.7l1.8 1.8', 'M6 18a2 2 0 1 0 0-.01'],
  models: ['M12 3 21 8l-9 5-9-5z', 'M3 13l9 5 9-5', 'M3 17.5 12 22.5l9-5'],
  stats: ['M4 20V11', 'M9.4 20V5', 'M14.8 20v-6', 'M20.2 20V8', 'M2.5 20h19'],
  live: ['M2 12h3.5l2-6 3.2 12L13.6 11l1.8 4h6.6'],
  balance: ['M3 7h18v10H3z', 'M3 11h18', 'M16.4 14.2h1.6'],
  settings: ['M4 7.5h9', 'M17.5 7.5h2.5', 'M4 16.5h4.5', 'M13 16.5h7', 'M15 7.5a1.7 1.7 0 1 0 0-.01', 'M11 16.5a1.7 1.7 0 1 0 0-.01'],
  cpu: ['M7 7h10v10H7z', 'M4 10h3M4 14h3M17 10h3M17 14h3M10 4v3M14 4v3M10 17v3M14 17v3'],
  copy: ['M9 9h10v10H9z', 'M5 15V5h10'],
  plus: ['M12 5v14', 'M5 12h14'],
  refresh: ['M20 11a8 8 0 1 0-2.3 6.3', 'M20 5v6h-6'],
  download: ['M12 4v11', 'M7.5 10.5 12 15l4.5-4.5', 'M4.5 19.5h15'],
  trash: ['M5 7h14', 'M9 7V5h6v2', 'M7 7l1 13h8l1-13'],
  edit: ['M5 19h3l10-10-3-3L5 16z', 'M14.5 5.5 18 9'],
  search: ['M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14', 'M16.2 16.2 20 20'],
  check: ['M5 12.5 10 17.5 19 7'],
  alert: ['M12 4 21 20H3z', 'M12 10v5', 'M12 17.5h.01'],
  arrow: ['M5 12h13', 'M13 7l5 5-5 5'],
  dot: ['M12 12h.01'],
};

function icon(name, size) {
  const paths = ICONS[name] || ICONS.dot;
  const box = size || 19;
  return svg('svg', { viewBox: '0 0 24 24', width: box, height: box, fill: 'none',
    stroke: 'currentColor', 'stroke-width': 1.6, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' },
    paths.map((d) => svg('path', { d })));
}

const fmt = {
  int(value) {
    const num = Number(value || 0);
    return num.toLocaleString('zh-CN', { maximumFractionDigits: 0 });
  },
  compact(value) {
    const num = Number(value || 0);
    if (!Number.isFinite(num)) return '—';
    const abs = Math.abs(num);
    if (abs >= 1e9) return (num / 1e9).toFixed(2) + 'B';
    if (abs >= 1e6) return (num / 1e6).toFixed(2) + 'M';
    if (abs >= 1e4) return (num / 1e3).toFixed(1) + 'k';
    if (abs >= 1e3) return (num / 1e3).toFixed(2) + 'k';
    return String(Math.round(num * 100) / 100);
  },
  pct(value, digits) {
    const num = Number(value || 0);
    return num.toFixed(digits === undefined ? 1 : digits) + '%';
  },
  ms(value) {
    const num = Number(value || 0);
    if (!num) return '—';
    if (num < 1000) return Math.round(num) + ' ms';
    return (num / 1000).toFixed(2) + ' s';
  },
  // 额度单位是 µ$（1e-6 美元）；小额费用需要更多小数位，大额配额两位就够
  money(units, currency) {
    const value = Number(units || 0) / 1e6;
    if (!value) return '0.00';
    const abs = Math.abs(value);
    if (abs >= 1) return value.toFixed(2);
    if (abs >= 0.01) return value.toFixed(4);
    if (abs >= 0.0001) return value.toFixed(6);
    return value.toExponential(2);
  },
  moneyLabel(currency) {
    return currency === 'CNY' ? '¥' : '$';
  },
  rel(iso) {
    if (!iso) return '—';
    const then = new Date(iso).getTime();
    if (!then) return '—';
    const diff = Math.max(0, Date.now() - then) / 1000;
    if (diff < 5) return '刚刚';
    if (diff < 60) return Math.floor(diff) + ' 秒前';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    if (diff < 86400 * 30) return Math.floor(diff / 86400) + ' 天前';
    return new Date(iso).toLocaleDateString('zh-CN');
  },
  dt(iso) {
    if (!iso) return '—';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '—';
    const pad = (n) => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  },
  clock(iso) {
    if (!iso) return '—';
    const date = new Date(iso);
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  },
  dur(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds || 0)));
    if (total < 60) return total + ' 秒';
    const minutes = Math.floor(total / 60);
    if (minutes < 60) return minutes + ' 分 ' + (total % 60) + ' 秒';
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + ' 时 ' + (minutes % 60) + ' 分';
    return Math.floor(hours / 24) + ' 天 ' + (hours % 24) + ' 时';
  },
  bytes(n) {
    const units = ['B', 'KB', 'MB', 'GB'];
    let value = Number(n || 0);
    let index = 0;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
    return value.toFixed(index === 0 ? 0 : 1) + ' ' + units[index];
  },
  speed(value) {
    const num = Number(value || 0);
    return num ? num.toFixed(1) + ' tok/s' : '—';
  },
  text(value, fallback) {
    const str = value === null || value === undefined ? '' : String(value);
    return str.trim() ? str : (fallback === undefined ? '—' : fallback);
  },
};

/* ======================================================================== */
/* 二、接口访问                                                              */
/* ======================================================================== */

const ADMIN = '/api/admin';

async function request(method, path, body) {
  const options = { method, credentials: 'same-origin', headers: {} };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    const wrapped = new Error('无法连接到网关服务，请确认进程仍在运行');
    wrapped.code = 'NETWORK';
    throw wrapped;
  }
  const text = await response.text();
  let data = null;
  if (text) { try { data = JSON.parse(text); } catch (_) { data = { message: text.slice(0, 400) }; } }
  if (!response.ok) {
    const message = (data && (data.message || (data.error && data.error.message))) || `HTTP ${response.status}`;
    const error = new Error(message);
    error.code = (data && data.code) || (data && data.error && data.error.code) || 'HTTP_' + response.status;
    error.status = response.status;
    error.details = data && data.details;
    if (response.status === 401) showGate(message);
    throw error;
  }
  return data;
}

const api = {
  get: (path) => request('GET', path),
  post: (path, body) => request('POST', path, body === undefined ? {} : body),
  put: (path, body) => request('PUT', path, body),
  del: (path) => request('DELETE', path),
};

/* ======================================================================== */
/* 三、提示与对话框                                                          */
/* ======================================================================== */

const toasts = [];
function toast(kind, title, text, ttl) {
  const host = $('#toasts');
  if (!host) return;
  const node = el('div', { class: 'toast toast--' + (kind || 'info') },
    icon(kind === 'err' ? 'alert' : kind === 'ok' ? 'check' : 'dot', 15),
    el('div', { class: 'toast__body' },
      el('div', { class: 'toast__title', text: title }),
      text ? el('div', { class: 'toast__text', text }) : null));
  host.appendChild(node);
  toasts.push(node);
  while (toasts.length > 4) { const old = toasts.shift(); old.remove(); }
  setTimeout(() => {
    node.style.transition = 'opacity .3s, transform .3s';
    node.style.opacity = '0';
    node.style.transform = 'translateY(8px)';
    setTimeout(() => node.remove(), 320);
  }, ttl || 4200);
}

function closeModal() { const root = $('#modal-root'); root.hidden = true; $('#modal-body').replaceChildren(); $('#modal-foot').replaceChildren(); }
function closeDrawer() { const root = $('#drawer-root'); root.hidden = true; $('#drawer-body').replaceChildren(); }

function openModal(title, body, foot) {
  $('#modal-title').textContent = title;
  const bodyHost = $('#modal-body');
  bodyHost.replaceChildren();
  if (body) bodyHost.appendChild(body);
  const footHost = $('#modal-foot');
  footHost.replaceChildren();
  (foot || []).forEach((node) => footHost.appendChild(node));
  $('#modal-root').hidden = false;
  const focusable = bodyHost.querySelector('input, select, textarea, button');
  if (focusable) setTimeout(() => focusable.focus(), 40);
}

function openDrawer(body) {
  const host = $('#drawer-body');
  host.replaceChildren();
  host.appendChild(body);
  $('#drawer-root').hidden = false;
}

function confirmDialog(title, text, confirmLabel, danger) {
  return new Promise((resolve) => {
    const ok = el('button', { class: 'btn ' + (danger ? 'btn--danger' : 'btn--primary'), text: confirmLabel || '确认' });
    const cancel = el('button', { class: 'btn', text: '取消' });
    ok.addEventListener('click', () => { closeModal(); resolve(true); });
    cancel.addEventListener('click', () => { closeModal(); resolve(false); });
    openModal(title, el('p', { class: 'panel__hint', text }), [cancel, ok]);
  });
}

/** 同步复制路径：execCommand 立即返回结果，不受权限弹窗与后台节流影响。
 *  已废弃但对本地控制台来说最可靠，所以优先用它。 */
function execCopy(text) {
  try {
    const area = el('textarea', { readonly: true, style: { position: 'fixed', top: '-1000px', left: '-1000px', opacity: '0' } });
    area.value = text;
    document.body.appendChild(area);
    area.focus();
    area.select();
    area.setSelectionRange(0, text.length);
    const ok = document.execCommand('copy');
    area.remove();
    return ok;
  } catch (_) {
    return false;
  }
}

async function copyText(text, label) {
  // 一、先走同步路径，点了立刻有反馈
  if (execCopy(text)) {
    toast('ok', '已复制', label || text);
    return true;
  }
  // 二、再试异步剪贴板 API；它可能被权限挡住、甚至在非用户手势下一直 pending，
  //     所以失败与超时都要落到「手动复制」面板，不能让按钮看着没反应。
  if (navigator.clipboard && window.isSecureContext) {
    let settled = false;
    const timer = setTimeout(() => {
      if (!settled) { showManualCopy(text, label); }
    }, 1500);
    try {
      await navigator.clipboard.writeText(text);
      settled = true;
      clearTimeout(timer);
      toast('ok', '已复制', label || text);
      return true;
    } catch (_) {
      settled = true;
      clearTimeout(timer);
    }
  }
  // 三、都不行：给一个能手动三连击选中的面板
  showManualCopy(text, label);
  return false;
}

/** 两条复制路径都不可用时，给一个能手动三连击选中的面板（而不是一闪而过的提示）。 */
function showManualCopy(text, label) {
  const box = el('input', { type: 'text', class: 'mono', readonly: true, value: text });
  const select = () => { box.focus(); box.select(); box.setSelectionRange(0, text.length); };
  const selectBtn = el('button', { class: 'btn btn--primary', text: '全选' });
  selectBtn.addEventListener('click', () => { select(); });
  const retry = el('button', { class: 'btn', text: '再试一次复制' });
  retry.addEventListener('click', () => {
    if (execCopy(text)) {
      closeModal();
      toast('ok', '已复制', label || text);
    } else {
      select();
      toast('warn', '这个环境不允许脚本写剪贴板', '已帮你全选，按 Ctrl+C 复制即可。');
    }
  });
  openModal('手动复制', el('div', { class: 'stack' },
    el('div', { class: 'notice notice--warn' }, icon('alert', 16),
      el('div', null,
        el('div', { text: '浏览器不允许脚本写入剪贴板' }),
        el('div', { class: 'panel__hint', text: '点「全选」后按 Ctrl+C（macOS 为 ⌘C）即可；也可以直接选中下面这段文本。' }))),
    box,
    el('div', { class: 'panel__hint', text: label || '' })),
    [selectBtn, retry, el('button', { class: 'btn', text: '关闭', onclick: () => closeModal() })]);
  setTimeout(select, 60);
}

function downloadFile(filename, content, type) {
  const blob = new Blob([content], { type: type || 'application/json;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = el('a', { href: url, download: filename });
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ---- 本地密钥的明文管理 ----
   本地密钥的明文是「加密存库、可反复取回」的（平台式密钥管理），所以创建之后
   随时能在控制台查看与复制多少次都行；每次取回都会在网关日志里留一条审计记录。 */
async function fetchKeySecret(key) {
  try {
    const data = await api.get(`${ADMIN}/keys/${key.key_id}/secret`);
    return data.plaintext || '';
  } catch (error) {
    toast('err', '取回明文失败', error.message, 8000);
    return '';
  }
}

async function copyKeySecret(key) {
  const plaintext = await fetchKeySecret(key);
  if (!plaintext) return false;
  copyText(plaintext, `已复制「${key.name}」的密钥`);
  return true;
}

async function revealKeySecret(key, host) {
  host.replaceChildren(el('p', { class: 'panel__hint', text: '正在取回明文…' }));
  const plaintext = await fetchKeySecret(key);
  if (!plaintext) {
    host.replaceChildren(el('div', { class: 'notice notice--warn' }, icon('alert', 16),
      el('div', null,
        el('div', { text: '这把密钥没有可取的明文' }),
        el('div', { class: 'panel__hint', text: '可能是「可再次查看」关闭时创建的，或数据目录的主密钥换过。点上方「重新生成密钥值」即可拿到新的。' }))));
    return;
  }
  host.replaceChildren(revealBox(
    plaintext,
    '明文加密存在本地库里，随时可以再点「显示明文」取回并复制。每次取回都会记一条审计日志。'));
}

/** 明文展示块：复制按钮可反复点击。 */
function revealBox(plaintext, hint) {
  return el('div', { class: 'reveal' },
    el('div', { class: 'micro', text: '明文密钥 · 可重复复制' }),
    el('div', { class: 'reveal__key', text: plaintext }),
    el('div', { class: 'row' },
      el('button', {
        class: 'btn btn--primary btn--tiny',
        onclick: () => copyText(plaintext, '已复制密钥'),
      }, icon('copy', 14), ' 复制'),
      el('span', { class: 'chip chip--ok', text: '可多次点击' })),
    el('div', { class: 'panel__hint', text: hint }));
}

/* ======================================================================== */
/* 四、SVG 图表（手写，无第三方依赖）                                          */
/* ======================================================================== */

function sparkline(values, options) {
  const opts = options || {};
  const width = opts.width || 220;
  const height = opts.height || 30;
  const stroke = opts.stroke || 'var(--cyan)';
  const series = (values && values.length ? values : [0, 0]).map((v) => Number(v) || 0);
  const max = Math.max.apply(null, series.concat([1]));
  const step = series.length > 1 ? width / (series.length - 1) : width;
  const points = series.map((v, i) => [i * step, height - (v / max) * (height - 4) - 2]);
  const line = points.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
  const area = line + ` L${width} ${height} L0 ${height} Z`;
  const gid = 'sg' + Math.random().toString(36).slice(2, 8);
  return svg('svg', { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: 'none', class: 'chart' },
    svg('defs', null,
      svg('linearGradient', { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 },
        svg('stop', { offset: '0%', 'stop-color': stroke, 'stop-opacity': '.38' }),
        svg('stop', { offset: '100%', 'stop-color': stroke, 'stop-opacity': '0' }))),
    svg('path', { d: area, fill: `url(#${gid})`, stroke: 'none' }),
    svg('path', { d: line, fill: 'none', stroke, 'stroke-width': 1.5, 'stroke-linejoin': 'round' }));
}

/** 面积/折线混合图：多条序列共用一套坐标轴。 */
function areaChart(config) {
  const width = 860;
  const height = config.height || 210;
  const pad = { top: 14, right: 16, bottom: 24, left: 46 };
  const series = config.series || [];
  const labels = config.labels || [];
  const count = Math.max(1, labels.length);
  const max = Math.max(1, ...series.flatMap((s) => s.values.map((v) => Number(v) || 0)));

  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const x = (i) => pad.left + (count === 1 ? plotW / 2 : (i / (count - 1)) * plotW);
  const y = (v) => pad.top + plotH - ((Number(v) || 0) / max) * plotH;

  const ticks = [0, 0.25, 0.5, 0.75, 1].map((ratio) => {
    const value = max * ratio;
    const yy = y(value);
    return svg('g', null,
      svg('line', { x1: pad.left, x2: width - pad.right, y1: yy, y2: yy }),
      svg('text', { x: pad.left - 8, y: yy + 3, 'text-anchor': 'end', class: 'chart__axis' }, fmt.compact(value)));
  });

  const labelStep = Math.max(1, Math.ceil(count / 8));
  const xLabels = labels.map((label, i) => (i % labelStep === 0
    ? svg('text', { x: x(i), y: height - 6, 'text-anchor': 'middle', class: 'chart__axis' }, label)
    : null));

  const groups = series.map((s, index) => {
    const points = s.values.map((v, i) => [x(i), y(v)]);
    const line = points.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
    const gid = 'ag' + index + Math.random().toString(36).slice(2, 7);
    const nodes = [];
    if (s.fill !== false) {
      nodes.push(svg('defs', null,
        svg('linearGradient', { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 },
          svg('stop', { offset: '0%', 'stop-color': s.color, 'stop-opacity': '.34' }),
          svg('stop', { offset: '100%', 'stop-color': s.color, 'stop-opacity': '0' }))));
      nodes.push(svg('path', { d: line + ` L${x(count - 1)} ${pad.top + plotH} L${x(0)} ${pad.top + plotH} Z`, fill: `url(#${gid})` }));
    }
    nodes.push(svg('path', { d: line, class: 'chart__line', stroke: s.color }));
    if (count <= 3) nodes.push(...points.map((p) => svg('circle', { cx: p[0], cy: p[1], r: 2.4, fill: s.color })));
    return svg('g', null, nodes);
  });

  return svg('svg', { viewBox: `0 0 ${width} ${height}`, class: 'chart', role: 'img', 'aria-label': config.label || '趋势图' },
    svg('g', { class: 'chart__grid' }, ticks),
    ...groups,
    svg('g', null, xLabels));
}

function barList(items, options) {
  const opts = options || {};
  const rows = items || [];
  const max = Math.max(1, ...rows.map((row) => Number(row.value) || 0));
  return el('div', { class: 'barlist' }, rows.map((row) => el('div', { class: 'barlist__row' },
    el('div', { class: 'barlist__name', title: row.name, text: row.name }),
    el('div', { class: 'barlist__track' },
      el('div', { class: 'barlist__fill', style: { width: Math.max(2, ((Number(row.value) || 0) / max) * 100) + '%', background: opts.color ? `linear-gradient(90deg, transparent, ${opts.color})` : undefined } })),
    el('div', { class: 'barlist__val', text: row.label || fmt.compact(row.value) }))));
}

function donut(okCount, errCount) {
  const total = okCount + errCount;
  const radius = 46;
  const circumference = 2 * Math.PI * radius;
  const okRatio = total ? okCount / total : 1;
  return svg('svg', { viewBox: '0 0 120 120', width: 118, height: 118, role: 'img', 'aria-label': '成功率' },
    svg('circle', { cx: 60, cy: 60, r: radius, fill: 'none', stroke: 'var(--panel-3)', 'stroke-width': 12 }),
    svg('circle', {
      cx: 60, cy: 60, r: radius, fill: 'none', stroke: 'var(--emerald)', 'stroke-width': 12,
      'stroke-linecap': 'round', 'stroke-dasharray': `${circumference * okRatio} ${circumference}`,
      transform: 'rotate(-90 60 60)',
    }),
    svg('text', { x: 60, y: 56, 'text-anchor': 'middle', fill: 'var(--ink)', 'font-size': 19, 'font-family': 'var(--font-mono)', 'font-weight': 600 },
      fmt.pct(okRatio * 100, 1)),
    svg('text', { x: 60, y: 73, 'text-anchor': 'middle', fill: 'var(--ink-faint)', 'font-size': 9.5, 'letter-spacing': '1.2' }, 'SUCCESS'));
}

/* ======================================================================== */
/* 五、全局状态                                                              */
/* ======================================================================== */

const state = {
  session: null,
  system: null,
  live: null,
  channels: [],
  keys: [],
  maps: [],
  balance: [],
  settings: null,
  health: null,
  stats: null,
  providers: [],
  spark: new Array(44).fill(0),
  lastTokens: null,
  view: 'dashboard',
  navToken: 0,
  statsWindow: 24,
  statsBucket: 'hour',
  ws: null,
  wsAttempts: 0,
  pollers: [],
  keyFilter: { search: '', status: '' },
  chanFilter: { search: '', status: '' },
  logFilter: { status: '', model: '' },
};

const VIEWS = [
  { id: 'dashboard', label: '仪表盘', icon: 'dashboard', render: viewDashboard },
  { id: 'live', label: '实时会话', icon: 'live', render: viewLive, live: true },
  { id: 'channels', label: '渠道', icon: 'channels', render: viewChannels },
  { id: 'services', label: '本地服务', icon: 'cpu', render: viewServices },
  { id: 'keys', label: '密钥', icon: 'keys', render: viewKeys },
  { id: 'models', label: '模型别名', icon: 'models', render: viewModels },
  { id: 'stats', label: '用量统计', icon: 'stats', render: viewStats },
  { id: 'balance', label: '余额', icon: 'balance', render: viewBalance },
  { id: 'settings', label: '设置', icon: 'settings', render: viewSettings },
];

/* 本地服务的状态词汇表（颜色 + 中文） */
const SERVICE_STATUS = {
  running: { label: '运行中', tone: 'ok' },
  starting: { label: '启动中', tone: 'live' },
  restarting: { label: '重启中', tone: 'warn' },
  degraded: { label: '探活失败', tone: 'warn' },
  stopped: { label: '已停止', tone: 'off' },
  failed: { label: '异常', tone: 'err' },
  disabled: { label: '未托管', tone: 'off' },
  unknown: { label: '待检查', tone: 'off' },
};

function serviceChip(state) {
  const meta = SERVICE_STATUS[state.status] || SERVICE_STATUS.unknown;
  return chip(meta.label, meta.tone);
}

function buildRail() {
  const rail = $('#rail');
  rail.replaceChildren();
  VIEWS.forEach((view) => {
    const button = el('button', { class: 'rail__item' + (state.view === view.id ? ' is-active' : ''), dataset: { view: view.id }, title: view.label, onclick: () => navigate(view.id) },
      icon(view.icon),
      el('span', { text: view.label }));
    if (view.live) button.appendChild(el('span', { class: 'rail__badge', dataset: { role: 'live-badge' }, hidden: true, text: '0' }));
    rail.appendChild(button);
  });
  rail.appendChild(el('div', { class: 'rail__spacer' }));
  rail.appendChild(el('div', { class: 'rail__foot' },
    el('div', { class: 'rail__ver', text: 'v' + ((state.system && state.system.version) || '—') }),
    el('div', { class: 'micro', text: (state.system && state.system.mode) || '' })));
}

function markActiveRail() {
  document.querySelectorAll('.rail__item').forEach((node) => {
    node.classList.toggle('is-active', node.dataset.view === state.view);
  });
}

function setAccent(color) {
  document.documentElement.style.setProperty('--accent', color || 'var(--cyan)');
}

async function navigate(id) {
  const view = VIEWS.find((item) => item.id === id) || VIEWS[0];
  state.view = id;
  state.navToken += 1;
  const token = state.navToken;
  stopPollers();
  markActiveRail();
  setAccent(view.id === 'live' ? 'var(--emerald)' : view.id === 'balance' ? 'var(--amber)' : 'var(--cyan)');
  if (location.hash !== '#/' + id) history.replaceState(null, '', '#/' + id);
  const host = $('#view');
  host.replaceChildren(el('div', { class: 'stack' },
    el('div', { class: 'panel anim' }, el('div', { class: 'panel__body' },
      el('div', { class: 'skeleton sk-line', style: { width: '32%' } }),
      el('div', { class: 'skeleton sk-line', style: { width: '64%' } }),
      el('div', { class: 'skeleton sk-line', style: { width: '48%' } })))));
  try {
    await view.render(host, token);
  } catch (error) {
    if (token !== state.navToken) return;
    host.replaceChildren(el('div', { class: 'panel panel--err' }, el('div', { class: 'panel__body' },
      el('div', { class: 'notice notice--err' }, icon('alert', 16), el('div', null,
        el('div', { text: '加载失败：' + error.message }),
        el('div', { class: 'panel__hint', text: '请检查网关进程与网络，或稍后重试。' }))),
      el('div', { class: 'row', style: { marginTop: '10px' } },
        el('button', { class: 'btn', text: '重试', onclick: () => navigate(id) })))));
  }
}

function startPoller(fn, intervalMs) {
  fn();
  state.pollers.push(setInterval(fn, intervalMs));
}
function stopPollers() {
  state.pollers.forEach((id) => clearInterval(id));
  state.pollers = [];
}

/* ======================================================================== */
/* 六、实时通道与信号条                                                      */
/* ======================================================================== */

function connectLive() {
  if (state.ws) { try { state.ws.close(); } catch (_) {} state.ws = null; }
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  let socket;
  try {
    socket = new WebSocket(`${scheme}://${location.host}${ADMIN}/live/ws`);
  } catch (_) {
    setTimeout(connectLive, 4000);
    return;
  }
  state.ws = socket;
  socket.addEventListener('open', () => { state.wsAttempts = 0; });
  socket.addEventListener('message', (event) => {
    let payload;
    try { payload = JSON.parse(event.data); } catch (_) { return; }
    if (payload.type === 'pong' || payload.type === 'tick') return;
    if (payload.type === 'live') applyLive(payload);
  });
  socket.addEventListener('close', () => {
    state.ws = null;
    state.wsAttempts += 1;
    const delay = Math.min(12000, 800 * Math.pow(1.6, Math.min(state.wsAttempts, 6)));
    setTimeout(connectLive, delay);
  });
  socket.addEventListener('error', () => { try { socket.close(); } catch (_) {} });
}

function applyLive(payload) {
  const previous = state.live;
  state.live = payload;
  const tokens = (payload.stats && payload.stats.tokens) || 0;
  if (state.lastTokens !== null) {
    state.spark.push(Math.max(0, tokens - state.lastTokens));
    while (state.spark.length > 44) state.spark.shift();
  }
  state.lastTokens = tokens;

  const badge = document.querySelector('[data-role="live-badge"]');
  const count = (payload.active || []).length;
  if (badge) {
    badge.textContent = String(count);
    badge.hidden = count === 0;
  }
  paintSignal();
  if (state.view === 'live') paintLiveView();
  if (state.view === 'dashboard') paintDashboardLive();
  if (!previous) return;
}

function paintSignal() {
  const spark = $('#signal-spark');
  if (spark) {
    const node = sparkline(state.spark, { width: 220, height: 40, stroke: 'var(--cyan)' });
    spark.replaceChildren(...Array.from(node.childNodes));
    spark.setAttribute('viewBox', '0 0 220 40');
  }
  const leds = $('#signal-leds');
  if (!leds) return;
  const stats = (state.live && state.live.stats) || { active: 0, started: 0, errors: 0 };
  const rate = state.stats && state.stats.overview ? state.stats.overview : null;
  // 窗口平均速度没加载时（例如停在实时会话页），退化为当前活跃会话的平均速度
  const activeSpeeds = ((state.live && state.live.active) || [])
    .map((session) => Number(session.speed_tok_s) || 0)
    .filter((value) => value > 0);
  const speedText = rate && rate.avg_speed_tok_s
    ? rate.avg_speed_tok_s.toFixed(1)
    : (activeSpeeds.length
      ? (activeSpeeds.reduce((sum, value) => sum + value, 0) / activeSpeeds.length).toFixed(1)
      : '—');
  const items = [
    { key: '活跃', value: String(stats.active || 0), tone: 'live' },
    { key: '请求', value: fmt.compact(stats.started || 0), tone: 'ok' },
    { key: '错误', value: String(stats.errors || 0), tone: (stats.errors || 0) > 0 ? 'err' : 'ok' },
    { key: '速度', value: speedText, tone: 'live' },
  ];
  leds.replaceChildren(...items.map((item) => el('div', { class: 'led led--' + item.tone },
    el('div', { class: 'led__val', text: item.value }),
    el('div', { class: 'led__key', text: item.key }))));
}

/* ======================================================================== */
/* 七、通用组件                                                              */
/* ======================================================================== */

function panel(title, body, options) {
  const opts = options || {};
  const head = el('div', { class: 'panel__head' },
    el('h3', { text: title }),
    opts.subtitle ? el('span', { class: 'panel__hint', text: opts.subtitle }) : null,
    el('div', { class: 'spacer' }),
    opts.actions || null);
  return el('div', { class: 'panel ' + (opts.class || '') + ' anim', style: { '--i': opts.index || 0 } },
    opts.headless ? null : head,
    el('div', { class: 'panel__body' + (opts.flush ? ' panel__body--flush' : '') }, body));
}

function kpiCard(config) {
  return el('div', { class: 'panel anim kpi', style: { '--i': config.index || 0, '--accent': config.accent || 'var(--cyan)' } },
    el('div', { class: 'kpi__key', text: config.key }),
    el('div', { class: 'kpi__val' }, config.value, config.unit ? el('small', { text: config.unit }) : null),
    el('div', { class: 'kpi__foot' }, config.foot || null),
    config.spark ? el('div', { class: 'kpi__spark' }, config.spark) : null);
}

function chip(text, kind) {
  return el('span', { class: 'chip ' + (kind ? 'chip--' + kind : ''), text });
}

function providerChip(provider) {
  const key = String(provider || '').toLowerCase();
  const known = ['openai', 'anthropic', 'gemini', 'deepseek'];
  const kind = known.includes(key) ? key : 'mono';
  const labels = { openai: 'OpenAI', anthropic: 'Claude', gemini: 'Gemini', deepseek: 'DeepSeek', 'openai-compatible': '兼容' };
  return chip(labels[key] || key || '未知', kind);
}

function meter(percent, label, options) {
  const opts = options || {};
  const value = Math.max(0, Number(percent) || 0);
  const tone = value >= 100 ? 'err' : value >= (opts.warnAt || 80) ? 'warn' : '';
  return el('div', { class: 'meter' + (tone === 'warn' ? ' meter--hazard' : '') },
    el('div', { class: 'meter__head' }, el('span', { text: opts.left || '' }), el('span', { text: label || '' })),
    el('div', { class: 'meter__track' }, el('div', { class: 'meter__fill ' + (tone ? 'meter__fill--' + tone : ''), style: { width: Math.min(100, value) + '%' } })));
}

/** 一个 checkbox + 文字的开关行（用于渠道表单里的托管选项）。 */
function lifeToggle(toggleNode, text) {
  return el('label', { class: 'toggle' },
    toggleNode.querySelector('input'), el('span', { class: 'toggle__track' }), el('span', { text }));
}

function field(label, control, hint) {
  // 原生控件用 <label> 换取点击聚焦；自定义控件（标签输入、开关）用 <div> 避免嵌套
  const native = control && ['INPUT', 'SELECT', 'TEXTAREA'].includes(control.tagName);
  const node = el(native ? 'label' : 'div', { class: 'field' },
    el('span', { class: 'field__label', text: label }),
    control,
    hint ? el('span', { class: 'field__hint', text: hint }) : null);
  return node;
}

function textInput(value, attrs) {
  return el('input', Object.assign({ type: 'text', value: value === null || value === undefined ? '' : String(value) }, attrs || {}));
}
function numberInput(value, attrs) {
  return el('input', Object.assign({ type: 'number', value: value === null || value === undefined ? '' : String(value) }, attrs || {}));
}
function selectInput(options, value, attrs) {
  const node = el('select', attrs || {});
  options.forEach((option) => {
    const item = el('option', { value: option.value, text: option.label });
    if (String(option.value) === String(value)) item.selected = true;
    node.appendChild(item);
  });
  return node;
}
function toggleInput(checked) {
  const input = el('input', { type: 'checkbox' });
  input.checked = !!checked;
  return el('label', { class: 'toggle' }, input, el('span', { class: 'toggle__track' }), el('span', { text: '启用' }));
}
function tagInput(values) {
  const tags = Array.isArray(values) ? values.slice() : [];
  const wrapper = el('div', { class: 'taginput' });
  const input = el('input', { type: 'text', placeholder: '输入后回车，如 claude-* 或 fast', spellcheck: 'false' });
  const render = () => {
    wrapper.replaceChildren(...tags.map((tag, index) => el('span', { class: 'chip chip--mono' }, tag,
      el('button', { class: 'iconbtn', style: { width: '16px', height: '16px', fontSize: '10px' }, text: '✕', type: 'button', onclick: () => { tags.splice(index, 1); render(); } }))), input);
    input.focus();
  };
  const commit = () => {
    const value = input.value.trim();
    if (value && !tags.includes(value)) tags.push(value);
    input.value = '';
    render();
  };
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ',') { event.preventDefault(); commit(); }
    else if (event.key === 'Backspace' && !input.value && tags.length) { tags.pop(); render(); }
  });
  input.addEventListener('blur', () => { if (input.value.trim()) commit(); });
  render();
  wrapper.getValues = () => tags.slice();
  return wrapper;
}

function emptyState(title, hint, action) {
  return el('div', { class: 'empty' },
    el('div', { class: 'empty__mark', text: '· · ·' }),
    el('h4', { text: title }),
    el('p', { text: hint }),
    action || null);
}

function dataTable(columns, rows, options) {
  const opts = options || {};
  const table = el('table', { class: 'tbl' },
    el('thead', null, el('tr', null, columns.map((column) => el('th', { class: column.align === 'right' ? 'num' : '', text: column.title })))),
    el('tbody', null, rows));
  return el('div', { class: 'tablewrap' + (opts.wrapClass ? ' ' + opts.wrapClass : '') }, table);
}

function sessionCard(session) {
  const stalled = session.status === 'stalled';
  const failed = session.status === 'error' || session.status === 'abandoned';
  const done = session.recent;
  const klass = 'session' + (stalled ? ' session--stalled' : failed ? ' session--error' : done ? ' session--done' : '');
  return el('div', { class: klass },
    el('div', { class: 'session__top' },
      el('span', { class: 'session__model', text: session.model || '—' }),
      session.upstream_model && session.upstream_model !== session.model
        ? chip('→ ' + session.upstream_model, 'mono') : null,
      providerChip(session.provider_type),
      session.channel_name ? chip(session.channel_name) : null,
      el('span', { class: 'session__id', text: session.request_id })),
    el('div', { class: 'session__facts' },
      el('span', { class: 'session__fact' }, '密钥 ', el('b', { text: session.key_name || session.key_prefix || '—' })),
      el('span', { class: 'session__fact' }, '已收 ', el('b', { text: fmt.int(session.total_tokens) }), ' tok'),
      el('span', { class: 'session__fact' }, '速度 ', el('b', { text: session.speed_tok_s ? session.speed_tok_s.toFixed(1) : '—' })),
      el('span', { class: 'session__fact' }, '首字 ', el('b', { text: fmt.ms(session.first_token_ms) })),
      el('span', { class: 'session__fact' }, done ? '耗时 ' : '已用 ', el('b', { text: session.elapsed_text || fmt.ms(session.elapsed_ms) })),
      el('span', { class: 'session__fact' }, '重试 ', el('b', { text: String(session.attempts || 1) })),
      stalled ? chip('静默 ' + Math.round(session.idle_seconds || 0) + 's', 'warn') : null,
      failed ? chip(session.error_code || '失败', 'err') : null,
      session.finish_reason ? chip(session.finish_reason, 'mono') : null),
    done || failed ? null : el('div', { class: 'session__activity' }, el('i')));
}

/* ======================================================================== */
/* 八、视图：仪表盘                                                          */
/* ======================================================================== */

async function viewDashboard(host, token) {
  const [stats, health] = await Promise.all([
    api.get(`${ADMIN}/stats?hours=${state.statsWindow}&bucket=${state.statsBucket}`),
    api.get(`${ADMIN}/health`).catch(() => null),
  ]);
  if (token !== state.navToken) return;
  state.stats = stats;
  state.health = health;
  state.providers = (state.system && state.system.providers) || state.providers;

  const overview = stats.overview || {};
  const series = stats.series || [];
  const labels = series.map((point) => state.statsBucket === 'hour'
    ? point.bucket.slice(11, 16)
    : point.bucket.slice(5, 10));

  const container = el('div', { class: 'stack' });

  container.appendChild(el('div', { class: 'view__head' },
    el('div', { class: 'view__title' },
      el('h1', { text: '仪表盘' }),
      el('div', { class: 'micro', text: `近 ${state.statsWindow} 小时 · 数据来自本机 SQLite 明细与预聚合桶` })),
    el('div', { class: 'view__actions' },
      windowSegmented((hours) => { state.statsWindow = hours; navigate('dashboard'); }),
      el('button', { class: 'btn', onclick: () => navigate('dashboard') }, icon('refresh', 15), ' 刷新'))));

  const kpis = el('div', { class: 'grid grid--kpi' },
    kpiCard({ key: '请求数', value: fmt.int(overview.requests), foot: el('span', { text: `${overview.streamed || 0} 次流式` }), index: 0 }),
    kpiCard({ key: '成功率', value: fmt.pct(100 - (overview.error_rate || 0)), unit: '', foot: el('span', { text: `失败 ${overview.errors || 0} 次` }), accent: 'var(--emerald)', index: 1 }),
    kpiCard({ key: 'Token 总量', value: fmt.compact(overview.total_tokens), foot: el('span', { text: `输入 ${fmt.compact(overview.prompt_tokens)} / 输出 ${fmt.compact(overview.completion_tokens)}` }), index: 2 }),
    kpiCard({ key: '平均首字延迟', value: fmt.ms(overview.avg_first_token_ms), foot: el('span', { text: `平均总耗时 ${fmt.ms(overview.avg_latency_ms)}` }), accent: 'var(--violet)', index: 3 }),
    kpiCard({ key: '平均输出速度', value: overview.avg_speed_tok_s ? overview.avg_speed_tok_s.toFixed(1) : '—', unit: 'tok/s', foot: el('span', { text: '按流式请求统计' }), accent: 'var(--amber)', index: 4 }),
    kpiCard({ key: '预估费用', value: fmt.moneyLabel(stats.pricing && stats.pricing.currency) + fmt.money(overview.cost_units), foot: el('span', { text: `${fmt.int(overview.cost_units)} µ$ · 按已配置单价` }), accent: 'var(--rose)', index: 5 }));
  container.appendChild(kpis);

  const throughput = panel('吞吐趋势', el('div', { class: 'stack' },
    el('div', { class: 'legend' },
      el('span', null, el('i', { style: { background: 'var(--cyan)' } }), '输入 tokens'),
      el('span', null, el('i', { style: { background: 'var(--emerald)' } }), '输出 tokens')),
    series.length
      ? areaChart({
        labels: labels.length ? labels : [''],
        series: [
          { color: 'var(--cyan)', values: series.map((p) => p.prompt_tokens) },
          { color: 'var(--emerald)', values: series.map((p) => p.completion_tokens) },
        ],
      })
      : emptyState('这个时间窗口还没有请求', '在「密钥」页创建一个本地密钥，然后用任意 OpenAI 客户端发一次请求试试。',
        el('button', { class: 'btn btn--primary', onclick: () => navigate('keys') }, '去创建密钥'))),
    { actions: el('span', { class: 'panel__hint', text: state.statsBucket === 'hour' ? '按小时聚合' : '按天聚合' }) });
  container.appendChild(throughput);

  const middle = el('div', { class: 'grid grid--3' });
  middle.appendChild(panel('模型分布', (stats.by_model || []).length
    ? barList(stats.by_model.map((row) => ({ name: row.model, value: row.requests, label: fmt.int(row.requests) + ' 次 · ' + fmt.compact(row.tokens) + ' tok' })))
    : el('p', { class: 'panel__hint', text: '暂无数据' }), { index: 1 }));

  middle.appendChild(panel('渠道分布', (stats.by_channel || []).length
    ? el('div', { class: 'stack' }, barList(stats.by_channel.map((row) => ({ name: row.channel_name || '（已删除）', value: row.requests, label: fmt.int(row.requests) + ' 次' }))),
      el('div', { class: 'legend' }, stats.by_channel.slice(0, 4).map((row) => el('span', null,
        el('i', { style: { background: 'var(--line-2)' } }),
        `${row.channel_name || '—'} · ${fmt.speed(row.avg_speed_tok_s)}`))))
    : el('p', { class: 'panel__hint', text: '暂无数据' }), { index: 2 }));

  middle.appendChild(panel('成功 / 失败', el('div', { class: 'donut' },
    donut((overview.requests || 0) - (overview.errors || 0), overview.errors || 0),
    el('div', { class: 'donut__meta' },
      el('div', null, el('div', { class: 'kpi__key', text: '成功' }), el('div', { class: 'mono', style: { fontSize: '16px' }, text: fmt.int((overview.requests || 0) - (overview.errors || 0)) })),
      el('div', null, el('div', { class: 'kpi__key', text: '失败' }), el('div', { class: 'mono', style: { fontSize: '16px', color: 'var(--rose)' }, text: fmt.int(overview.errors || 0) })),
      el('div', { class: 'panel__hint', text: `流式 ${overview.streamed || 0} 次` }))), { index: 3 }));
  container.appendChild(middle);

  const livePanel = el('div', { class: 'panel panel--live anim', 'data-role': 'dash-live', style: { '--i': 4 } });
  container.appendChild(livePanel);
  paintDashboardLive(livePanel);

  const opsPanel = panel('运行时', el('div', { class: 'grid grid--2' },
    el('dl', { class: 'kv' },
      el('dt', { text: '监听地址' }), el('dd', { text: `${(state.system && state.system.host) || ''}:${(state.system && state.system.port) || ''}` }),
      el('dt', { text: '数据目录' }), el('dd', { text: (state.system && state.system.data_dir) || '—' }),
      el('dt', { text: 'SQLite' }), el('dd', { text: (state.system && state.system.sqlite_version) || '—' }),
      el('dt', { text: '已运行' }), el('dd', { text: fmt.dur(state.system && state.system.uptime_seconds) })),
    el('div', { class: 'stack' },
      el('div', { class: 'row' },
        el('button', { class: 'btn', onclick: () => copyText((state.system && state.system.openai_base_url) || '', 'OpenAI 兼容基地址') }, icon('copy', 15), ' 复制基地址'),
        el('button', { class: 'btn', onclick: () => window.open('/docs', '_blank') }, '接口文档')),
      el('div', { class: 'panel__hint', text: '把这个基地址填进客户端（Cursor / OpenWebUI / 脚本），密钥用「密钥」页生成的本地密钥。' }))),
    { index: 4 });
  container.appendChild(opsPanel);

  host.replaceChildren(container);
  startPoller(async () => {
    const fresh = await api.get(`${ADMIN}/stats?hours=${state.statsWindow}&bucket=${state.statsBucket}`);
    state.stats = fresh;
    paintSignal();
  }, 15000);
}

function paintDashboardLive(existing) {
  const target = existing || $('[data-role="dash-live"]');
  if (!target) return;
  const live = state.live || { active: [], recent: [], stats: {} };
  const active = (live.active || []).slice(0, 4);
  const recent = (live.recent || []).slice(0, 6);
  target.replaceChildren(el('div', { class: 'panel__head' },
    el('h3', { text: '实时会话' }),
    el('span', { class: 'panel__hint', text: `${(live.active || []).length} 个进行中 · 峰值 ${(live.stats && live.stats.peak_active) || 0}` }),
    el('div', { class: 'spacer' }),
    el('button', { class: 'btn btn--tiny', onclick: () => navigate('live') }, '全部')),
    el('div', { class: 'panel__body' },
      active.length
        ? el('div', { class: 'sessions' }, active.map(sessionCard))
        : el('div', { class: 'row' }, el('span', { class: 'chip chip--off' }, el('span', { class: 'dot', style: { color: 'var(--ink-ghost)' } }), '当前没有正在处理的请求')),
      recent.length
        ? el('div', { style: { marginTop: '14px' } },
          el('div', { class: 'micro', style: { marginBottom: '8px' }, text: '最近完成' }),
          dataTable([
            { title: '时间' }, { title: '模型' }, { title: '密钥' }, { title: '渠道' },
            { title: 'Tokens', align: 'right' }, { title: '速度', align: 'right' }, { title: '用时', align: 'right' }, { title: '状态' },
          ], recent.map((item) => el('tr', null,
            el('td', { class: 'mono', text: fmt.clock(item.started_at) }),
            el('td', { class: 'mono', text: item.model }),
            el('td', { text: item.key_name || item.key_prefix || '—' }),
            el('td', { text: item.channel_name || '—' }),
            el('td', { class: 'num mono', text: fmt.int(item.total_tokens) }),
            el('td', { class: 'num mono', text: item.speed_tok_s ? item.speed_tok_s.toFixed(1) : '—' }),
            el('td', { class: 'num mono', text: fmt.ms(item.elapsed_ms) }),
            el('td', null, item.status === 'ok' ? chip('成功', 'ok') : chip(item.error_code || item.status, 'err'))))))
        : null));
}

/* ======================================================================== */
/* 九、视图：实时会话                                                        */
/* ======================================================================== */

async function viewLive(host, token) {
  const snapshot = await api.get(`${ADMIN}/live`);
  if (token !== state.navToken) return;
  state.live = snapshot;

  const container = el('div', { class: 'stack' });
  container.appendChild(el('div', { class: 'view__head' },
    el('div', { class: 'view__title' },
      el('h1', { text: '实时会话' }),
      el('div', { class: 'micro', text: '活跃态只在内存中，历史统计走用量明细；通过 WebSocket 增量推送' })),
    el('div', { class: 'view__actions' },
      el('span', { class: 'chip', id: 'ws-state', text: state.ws ? '推送已连接' : '推送重连中' }),
      el('button', { class: 'btn', onclick: () => navigate('live') }, icon('refresh', 15), ' 刷新'))));

  container.appendChild(el('div', { class: 'grid grid--kpi', 'data-role': 'live-kpis' }));
  container.appendChild(el('div', { class: 'panel panel--live anim', style: { '--i': 1 } },
    el('div', { class: 'panel__head' }, el('h3', { text: '进行中' }), el('div', { class: 'spacer' }),
      el('span', { class: 'panel__hint', text: '按开始时间倒序' })),
    el('div', { class: 'panel__body' }, el('div', { class: 'sessions', 'data-role': 'live-active' }))));
  container.appendChild(panel('最近完成', el('div', { class: 'sessions', 'data-role': 'live-recent' }), { index: 2 }));
  host.replaceChildren(container);
  paintLiveView();
}

function paintLiveView() {
  const live = state.live;
  if (!live) return;
  const stats = live.stats || {};
  const kpis = $('[data-role="live-kpis"]');
  if (kpis) {
    kpis.replaceChildren(
      kpiCard({ key: '进行中', value: String((live.active || []).length), foot: el('span', { text: `峰值 ${stats.peak_active || 0}` }), accent: 'var(--emerald)', index: 0 }),
      kpiCard({ key: '累计请求', value: fmt.compact(stats.started || 0), foot: el('span', { text: `完成 ${stats.finished || 0}` }), index: 1 }),
      kpiCard({ key: '累计错误', value: String(stats.errors || 0), accent: 'var(--rose)', foot: el('span', { text: '含上游失败与超时' }), index: 2 }),
      kpiCard({ key: '累计 Tokens', value: fmt.compact(stats.tokens || 0), foot: el('span', { text: '本进程生命周期' }), accent: 'var(--violet)', index: 3 }));
  }
  const activeHost = $('[data-role="live-active"]');
  if (activeHost) {
    const active = live.active || [];
    activeHost.replaceChildren(...(active.length
      ? active.map(sessionCard)
      : [emptyState('当前没有进行中的请求', '向 /v1/chat/completions 发一个流式请求，这里会实时出现。')]));
  }
  const recentHost = $('[data-role="live-recent"]');
  if (recentHost) {
    const recent = live.recent || [];
    recentHost.replaceChildren(...(recent.length
      ? recent.slice(0, 20).map(sessionCard)
      : [el('p', { class: 'panel__hint', text: '本进程还没有完成的请求。' })]));
  }
  const wsState = $('#ws-state');
  if (wsState) wsState.textContent = state.ws ? '推送已连接' : '推送重连中';
}

/* ======================================================================== */
/* 十、视图：渠道                                                            */
/* ======================================================================== */

async function viewChannels(host, token) {
  const query = new URLSearchParams();
  if (state.chanFilter.search) query.set('search', state.chanFilter.search);
  if (state.chanFilter.status) query.set('status', state.chanFilter.status);
  const data = await api.get(`${ADMIN}/channels?${query.toString()}`);
  if (token !== state.navToken) return;
  state.channels = data.items || [];
  state.providers = data.providers || state.providers;
  const [balance, services] = await Promise.all([
    api.get(`${ADMIN}/balance`).catch(() => ({ items: [] })),
    api.get(`${ADMIN}/services`).catch(() => ({ items: [] })),
  ]);
  if (token !== state.navToken) return;
  state.balance = balance.items || [];
  state.services = services;

  const container = el('div', { class: 'stack' });
  container.appendChild(el('div', { class: 'view__head' },
    el('div', { class: 'view__title' },
      el('h1', { text: '上游渠道' }),
      el('div', { class: 'micro', text: `共 ${data.total} 条 · 上游密钥加密落库，界面只显示掩码` })),
    el('div', { class: 'view__actions' },
      el('button', { class: 'btn btn--primary', onclick: () => channelForm(null) }, icon('plus', 15), ' 新建渠道'))));

  const search = el('input', { type: 'search', placeholder: '搜索名称 / 协议 / 地址', value: state.chanFilter.search });
  search.addEventListener('keydown', (event) => { if (event.key === 'Enter') { state.chanFilter.search = search.value.trim(); navigate('channels'); } });
  search.addEventListener('search', () => { state.chanFilter.search = search.value.trim(); navigate('channels'); });

  container.appendChild(el('div', { class: 'toolbar' },
    icon('search', 15), search,
    selectInput([{ value: '', label: '全部状态' }, { value: 'active', label: '启用中' }, { value: 'disabled', label: '已禁用' }], state.chanFilter.status, {
      style: { width: '128px' }, onchange: (event) => { state.chanFilter.status = event.target.value; navigate('channels'); },
    }),
    el('div', { class: 'spacer' }),
    el('button', { class: 'btn btn--tiny', onclick: () => { state.chanFilter = { search: '', status: '' }; navigate('channels'); } }, '重置')));

  const balanceMap = {};
  state.balance.forEach((item) => { balanceMap[item.channel_id] = item; });

  if (!state.channels.length) {
    container.appendChild(panel('还没有渠道', emptyState('先把上游接进来',
      '新建一条渠道，填入厂商给的 base_url 与 API Key。支持 OpenAI 兼容、Anthropic、Gemini 三类协议，同一个厂商可以配多条做负载均衡。',
      el('button', { class: 'btn btn--primary', onclick: () => channelForm(null) }, '新建渠道')), { index: 1 }));
  } else {
    container.appendChild(panel('渠道列表', dataTable([
      { title: '渠道' }, { title: '协议' }, { title: '模型白名单' }, { title: '调度' },
      { title: '健康' }, { title: '余额' }, { title: '状态' }, { title: '操作' },
    ], state.channels.map((channel) => channelRow(channel, balanceMap[channel.channel_id]))), { flush: true, index: 1 }));
  }
  host.replaceChildren(container);
}

/** 从本地服务快照里取运行时状态，用于渠道列表上的小标识。 */
function serviceRuntimeChip(channelId) {
  const items = (state.services && state.services.items) || [];
  const item = items.find((entry) => entry.channel_id === channelId);
  if (!item) return chip('状态待查', 'off');
  return serviceChip(item);
}

function channelRow(channel, balance) {
  const health = channel.health || {};
  const models = channel.models || [];
  return el('tr', null,
    el('td', null, el('div', { class: 'tbl__name' },
      el('strong', { text: channel.name || channel.channel_id }),
      el('span', { class: 'tbl__sub cell-ellip', text: (channel.base_url || '（未填 base_url）') }),
      channel.managed
        ? el('div', { class: 'row', style: { marginTop: '3px' } },
          serviceRuntimeChip(channel.channel_id),
          el('span', {
            class: 'chip chip--mono',
            title: channel.lifecycle.command,
          }, '本地托管'))
        : null)),
    el('td', null, providerChip(channel.provider_type)),
    el('td', null, models.length
      ? el('div', { class: 'row' }, models.slice(0, 3).map((model) => chip(model, 'mono')),
        models.length > 3 ? chip('+' + (models.length - 3), 'off') : null)
      : el('span', { class: 'panel__hint', text: '不限' })),
    el('td', null, el('span', { class: 'mono', text: `P${channel.priority} · W${channel.weight}` })),
    el('td', null, health.cooling
      ? chip('冷却 ' + Math.round(health.cooldown_remaining || 0) + 's', 'warn')
      : (channel.last_error
        ? chip('失败 ×' + (channel.consecutive_failures || 1), 'err')
        : chip('正常', 'ok'))),
    el('td', null, balance
      ? (balance.supported
        ? (balance.fetched_at
          ? el('span', { class: 'mono', text: `${balance.currency || ''} ${Number(balance.total || 0).toFixed(2)}` })
          : el('span', { class: 'panel__hint', text: '待查询' }))
        : el('span', { class: 'panel__hint', text: '无接口' }))
      : el('span', { class: 'panel__hint', text: '—' })),
    el('td', null, channel.status === 'active' ? chip('启用', 'ok') : chip('禁用', 'off')),
    el('td', { class: 'actions' },
      el('button', { class: 'btn btn--tiny', onclick: () => probeChannel(channel) }, '探针'),
      el('button', { class: 'btn btn--tiny', onclick: () => channelForm(channel) }, '编辑'),
      health.cooling ? el('button', { class: 'btn btn--tiny', onclick: () => resetCooldown(channel) }, '解除冷却') : null,
      el('button', { class: 'btn btn--tiny', onclick: () => toggleChannel(channel) }, channel.status === 'active' ? '禁用' : '启用'),
      el('button', { class: 'btn btn--tiny btn--danger', onclick: () => removeChannel(channel) }, '删除')));
}

function channelForm(channel) {
  const editing = !!channel;
  const providerOptions = (state.providers.length ? state.providers : [
    { type: 'openai', label: 'OpenAI 兼容' }, { type: 'deepseek', label: 'DeepSeek' },
    { type: 'anthropic', label: 'Anthropic Claude' }, { type: 'gemini', label: 'Google Gemini' },
  ]).map((item) => ({ value: item.type, label: item.label + '（' + item.protocol + '）' }));

  const inputs = {
    name: textInput(channel ? channel.name : '', { placeholder: '例如：DeepSeek 主力' }),
    provider_type: selectInput(providerOptions, channel ? channel.provider_type : 'openai'),
    base_url: textInput(channel ? channel.base_url : '', { placeholder: 'https://api.deepseek.com/v1', class: 'mono' }),
    api_key: el('input', { type: 'password', placeholder: editing ? '留空表示不修改' : 'sk-...', class: 'mono' }),
    priority: numberInput(channel ? channel.priority : 0, { min: 0, step: 1 }),
    weight: numberInput(channel ? channel.weight : 1, { min: 0, step: 1 }),
    models: tagInput(channel ? channel.models : []),
    balance_url: textInput(channel ? channel.balance_url : '', { placeholder: '留空则用厂商内置适配器', class: 'mono' }),
    balance_json_path: textInput(channel ? channel.balance_json_path : '', { placeholder: '如 data.balance', class: 'mono' }),
    balance_currency: textInput(channel ? channel.balance_currency : '', { placeholder: '如 CNY', class: 'mono' }),
    timeout_seconds: numberInput(channel && channel.timeout_seconds ? channel.timeout_seconds : '', { min: 1, step: 1, placeholder: '可选' }),
    extra_headers: el('textarea', { class: 'mono', placeholder: '{"x-extra": "1"}', value: channel && Object.keys(channel.extra_headers || {}).length ? JSON.stringify(channel.extra_headers, null, 2) : '' }),
    extra_body: el('textarea', { class: 'mono', placeholder: '{"top_k": 40}', value: channel && Object.keys(channel.extra_body || {}).length ? JSON.stringify(channel.extra_body, null, 2) : '' }),
    note: textInput(channel ? channel.note : ''),
    status: selectInput([{ value: 'active', label: '启用' }, { value: 'disabled', label: '禁用' }], channel ? channel.status : 'active'),
  };

  // ---- 本地进程托管（可选）----
  const life = Object.assign({
    enabled: false, command: '', args: [], workdir: '', env: {}, stop_command: '',
    stop_strategy: 'auto', health_path: '/v1/models', startup_grace_seconds: 40,
    auto_start: true, auto_restart: true, stop_on_shutdown: false,
    check_interval_seconds: 15, failure_threshold: 3, max_restarts: 0, restart_backoff_seconds: 30,
  }, (channel && channel.lifecycle) || {});
  const lifeInputs = {
    enabled: toggleInput(!!life.enabled),
    command: textInput(life.command, { placeholder: 'D:\my-llm\start.bat', class: 'mono' }),
    args: textInput(Array.isArray(life.args) ? life.args.join(' ') : life.args, { placeholder: '可选，空格分隔', class: 'mono' }),
    workdir: textInput(life.workdir, { placeholder: '默认取脚本所在目录', class: 'mono' }),
    stop_command: textInput(life.stop_command, { placeholder: '可选，如 D:\my-llm\stop.bat', class: 'mono' }),
    stop_strategy: selectInput([
      { value: 'auto', label: '自动（先杀自己拉起的进程树，再按端口）' },
      { value: 'process', label: '只杀自己拉起的进程树' },
      { value: 'port', label: '按端口终止监听进程' },
      { value: 'command', label: '执行停止命令' },
    ], life.stop_strategy),
    health_path: textInput(life.health_path, { placeholder: '/v1/models', class: 'mono' }),
    startup_grace_seconds: numberInput(life.startup_grace_seconds, { min: 0, max: 3600, step: 5 }),
    check_interval_seconds: numberInput(life.check_interval_seconds, { min: 5, max: 3600, step: 5 }),
    failure_threshold: numberInput(life.failure_threshold, { min: 1, max: 60, step: 1 }),
    max_restarts: numberInput(life.max_restarts, { min: 0, max: 1000, step: 1 }),
    restart_backoff_seconds: numberInput(life.restart_backoff_seconds, { min: 5, max: 3600, step: 5 }),
    auto_start: toggleInput(!!life.auto_start),
    auto_restart: toggleInput(!!life.auto_restart),
    stop_on_shutdown: toggleInput(!!life.stop_on_shutdown),
  };
  const lifecycleSection = el('div', { class: 'panel', style: { '--accent': 'var(--amber)' } },
    el('div', { class: 'panel__head' }, el('h3', { text: '本地进程托管（可选）' }),
      el('div', { class: 'spacer' }),
      el('span', { class: 'panel__hint', text: '本机推理服务，如 start.bat 拉起的 llama.cpp' })),
    el('div', { class: 'panel__body stack' },
      el('div', { class: 'notice notice--warn' }, icon('alert', 15),
        el('div', null,
          el('div', { text: '这里的命令会以当前用户身份在本机执行' }),
          el('div', { class: 'panel__hint', text: '只填你自己信任的脚本。网关只执行你配置的命令，停止时只终止自己拉起的进程树或占用该渠道端口的进程。' }))),
      el('div', { class: 'formgrid' },
        el('div', { class: 'span-2' }, field('启动命令', lifeInputs.command, '可以是 .bat / .cmd / .sh / .exe，或任意命令行')),
        field('启动参数', lifeInputs.args),
        field('工作目录', lifeInputs.workdir),
        field('停止命令', lifeInputs.stop_command, '留空则按下面的停止策略处理'),
        field('停止策略', lifeInputs.stop_strategy),
        field('健康检查路径', lifeInputs.health_path, '对该路径发一次 GET，任何 HTTP 响应都算「服务在跑」')),
      el('div', { class: 'formgrid' },
        field('启动宽限(秒)', lifeInputs.startup_grace_seconds, '拉起后等这么久再判定，避免模型还在加载就被重启'),
        field('探活间隔(秒)', lifeInputs.check_interval_seconds),
        field('失败几次才重启', lifeInputs.failure_threshold, '避免一次抖动就重启'),
        field('重启上限', lifeInputs.max_restarts, '0 = 不限；达到上限后停止自动重启并标记异常'),
        field('重启退避(秒)', lifeInputs.restart_backoff_seconds, '按重启次数线性放大，最多 10 倍')),
      el('div', { class: 'row' },
        lifeToggle(lifeInputs.enabled, '启用托管'),
        lifeToggle(lifeInputs.auto_start, '网关启动时自动拉起'),
        lifeToggle(lifeInputs.auto_restart, '掉线自动重启'),
        lifeToggle(lifeInputs.stop_on_shutdown, '网关退出时一并关闭'))));

  inputs.provider_type.addEventListener('change', () => {
    const preset = (state.providers.find((item) => item.type === inputs.provider_type.value) || {}).default_base_url || '';
    if (preset && !inputs.base_url.value.trim()) inputs.base_url.value = preset;
  });

  const form = el('div', { class: 'stack' },
    el('div', { class: 'formgrid' },
      field('名称', inputs.name),
      field('协议', inputs.provider_type, '决定用哪套适配器翻译请求'),
      el('div', { class: 'span-2' }, field('上游 base_url', inputs.base_url, '填厂商给的端点根地址；OpenAI 兼容类必填')),
      el('div', { class: 'span-2' }, field(editing ? '上游 API Key（留空不修改）' : '上游 API Key', inputs.api_key)),
      el('div', { class: 'span-2' }, field('模型白名单', inputs.models, '留空表示该渠道服务所有模型；支持通配，如 claude-*、deepseek-*')),
      field('优先级', inputs.priority, '数字越小越优先，0 最高'),
      field('权重', inputs.weight, '同优先级内按权重加权随机'),
      field('状态', inputs.status),
      field('单渠道超时(秒)', inputs.timeout_seconds, '留空用全局设置')),
    el('div', { class: 'panel' }, el('div', { class: 'panel__head' }, el('h3', { text: '余额查询（可选）' })),
      el('div', { class: 'panel__body' }, el('div', { class: 'formgrid' },
        field('余额接口 URL', inputs.balance_url, 'DeepSeek 可留空；其它厂商填自定义地址'),
        field('取值路径', inputs.balance_json_path, '形如 data.balance'),
        field('币种覆盖', inputs.balance_currency, '留空按接口返回')))),
    el('div', { class: 'formgrid' },
      field('附加请求头（JSON）', inputs.extra_headers),
      field('附加请求体（JSON）', inputs.extra_body)),
    field('备注', inputs.note),
    lifecycleSection);

  const errorLine = el('div', { class: 'field__error', hidden: true });
  const save = el('button', { class: 'btn btn--primary', text: editing ? '保存修改' : '创建渠道' });
  const cancel = el('button', { class: 'btn', text: '取消', onclick: () => closeDrawer() });

  save.addEventListener('click', async () => {
    errorLine.hidden = true;
    let extraHeaders = {};
    let extraBody = {};
    try {
      if (inputs.extra_headers.value.trim()) extraHeaders = JSON.parse(inputs.extra_headers.value);
      if (inputs.extra_body.value.trim()) extraBody = JSON.parse(inputs.extra_body.value);
    } catch (parseError) {
      errorLine.textContent = '附加请求头/请求体不是合法 JSON：' + parseError.message;
      errorLine.hidden = false;
      return;
    }
    const payload = {
      name: inputs.name.value.trim(),
      provider_type: inputs.provider_type.value,
      base_url: inputs.base_url.value.trim(),
      priority: Number(inputs.priority.value || 0),
      weight: Number(inputs.weight.value || 0),
      models: inputs.models.getValues(),
      balance_url: inputs.balance_url.value.trim(),
      balance_json_path: inputs.balance_json_path.value.trim(),
      balance_currency: inputs.balance_currency.value.trim(),
      extra_headers: extraHeaders,
      extra_body: extraBody,
      note: inputs.note.value.trim(),
      status: inputs.status.value,
    };
    payload.lifecycle = {
      enabled: lifeInputs.enabled.querySelector('input').checked,
      command: lifeInputs.command.value.trim(),
      args: lifeInputs.args.value.trim(),
      workdir: lifeInputs.workdir.value.trim(),
      stop_command: lifeInputs.stop_command.value.trim(),
      stop_strategy: lifeInputs.stop_strategy.value,
      health_path: lifeInputs.health_path.value.trim() || '/v1/models',
      startup_grace_seconds: Number(lifeInputs.startup_grace_seconds.value || 40),
      check_interval_seconds: Number(lifeInputs.check_interval_seconds.value || 15),
      failure_threshold: Number(lifeInputs.failure_threshold.value || 3),
      max_restarts: Number(lifeInputs.max_restarts.value || 0),
      restart_backoff_seconds: Number(lifeInputs.restart_backoff_seconds.value || 30),
      auto_start: lifeInputs.auto_start.querySelector('input').checked,
      auto_restart: lifeInputs.auto_restart.querySelector('input').checked,
      stop_on_shutdown: lifeInputs.stop_on_shutdown.querySelector('input').checked,
    };
    if (payload.lifecycle.enabled && !payload.lifecycle.command) {
      errorLine.textContent = '启用本地进程托管时必须填写启动命令';
      errorLine.hidden = false;
      save.disabled = false;
      return;
    }
    if (inputs.timeout_seconds.value) payload.timeout_seconds = Number(inputs.timeout_seconds.value);
    if (inputs.api_key.value.trim()) payload.api_key = inputs.api_key.value.trim();
    if (!editing && !payload.api_key) {
      errorLine.textContent = '请填写上游 API Key';
      errorLine.hidden = false;
      return;
    }
    save.disabled = true;
    try {
      if (editing) await api.put(`${ADMIN}/channels/${channel.channel_id}`, payload);
      else await api.post(`${ADMIN}/channels`, payload);
      closeDrawer();
      toast('ok', editing ? '渠道已更新' : '渠道已创建', payload.name);
      navigate('channels');
    } catch (error) {
      errorLine.textContent = error.message;
      errorLine.hidden = false;
    } finally {
      save.disabled = false;
    }
  });

  openDrawer(el('div', { class: 'stack' },
    el('div', { class: 'view__head' },
      el('div', { class: 'view__title' },
        el('h1', { text: editing ? '编辑渠道' : '新建渠道' }),
        el('div', { class: 'micro', text: editing ? channel.channel_id : '上游密钥将以本地对称密钥加密存储' }),
        errorLine)),
    form,
    el('div', { class: 'row' }, save, cancel,
      editing ? el('button', { class: 'btn', onclick: () => probeChannel(channel, true) }, '运行探针') : null,
      editing ? el('button', { class: 'btn', onclick: () => fetchChannelModels(channel) }, '拉取模型列表') : null)));
}

async function probeChannel(channel, stay) {
  toast('info', '正在探针', '向上游发起一次最小调用…', 2000);
  try {
    const result = await api.post(`${ADMIN}/channels/${channel.channel_id}/probe`, { model: (channel.models || []).find((m) => !m.includes('*')) || '' });
    if (result.ok) {
      toast('ok', '连通正常', `${channel.name} · ${fmt.ms(result.latency_ms)} · ${result.reply ? '回复「' + result.reply + '」' : '无文本回复'}`);
    } else {
      toast('err', '探针失败', `${result.code || ''} ${result.message || ''}`.trim(), 7000);
    }
    if (!stay) navigate('channels');
  } catch (error) {
    toast('err', '探针请求失败', error.message);
  }
}

async function fetchChannelModels(channel) {
  try {
    const data = await api.get(`${ADMIN}/channels/${channel.channel_id}/models`);
    if (!data.items.length) { toast('warn', '上游没有返回模型列表', '该协议可能不支持 /models，可手动填写白名单'); return; }
    openModal('上游可用模型 · ' + channel.name,
      el('div', { class: 'stack' },
        el('p', { class: 'panel__hint', text: '点「加入白名单」把模型 id 追加到该渠道的白名单，或全选复制。' }),
        el('div', { class: 'row' }, data.items.map((item) => el('button', {
          class: 'chip chip--mono', style: { cursor: 'pointer' },
          onclick: () => {
            const current = channel.models || [];
            if (!current.includes(item.id)) current.push(item.id);
            api.put(`${ADMIN}/channels/${channel.channel_id}`, { models: current })
              .then(() => toast('ok', '已加入白名单', item.id))
              .catch((error) => toast('err', '更新失败', error.message));
          },
        }, item.id, el('span', { class: 'panel__hint', text: item.owned_by ? ' · ' + item.owned_by : '' })))),
        el('div', { class: 'codetext', text: data.items.map((item) => item.id).join('\n') })),
      [el('button', { class: 'btn', text: '关闭', onclick: () => closeModal() })]);
  } catch (error) {
    toast('err', '拉取模型失败', error.message);
  }
}

async function toggleChannel(channel) {
  try {
    await api.put(`${ADMIN}/channels/${channel.channel_id}`, { status: channel.status === 'active' ? 'disabled' : 'active' });
    toast('ok', channel.status === 'active' ? '渠道已禁用' : '渠道已启用', channel.name);
    navigate('channels');
  } catch (error) { toast('err', '操作失败', error.message); }
}

async function resetCooldown(channel) {
  try {
    await api.post(`${ADMIN}/channels/${channel.channel_id}/reset-cooldown`);
    toast('ok', '已解除冷却', channel.name);
    navigate('channels');
  } catch (error) { toast('err', '操作失败', error.message); }
}

async function removeChannel(channel) {
  const sure = await confirmDialog('删除渠道', `确定删除「${channel.name}」吗？该操作不可撤销，已产生的用量明细会保留。`, '删除', true);
  if (!sure) return;
  try {
    await api.del(`${ADMIN}/channels/${channel.channel_id}`);
    toast('ok', '渠道已删除', channel.name);
    navigate('channels');
  } catch (error) { toast('err', '删除失败', error.message); }
}

/* ======================================================================== */
/* 十一、视图：密钥                                                          */
/* ======================================================================== */

async function viewKeys(host, token) {
  const query = new URLSearchParams();
  if (state.keyFilter.search) query.set('search', state.keyFilter.search);
  if (state.keyFilter.status) query.set('status', state.keyFilter.status);
  const data = await api.get(`${ADMIN}/keys?${query.toString()}`);
  if (token !== state.navToken) return;
  state.keys = data.items || [];

  const container = el('div', { class: 'stack' });
  container.appendChild(el('div', { class: 'view__head' },
    el('div', { class: 'view__title' },
      el('h1', { text: '本地密钥' }),
      el('div', { class: 'micro', text: `共 ${data.total} 个 · 明文只在创建时展示一次，库中只存哈希` })),
    el('div', { class: 'view__actions' },
      el('button', { class: 'btn btn--primary', onclick: () => keyForm() }, icon('plus', 15), ' 创建密钥'))));

  const search = el('input', { type: 'search', placeholder: '搜索名称 / 前缀 / 备注', value: state.keyFilter.search });
  search.addEventListener('keydown', (event) => { if (event.key === 'Enter') { state.keyFilter.search = search.value.trim(); navigate('keys'); } });
  container.appendChild(el('div', { class: 'toolbar' },
    icon('search', 15), search,
    selectInput([{ value: '', label: '全部状态' }, { value: 'active', label: '生效中' }, { value: 'disabled', label: '已禁用' }], state.keyFilter.status, {
      style: { width: '128px' }, onchange: (event) => { state.keyFilter.status = event.target.value; navigate('keys'); },
    }),
    el('div', { class: 'spacer' }),
    el('span', {
      class: 'chip',
      title: '本地密钥明文加密存库，可随时再次取回与复制',
    }, '平台式密钥管理：明文可反复取回'),
    el('button', { class: 'btn btn--tiny', onclick: () => { state.keyFilter = { search: '', status: '' }; navigate('keys'); } }, '重置')));

  if (!state.keys.length) {
    container.appendChild(panel('还没有本地密钥', emptyState('创建第一个分发密钥',
      '本地密钥是发给客户端的凭证，与上游厂商密钥完全解耦。可以为每个客户端单独配额度、限速与可用模型，并分别统计用量。',
      el('button', { class: 'btn btn--primary', onclick: () => keyForm() }, '创建密钥')), { index: 1 }));
  } else {
    container.appendChild(panel('密钥列表', dataTable([
      { title: '名称' }, { title: '密钥' }, { title: '状态' }, { title: '配额' },
      { title: '限流' }, { title: '允许模型' }, { title: '有效期' }, { title: '最近使用' }, { title: '操作' },
    ], state.keys.map(keyRow)), { flush: true, index: 1 }));
  }
  host.replaceChildren(container);
}

function keyRow(key) {
  const quota = key.quota || {};
  const limited = key.quota_limit > 0;
  const models = key.model_allowed || [];
  const symbol = fmt.moneyLabel(state.stats && state.stats.pricing && state.stats.pricing.currency);
  return el('tr', null,
    el('td', null, el('div', { class: 'tbl__name' },
      el('strong', { text: key.name }),
      el('span', { class: 'tbl__sub', text: key.note || key.key_id }))),
    el('td', null, el('span', { class: 'mono', text: key.masked }),
      key.has_secret
        ? null
        : el('div', null, el('span', {
          class: 'chip chip--warn',
          title: '该密钥创建于「本地密钥可再次查看」启用之前，库里只有哈希；点「重新生成密钥值」可拿到新明文',
        }, '明文不可取回'))),
    el('td', null, key.status !== 'active' ? chip('禁用', 'off') : (key.expired ? chip('已过期', 'err') : chip('生效', 'ok'))),
    el('td', null, limited
      ? meter(quota.percent, `${symbol}${fmt.money(quota.used)} / ${symbol}${fmt.money(quota.limit)}`, { left: fmt.pct(quota.percent) })
      : el('span', { class: 'panel__hint', text: `不限 · 已用 ${symbol}${fmt.money(key.quota_used)}` })),
    el('td', null, el('span', { class: 'mono', text: `${key.rpm_limit || '∞'} rpm / ${key.tpm_limit || '∞'} tpm` }),
      key.ratelimit ? el('div', { class: 'panel__hint', text: `当前窗口 ${key.ratelimit.rpm_used || 0} 次 / ${fmt.compact(key.ratelimit.tpm_used || 0)} tok` }) : null),
    el('td', null, models.length
      ? el('div', { class: 'row' }, models.slice(0, 2).map((model) => chip(model, 'mono')), models.length > 2 ? chip('+' + (models.length - 2), 'off') : null)
      : el('span', { class: 'panel__hint', text: '全部' })),
    el('td', null, el('span', { class: 'panel__hint', text: key.expires_at ? fmt.dt(key.expires_at) : '永不过期' })),
    el('td', null, el('span', { class: 'panel__hint', text: key.last_used_at ? fmt.rel(key.last_used_at) : '从未使用' })),
    el('td', { class: 'actions' },
      key.has_secret
        ? el('button', {
          class: 'btn btn--tiny',
          title: '取回明文并复制到剪贴板',
          onclick: () => copyKeySecret(key),
        }, icon('copy', 13), ' 复制明文')
        : null,
      el('button', { class: 'btn btn--tiny', onclick: () => keyDetail(key) }, '详情'),
      el('button', { class: 'btn btn--tiny', onclick: () => keyForm(key) }, '编辑'),
      el('button', { class: 'btn btn--tiny', onclick: () => toggleKey(key) }, key.status === 'active' ? '禁用' : '启用'),
      el('button', { class: 'btn btn--tiny btn--danger', onclick: () => removeKey(key) }, '删除')));
}

function keyForm(key) {
  const editing = !!key;
  const inputs = {
    name: textInput(key ? key.name : '', { placeholder: '例如：给 Cursor 用' }),
    note: textInput(key ? key.note : '', { placeholder: '可选' }),
    quota: numberInput(key ? key.quota_limit : 0, { min: 0, step: 1, placeholder: '以微美元计，0=不限' }),
    quotaMoney: textInput(key && key.quota_limit ? String(key.quota_limit / 1e6) : '', { placeholder: '也可以直接填金额（美元）' }),
    rpm: numberInput(key ? key.rpm_limit : 0, { min: 0, step: 1 }),
    tpm: numberInput(key ? key.tpm_limit : 0, { min: 0, step: 1 }),
    models: tagInput(key ? key.model_allowed : []),
    expires: textInput(key && key.expires_at ? key.expires_at.slice(0, 10) : '', { type: 'date' }),
    status: selectInput([{ value: 'active', label: '启用' }, { value: 'disabled', label: '禁用' }], key ? key.status : 'active'),
  };
  inputs.quotaMoney.addEventListener('input', () => {
    const value = parseFloat(inputs.quotaMoney.value);
    if (!Number.isNaN(value)) inputs.quota.value = String(Math.round(value * 1e6));
  });

  const errorLine = el('div', { class: 'field__error', hidden: true });
  const save = el('button', { class: 'btn btn--primary', text: editing ? '保存' : '创建密钥' });
  const cancel = el('button', { class: 'btn', text: '取消', onclick: () => closeModal() });
  const revealHost = el('div', { hidden: true });

  save.addEventListener('click', async () => {
    errorLine.hidden = true;
    const payload = {
      name: inputs.name.value.trim(),
      note: inputs.note.value.trim(),
      quota_limit: Number(inputs.quota.value || 0),
      rpm_limit: Number(inputs.rpm.value || 0),
      tpm_limit: Number(inputs.tpm.value || 0),
      model_allowed: inputs.models.getValues(),
      status: inputs.status.value,
    };
    if (inputs.expires.value) payload.expires_at = inputs.expires.value + 'T23:59:59Z';
    else if (editing) payload.expires_at = null;
    if (!payload.name) { errorLine.textContent = '请填写名称'; errorLine.hidden = false; return; }
    save.disabled = true;
    try {
      if (editing) {
        await api.put(`${ADMIN}/keys/${key.key_id}`, payload);
        toast('ok', '密钥已更新', payload.name);
        closeModal();
        navigate('keys');
      } else {
        const result = await api.post(`${ADMIN}/keys`, payload);
        revealHost.hidden = false;
        revealHost.replaceChildren(revealBox(
          result.plaintext,
          '明文已加密存库：之后随时可以在密钥列表点「复制明文」，或在详情里「显示明文」再次取回。'));
        save.disabled = true;
        save.textContent = '已创建';
        cancel.textContent = '关闭';
        cancel.onclick = () => { closeModal(); navigate('keys'); };
        cancel.hidden = false;
        toast('ok', '密钥已创建', '以后可随时再查看与复制');
      }
    } catch (error) {
      errorLine.textContent = error.message;
      errorLine.hidden = false;
      save.disabled = false;
    }
  });

  openModal(editing ? '编辑密钥 · ' + key.name : '创建本地密钥',
    el('div', { class: 'stack' },
      el('div', { class: 'formgrid' },
        field('名称', inputs.name),
        field('备注', inputs.note),
        field('配额（µ$）', inputs.quota, '额度单位是微美元（1e-6 美元），0 表示不限'),
        field('配额（美元）', inputs.quotaMoney, '填这里会自动换算成 µ$'),
        field('RPM 上限', inputs.rpm, '每分钟请求数，0 不限'),
        field('TPM 上限', inputs.tpm, '每分钟 token 数，0 不限'),
        field('过期日期', inputs.expires, '留空表示长期有效'),
        field('状态', inputs.status)),
      el('div', { class: 'span-2' }, field('允许的模型', inputs.models, '留空表示允许全部；支持通配，如 fast、claude-*')),
      errorLine,
      revealHost),
    [save, cancel]);
}

async function keyDetail(key) {
  const detail = await api.get(`${ADMIN}/keys/${key.key_id}`);
  const byModel = (detail.usage && detail.usage.by_model) || [];
  const totals = detail.totals || {};
  const secretHost = el('div', { class: 'stack' });
  const revealBtn = el('button', { class: 'btn btn--tiny' }, icon('copy', 13), ' 显示明文');
  revealBtn.addEventListener('click', () => revealKeySecret(key, secretHost));
  const body = el('div', { class: 'stack' },
    el('div', { class: 'view__head' },
      el('div', { class: 'view__title' },
        el('h1', { text: key.name }),
        el('div', { class: 'micro', text: key.masked })),
      el('div', { class: 'view__actions' },
        key.has_secret ? revealBtn : null,
        el('button', { class: 'btn btn--tiny', onclick: () => keyForm(key) }, '编辑'),
        el('button', { class: 'btn btn--tiny', onclick: () => rotateKey(key) }, '重新生成密钥值'),
        el('button', { class: 'btn btn--tiny', onclick: () => resetKeyUsage(key) }, '重置已用额度'))),
    key.has_secret
      ? secretHost
      : el('div', { class: 'notice notice--warn' }, icon('alert', 16),
        el('div', null,
          el('div', { text: '这把密钥没有可取的明文' }),
          el('div', { class: 'panel__hint', text: '它是在「设置 → 安全 → 本地密钥可再次查看」关闭时创建的（或数据目录的主密钥换过）。点上方「重新生成密钥值」即可拿到新明文，配额、限速、模型授权与统计都会保留。' }))),
    el('div', { class: 'grid grid--3' },
      kpiCard({ key: '累计请求', value: fmt.int(totals.requests), index: 0 }),
      kpiCard({ key: '累计 Tokens', value: fmt.compact(totals.tokens), accent: 'var(--violet)', index: 1 }),
      kpiCard({ key: '累计费用', value: fmt.money(totals.cost_units) + ' µ$', accent: 'var(--rose)', index: 2 })),
    el('dl', { class: 'kv' },
      el('dt', { text: '密钥 ID' }), el('dd', { text: key.key_id }),
      el('dt', { text: '创建时间' }), el('dd', { text: fmt.dt(key.created_at) }),
      el('dt', { text: '过期时间' }), el('dd', { text: key.expires_at ? fmt.dt(key.expires_at) : '永不过期' }),
      el('dt', { text: '配额' }), el('dd', { text: key.quota_limit ? `${fmt.money(key.quota_used)} / ${fmt.money(key.quota_limit)} µ$（${fmt.pct((key.quota || {}).percent)}）` : '不限' }),
      el('dt', { text: '限流' }), el('dd', { text: `${key.rpm_limit || '∞'} rpm / ${key.tpm_limit || '∞'} tpm` }),
      el('dt', { text: '允许模型' }), el('dd', { text: (key.model_allowed || []).join(', ') || '全部' })),
    panel('分模型用量', byModel.length
      ? dataTable([
        { title: '模型' }, { title: '请求', align: 'right' }, { title: 'Tokens', align: 'right' },
        { title: '输出 Tokens', align: 'right' }, { title: '费用 µ$', align: 'right' }, { title: '最近使用' },
      ], byModel.map((row) => el('tr', null,
        el('td', { class: 'mono', text: row.model }),
        el('td', { class: 'num mono', text: fmt.int(row.requests) }),
        el('td', { class: 'num mono', text: fmt.int(row.tokens) }),
        el('td', { class: 'num mono', text: fmt.int(row.completion_tokens) }),
        el('td', { class: 'num mono', text: fmt.int(row.cost_units) }),
        el('td', { class: 'panel__hint', text: fmt.rel(row.last_ts) }))), { flush: true })
      : el('p', { class: 'panel__hint', text: '该密钥还没有产生用量。' }), { flush: !byModel.length }));
  openDrawer(body);
}

async function toggleKey(key) {
  try {
    await api.put(`${ADMIN}/keys/${key.key_id}`, { status: key.status === 'active' ? 'disabled' : 'active' });
    toast('ok', key.status === 'active' ? '已禁用' : '已启用', key.name);
    navigate('keys');
  } catch (error) { toast('err', '操作失败', error.message); }
}

async function resetKeyUsage(key) {
  const sure = await confirmDialog('重置已用额度', `把「${key.name}」的已用额度清零？统计明细不会被删除，只重置配额计数。`, '重置');
  if (!sure) return;
  try {
    await api.post(`${ADMIN}/keys/${key.key_id}/reset-usage`);
    toast('ok', '已重置', key.name);
    closeDrawer();
    navigate('keys');
  } catch (error) { toast('err', '重置失败', error.message); }
}

/** 重新生成密钥值：拿到一把新明文，旧值立即失效，配置与统计保留。 */
async function rotateKey(key) {
  const sure = await confirmDialog(
    '重新生成密钥值',
    `将为「${key.name}」生成一把新的密钥值，旧值立即失效——正在用它的客户端需要换成新值。`
    + '配额用量、限速、模型授权、有效期与历史统计都会保留。',
    '重新生成',
  );
  if (!sure) return;
  try {
    const result = await api.post(`${ADMIN}/keys/${key.key_id}/rotate`);
    closeDrawer();
    const done = el('button', { class: 'btn btn--primary', text: '复制并关闭' });
    done.addEventListener('click', () => { copyText(result.plaintext, `已复制「${key.name}」的密钥`); closeModal(); navigate('keys'); });
    openModal('新的密钥值 · ' + key.name,
      el('div', { class: 'stack' },
        revealBox(result.plaintext, '新明文已加密存库，之后仍可随时取回；旧值已经失效，记得同步更新客户端。'),
        el('div', { class: 'notice notice--warn' }, icon('alert', 16),
          el('div', { text: '前缀已更新为 ' + result.key.masked })),
        el('div', { class: 'row' },
          el('span', { class: 'panel__hint', text: `旧前缀 ${key.masked} 立即失效` }))),
      [done, el('button', { class: 'btn', text: '关闭', onclick: () => { closeModal(); navigate('keys'); } })]);
    toast('ok', '密钥值已重新生成', '本会话内可重复复制');
  } catch (error) {
    toast('err', '重新生成失败', error.message);
  }
}

async function removeKey(key) {
  const sure = await confirmDialog('删除密钥', `确定删除「${key.name}」吗？使用该密钥的客户端会立即失效。`, '删除', true);
  if (!sure) return;
  try {
    await api.del(`${ADMIN}/keys/${key.key_id}`);
    toast('ok', '密钥已删除', key.name);
    navigate('keys');
  } catch (error) { toast('err', '删除失败', error.message); }
}

/* ======================================================================== */
/* 十二、视图：本地服务托管                                                  */
/* ======================================================================== */

async function viewServices(host, token) {
  const data = await api.get(`${ADMIN}/services`);
  if (token !== state.navToken) return;
  state.services = data;

  const container = el('div', { class: 'stack' });
  container.appendChild(el('div', { class: 'view__head' },
    el('div', { class: 'view__title' },
      el('h1', { text: '本地服务' }),
      el('div', { class: 'micro', text: data.enabled
        ? `托管已启用 · 默认探活间隔 ${data.interval_seconds}s · 日志在 ${data.log_dir}`
        : '托管已在设置里关闭（设置 → 本地服务托管）' })),
    el('div', { class: 'view__actions' },
      el('button', { class: 'btn btn--tiny', onclick: () => navigate('services') }, icon('refresh', 14), ' 刷新'),
      el('button', { class: 'btn btn--tiny', onclick: () => bulkService('start') }, '全部启动'),
      el('button', { class: 'btn btn--tiny', onclick: () => bulkService('stop') }, '全部停止'),
      el('button', { class: 'btn btn--tiny', onclick: () => bulkService('restart') }, '全部重启'))));

  if (!data.items.length) {
    container.appendChild(panel('还没有托管任何本地服务', emptyState(
      '把本机推理服务交给网关托管',
      '在「渠道」里新建或编辑一条指向本机地址的渠道（如 http://127.0.0.1:8080/v1），'
      + '在「本地进程」一节填上启动命令（例如 D:\\my-llm\\start.bat）。'
      + '之后网关会探活它、掉线自动重启，也可以在这里一键启停。',
      el('button', { class: 'btn btn--primary', onclick: () => navigate('channels') }, '去配置渠道')), { index: 0 }));
  } else {
    container.appendChild(el('div', { class: 'grid grid--cards', 'data-role': 'services-grid' },
      data.items.map((item, index) => serviceCard(item, index))));
    container.appendChild(el('div', { class: 'notice' }, icon('alert', 15),
      el('div', null,
        el('div', { text: '托管只会执行你在渠道里配置的命令，并且只终止自己拉起的进程树或占用该渠道端口的进程。' }),
        el('div', { class: 'panel__hint', text: '已经手动跑着的服务不会被重复拉起；探活通过即视为正常。想在网关退出时一并关闭，勾选渠道里的「退出时一并关闭」或在设置里打开全局开关。' }))));
  }
  host.replaceChildren(container);

  // 状态会随守护循环变化，页面开着时轻量轮询
  startPoller(async () => {
    try {
      const fresh = await api.get(`${ADMIN}/services`);
      state.services = fresh;
      const grid = $('[data-role="services-grid"]');
      if (grid) grid.replaceChildren(...fresh.items.map((item, index) => serviceCard(item, index)));
    } catch (_) { /* 忽略轮询失败 */ }
  }, 4000);
}

function serviceCard(item, index) {
  const meta = SERVICE_STATUS[item.status] || SERVICE_STATUS.unknown;
  const toneClass = ['ok', 'live'].includes(meta.tone) ? '' : meta.tone === 'err' ? 'panel--err' : meta.tone === 'warn' ? 'panel--warn' : '';
  const events = item.events || [];
  return el('div', { class: 'panel anim ' + toneClass, style: { '--i': index } },
    el('div', { class: 'panel__head' },
      el('h3', { text: item.channel_name }),
      providerChip(item.provider_type),
      el('div', { class: 'spacer' }),
      serviceChip(item),
      item.managed ? chip('由网关拉起', 'mono') : (item.healthy ? chip('外部进程', 'mono') : null)),
    el('div', { class: 'panel__body stack' },
      el('div', { class: 'svc__facts' },
        el('span', { class: 'svc__fact' }, '地址 ', el('b', { text: (item.base_url || '—').replace(/^https?:\/\//, '') })),
        el('span', { class: 'svc__fact' }, 'PID ', el('b', { text: item.pid ? String(item.pid) : '—' })),
        el('span', { class: 'svc__fact' }, '已运行 ', el('b', { text: item.uptime_seconds ? fmt.dur(item.uptime_seconds) : '—' })),
        el('span', { class: 'svc__fact' }, '重启 ', el('b', { text: String(item.restarts || 0) })),
        el('span', { class: 'svc__fact' }, '最近探活 ', el('b', {
          text: item.last_check_at
            ? `${fmt.clock(item.last_check_at)}${item.latency_ms ? ' · ' + fmt.ms(item.latency_ms) : ''}`
            : '—',
        })),
        item.consecutive_failures ? el('span', { class: 'svc__fact' }, '连续失败 ', el('b', { text: String(item.consecutive_failures) })) : null,
        item.retry_in_seconds > 0 ? el('span', { class: 'svc__fact' }, '下次重试 ', el('b', { text: item.retry_in_seconds + 's 后' })) : null),
      item.detail ? el('div', { class: 'panel__hint', text: item.detail }) : null,
      el('div', { class: 'codetext', text: item.command + (item.workdir ? `\n# 工作目录 ${item.workdir}` : '') }),
      el('div', { class: 'row' },
        el('button', { class: 'btn btn--tiny', onclick: () => serviceAction(item.channel_id, 'start') }, '启动'),
        el('button', { class: 'btn btn--tiny', onclick: () => serviceAction(item.channel_id, 'stop') }, '停止'),
        el('button', { class: 'btn btn--tiny', onclick: () => serviceAction(item.channel_id, 'restart') }, '重启'),
        el('button', { class: 'btn btn--tiny', onclick: () => serviceAction(item.channel_id, 'check') }, '立即探活'),
        el('button', { class: 'btn btn--tiny', onclick: () => showServiceLog(item) }, '查看日志'),
        el('div', { class: 'spacer' }),
        el('span', { class: 'panel__hint', text: `自动启动 ${item.auto_start ? '开' : '关'} · 自动重启 ${item.auto_restart ? '开' : '关'} · 退出时关闭 ${item.stop_on_shutdown ? '开' : '关'}` })),
      events.length
        ? el('div', { class: 'svc__events' }, events.slice(0, 5).map((event) => el('div', {
          class: 'svc__event svc__event--' + (event.level || 'info'),
        }, el('span', { class: 'mono', text: fmt.clock(event.at) }), ' ', event.message)))
        : null));
}

async function serviceAction(channelId, action) {
  const labels = { start: '启动', stop: '停止', restart: '重启', check: '探活' };
  const sure = action === 'stop' || action === 'restart';
  if (sure) {
    const ok = await confirmDialog(
      `${labels[action]}本地服务`,
      action === 'stop'
        ? '会终止网关拉起的进程树；若是外部启动的，则终止占用该渠道端口的进程。'
        : '会先停掉当前进程再重新拉起，正在进行的推理会被中断。',
      labels[action],
    );
    if (!ok) return;
  }
  try {
    const state2 = await api.post(`${ADMIN}/services/${channelId}/${action}`);
    const meta = SERVICE_STATUS[state2.status] || SERVICE_STATUS.unknown;
    toast(state2.status === 'failed' ? 'err' : 'ok', `${labels[action]}已执行`, `${meta.label}：${state2.detail || ''}`);
    navigate('services');
  } catch (error) {
    toast('err', `${labels[action]}失败`, error.message, 8000);
  }
}

async function bulkService(action) {
  const labels = { start: '全部启动', stop: '全部停止', restart: '全部重启' };
  if (action !== 'start') {
    const ok = await confirmDialog(labels[action], '会对所有托管的本地服务执行该操作。', labels[action]);
    if (!ok) return;
  }
  try {
    const result = await api.post(`${ADMIN}/services/bulk/${action}`);
    const failed = result.items.filter((item) => item.status === 'failed').length;
    toast(failed ? 'warn' : 'ok', `${labels[action]}完成`, `共 ${result.items.length} 个服务${failed ? `，其中 ${failed} 个异常` : ''}`);
    navigate('services');
  } catch (error) {
    toast('err', `${labels[action]}失败`, error.message, 8000);
  }
}

async function showServiceLog(item) {
  try {
    const data = await api.get(`${ADMIN}/services/${item.channel_id}/log?lines=300`);
    openModal('进程日志 · ' + item.channel_name,
      el('div', { class: 'stack' },
        el('div', { class: 'panel__hint', text: `共 ${data.lines} 行 · ${data.path}` }),
        data.content
          ? el('pre', { class: 'svc__log', text: data.content })
          : el('p', { class: 'panel__hint', text: '还没有输出。这个文件在网关启动该服务时创建。' })),
      [el('button', { class: 'btn', text: '关闭', onclick: () => closeModal() })]);
  } catch (error) {
    toast('err', '读取日志失败', error.message);
  }
}

/* ======================================================================== */
/* 十三、视图：模型别名                                                      */
/* ======================================================================== */

async function viewModels(host, token) {
  const [data, channelData] = await Promise.all([
    api.get(`${ADMIN}/models/map`),
    api.get(`${ADMIN}/channels`).catch(() => ({ items: [] })),
  ]);
  if (token !== state.navToken) return;
  state.maps = data.items || [];
  state.channels = channelData.items || [];

  const container = el('div', { class: 'stack' });
  container.appendChild(el('div', { class: 'view__head' },
    el('div', { class: 'view__title' },
      el('h1', { text: '模型别名' }),
      el('div', { class: 'micro', text: `${state.maps.length} 条映射 · 把厂商的长模型 id 折叠成好记的别名` })),
    el('div', { class: 'view__actions' },
      el('button', { class: 'btn', onclick: () => importMaps() }, '批量导入'),
      el('button', { class: 'btn btn--primary', onclick: () => mapForm(null) }, icon('plus', 15), ' 新增映射'))));

  const probeInput = textInput('', { placeholder: '输入一个模型名，看它会走哪条渠道', class: 'mono', style: { width: '260px' } });
  const probeResult = el('div', { class: 'stack' });
  const runProbe = async () => {
    const model = probeInput.value.trim();
    if (!model) return;
    probeResult.replaceChildren(el('p', { class: 'panel__hint', text: '试算中…' }));
    try {
      const result = await api.get(`${ADMIN}/models/resolve?model=${encodeURIComponent(model)}`);
      const resolved = result.resolved;
      probeResult.replaceChildren(el('div', { class: 'stack' },
        el('div', { class: 'row' },
          chip(model, 'mono'), icon('arrow', 15),
          chip(resolved.upstream, 'ok'),
          resolved.alias ? chip('命中别名 ' + resolved.alias, 'warn') : chip('未命中别名，原样透传', 'off')),
        result.candidates.length
          ? el('div', { class: 'row' }, result.candidates.map((item) => chip(`${item.name} · P${item.priority} W${item.weight}`, item.health && item.health.cooling ? 'err' : 'mono')))
          : el('div', { class: 'notice notice--warn' }, icon('alert', 15), el('div', { text: '没有可用渠道：' + (result.reason || '未匹配到渠道') }))));
    } catch (error) {
      probeResult.replaceChildren(el('div', { class: 'notice notice--err', text: error.message }));
    }
  };
  probeInput.addEventListener('keydown', (event) => { if (event.key === 'Enter') runProbe(); });
  container.appendChild(panel('路由试算', el('div', { class: 'stack' },
    el('div', { class: 'row' }, probeInput, el('button', { class: 'btn', onclick: runProbe }, '试算')),
    probeResult,
    el('p', { class: 'panel__hint', text: '未命中别名的请求会把模型名原样发给上游；也可以在设置里配置「兜底模型别名」。' })), { index: 0 }));

  container.appendChild(panel('映射表', state.maps.length
    ? dataTable([
      { title: '对外别名' }, { title: '上游真实模型' }, { title: '绑定渠道' }, { title: '协议' },
      { title: '类型' }, { title: '状态' }, { title: '备注' }, { title: '操作' },
    ], state.maps.map((item) => el('tr', null,
      el('td', null, el('span', { class: 'mono', text: item.alias })),
      el('td', null, el('span', { class: 'mono cell-ellip', text: item.upstream_model })),
      el('td', { class: 'panel__hint', text: item.channel_id || '自动路由' }),
      el('td', null, item.provider_type ? providerChip(item.provider_type) : el('span', { class: 'panel__hint', text: '不限' })),
      el('td', null, item.wildcard ? chip('通配', 'warn') : chip('精确', 'mono')),
      el('td', null, item.enabled ? chip('启用', 'ok') : chip('停用', 'off')),
      el('td', { class: 'panel__hint cell-ellip', text: item.note || '—' }),
      el('td', { class: 'actions' },
        el('button', { class: 'btn btn--tiny', onclick: () => mapForm(item) }, '编辑'),
        el('button', { class: 'btn btn--tiny', onclick: () => toggleMap(item) }, item.enabled ? '停用' : '启用'),
        el('button', { class: 'btn btn--tiny btn--danger', onclick: () => removeMap(item) }, '删除')))), { flush: true })
    : emptyState('还没有别名映射', '为常用的长模型 id 建个别名，比如把 claude-sonnet-4-5-20250929 映射成 claude-4。',
      el('button', { class: 'btn btn--primary', onclick: () => mapForm(null) }, '新增映射')), { flush: state.maps.length > 0, index: 1 }));
  host.replaceChildren(container);
}

function mapForm(item) {
  const editing = !!item;
  const inputs = {
    alias: textInput(item ? item.alias : '', { placeholder: 'fast 或 claude-*', class: 'mono' }),
    upstream: textInput(item ? item.upstream_model : '', { placeholder: 'deepseek-v4-flash', class: 'mono' }),
    channel: selectInput([{ value: '', label: '自动路由（不绑定）' }].concat(state.channels.map((channel) => ({ value: channel.channel_id, label: channel.name }))), item ? item.channel_id || '' : ''),
    provider: selectInput([{ value: '', label: '不限协议' }].concat((state.providers.length ? state.providers : []).map((provider) => ({ value: provider.type, label: provider.label }))), item ? item.provider_type || '' : ''),
    note: textInput(item ? item.note : ''),
    enabled: toggleInput(item ? item.enabled : true),
  };

  const errorLine = el('div', { class: 'field__error', hidden: true });
  const save = el('button', { class: 'btn btn--primary', text: editing ? '保存' : '创建' });
  const cancel = el('button', { class: 'btn', text: '取消', onclick: () => closeModal() });
  save.addEventListener('click', async () => {
    const payload = {
      alias: inputs.alias.value.trim(),
      upstream_model: inputs.upstream.value.trim(),
      channel_id: inputs.channel.value || null,
      provider_type: inputs.provider.value,
      note: inputs.note.value.trim(),
      enabled: inputs.enabled.querySelector('input').checked,
    };
    if (!payload.alias || !payload.upstream_model) {
      errorLine.textContent = '别名与上游模型 id 都要填写';
      errorLine.hidden = false;
      return;
    }
    save.disabled = true;
    try {
      if (editing) await api.put(`${ADMIN}/models/map/${item.id}`, payload);
      else await api.post(`${ADMIN}/models/map`, payload);
      toast('ok', editing ? '映射已更新' : '映射已创建', `${payload.alias} → ${payload.upstream_model}`);
      closeModal();
      navigate('models');
    } catch (error) {
      errorLine.textContent = error.message;
      errorLine.hidden = false;
      save.disabled = false;
    }
  });

  openModal(editing ? '编辑映射' : '新增映射',
    el('div', { class: 'stack' },
      el('div', { class: 'formgrid' },
        field('对外别名', inputs.alias, '客户端请求时填的名字'),
        field('上游真实模型', inputs.upstream, '实际发给厂商的模型 id'),
        field('绑定渠道', inputs.channel, '可留空由路由自动挑选'),
        field('限定协议', inputs.provider, '留空表示任意协议渠道都可以'),
        field('备注', inputs.note),
        field('状态', inputs.enabled)),
      errorLine),
    [save, cancel]);
}

function importMaps() {
  const textarea = el('textarea', { class: 'mono', style: { minHeight: '200px' }, placeholder: 'claude-4 = claude-sonnet-4-5-20250929\nfast = deepseek-v4-flash\n\n或直接粘贴 JSON 数组：[{"alias":"fast","upstream_model":"deepseek-flash"}]' });
  const errorLine = el('div', { class: 'field__error', hidden: true });
  const submit = el('button', { class: 'btn btn--primary', text: '导入' });
  submit.addEventListener('click', async () => {
    errorLine.hidden = true;
    const raw = textarea.value.trim();
    if (!raw) { errorLine.textContent = '请粘贴要导入的内容'; errorLine.hidden = false; return; }
    let entries = [];
    try {
      if (raw.startsWith('[') || raw.startsWith('{')) {
        const parsed = JSON.parse(raw);
        entries = Array.isArray(parsed) ? parsed : (parsed.entries || []);
      } else {
        entries = raw.split('\n').map((line) => line.trim()).filter(Boolean).filter((line) => !line.startsWith('#')).map((line) => {
          const parts = line.split(/\s*[=,>\t]\s*/);
          return { alias: parts[0], upstream_model: parts[1], note: parts[2] || '' };
        }).filter((item) => item.alias && item.upstream_model);
      }
    } catch (error) {
      errorLine.textContent = 'JSON 解析失败：' + error.message;
      errorLine.hidden = false;
      return;
    }
    if (!entries.length) { errorLine.textContent = '没有解析出任何映射'; errorLine.hidden = false; return; }
    submit.disabled = true;
    try {
      const result = await api.post(`${ADMIN}/models/map/import`, { entries });
      toast('ok', '导入完成', `新增 ${result.created} 条，更新 ${result.updated} 条`);
      closeModal();
      navigate('models');
    } catch (error) {
      errorLine.textContent = error.message;
      errorLine.hidden = false;
      submit.disabled = false;
    }
  });
  openModal('批量导入映射',
    el('div', { class: 'stack' },
      el('p', { class: 'panel__hint', text: '每行一条，支持 `别名 = 上游模型` 这种写法，也支持 JSON 数组。' }),
      textarea, errorLine),
    [el('button', { class: 'btn', text: '取消', onclick: () => closeModal() }), submit]);
}

async function toggleMap(item) {
  try {
    await api.put(`${ADMIN}/models/map/${item.id}`, { enabled: !item.enabled });
    navigate('models');
  } catch (error) { toast('err', '操作失败', error.message); }
}

async function removeMap(item) {
  const sure = await confirmDialog('删除映射', `删除别名「${item.alias}」？该名字之后会原样透传给上游。`, '删除', true);
  if (!sure) return;
  try {
    await api.del(`${ADMIN}/models/map/${item.id}`);
    navigate('models');
  } catch (error) { toast('err', '删除失败', error.message); }
}

/* ======================================================================== */
/* 十四、视图：用量统计                                                      */
/* ======================================================================== */

async function viewStats(host, token) {
  const stats = await api.get(`${ADMIN}/stats?hours=${state.statsWindow}&bucket=${state.statsBucket}`);
  if (token !== state.navToken) return;
  state.stats = stats;
  const logs = await api.get(`${ADMIN}/stats/logs?limit=200${state.logFilter.status ? '&status=' + state.logFilter.status : ''}${state.logFilter.model ? '&model=' + encodeURIComponent(state.logFilter.model) : ''}`);
  if (token !== state.navToken) return;

  const overview = stats.overview || {};
  const series = stats.series || [];
  const labels = series.map((point) => state.statsBucket === 'hour' ? point.bucket.slice(11, 16) : point.bucket.slice(5, 10));

  const container = el('div', { class: 'stack' });
  container.appendChild(el('div', { class: 'view__head' },
    el('div', { class: 'view__title' },
      el('h1', { text: '用量统计' }),
      el('div', { class: 'micro', text: '明细表是权威口径，图表读分钟级预聚合桶，两者可相互印证' })),
    el('div', { class: 'view__actions' },
      windowSegmented((hours) => { state.statsWindow = hours; navigate('stats'); }),
      el('div', { class: 'seg' },
        el('button', { class: state.statsBucket === 'hour' ? 'is-active' : '', text: '按小时', onclick: () => { state.statsBucket = 'hour'; navigate('stats'); } }),
        el('button', { class: state.statsBucket === 'day' ? 'is-active' : '', text: '按天', onclick: () => { state.statsBucket = 'day'; navigate('stats'); } })),
      el('button', { class: 'btn', onclick: exportCsv }, icon('download', 15), ' 导出 CSV'))));

  container.appendChild(el('div', { class: 'grid grid--kpi' },
    kpiCard({ key: '请求数', value: fmt.int(overview.requests), foot: el('span', { text: `其中流式 ${overview.streamed || 0}` }), index: 0 }),
    kpiCard({ key: '错误数', value: fmt.int(overview.errors), accent: 'var(--rose)', foot: el('span', { text: '错误率 ' + fmt.pct(overview.error_rate || 0) }), index: 1 }),
    kpiCard({ key: 'Token 总量', value: fmt.compact(overview.total_tokens), foot: el('span', { text: `输入 ${fmt.compact(overview.prompt_tokens)} · 输出 ${fmt.compact(overview.completion_tokens)}` }), accent: 'var(--violet)', index: 2 }),
    kpiCard({ key: '平均首字', value: fmt.ms(overview.avg_first_token_ms), foot: el('span', { text: '流式首包延迟' }), index: 3 }),
    kpiCard({ key: '平均速度', value: overview.avg_speed_tok_s ? overview.avg_speed_tok_s.toFixed(1) : '—', unit: 'tok/s', accent: 'var(--amber)', index: 4 }),
    kpiCard({ key: '活跃密钥', value: fmt.int(overview.active_keys), foot: el('span', { text: `窗口内出现过用量的密钥` }), index: 5 })));

  const chartRow = el('div', { class: 'grid grid--2' });
  chartRow.appendChild(panel('Token 趋势', series.length
    ? el('div', { class: 'stack' },
      el('div', { class: 'legend' },
        el('span', null, el('i', { style: { background: 'var(--cyan)' } }), '输入'),
        el('span', null, el('i', { style: { background: 'var(--emerald)' } }), '输出')),
      areaChart({ labels, series: [
        { color: 'var(--cyan)', values: series.map((p) => p.prompt_tokens) },
        { color: 'var(--emerald)', values: series.map((p) => p.completion_tokens) },
      ] }))
    : el('p', { class: 'panel__hint', text: '窗口内没有数据' }), { index: 0 }));
  chartRow.appendChild(panel('请求与错误', series.length
    ? el('div', { class: 'stack' },
      el('div', { class: 'legend' },
        el('span', null, el('i', { style: { background: 'var(--amber)' } }), '请求数'),
        el('span', null, el('i', { style: { background: 'var(--rose)' } }), '错误数')),
      areaChart({ labels, series: [
        { color: 'var(--amber)', values: series.map((p) => p.requests) },
        { color: 'var(--rose)', values: series.map((p) => p.errors), fill: false },
      ] }))
    : el('p', { class: 'panel__hint', text: '窗口内没有数据' }), { index: 1 }));
  container.appendChild(chartRow);

  const breakdown = el('div', { class: 'grid grid--3' });
  breakdown.appendChild(panel('按模型', (stats.by_model || []).length ? dataTable([
    { title: '模型' }, { title: '请求', align: 'right' }, { title: 'Tokens', align: 'right' }, { title: '费用 µ$', align: 'right' },
  ], stats.by_model.map((row) => el('tr', null,
    el('td', { class: 'mono cell-ellip', text: row.model }),
    el('td', { class: 'num mono', text: fmt.int(row.requests) }),
    el('td', { class: 'num mono', text: fmt.compact(row.tokens) }),
    el('td', { class: 'num mono', text: fmt.int(row.cost_units) }))), { flush: true })
    : el('p', { class: 'panel__hint', text: '暂无数据' }), { flush: true, index: 2 }));
  breakdown.appendChild(panel('按密钥', (stats.by_key || []).length ? dataTable([
    { title: '密钥' }, { title: '请求', align: 'right' }, { title: 'Tokens', align: 'right' },
  ], stats.by_key.map((row) => el('tr', null,
    el('td', null, el('div', { class: 'tbl__name' }, el('strong', { text: row.name || '（已删除）' }), el('span', { class: 'tbl__sub', text: row.prefix || '' }))),
    el('td', { class: 'num mono', text: fmt.int(row.requests) }),
    el('td', { class: 'num mono', text: fmt.compact(row.tokens) }))), { flush: true })
    : el('p', { class: 'panel__hint', text: '暂无数据' }), { flush: true, index: 3 }));
  breakdown.appendChild(panel('按渠道', (stats.by_channel || []).length ? dataTable([
    { title: '渠道' }, { title: '请求', align: 'right' }, { title: '速度', align: 'right' }, { title: '延迟', align: 'right' },
  ], stats.by_channel.map((row) => el('tr', null,
    el('td', null, el('div', { class: 'tbl__name' }, el('strong', { text: row.channel_name || '（已删除）' }), el('span', { class: 'tbl__sub', text: row.provider_type || '' }))),
    el('td', { class: 'num mono', text: fmt.int(row.requests) }),
    el('td', { class: 'num mono', text: row.avg_speed_tok_s ? row.avg_speed_tok_s.toFixed(1) : '—' }),
    el('td', { class: 'num mono', text: fmt.ms(row.avg_latency_ms) }))), { flush: true })
    : el('p', { class: 'panel__hint', text: '暂无数据' }), { flush: true, index: 4 }));
  container.appendChild(breakdown);

  const logFilter = selectInput([{ value: '', label: '全部状态' }, { value: 'ok', label: '仅成功' }, { value: 'error', label: '仅失败' }], state.logFilter.status, {
    style: { width: '120px' }, onchange: (event) => { state.logFilter.status = event.target.value; navigate('stats'); },
  });
  container.appendChild(panel('请求明细', dataTable([
    { title: '时间' }, { title: '模型' }, { title: '密钥' }, { title: '渠道' }, { title: 'Tokens', align: 'right' },
    { title: '首字', align: 'right' }, { title: '速度', align: 'right' }, { title: '耗时', align: 'right' },
    { title: '流式' }, { title: '状态' }, { title: '消耗 µ$', align: 'right' },
  ], (logs.items || []).map((row) => el('tr', null,
    el('td', { class: 'mono', text: fmt.dt(row.ts) }),
    el('td', { class: 'mono cell-ellip', text: row.model }),
    el('td', { text: row.key_name || row.key_prefix || '—' }),
    el('td', { text: row.channel_name || '—' }),
    el('td', { class: 'num mono', text: fmt.int(row.total_tokens) }),
    el('td', { class: 'num mono', text: row.first_token_ms ? fmt.ms(row.first_token_ms) : '—' }),
    el('td', { class: 'num mono', text: row.speed_tok_s ? row.speed_tok_s.toFixed(1) : '—' }),
    el('td', { class: 'num mono', text: fmt.ms(row.latency_ms) }),
    el('td', null, row.stream ? chip('流式', 'mono') : chip('同步', 'off')),
    el('td', null, row.status === 'ok' ? chip('成功', 'ok') : chip(row.error_code || '失败', 'err')),
    el('td', { class: 'num mono', text: fmt.int(row.cost_units) }))), { flush: true, wrapClass: 'tablewrap--tall' }),
    { flush: true, index: 5, actions: el('div', { class: 'row' }, logFilter) }));

  host.replaceChildren(container);
}

function windowSegmented(onPick) {
  const options = [[1, '1h'], [6, '6h'], [24, '24h'], [24 * 7, '7d'], [24 * 30, '30d']];
  return el('div', { class: 'seg' }, options.map(([hours, label]) => el('button', {
    class: state.statsWindow === hours ? 'is-active' : '', text: label, onclick: () => onPick(hours),
  })));
}

function exportCsv() {
  const link = el('a', { href: `${ADMIN}/stats/export.csv?hours=${state.statsWindow}` });
  document.body.appendChild(link);
  link.click();
  link.remove();
  toast('ok', '已开始下载', '用量明细 CSV');
}

/* ======================================================================== */
/* 十五、视图：余额                                                          */
/* ======================================================================== */

async function viewBalance(host, token) {
  const data = await api.get(`${ADMIN}/balance`);
  if (token !== state.navToken) return;
  state.balance = data.items || [];

  const container = el('div', { class: 'stack' });
  container.appendChild(el('div', { class: 'view__head' },
    el('div', { class: 'view__title' },
      el('h1', { text: '上游余额' }),
      el('div', { class: 'micro', text: '这里看的是「上游账上还剩多少」，与本地密钥的消费统计是两本账' })),
    el('div', { class: 'view__actions' },
      el('button', { class: 'btn btn--primary', onclick: () => refreshBalance() }, icon('refresh', 15), ' 立即刷新'))));

  const warn = state.balance.filter((item) => item.low);
  if (warn.length) {
    container.appendChild(el('div', { class: 'notice notice--warn' },
      icon('alert', 16),
      el('div', null,
        el('div', { text: `有 ${warn.length} 个渠道余额低于告警阈值（${data.warn_threshold}）` }),
        el('div', { class: 'panel__hint', text: warn.map((item) => `${item.channel_name}：${item.currency} ${item.total}`).join('，') }))));
  }

  if (!state.balance.length) {
    container.appendChild(panel('没有渠道', emptyState('先接一个上游渠道', '余额查询依赖渠道配置。DeepSeek 内置适配器可直接用；其它厂商可填自定义余额接口。',
      el('button', { class: 'btn btn--primary', onclick: () => navigate('channels') }, '去配置渠道')), { index: 1 }));
  } else {
    container.appendChild(el('div', { class: 'grid grid--cards' }, state.balance.map((item, index) => {
      const tone = !item.supported ? 'panel--warn' : item.low ? 'panel--err' : '';
      return el('div', { class: 'panel anim ' + tone, style: { '--i': index } },
        el('div', { class: 'panel__head' },
          el('h3', { text: item.channel_name }),
          providerChip(item.provider_type),
          el('div', { class: 'spacer' }),
          item.status === 'active' ? chip('启用', 'ok') : chip('禁用', 'off')),
        el('div', { class: 'panel__body stack' },
          item.supported
            ? el('div', null,
              el('div', { class: 'kpi__key', text: '可用余额' }),
              el('div', { class: 'kpi__val' }, item.currency ? item.currency + ' ' : '', Number(item.total || 0).toFixed(2)),
              el('div', { class: 'kpi__foot' },
                el('span', { text: `充值 ${Number(item.topped_up || 0).toFixed(2)} · 赠送 ${Number(item.granted || 0).toFixed(2)}` }),
                item.is_available ? chip('可用', 'ok') : chip('不可用', 'err')))
            : el('div', { class: 'notice notice--warn' }, icon('alert', 15),
              el('div', null, el('div', { text: '该渠道没有余额查询接口' }),
                el('div', { class: 'panel__hint', text: '可在渠道编辑里填自定义余额 URL 与取值路径。' }))),
          el('div', { class: 'row' },
            el('span', { class: 'panel__hint', text: item.fetched_at ? '更新于 ' + fmt.rel(item.fetched_at) : (item.message || '尚未查询') }),
            el('div', { class: 'spacer' }),
            el('button', { class: 'btn btn--tiny', onclick: () => refreshBalance(item.channel_id) }, '刷新'),
            el('button', { class: 'btn btn--tiny', onclick: () => balanceHistory(item) }, '历史'))));
    })));
  }
  host.replaceChildren(container);
}

async function refreshBalance(channelId) {
  try {
    await api.post(`${ADMIN}/balance/refresh`, channelId ? { channel_id: channelId } : {});
    toast('ok', '余额已刷新');
    navigate('balance');
  } catch (error) { toast('err', '刷新失败', error.message); }
}

async function balanceHistory(item) {
  try {
    const data = await api.get(`${ADMIN}/balance/${item.channel_id}/history?limit=100`);
    const rows = (data.items || []).slice().reverse();
    openModal('余额历史 · ' + item.channel_name,
      el('div', { class: 'stack' },
        rows.length ? areaChart({
          height: 170,
          labels: rows.map((row) => fmt.clock(row.fetched_at)),
          series: [{ color: 'var(--emerald)', values: rows.map((row) => row.total) }],
        }) : el('p', { class: 'panel__hint', text: '还没有历史记录' }),
        dataTable([{ title: '时间' }, { title: '余额', align: 'right' }, { title: '充值', align: 'right' }, { title: '赠送', align: 'right' }],
          (data.items || []).map((row) => el('tr', null,
            el('td', { class: 'mono', text: fmt.dt(row.fetched_at) }),
            el('td', { class: 'num mono', text: Number(row.total || 0).toFixed(2) }),
            el('td', { class: 'num mono', text: Number(row.topped_up || 0).toFixed(2) }),
            el('td', { class: 'num mono', text: Number(row.granted || 0).toFixed(2) }))))),
      [el('button', { class: 'btn', text: '关闭', onclick: () => closeModal() })]);
  } catch (error) { toast('err', '读取历史失败', error.message); }
}

/* ======================================================================== */
/* 十六、视图：设置                                                          */
/* ======================================================================== */

async function viewSettings(host, token) {
  const data = await api.get(`${ADMIN}/settings`);
  if (token !== state.navToken) return;
  state.settings = data;
  const values = Object.assign({}, data.values);
  const dirty = new Map();

  const container = el('div', { class: 'stack' });
  container.appendChild(el('div', { class: 'view__head' },
    el('div', { class: 'view__title' },
      el('h1', { text: '设置' }),
      el('div', { class: 'micro', text: data.pending_restart ? '有改动需要重启服务后生效' : '网络类参数由进程内宿主热重绑定，无需重启' })),
    el('div', { class: 'view__actions' },
      el('button', { class: 'btn', onclick: () => resetSettings() }, '恢复默认'),
      el('button', { class: 'btn btn--primary', onclick: () => saveSettings() }, '保存改动'))));

  if (data.pending_restart) {
    container.appendChild(el('div', { class: 'notice notice--warn' }, icon('alert', 16),
      el('div', null, el('div', { text: '监听地址或端口已修改' }),
        el('div', { class: 'panel__hint', text: data.restart_managed ? '宿主会自动重新绑定，通常几秒内恢复。' : '当前由外部进程托管（systemd / Docker / 命令行），请重启服务使其生效。' }))));
  }

  const formsHost = el('div', { class: 'grid grid--2' });
  (data.schema || []).forEach((group, index) => {
    const body = el('div', { class: 'stack' });
    group.items.forEach((spec) => {
      const value = values[spec.key];
      let control;
      const onChange = (next) => { dirty.set(spec.key, next); };
      if (spec.type === 'bool') {
        control = toggleInput(!!value);
        const input = control.querySelector('input');
        input.addEventListener('change', () => onChange(input.checked));
      } else if (spec.choices) {
        control = selectInput(spec.choices.map((choice) => ({ value: choice, label: choice })), value);
        control.addEventListener('change', () => onChange(control.value));
      } else if (spec.type === 'int' || spec.type === 'float') {
        control = numberInput(value, { step: spec.type === 'float' ? 'any' : 1, min: spec.minimum !== null && spec.minimum !== undefined ? spec.minimum : undefined, max: spec.maximum !== null && spec.maximum !== undefined ? spec.maximum : undefined });
        control.addEventListener('change', () => onChange(spec.type === 'int' ? Number(control.value) : parseFloat(control.value)));
      } else if (spec.type === 'json') {
        control = el('textarea', { class: 'mono', value: typeof value === 'string' ? value : JSON.stringify(value, null, 2) });
        control.addEventListener('change', () => {
          try { onChange(JSON.parse(control.value || '{}')); control.style.borderColor = ''; }
          catch (_) { control.style.borderColor = 'var(--rose)'; }
        });
      } else {
        control = textInput(value);
        control.addEventListener('change', () => onChange(control.value));
      }
      body.appendChild(el('div', { class: 'field' },
        el('div', { class: 'row' }, el('span', { class: 'field__label', text: spec.label }),
          spec.requires_restart ? chip('需重启', 'warn') : null),
        control,
        spec.description ? el('span', { class: 'field__hint', text: spec.description }) : null));
    });
    formsHost.appendChild(el('div', { class: 'panel anim', style: { '--i': index } },
      el('div', { class: 'panel__head' }, el('h3', { text: group.label }),
        el('div', { class: 'spacer' }), el('span', { class: 'panel__hint', text: `${group.items.length} 项` })),
      el('div', { class: 'panel__body' }, body)));
  });
  container.appendChild(formsHost);

  const ops = el('div', { class: 'panel', style: { '--accent': 'var(--rose)' } },
    el('div', { class: 'panel__head' }, el('h3', { text: '运维' })),
    el('div', { class: 'panel__body stack' },
      el('dl', { class: 'kv' },
        el('dt', { text: '数据目录' }), el('dd', { text: data.data_dir }),
        el('dt', { text: '数据库' }), el('dd', { text: (state.system && state.system.db_path) || '—' }),
        el('dt', { text: 'SQLite' }), el('dd', { text: (state.system && state.system.sqlite_version) || '—' }),
        el('dt', { text: '运行模式' }), el('dd', { text: (state.system && state.system.mode) || '—' }),
        el('dt', { text: '已运行' }), el('dd', { text: fmt.dur(state.system && state.system.uptime_seconds) })),
      el('div', { class: 'row' },
        el('button', { class: 'btn', onclick: rotateToken }, '轮换管理员令牌'),
        el('button', { class: 'btn', onclick: backupConfig }, '导出配置快照'),
        el('button', { class: 'btn', onclick: restartService }, '重新绑定监听'),
        el('button', { class: 'btn', onclick: () => window.open('/docs', '_blank') }, '接口文档')),
      el('p', { class: 'panel__hint', text: '配置快照不含任何密钥明文，只用于核对配置结构。' })));
  container.appendChild(ops);

  host.replaceChildren(container);

  async function saveSettings() {
    if (!dirty.size) { toast('info', '没有需要保存的改动'); return; }
    const payload = {};
    dirty.forEach((value, key) => { payload[key] = value; });
    try {
      const result = await api.put(`${ADMIN}/settings`, { values: payload });
      toast('ok', '设置已保存', `更新 ${result.changed.length} 项${result.pending_restart ? ' · 监听参数已触发重新绑定' : ''}`);
      dirty.clear();
      const system = await api.get(`${ADMIN}/system`).catch(() => null);
      if (system) state.system = system;
      setTimeout(() => navigate('settings'), 1200);
    } catch (error) { toast('err', '保存失败', error.message); }
  }

  async function resetSettings() {
    const sure = await confirmDialog('恢复默认设置', '把所有设置项恢复为出厂默认值？渠道、密钥与统计不受影响。', '恢复默认', true);
    if (!sure) return;
    try {
      await api.post(`${ADMIN}/settings/reset`, {});
      toast('ok', '已恢复默认');
      navigate('settings');
    } catch (error) { toast('err', '操作失败', error.message); }
  }
}

async function rotateToken() {
  const sure = await confirmDialog('轮换管理员令牌', '旧令牌会立即失效。如果你在别的设备上登录着控制台，需要重新用新令牌登录。', '轮换');
  if (!sure) return;
  try {
    const result = await api.post(`${ADMIN}/system/rotate-token`);
    openModal('新的管理员令牌',
      el('div', { class: 'stack' },
        el('div', { class: 'reveal' },
          el('div', { class: 'micro', text: '请立即保存' }),
          el('div', { class: 'reveal__key', text: result.admin_token }),
          el('button', { class: 'btn btn--primary btn--tiny', onclick: () => copyText(result.admin_token, '已复制管理员令牌') }, icon('copy', 14), ' 复制')),
        el('p', { class: 'panel__hint', text: '该令牌也会写入数据目录的 secrets.json，可用 python -m airelay --print-token 再次查看。' })),
      [el('button', { class: 'btn', text: '关闭', onclick: () => closeModal() })]);
    if (state.system) state.system._token = result.admin_token;
  } catch (error) { toast('err', '轮换失败', error.message); }
}

async function backupConfig() {
  try {
    const data = await api.post(`${ADMIN}/system/backup`);
    downloadFile('airelay-config-snapshot.json', JSON.stringify(data, null, 2));
    toast('ok', '配置快照已导出', '不含密钥明文');
  } catch (error) { toast('err', '导出失败', error.message); }
}

async function restartService() {
  try {
    const result = await api.post(`${ADMIN}/system/restart`);
    if (result.ok) toast('ok', '正在重新绑定', result.message);
    else toast('warn', '需要手动重启', result.message, 8000);
  } catch (error) {
    toast('warn', '连接中断（通常说明正在重新绑定）', '请稍等几秒后刷新页面。', 8000);
  }
}

/* ======================================================================== */
/* 十六、登录闸门与启动                                                      */
/* ======================================================================== */

function showGate(message) {
  const gate = $('#gate');
  if (!gate.hidden) return;
  gate.hidden = false;
  $('#shell').hidden = true;
  if (state.ws) { try { state.ws.close(); } catch (_) {} state.ws = null; }
  if (message) {
    const error = $('#gate-error');
    error.textContent = message;
    error.hidden = false;
  }
}

function hideGate() {
  $('#gate').hidden = true;
  $('#shell').hidden = false;
}

async function loadSystem() {
  state.system = await api.get(`${ADMIN}/system`);
  state.providers = state.system.providers || [];
}

async function boot() {
  // 主题
  const savedTheme = localStorage.getItem('airelay.theme');
  if (savedTheme) document.documentElement.dataset.theme = savedTheme;

  $('#theme-toggle').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('airelay.theme', next);
  });

  $('#copy-openai').addEventListener('click', () => copyText((state.system && state.system.openai_base_url) || '', 'OpenAI 兼容基地址'));
  $('#copy-console').addEventListener('click', () => copyText(location.origin + '/admin', '控制台地址'));

  // 抽屉 / 模态关闭
  document.querySelectorAll('[data-close]').forEach((node) => {
    node.addEventListener('click', () => { closeModal(); closeDrawer(); });
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { closeModal(); closeDrawer(); $('#cmdk').hidden = true; }
    if ((event.key === 'k' || event.key === 'K') && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      openCommandPalette();
    }
  });

  // 登录表单
  $('#gate-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = $('#gate-form button[type=submit]');
    button.disabled = true;
    try {
      await api.post(`${ADMIN}/session`, { token: $('#gate-token').value.trim() });
      $('#gate-error').hidden = true;
      hideGate();
      await start();
    } catch (error) {
      const errorLine = $('#gate-error');
      errorLine.textContent = error.message;
      errorLine.hidden = false;
    } finally {
      button.disabled = false;
    }
  });

  try {
    const session = await api.get(`${ADMIN}/session`);
    state.session = session;
    if (!session.authenticated) { showGate(''); return; }
    hideGate();
    await start();
  } catch (error) {
    showGate(error.message);
  }
}

async function start() {
  await loadSystem();
  buildRail();
  paintSignal();
  connectLive();
  const hash = (location.hash || '').replace('#/', '');
  await navigate(VIEWS.some((view) => view.id === hash) ? hash : 'dashboard');
  window.addEventListener('hashchange', () => {
    const target = (location.hash || '').replace('#/', '');
    if (target && target !== state.view && VIEWS.some((view) => view.id === target)) navigate(target);
  });
}

/* ---- 命令面板 ---- */
const CMDS = [
  { label: '仪表盘', hint: '总览与趋势', run: () => navigate('dashboard') },
  { label: '实时会话', hint: '正在处理的请求', run: () => navigate('live') },
  { label: '新建渠道', hint: '接入一个上游', run: () => channelForm(null) },
  { label: '新建密钥', hint: '创建一个本地分发密钥', run: () => keyForm() },
  { label: '模型别名', hint: '长模型 id 折叠', run: () => navigate('models') },
  { label: '用量统计', hint: '按模型 / 密钥 / 渠道', run: () => navigate('stats') },
  { label: '导出用量 CSV', hint: '下载明细', run: () => exportCsv() },
  { label: '刷新余额', hint: '查询上游余额', run: () => refreshBalance() },
  { label: '设置', hint: '网络、限流、计价…', run: () => navigate('settings') },
  { label: '复制基地址', hint: 'OpenAI 兼容端点', run: () => copyText((state.system && state.system.openai_base_url) || '') },
  { label: '接口文档', hint: '打开 /docs', run: () => window.open('/docs', '_blank') },
  { label: '切换主题', hint: '深色 / 浅色', run: () => $('#theme-toggle').click() },
];

function openCommandPalette() {
  const host = $('#cmdk');
  const input = $('#cmdk-input');
  const list = $('#cmdk-list');
  let filtered = CMDS.slice();
  let cursor = 0;
  const paint = () => {
    list.replaceChildren(...filtered.map((cmd, index) => el('li', {
      'aria-selected': index === cursor ? 'true' : 'false',
      onclick: () => { host.hidden = true; cmd.run(); },
    }, icon('arrow', 14), cmd.label, el('span', { class: 'micro', text: cmd.hint }))));
  };
  const filter = () => {
    const query = input.value.trim().toLowerCase();
    filtered = CMDS.filter((cmd) => !query || (cmd.label + cmd.hint).toLowerCase().includes(query));
    cursor = 0;
    paint();
  };
  input.value = '';
  filter();
  host.hidden = false;
  input.focus();
  input.oninput = filter;
  input.onkeydown = (event) => {
    if (event.key === 'ArrowDown') { cursor = Math.min(cursor + 1, filtered.length - 1); paint(); event.preventDefault(); }
    else if (event.key === 'ArrowUp') { cursor = Math.max(cursor - 1, 0); paint(); event.preventDefault(); }
    else if (event.key === 'Enter' && filtered[cursor]) { host.hidden = true; filtered[cursor].run(); }
  };
  host.onclick = (event) => { if (event.target === host) host.hidden = true; };
}

let booted = false;
function bootOnce() {
  if (booted) return;
  booted = true;
  boot();
}

document.addEventListener('DOMContentLoaded', bootOnce);
if (document.readyState !== 'loading') bootOnce();

})();
