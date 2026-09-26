// 统一后端基地址：默认同源；支持 ?api=https://host 或 window.API_BASE 覆盖（前端静态托管、后端另部署场景）
(function () {
  try {
    const params = new URLSearchParams(location.search);
    const raw = (params.get('api') || window.API_BASE || '').trim().replace(/\/+$/, '');
    if (raw) {
      const origFetch = window.fetch.bind(window);
      window.fetch = function (url, opts) {
        if (typeof url === 'string' && url.charAt(0) === '/' && url.charAt(1) !== '/') {
          return origFetch(raw + url, opts);
        }
        return origFetch(url, opts);
      };
      const OrigES = window.EventSource;
      window.EventSource = function (url, opts) {
        const u = (typeof url === 'string' && url.charAt(0) === '/' && url.charAt(1) !== '/') ? raw + url : url;
        return new OrigES(u, opts);
      };
      window.EventSource.prototype = OrigES.prototype;
      window.assetUrl = function (p) {
        return (typeof p === 'string' && p.charAt(0) === '/' && p.charAt(1) !== '/') ? raw + p : p;
      };
    } else {
      window.assetUrl = function (p) { return p; };
    }
  } catch (e) {
    window.assetUrl = function (p) { return p; };
  }
})();

// 后端连通性自检：后端不可达时自动进入「静态演示模式」。
// GitHub Pages 纯静态托管 / 本地未启动后端时：页面完整渲染、加载演示数据、
// 按钮给出友好引导；真实解析 / 下载 / 登录功能需本地运行 Python 后端（README 有说明）。
let DEMO = false;

// ---- 演示模式数据（仅用于静态预览时填充 UI，均为示例内容） ----
const DEMO_PLATFORMS = [
  { platform: 'bilibili', label: 'Bilibili', configured: true, logged_in: true, expired: false, count: 18,
    expires_text: '2027-01-12', saved_at: '2026-09-01 10:24:00', device_ready: true, profile_ready: true },
  { platform: 'douyin', label: '抖音', configured: true, logged_in: true, expired: false, count: 9,
    expires_text: '2026-12-30', saved_at: '2026-09-10 22:05:31', device_ready: true, profile_ready: true },
  { platform: 'kuaishou', label: '快手', configured: true, logged_in: false, expired: false, count: 4,
    expires_text: '匿名', saved_at: '2026-09-02 09:12:44', device_ready: false, profile_ready: false },
  { platform: 'weibo', label: '微博', configured: false, count: 0, expires_text: '—' },
  { platform: 'xiaohongshu', label: '小红书', configured: false, count: 0, expires_text: '—' },
  { platform: 'youtube', label: 'YouTube', configured: true, logged_in: false, expired: false, count: 2,
    expires_text: '匿名', saved_at: '2026-09-03 15:40:12', device_ready: false, profile_ready: false },
];
const DEMO_ALL = [
  { id: 'demo-b1', platform: 'bilibili', keyword: '前端动画', title: '【前端动画】用 CSS 实现丝滑的滚动视差效果', author: '自然-', duration: 812, likes: 23451, views: 1023489, url: 'https://www.bilibili.com/video/BV1Demo00001' },
  { id: 'demo-b2', platform: 'bilibili', keyword: 'JavaScript', title: 'JavaScript 事件循环机制完全解析（微任务 / 宏任务）', author: '前端食堂', duration: 1254, likes: 9876, views: 456123, url: 'https://www.bilibili.com/video/BV1Demo00002' },
  { id: 'demo-d1', platform: 'douyin', keyword: '美食', title: '三分钟教会你外酥里嫩的黄金脆皮炸鸡', author: '小厨娘', duration: 187, likes: 45213, views: 2310000, url: 'https://www.douyin.com/video/7300000000000000001' },
  { id: 'demo-d2', platform: 'douyin', keyword: '旅行', title: '老城街巷的烟火气，跟着镜头走一遍', author: 'Ashley_hh_', duration: 243, likes: 12034, views: 890123, url: 'https://www.douyin.com/video/7300000000000000002' },
  { id: 'demo-k1', platform: 'kuaishou', keyword: '健身', title: '居家徒手增肌训练计划（新手友好）', author: '铁馆老张', duration: 556, likes: 7654, views: 234567, url: 'https://www.kuaishou.com/short-video/3xDemoK0001' },
  { id: 'demo-w1', platform: 'weibo', keyword: '科技', title: '国产大模型新版本实测：代码能力横向对比', author: '科技老白', duration: 640, likes: 3456, views: 120456, url: 'https://weibo.com/1234567890/OKDemoW0001' },
  { id: 'demo-x1', platform: 'xiaohongshu', keyword: '穿搭', title: '通勤穿搭｜5 套显瘦又不失高级感的基础款', author: 'Momo酱', duration: 95, likes: 19876, views: 765432, url: 'https://www.xiaohongshu.com/explore/65demo0001' },
  { id: 'demo-y1', platform: 'youtube', keyword: 'music', title: 'Lo-fi Beats to Study / Relax To（4K 循环）', author: 'Lofi Girl', duration: 3600, likes: 567890, views: 8890012, url: 'https://www.youtube.com/watch?v=demo0001' },
];
const DEMO_BATCH = [
  { id: 'bd-1', platform: 'bilibili', title: '【合集】Vue3 源码精读 01-05', uploader: '自然-', duration: 18600, likes: 15230, views: 980123, url: 'https://www.bilibili.com/video/BV1Demo00003', thumbnail: '', cover_file: '' },
  { id: 'bd-2', platform: 'bilibili', title: '【分P】TypeScript 类型体操入门（共 8P）', uploader: '前端食堂', duration: 5400, likes: 4321, views: 234567, url: 'https://www.bilibili.com/video/BV1Demo00004', thumbnail: '', cover_file: '' },
  { id: 'bd-3', platform: 'douyin', title: '泉州蟳埔簪花围体验全记录', uploader: 'Ashley_hh_', duration: 218, likes: 8765, views: 654321, url: 'https://www.douyin.com/video/7300000000000000003', thumbnail: '', cover_file: '' },
  { id: 'bd-4', platform: 'kuaishou', title: '健身房器械使用避坑指南（上）', uploader: '铁馆老张', duration: 920, likes: 2345, views: 123456, url: 'https://www.kuaishou.com/short-video/3xDemoK0002', thumbnail: '', cover_file: '' },
  { id: 'bd-5', platform: 'youtube', title: 'Build a Full-Stack App in 40 Minutes', uploader: 'Dev World', duration: 2400, likes: 12345, views: 890123, url: 'https://www.youtube.com/watch?v=demo0002', thumbnail: '', cover_file: '' },
];
const DEMO_HISTORY = [
  { id: 'dh-1', platform: 'bilibili', title: '【前端动画】用 CSS 实现丝滑的滚动视差效果', author: '自然-', likes: 23451, size: 268435456, status: 'done', finished_at: '2026-09-25 21:14:03', path: 'downloads/【前端动画】用CSS实现丝滑的滚动视差效果 - 自然-[13分32秒].mp4', cover: '', cover_file: '' },
  { id: 'dh-2', platform: 'bilibili', title: 'JavaScript 事件循环机制完全解析（微任务 / 宏任务）', author: '前端食堂', likes: 9876, size: 429496730, status: 'done', finished_at: '2026-09-25 20:02:11', path: 'downloads/JavaScript 事件循环机制完全解析 - 前端食堂-[20分54秒].mp4', cover: '', cover_file: '' },
  { id: 'dh-3', platform: 'douyin', title: '三分钟教会你外酥里嫩的黄金脆皮炸鸡', author: '小厨娘', likes: 45213, size: 52428800, status: 'done', finished_at: '2026-09-24 19:33:27', path: 'downloads/三分钟教会你外酥里嫩的黄金脆皮炸鸡 - 小厨娘[03分07秒].mp4', cover: '', cover_file: '' },
  { id: 'dh-4', platform: 'youtube', title: 'Lo-fi Beats to Study / Relax To（4K 循环）', author: 'Lofi Girl', likes: 567890, size: 1073741824, status: 'skipped', finished_at: '2026-09-24 10:15:09', path: 'downloads/Lo-fi Beats to Study 4K - Lofi Girl[60分00秒].mp4', cover: '', cover_file: '' },
  { id: 'dh-5', platform: 'kuaishou', title: '居家徒手增肌训练计划（新手友好）', author: '铁馆老张', likes: 7654, size: 67108864, status: 'error', finished_at: '2026-09-23 22:41:55', path: 'downloads/居家徒手增肌训练计划 - 铁馆老张[09分16秒].mp4', cover: '', cover_file: '' },
];

// ---- 演示模式工具函数 ----
function demoJson(obj, status) {
  return Promise.resolve(new Response(JSON.stringify(obj), {
    status: status || 200, headers: { 'Content-Type': 'application/json' },
  }));
}
function demoText(text, type) {
  return Promise.resolve(new Response(text, {
    status: 200, headers: { 'Content-Type': type || 'text/plain; charset=utf-8' },
  }));
}
function demoSse(message) {
  const enc = new TextEncoder();
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(enc.encode('event: error\ndata: ' + JSON.stringify({ message: message || '演示模式：该功能需本地运行后端' }) + '\n\n'));
      controller.close();
    },
  });
  return Promise.resolve(new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }));
}
function demoImg() {
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180">'
    + '<rect width="100%" height="100%" fill="#e9edf6"/>'
    + '<text x="50%" y="46%" font-size="15" fill="#8a94b0" text-anchor="middle">演示封面</text>'
    + '<text x="50%" y="66%" font-size="12" fill="#aab3c8" text-anchor="middle">真实图片由本地后端提供</text>'
    + '</svg>';
  return Promise.resolve(new Response(svg, { status: 200, headers: { 'Content-Type': 'image/svg+xml; charset=utf-8' } }));
}
function demoImportResponse(opts) {
  let body = {};
  try { body = JSON.parse((opts && opts.body) || '{}'); } catch (e) {}
  const raw = String(body.raw || '');
  const count = raw ? Math.max(1, Math.min(20, Math.ceil(raw.length / 15))) : 0;
  const plat = (body.platform && body.platform !== 'auto') ? body.platform : '自动识别';
  return demoJson({ ok: true, label: '（演示）' + plat, format: 'demo', count: count, logged_in: true,
    expires_text: '演示模式·未落盘', netscape_file: 'demo_cookies.txt',
    warning: '演示模式：Cookie 未真正写入本地文件，仅展示完整流程。',
    platforms: DEMO_PLATFORMS });
}

// 演示模式下拦截所有 /api 与 /downloads 请求，返回演示响应或友好引导
function wrapFetchForDemo() {
  const origFetch = window.fetch.bind(window);
  window.fetch = function (url, opts) {
    const u = String(url);
    const p = u.split('?')[0].split('#')[0];
    const isApi = p.includes('/api/');
    const isDl = p.includes('/downloads/');
    if (!isApi && !isDl) return origFetch(url, opts);
    if (isDl) return demoJson({ ok: false, error: '演示模式：文件不存在（需本地后端）' }, 404);
    const method = ((opts && opts.method) || 'GET').toUpperCase();
    if (p === '/api/health') return demoJson({ ok: false }, 503);
    if (p === '/api/cookies') return demoJson({ ok: true, playwright: false, platforms: DEMO_PLATFORMS });
    if (p === '/api/cookies/login_status') return demoJson({ ok: true, running: false, message: '演示模式：未启动浏览器登录' });
    if (p === '/api/cookies/export') return demoText('# Netscape HTTP Cookie File（演示导出）\n# 真实 Cookie 需本地运行后端后导出\n', 'text/plain; charset=utf-8');
    if (p === '/api/ytdlp/check') return demoJson({ ok: true, available: false, version: '—', ffmpeg: false, demo: true });
    if (p === '/api/downloads/history') return demoJson({ ok: true, items: DEMO_HISTORY });
    if (p === '/api/img' || p === '/api/files/serve') return demoImg();
    if (method === 'GET') return demoJson({ ok: false, error: '演示模式：该接口需本地运行 Python 后端' }, 404);
    if (p === '/api/collect' || p === '/api/download' || p === '/api/batch/download')
      return demoSse('演示模式：采集 / 下载需本地运行 Python 后端（README 有部署说明）');
    if (p === '/api/cookies/import') return demoImportResponse(opts);
    if (p === '/api/cookies/probe') return demoJson({ ok: true, status: 'valid', message: '演示模式：未发起真实探活', platforms: DEMO_PLATFORMS });
    if (p === '/api/cookies/selfcheck') return demoJson({
      ok: true, headline: '演示模式 · 未在线探活（本地后端可做真实自检）',
      platforms: DEMO_PLATFORMS.map(function (pp) {
        return { label: pp.label, platform: pp.platform, configured: pp.configured,
                 logged_in: pp.logged_in, grade: pp.configured ? 'warn' : 'skip', probe: null, checks: [] };
      }),
      advice: ['运行本地后端后，可在「Cookie 管理」发起真实在线自检。'],
      platforms_status: DEMO_PLATFORMS,
    });
    if (p === '/api/cookies/clear') return demoJson({ ok: true, removed: false, platforms: DEMO_PLATFORMS });
    if (p === '/api/cookies/login' || p === '/api/cookies/login_capture') return demoJson({ ok: false, error: '演示模式：浏览器登录需本地运行 Python 后端' });
    if (p === '/api/batch/info') return demoJson({ ok: true, items: DEMO_BATCH, notes: ['演示模式：返回示例链接清单'], count: DEMO_BATCH.length, playlist_detected: [] });
    if (p === '/api/batch/detect') return demoJson({ ok: true, is_playlist: false });
    if (p === '/api/download/info') return demoJson({ ok: false, error: '演示模式：格式解析需本地运行 Python 后端' });
    if (p === '/api/downloads/delete') return demoJson({ ok: true, removed_ids: [] });
    if (p === '/api/downloads/save') return demoJson({ ok: true });
    if (p === '/api/dl/control') return demoJson({ ok: true });
    if (p === '/api/collect_links') return demoJson({ ok: true, items: DEMO_ALL, notes: ['演示模式'], count: DEMO_ALL.length });
    return demoJson({ ok: false, error: '演示模式：真实功能需本地运行 Python 后端（README 有部署说明）' });
  };
}

// 一键填充四个页签的演示数据
function loadDemoData() {
  ALL = DEMO_ALL.slice();
  render();
  const ns = $('notes'); if (ns) ns.classList.add('hidden');
  const md = $('mdCard'); if (md) md.classList.add('hidden');
  BATCH = DEMO_BATCH.slice();
  BSEL.clear();
  BATCH.forEach(function (en) { BSEL.add(en.id); });
  renderBatch();
  if (!DL_INIT) loadDownloads();
  DL_HISTORY = DEMO_HISTORY.slice();
  renderDownloads();
  CK_LIST = DEMO_PLATFORMS.slice();
  renderCookies(CK_LIST);
  toast('演示数据已加载：四个页签均已填充示例内容');
}

function showDemoBanner() {
  const old = document.getElementById('backendWarn');
  if (old) old.remove();
  let bar = document.getElementById('demoBanner');
  if (!bar) { bar = document.createElement('div'); bar.id = 'demoBanner'; document.body.appendChild(bar); }
  bar.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:9999;'
    + 'background:linear-gradient(135deg,#5b6cff,#b15bff);color:#fff;font-size:13px;'
    + 'padding:12px 16px;line-height:1.8;box-shadow:0 2px 14px rgba(0,0,0,.2)';
  bar.innerHTML = '<div style="max-width:980px;margin:0 auto;display:flex;gap:14px;align-items:flex-start;flex-wrap:wrap">'
    + '<div style="flex:1;min-width:260px">'
    + '<b>🎬 GitHub Pages 静态演示模式</b> —— 界面已完整渲染；真实的链接解析 / 下载 / Cookie 登录需本地运行 Python 后端。<br/>'
    + '① 本地运行：<code style="background:rgba(255,255,255,.18);padding:1px 6px;border-radius:5px">python app.py</code> 后访问 '
    + '<code style="background:rgba(255,255,255,.18);padding:1px 6px;border-radius:5px">http://127.0.0.1:8000/</code>；'
    + '② 或在本页 URL 后加 <code style="background:rgba(255,255,255,.18);padding:1px 6px;border-radius:5px">?api=https://你的后端地址</code>。'
    + '</div>'
    + '<button id="demoLoadBtn" style="background:#fff;color:#5b6cff;border:none;border-radius:10px;'
    + 'padding:9px 18px;font-size:13px;font-weight:600;cursor:pointer;margin-top:2px;white-space:nowrap">🎬 加载演示数据</button>'
    + '</div>';
  const btn = bar.querySelector('#demoLoadBtn');
  if (btn) btn.onclick = function () { loadDemoData(); btn.textContent = '✓ 演示数据已加载'; };
}

function setDemoMode() {
  if (DEMO) return;
  DEMO = true;
  wrapFetchForDemo();
  showDemoBanner();
  document.body.style.paddingTop = '74px';
  if (typeof loadCookies === 'function') loadCookies();
  if (typeof checkYtdlp === 'function') checkYtdlp();
}

// 后端连通性自检：不可达则进入演示模式（GitHub Pages / 未启动后端）
(function () {
  const au = (typeof window.assetUrl === 'function') ? window.assetUrl : function (p) { return p; };
  try {
    fetch(au('/api/health')).then(function (r) { return r.ok ? null : Promise.reject(); })
      .catch(function () { setTimeout(setDemoMode, 0); });
  } catch (e) { setTimeout(setDemoMode, 0); }
})();

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const escAttr = (s) => esc(s).replace(/"/g, '&quot;');


const themeBtn = $('themeBtn'), html = document.documentElement;
const themeLabels = { light: '☀ 明亮', dark: '🌙 暗黑', system: '🌗 跟随系统' };
function applyTheme(t) {
  html.setAttribute('data-theme', t === 'system'
    ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : t);
  themeBtn.textContent = themeLabels[t];
}
let theme = localStorage.getItem('theme') || 'system';
applyTheme(theme);
themeBtn.onclick = () => {
  theme = theme === 'system' ? 'light' : theme === 'light' ? 'dark' : 'system';
  localStorage.setItem('theme', theme); applyTheme(theme);
};
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change',
  () => { if (theme === 'system') applyTheme('system'); });


const NP = (() => {
  const el = $('topbar');
  let live = 0, timer = null;
  return {
    start() {
      live++;
      if (live > 1) return;
      clearInterval(timer);
      el.style.opacity = '1'; el.style.width = '10%';
      let p = 10;
      timer = setInterval(() => { p += Math.max(.35, (92 - p) * .075); if (p > 92) p = 92; el.style.width = p + '%'; }, 200);
    },
    done() {
      live = Math.max(0, live - 1);
      if (live) return;
      clearInterval(timer); el.style.width = '100%';
      setTimeout(() => { el.style.opacity = '0'; setTimeout(() => { el.style.width = '0'; }, 340); }, 200);
    }
  };
})();


function busy(el, text) {
  if (!el || el.dataset.busy === '1') return false;
  el.dataset.busy = '1';
  el.dataset.prev = el.innerHTML;
  el.disabled = true;
  const light = !el.classList.contains('primary') && !el.classList.contains('run');
  el.innerHTML = '<span class="spinner' + (light ? ' alt' : '') + '"></span>' + (text || '处理中…');
  return true;
}
function idle(el) {
  if (!el) return;
  el.disabled = false;
  if (el.dataset.prev !== undefined) el.innerHTML = el.dataset.prev;
  delete el.dataset.busy; delete el.dataset.prev;
}

async function withBusy(el, text, fn) {
  if (el && !busy(el, text)) return;
  NP.start();
  try { return await fn(); }
  finally { idle(el); NP.done(); }
}

async function postJSON(url, body) {
  const r = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
  });
  return await r.json();
}




const LP_MS = 450;
function bindLongPress(el, onLongPress) {
  let timer = null, fired = false;
  const start = (e) => {
    fired = false;
    timer = setTimeout(() => { fired = true; el.classList.add('lp-active'); onLongPress(); }, LP_MS);
  };
  const cancel = () => { if (timer) { clearTimeout(timer); timer = null; } el.classList.remove('lp-active'); };
  el.addEventListener('pointerdown', start);
  el.addEventListener('pointerup', cancel);
  el.addEventListener('pointerleave', cancel);
  el.addEventListener('pointercancel', cancel);

  el.addEventListener('click', (e) => {
    if (fired) { e.stopPropagation(); e.preventDefault(); fired = false; el.classList.remove('lp-active'); }
  }, true);
}





let SUPPRESS_NEXT_CLICK = false;
function setSel(id, rowEl, val) {
  if (val) DL_SEL.add(id); else DL_SEL.delete(id);
  const cb = rowEl.querySelector('input[type=checkbox][data-id]');
  if (cb) cb.checked = val;
  rowEl.classList.toggle('sel', val);
}










let _dlDrag = null;
let _dlDragReady = false;
function _dlDragInit() {
  if (_dlDragReady) return; _dlDragReady = true;
  const interactive = (t) => t.closest('button, input, a, .dl-cb, [data-action], [data-detail-id]');
  const onDown = (e) => {
    SUPPRESS_NEXT_CLICK = false;
    if (e.button !== undefined && e.button !== 0) return;
    const card = e.target.closest('.dl-card, .dl-row');
    if (!card || !card.dataset.histId) return;
    if (interactive(e.target)) return;
    _dlDrag = { pid: e.pointerId, startX: e.clientX, startY: e.clientY,
                baseSel: !DL_SEL.has(card.dataset.histId), dragging: false,
                longFired: false, timer: null, srcEl: card };
    _dlDrag.timer = setTimeout(() => {
      if (!_dlDrag) return;
      _dlDrag.longFired = true;
      card.classList.add('lp-active');
      setSel(card.dataset.histId, card, _dlDrag.baseSel);
    }, LP_MS);
  };
  const onMove = (e) => {
    if (!_dlDrag) return;
    if (_dlDrag.pid !== null && e.pointerId !== _dlDrag.pid) return;
    if (_dlDrag.longFired) { e.preventDefault(); return; }
    const dx = e.clientX - _dlDrag.startX, dy = e.clientY - _dlDrag.startY;
    if (!_dlDrag.dragging && (dx * dx + dy * dy) > 64) {
      clearTimeout(_dlDrag.timer); _dlDrag.dragging = true; _dlDrag.srcEl.classList.add('lp-active');
    }
    if (_dlDrag.dragging) {
      e.preventDefault();
      const over = document.elementFromPoint(e.clientX, e.clientY);
      const c = over && over.closest('.dl-card, .dl-row');
      if (c && c.dataset && c.dataset.histId) setSel(c.dataset.histId, c, _dlDrag.baseSel);
    }
  };
  const onUp = () => {
    if (!_dlDrag) return;
    clearTimeout(_dlDrag.timer);
    if (_dlDrag.srcEl) _dlDrag.srcEl.classList.remove('lp-active');
    const wasDragging = _dlDrag.dragging, wasLong = _dlDrag.longFired;
    _dlDrag = null;
    if (wasDragging || wasLong) SUPPRESS_NEXT_CLICK = true;
  };
  const onClick = (e) => {
    if (SUPPRESS_NEXT_CLICK) {
      SUPPRESS_NEXT_CLICK = false; e.stopPropagation(); e.preventDefault(); return;
    }
  };
  document.addEventListener('pointerdown', onDown, true);
  document.addEventListener('pointermove', onMove, true);
  document.addEventListener('pointerup', onUp, true);
  document.addEventListener('pointercancel', onUp, true);
  document.addEventListener('click', onClick, true);
}


function toast(msg, kind) {
  let el = $('appToast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'appToast';
    el.style.cssText = 'position:fixed;left:50%;bottom:28px;transform:translateX(-50%);'
      + 'background:rgba(20,20,28,.92);color:#fff;padding:10px 16px;border-radius:10px;'
      + 'font-size:13px;z-index:9999;box-shadow:0 8px 30px rgba(0,0,0,.35);max-width:80vw;';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.style.borderLeft = kind === 'warn' ? '4px solid #f5a623' : '4px solid #3ddc97';
  el.style.display = 'block';
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.style.display = 'none'; }, 3200);
}



async function openLink(platform, url) {
  if (!url) return;
  try {
    const r = await fetch('/api/open?platform=' + encodeURIComponent(platform || '')
      + '&url=' + encodeURIComponent(url));
    const d = await r.json();
    if (d && d.ok) {
      toast(d.message || '已在已登录浏览器窗口打开');
      return;
    }

    window.open(url, '_blank', 'noopener');
    toast('已在系统浏览器打开（未走登录态，部分平台可能受限）', 'warn');
  } catch (e) {
    window.open(url, '_blank', 'noopener');
    toast('已在系统浏览器打开', 'warn');
  }
}


let CURRENT_MODE = 'search';
function setMode(m) {
  CURRENT_MODE = m;
  document.querySelectorAll('.mode-tab').forEach(t => t.classList.toggle('active', t.dataset.mode === m));
  $('modeSearch').classList.toggle('hidden', m !== 'search');
  $('modeDownload').classList.toggle('hidden', m !== 'download');
  $('modeCookie').classList.toggle('hidden', m !== 'cookie');
  $('modeDownloads').classList.toggle('hidden', m !== 'downloads');
  if (m === 'downloads') loadDownloads();
  localStorage.setItem('mode', m);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
document.querySelectorAll('.mode-tab').forEach(t => t.onclick = () => setMode(t.dataset.mode));

document.addEventListener('click', (e) => {
  const t = e.target.closest && e.target.closest('.goto-dl');
  if (t) { setMode('downloads'); reloadHistory(); }
});


let PW_OK = false, loginTimer = null, CK_LIST = [], detectModalShown = false;

function ckNotice(text, kind) {
  const el = $('ckMsg');
  el.className = 'notice ' + (kind || 'info');
  el.innerHTML = text;
  el.classList.remove('hidden');
}

function stateOf(p) {
  if (!p.configured) return { dot: 'bad', text: '未配置' };
  if (p.expired) return { dot: 'bad', text: 'Cookie 已过期' };
  if (p.logged_in) return { dot: 'ok', text: '已登录' };
  return { dot: 'warn', text: '匿名 Cookie' };
}

function renderCookies(list) {
  CK_LIST = list || [];
  $('ckGrid').innerHTML = CK_LIST.map(p => {
    const s = stateOf(p);
    return `<div class="ck-item" data-card="${p.platform}">
      <div class="ck-head">
        <span class="ck-name"><span class="dot ${s.dot}"></span>${esc(p.label)}</span>
        <span class="ck-state">${s.text}</span>
      </div>
      <div class="ck-meta">
        字段 ${p.count} 条${p.device_ready ? ' · 设备指纹完整' : ''}<br/>
        有效期：${esc(p.expires_text)}<br/>
        ${p.saved_at ? '保存于 ' + esc(p.saved_at) : '尚未保存任何 Cookie'}
        ${p.profile_ready ? '<br/>浏览器配置已就绪（免重复登录）' : ''}
      </div>
      <div class="ck-acts">
        <button class="mini" data-act="login" data-p="${p.platform}">登录获取</button>
        <button class="mini" data-act="probe" data-p="${p.platform}" ${p.configured ? '' : 'disabled'}>检测</button>
        <button class="mini" data-act="export" data-p="${p.platform}" ${p.configured ? '' : 'disabled'}>导出 txt</button>
        <button class="mini danger" data-act="clear" data-p="${p.platform}" ${p.configured ? '' : 'disabled'}>清除</button>
      </div>
    </div>`;
  }).join('');
  $('ckGrid').querySelectorAll('button[data-act]').forEach(b => {
    b.onclick = () => cookieAction(b.dataset.act, b.dataset.p, b);
  });
  renderPlatformOptions(CK_LIST);
  renderCkBar(CK_LIST);
}


function renderCkBar(list) {
  const cfg = list.filter(p => p.configured);
  const logged = cfg.filter(p => p.logged_in && !p.expired).length;
  $('tabCkBadge').textContent = cfg.length ? `${logged}/${cfg.length}` : '0';
  const chips = cfg.length
    ? cfg.map(p => {
        const s = stateOf(p);
        return `<span class="ck-chip"><span class="dot ${s.dot}"></span>${esc(p.label)} · ${s.text}</span>`;
      }).join('')
    : '<span class="ck-chip muted">尚未配置任何 Cookie，受限内容可能无法采集/下载</span>';
  const html = `<span class="ck-bar-t">🔑 全局共享 Cookie</span>
    <div class="ck-chips">${chips}</div>
    <button class="mini" data-goto="1">前往管理 →</button>`;
  ['ckBar1', 'ckBar2'].forEach(id => {
    const el = $(id); if (!el) return;
    el.innerHTML = html;
    el.querySelector('[data-goto]').onclick = () => setMode('cookie');
  });
}

async function loadCookies() {
  if (DEMO) {
    PW_OK = false;
    $('pwTag').textContent = '演示模式 · 本地后端可用 Playwright';
    renderCookies(DEMO_PLATFORMS);
    checkYtdlp();
    return;
  }
  try {
    const r = await fetch('/api/cookies'); const d = await r.json();
    PW_OK = !!d.playwright;
    $('pwTag').textContent = PW_OK ? 'Playwright 可用 · 支持一键登录' : 'Playwright 未安装 · 仅支持粘贴导入';
    renderCookies(d.platforms || []);
    checkYtdlp();
  } catch (e) { ckNotice('加载 Cookie 状态失败：' + e.message, 'bad'); }
}

const PLAT_ORDER = ['bilibili','douyin','kuaishou','weibo','xiaohongshu','youtube'];
function renderPlatformOptions(list) {
  const items = list.slice().sort((a, b) =>
    PLAT_ORDER.indexOf(a.platform) - PLAT_ORDER.indexOf(b.platform));
  const prevChecked = new Set(Array.from($('platChecks').querySelectorAll('input:checked')).map(c => c.value));
  $('platChecks').innerHTML = items.map(p => {
    const checked = (prevChecked.size ? prevChecked.has(p.platform)
      : (p.platform === 'bilibili' || p.logged_in)) ? 'checked' : '';
    return `<label class="check"><input type="checkbox" value="${p.platform}" ${checked}/> ${esc(p.label)}</label>`;
  }).join('');
  const fp = $('filterPlatform'), keepFp = fp.value;
  fp.innerHTML = '<option value="">全部</option>' +
    items.map(p => `<option value="${p.platform}">${esc(p.label)}</option>`).join('');
  fp.value = keepFp;
  const yd = $('ydPlatform'), keepYd = yd.value;
  yd.innerHTML = '<option value="">自动匹配链接域名</option>' +
    items.filter(p => p.configured).map(p =>
      `<option value="${p.platform}">${esc(p.label)}（${p.logged_in ? '已登录' : '匿名'}）</option>`).join('');
  yd.value = keepYd;
}

function cardBusy(platform, on) {
  const c = $('ckGrid').querySelector(`[data-card="${platform}"]`);
  if (c) c.classList.toggle('busy', !!on);
}

async function cookieAction(act, platform, btn) {
  if (act === 'export') {
    await withBusy(btn, '导出中', async () => {
      window.open('/api/cookies/export?platform=' + encodeURIComponent(platform), '_blank');
      await new Promise(r => setTimeout(r, 550));
    });
    return;
  }
  if (act === 'clear') {
    if (!confirm('确定清除 ' + platform + ' 的 Cookie？此操作不可撤销。')) return;
    cardBusy(platform, true);
    await withBusy(btn, '清除中', async () => {
      try {
        const d = await postJSON('/api/cookies/clear', { platform });
        renderCookies(d.platforms || []);
        ckNotice(d.ok ? '已清除 ' + platform + ' 的 Cookie。' : (d.error || '清除失败'), d.ok ? 'info' : 'bad');
      } catch (e) { ckNotice('清除异常：' + e.message, 'bad'); }
    });
    cardBusy(platform, false);
    return;
  }
  if (act === 'probe') {
    cardBusy(platform, true);
    ckNotice('<span class="spinner alt"></span>正在对 <b>' + esc(platform) + '</b> 发起真实请求探活，请稍候…', 'info');
    await withBusy(btn, '检测中', async () => {
      try {
        const d = await postJSON('/api/cookies/probe', { platform });
        const kind = d.status === 'valid' ? 'ok' : d.status === 'invalid' ? 'bad' : 'warn';
        ckNotice('<b>' + esc(platform) + ' 检测结果（' + esc(d.status) + '）：</b>' + esc(d.message || ''), kind);
        if (d.platforms) renderCookies(d.platforms);
      } catch (e) { ckNotice('检测异常：' + e.message, 'bad'); }
    });
    cardBusy(platform, false);
    return;
  }
  if (act === 'login') {
    const url = platform.startsWith('custom_') ? 'https://' + platform.slice('custom_'.length) : null;
    startLogin(platform, btn, url);
  }
}

async function startLogin(platform, btn, url) {
  if (!PW_OK) {
    ckNotice('未安装 Playwright，无法一键登录。请先执行：<br/><code>pip install playwright</code><br/>' +
      '<code>playwright install chromium</code><br/>装好后重启服务即可。' +
      '（也可以直接用左侧「粘贴 Cookie」方式，效果一样）', 'warn');
    return;
  }
  cardBusy(platform, true);
  const ok = await withBusy(btn, '启动中', async () => {
    const payload = { platform, timeout: parseInt($('ckTimeout').value) || 180 };
    // 远程/服务器模式：浏览器在服务端无头运行，登录页截图随状态返回供扫码（本地弹窗模式不受影响）
    if (!/^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname)) payload.headless = true;
    if (url) payload.url = url;
    const d = await postJSON('/api/cookies/login', payload);
    if (!d.ok) { ckNotice(d.error || '启动失败', 'bad'); return false; }
    ckNotice('<span class="spinner alt"></span>' + esc(d.message || '已启动浏览器…'), 'info');
    return true;
  });
  if (!ok) { cardBusy(platform, false); return; }



  $('ckCaptureNow').classList.remove('hidden');
  $('ckCaptureNow').disabled = false;
  const stopBtn = $('ckStopAll'); if (stopBtn) stopBtn.classList.remove('hidden');
  $('ckCaptureNow').textContent = '✅ 我已完成登录，现在抓取 Cookie';
  detectModalShown = false;


  NP.start();
  if (loginTimer) clearInterval(loginTimer);
  const stop = (msg, kind) => {
    clearInterval(loginTimer); loginTimer = null;
    cardBusy(platform, false); NP.done();
    $('ckCaptureNow').classList.add('hidden');
    hideDetectModal();
    const shotEl = $('ckShotWrap'); if (shotEl) shotEl.classList.add('hidden');
    const stopBtn = $('ckStopAll'); if (stopBtn) stopBtn.classList.add('hidden');
    if (msg) ckNotice(esc(msg), kind || 'warn');
    loadCookies();
  };
  loginTimer = setInterval(async () => {
    try {
      const s = await (await fetch('/api/cookies/login_status')).json();
      const shotEl = $('ckShotWrap'), shotImg = $('ckLoginShot');
      if (shotEl && shotImg) {
        if (s.running && s.screenshot) {
          shotImg.src = 'data:image/jpeg;base64,' + s.screenshot;
          shotEl.classList.remove('hidden');
          const PL = { bilibili: 'Bilibili', douyin: '抖音', kuaishou: '快手', weibo: '微博', xiaohongshu: '小红书', youtube: 'YouTube' };
          const pl = $('ckShotPlat'); if (pl) pl.textContent = PL[s.platform] || s.platform || '';
        }
        else if (!s.running) shotEl.classList.add('hidden');
      }
      if (s.running) {

        if (s.login_detected) {
          const m = (s.message || '').match(/共\s*(\d+)\s*条/);
          showDetectModal(s.platform || platform, m ? m[1] : '');
        }
        ckNotice('<span class="spinner alt"></span>' + esc(s.message || '等待登录…') +
          '　登录完成后可点【✅ 我已完成登录，现在抓取 Cookie】或弹窗【确认保存】。', 'info');
      } else {
        stop(s.message || '已结束', s.ok ? 'ok' : 'warn');
      }
    } catch (e) { stop('登录状态轮询中断：' + e.message, 'bad'); }
  }, 1500);
}


async function doCapture() {
  const btn = $('ckCaptureNow');
  btn.disabled = true; btn.textContent = '⏳ 正在抓取…';
  try {
    const d = await postJSON('/api/cookies/login_capture', {});
    if (!d.ok) {
      ckNotice(esc(d.error || '抓取失败'), 'bad');
      btn.disabled = false; btn.textContent = '✅ 我已完成登录，现在抓取 Cookie';
    } else {
      ckNotice('<span class="spinner alt"></span>' + esc(d.message || '正在抓取…'), 'info');
    }
  } catch (err) {
    ckNotice('抓取请求异常：' + err.message, 'bad');
    btn.disabled = false; btn.textContent = '✅ 我已完成登录，现在抓取 Cookie';
  }
}
$('ckCaptureNow').onclick = () => doCapture();

$('ckStopAll').onclick = async () => {
  try {
    const d = await postJSON('/api/cookies/login_stop', {});
    ckNotice(esc((d && d.message) || (d && d.error) || '已请求停止登录'), (d && d.ok) ? 'info' : 'warn');
  } catch (err) { ckNotice('停止请求异常：' + err.message, 'bad'); }
  const stb = $('ckStopAll'); if (stb) stb.classList.add('hidden');
  loadCookies();
};
$('ckShotClose').onclick = async () => {
  try { await postJSON('/api/cookies/login_stop', {}); } catch (err) {}
  const sw = $('ckShotWrap'); if (sw) sw.classList.add('hidden');
  const stb = $('ckStopAll'); if (stb) stb.classList.add('hidden');
  loadCookies();
};
$('ckLoginShot').onclick = async (e) => {
  const img = e.currentTarget;
  if (!img.getBoundingClientRect) return;
  const r = img.getBoundingClientRect();
  if (!r.width || !r.height) return;
  const x = Math.round((e.clientX - r.left) / r.width * 1000);
  const y = Math.round((e.clientY - r.top) / r.height * 1000);
  try {
    const d = await postJSON('/api/cookies/login_click', { x, y });
    if (d && !d.ok) ckNotice(esc(d.error || '点击失败'), 'warn');
  } catch (err) { ckNotice('点击发送失败：' + err.message, 'bad'); }
};
async function sendShotInput() {
  const inp = $('ckShotInput');
  if (!inp) return;
  const text = inp.value.trim();
  if (!text) return;
  try {
    const d = await postJSON('/api/cookies/login_input', { text });
    if (d && !d.ok) { ckNotice(esc(d.error || '发送失败'), 'warn'); return; }
    inp.value = '';
    ckNotice('已发送到浏览器：' + esc(text.slice(0, 24)) + (text.length > 24 ? '…' : ''), 'info');
  } catch (err) { ckNotice('发送失败：' + err.message, 'bad'); }
}
const shotSend = $('ckShotSend'); if (shotSend) shotSend.onclick = () => sendShotInput();
const shotInp = $('ckShotInput'); if (shotInp) shotInp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); sendShotInput(); } });


function showDetectModal(plat, count) {
  if (detectModalShown) return;
  detectModalShown = true;
  $('ckDetectPlat').textContent = plat || '该网站';
  $('ckDetectCount').textContent = count || 0;
  $('ckDetectOverlay').classList.remove('hidden');
}
function hideDetectModal() { $('ckDetectOverlay').classList.add('hidden'); }
$('ckDetectConfirm').onclick = () => { hideDetectModal(); doCapture(); };
$('ckDetectLater').onclick = () => { hideDetectModal(); };
$('ckDetectClose').onclick = () => { hideDetectModal(); };
$('ckDetectOverlay').addEventListener('click', (e) => {
  if (e.target === $('ckDetectOverlay')) hideDetectModal();
});

$('ckLogin').onclick = (e) =>
  startLogin($('ckPlatform').value === 'auto' ? 'douyin' : $('ckPlatform').value, e.currentTarget);

$('ckUrlLogin').onclick = (e) => {
  const url = $('ckUrl').value.trim();
  if (!url) { ckNotice('请输入要登录的网站网址。', 'warn'); return; }
  if (!/^https?:\/\//i.test(url)) { ckNotice('网址需以 http:// 或 https:// 开头。', 'warn'); return; }
  startLogin('auto', e.currentTarget, url);
};

$('ckRefresh').onclick = (e) => withBusy(e.currentTarget, '刷新中', async () => {
  await loadCookies(); ckNotice('已刷新全部平台 Cookie 状态。', 'info');
});

$('ckImport').onclick = (e) => withBusy(e.currentTarget, '解析中…', async () => {
  const raw = $('ckRaw').value.trim();
  if (!raw) { ckNotice('请先粘贴 Cookie 内容。', 'warn'); return; }
  try {
    const d = await postJSON('/api/cookies/import', { raw, platform: $('ckPlatform').value });
    if (!d.ok) { ckNotice(esc(d.error || '解析失败'), 'bad'); return; }
    let msg = `<b>✅ 已保存 ${esc(d.label)} Cookie</b>：识别格式「${esc(d.format)}」，共 ${d.count} 条字段，` +
      (d.logged_in ? '<b>含登录态</b>' : '匿名态') + `，有效期 ${esc(d.expires_text)}。<br/>` +
      `已同步生成 yt-dlp 可用文件：<code>${esc((d.netscape_file || '').split(/[\\/]/).pop())}</code>（下载功能自动复用）`;
    if (d.warning) msg += '<br/>⚠ ' + esc(d.warning);
    ckNotice(msg, d.logged_in ? 'ok' : 'warn');
    $('ckRaw').value = '';
    renderCookies(d.platforms || []);
  } catch (e2) { ckNotice('请求异常：' + e2.message, 'bad'); }
});


const SK_DOT = { pass: 'ok', warn: 'warn', fail: 'bad', skip: 'skip' };
function renderSelfCheck(report) {
  const wrap = $('selfCheckReport');
  const summary = `<div class="sk-summary">${esc(report.headline || '')}</div>`;
  const rows = (report.platforms || []).map(p => {
    const g = p.grade || (p.configured ? 'warn' : 'skip');
    const probe = p.probe;
    let pill = '';
    if (!p.configured) pill = '<span class="pill sk-missing">未配置</span>';
    else if (probe && probe.status) pill = `<span class="pill sk-${esc(probe.status)}">${esc(probe.status)}</span>`;
    const bad = (p.checks || []).find(c => c.grade === 'fail') || (p.checks || []).find(c => c.grade === 'warn');
    const reason = (bad && bad.detail) || (probe && probe.message) || '无需处理';
    return `<div class="sk-row">
      <span class="dot ${SK_DOT[g] || 'skip'}"></span>
      <span class="sk-name">${esc(p.label)}</span>
      ${pill}
      <span class="sk-reason">${esc(reason)}</span>
    </div>`;
  }).join('');
  const advice = (report.advice && report.advice.length)
    ? `<div class="sk-advice"><b>建议处理：</b><ul>${report.advice.map(a => `<li>${esc(a)}</li>`).join('')}</ul></div>` : '';
  wrap.innerHTML = summary + rows + advice;
  wrap.classList.remove('hidden');
}

$('skBtn').onclick = (e) => withBusy(e.currentTarget, '自检中…（在线探活约需 10~30 秒）', async () => {
  $('skTime').textContent = '';
  $('ckGrid').querySelectorAll('.ck-item').forEach(c => c.classList.add('busy'));
  try {
    const d = await postJSON('/api/cookies/selfcheck', { platforms: [], online: $('skOnline').checked });
    if (!d.ok) { ckNotice(esc(d.error || '自检失败'), 'bad'); return; }
    renderSelfCheck(d);
    if (d.generated_at) $('skTime').textContent =
      '生成于 ' + d.generated_at + (d.online ? '（含在线探活）' : '（仅离线结构校验）');
    if (d.platforms_status) renderCookies(d.platforms_status);
  } catch (e2) { ckNotice('自检请求异常：' + e2.message, 'bad'); }
  finally { $('ckGrid').querySelectorAll('.ck-item').forEach(c => c.classList.remove('busy')); }
});


let FMT = null;
let SEL = { mode: 'av', v: '', a: '', m: '' };
let FMODE = 'preset';
let DY_DIRECT = false;

function ydMsg(text, kind) {
  const el = $('ydMsg'); el.className = 'notice ' + (kind || 'info');
  el.innerHTML = text; el.classList.remove('hidden');
}
const fmtBytes = (n) => !n ? '—'
  : n >= 1073741824 ? (n / 1073741824).toFixed(2) + ' GB'
  : n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB'
  : (n / 1024).toFixed(0) + ' KB';
const fmtRate = (k) => !k ? '—' : k >= 1000 ? (k / 1000).toFixed(2) + ' Mbps' : Math.round(k) + ' kbps';
const fmtSec = (s) => { if (!s) return '—'; s = Math.round(s); const h = Math.floor(s / 3600),
  m = Math.floor(s % 3600 / 60), x = s % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0'); };

async function checkYtdlp() {
  if (DEMO) { $('ydTag').textContent = 'yt-dlp 未连接 · 演示模式（本地运行后端后可用）'; return; }
  try {
    const d = await (await fetch('/api/ytdlp/check')).json();
    $('ydTag').textContent = d.available
      ? `yt-dlp ${d.version} · ffmpeg ${d.ffmpeg ? '可用（支持合并）' : '缺失（分轨合并会失败）'}`
      : 'yt-dlp 未安装';
    $('ydRun').disabled = !d.available;
    $('ydInfo').disabled = !d.available;
    if (!d.available) ydMsg('未检测到 yt-dlp，请先安装：<code>pip install yt-dlp</code> 后重启服务。', 'warn');
    else if (!d.ffmpeg) ydMsg('未检测到 ffmpeg：可以下载「已合成」轨，但<b>分轨合并（视频+音频）会失败</b>。' +
      '安装 ffmpeg 并加入 PATH 后重启服务即可。', 'warn');
  } catch (e) { $('ydTag').textContent = '检测失败'; }
}

async function checkAndUpdateYtdlp() {
  const btn = $('ydUpdate');
  const oldTxt = btn.textContent;
  btn.disabled = true;
  try {
    btn.textContent = '🔄 检查中…';
    const chk = await (await fetch('/api/ytdlp/update?action=check')).json();
    if (!chk.available) {
      ydMsg('未检测到 yt-dlp，无法自动更新。请先安装：<code>pip install yt-dlp</code> 后重启服务。', 'warn');
      return;
    }
    if (chk.up_to_date) {
      ydMsg(`✅ yt-dlp 已是最新版 <b>${esc(chk.current)}</b>（官网最新 ${esc(chk.latest)}），无需更新。`, 'ok');
      return;
    }
    if (!confirm(`当前 yt-dlp 为 ${chk.current}，官网最新为 ${chk.latest}。\n确定要更新到最新版吗？`)) {
      ydMsg(`当前 yt-dlp <b>${esc(chk.current)}</b> 不是最新（最新 ${esc(chk.latest)}），已取消更新。`, 'info');
      return;
    }
    btn.textContent = '⬆ 更新中…';
    ydMsg(`正在更新 yt-dlp：<b>${esc(chk.current)}</b> → <b>${esc(chk.latest)}</b>（约需数秒至数十秒）…`, 'info');
    const r = await (await fetch('/api/ytdlp/update?action=update')).json();
    if (!r.ok) {
      ydMsg('❌ 更新失败：' + esc(r.error || '未知错误') +
        (r.output ? '<pre style="white-space:pre-wrap;max-height:160px;overflow:auto;border-top:1px solid #ccc;margin-top:6px;padding-top:6px">' + esc(r.output) + '</pre>' : ''), 'bad');
      return;
    }
    await checkYtdlp();
    if (r.up_to_date) {
      ydMsg(`✅ 已更新到最新版 <b>${esc(r.current)}</b>（官网最新 ${esc(r.latest)}）。`, 'ok');
    } else {
      ydMsg(`已更新 yt-dlp：<b>${esc(r.previous)}</b> → <b>${esc(r.current)}</b>（官网最新 ${esc(r.latest)}）。` +
        (r.updated ? '' : ' 版本号未变化（可能已是该安装源的最新，或网络/缓存原因）。'), r.updated ? 'ok' : 'warn');
    }
  } catch (e) {
    ydMsg('检查 / 更新异常：' + e.message, 'bad');
  } finally {
    btn.disabled = false;
    btn.textContent = oldTxt;
  }
}


document.querySelectorAll('#ydModeSeg button').forEach(b => b.onclick = () => {
  FMODE = b.dataset.fmode;
  document.querySelectorAll('#ydModeSeg button').forEach(x => x.classList.toggle('active', x === b));
  $('ydPresetBox').classList.toggle('hidden', FMODE !== 'preset');
  $('ydFormats').classList.toggle('hidden', FMODE !== 'manual' || !FMT);
  if (FMODE === 'manual' && !FMT) {
    ydMsg('逐轨精选需要先解析：点击「🔎 解析视频信息 / 全部品质」即可列出该视频的<b>全部音视频轨</b>。', 'info');
    if ($('ydUrl').value.trim()) $('ydInfo').click();
  }
});


function rowVideo(f) {
  const res = f.height ? `${f.height}P${f.fps >= 48 ? f.fps : ''}` : (f.note || f.format_id);
  return { main: res, tags: [f.hdr ? `<span class="tag-soft hdr">${esc(f.hdr)}</span>` : '',
           f.note && f.height ? `<span class="tag-soft">${esc(f.note)}</span>` : ''].join(''),
           c1: esc(f.vcodec || '—'), c2: fmtRate(f.vbr || f.tbr), c3: fmtBytes(f.filesize) + (f.approx ? '≈' : ''),
           c4: esc(f.ext) };
}
function rowAudio(f) {
  return { main: (f.abr ? Math.round(f.abr) + ' kbps' : (f.note || f.format_id)),
           tags: [f.lang ? `<span class="tag-soft">${esc(f.lang)}</span>` : '',
                  f.note && f.abr ? `<span class="tag-soft">${esc(f.note)}</span>` : ''].join(''),
           c1: esc(f.acodec || '—'), c2: f.asr ? (f.asr / 1000).toFixed(1) + ' kHz' : '—',
           c3: fmtBytes(f.filesize) + (f.approx ? '≈' : ''), c4: esc(f.ext) };
}
function rowMuxed(f) {
  const res = f.height ? `${f.height}P${f.fps >= 48 ? f.fps : ''}` : (f.note || f.format_id);
  return { main: res, tags: `<span class="tag-soft">已含音频</span>`,
           c1: esc((f.vcodec || '?') + '/' + (f.acodec || '?')), c2: fmtRate(f.tbr),
           c3: fmtBytes(f.filesize) + (f.approx ? '≈' : ''), c4: esc(f.ext) };
}

function renderList(kind, arr, name, mapper) {
  if (!arr.length) return '';
  const rows = arr.map(f => {
    const r = mapper(f);
    const on = (kind === 'v' && SEL.v === f.format_id && SEL.mode === 'av')
            || (kind === 'a' && SEL.a === f.format_id && SEL.mode === 'av')
            || (kind === 'm' && SEL.m === f.format_id && SEL.mode === 'muxed');
    return `<label class="fmt-row ${on ? 'on' : ''}" data-k="${kind}" data-id="${escAttr(f.format_id)}">
      <input type="radio" name="${name}" ${on ? 'checked' : ''} />
      <span class="fmt-main">${esc(r.main)} <span class="id">${esc(f.format_id)}</span>${r.tags}</span>
      <span class="fmt-sub">${r.c1}</span>
      <span class="fmt-sub c-hide">${r.c2}</span>
      <span class="fmt-sub">${r.c3}</span>
      <span class="fmt-sub c-hide">${r.c4}</span>
    </label>`;
  }).join('');
  return `<div class="fmt-list ${arr.length > 6 ? 'scroll' : ''}">${rows}</div>`;
}

function renderFormats() {
  if (!FMT) return;
  const box = $('ydFormats');
  const blocks = [];
  if (FMT.video.length) blocks.push(`<div class="fmt-block">
    <div class="fmt-h">📹 视频轨 <span class="n">${FMT.video.length} 个 · 仅画面，需搭配音频轨合并</span></div>
    ${renderList('v', FMT.video, 'fv', rowVideo)}</div>`);
  if (FMT.audio.length) blocks.push(`<div class="fmt-block">
    <div class="fmt-h">🎵 音频轨 <span class="n">${FMT.audio.length} 个</span></div>
    ${renderList('a', FMT.audio, 'fa', rowAudio)}</div>`);
  if (FMT.muxed.length) blocks.push(`<div class="fmt-block">
    <div class="fmt-h">🎬 已合成（音视频一体，无需 ffmpeg）<span class="n">${FMT.muxed.length} 个</span></div>
    ${renderList('m', FMT.muxed, 'fm', rowMuxed)}</div>`);
  if (!blocks.length) blocks.push('<div class="notice warn">该链接未返回任何可下载轨道。</div>');
  box.innerHTML = blocks.join('') + `
    <div class="fmt-sum">
      <div class="grow" id="fmtSum"></div>
      <div>
        <select id="ydContainer" title="分轨合并后的容器格式">
          <option value="mp4">合并为 MP4</option>
          <option value="mkv">合并为 MKV</option>
        </select>
      </div>
      <button class="mini" id="fmtReset">↺ 恢复推荐最佳</button>
    </div>`;
  box.querySelectorAll('.fmt-row').forEach(r => r.onclick = () => {
    const k = r.dataset.k, id = r.dataset.id;
    if (k === 'm') { SEL.mode = 'muxed'; SEL.m = id; }
    else { SEL.mode = 'av'; SEL.m = ''; SEL[k] = (SEL[k] === id && k === 'a') ? '' : id; }
    renderFormats();
  });
  $('fmtReset').onclick = () => { pickRecommend(); renderFormats(); };
  box.classList.remove('hidden');
  updateSum();
}

function currentFormatId() {
  if (!FMT) return '';
  if (SEL.mode === 'muxed') return SEL.m || '';
  if (SEL.v && SEL.a) return SEL.v + '+' + SEL.a;
  return SEL.v || SEL.a || '';
}

function updateSum() {
  const el = $('fmtSum'); if (!el) return;
  const find = (arr, id) => arr.find(x => x.format_id === id);
  const fid = currentFormatId();
  if (!fid) { el.innerHTML = '<b>未选择</b>　请在上方点选想要的轨道（视频轨 + 音频轨 会自动合并）。'; return; }
  let size = 0, desc = [];
  if (SEL.mode === 'muxed') {
    const m = find(FMT.muxed, SEL.m);
    if (m) { size = m.filesize; desc.push(`${m.height ? m.height + 'P' : m.note} ${m.ext}（已含音频，直接落盘）`); }
  } else {
    const v = find(FMT.video, SEL.v), a = find(FMT.audio, SEL.a);
    if (v) { size += v.filesize; desc.push(`视频 ${v.height ? v.height + 'P' + (v.fps >= 48 ? v.fps : '') : v.note} ${v.vcodec}`); }
    if (a) { size += a.filesize; desc.push(`音频 ${a.abr ? Math.round(a.abr) + 'kbps' : a.note} ${a.acodec}`); }
  }
  const merge = fid.includes('+');
  const cont = $('ydContainer') ? $('ydContainer').value.toUpperCase() : 'MP4';
  el.innerHTML = `已选 <b>${esc(fid)}</b>　${esc(desc.join(' ＋ '))}<br/>` +
    `预计体积 <b>${fmtBytes(size)}</b>` +
    (merge ? `　·　将由 ffmpeg <b>自动合并为 ${cont}</b>` +
      (FMT.ffmpeg ? '' : '　<span style="color:var(--bad)">（当前未检测到 ffmpeg，合并会失败）</span>')
     : (SEL.mode === 'av' && SEL.v && !SEL.a ? '　·　仅视频轨（无声音）'
        : SEL.mode === 'av' && !SEL.v && SEL.a ? '　·　仅音频轨' : '　·　单文件直接下载'));
}

function pickRecommend() {
  if (!FMT) return;
  const r = FMT.recommend || {};
  if (FMT.video.length && FMT.audio.length) { SEL = { mode: 'av', v: r.video || '', a: r.audio || '', m: '' }; }
  else if (FMT.muxed.length) { SEL = { mode: 'muxed', v: '', a: '', m: r.muxed || '' }; }
  else if (FMT.video.length) { SEL = { mode: 'av', v: r.video || '', a: '', m: '' }; }
  else if (FMT.audio.length) { SEL = { mode: 'av', v: '', a: r.audio || '', m: '' }; }
}

$('ydInfo').onclick = (e) => withBusy(e.currentTarget, '解析中…（首次可能 10~30 秒）', async () => {
  if (DEMO) { loadDemoData(); toast('演示模式：已加载示例；真实解析需本地运行后端', 'warn'); return; }
  const url = $('ydUrl').value.trim();
  if (!url) { ydMsg('请先粘贴视频链接', 'warn'); return; }
  $('ydFormats').classList.add('hidden');
  try {
    const d = await postJSON('/api/download/info', { url, platform: $('ydPlatform').value });
    if (!d.ok) { ydMsg('解析失败：' + esc(d.error || '未知'), 'bad'); return; }
    FMT = d;
    DY_DIRECT = false;
    if (d.media_unavailable) {
      $('ydInfoBox').innerHTML = `<div class="yd-info">
        ${d.thumbnail ? `<img src="${assetUrl('/api/img?u=') + encodeURIComponent(d.thumbnail)}" onerror="this.style.display='none'" alt=""/>` : ''}
        <div>
          <div class="t">${esc(d.title || '(无标题)')}</div>
          <div class="ck-meta">
            时长 ${fmtSec(d.duration)} · 来源 ${esc(d.extractor || d.platform || '—')}
            ${d.uploader ? ' · 作者 ' + esc(d.uploader) : ''}${d.likes ? ' · 点赞 ' + esc(fmtNum(d.likes)) : ''}${d.views ? ' · 播放 ' + esc(fmtNum(d.views)) : ''}<br/>
            <span style="color:#e8a33d;font-weight:600">⚠ 直链不可用</span> ${esc(d.note || '该平台视频直链暂无法获取')}
          </div>
        </div></div>`;
      $('ydInfoBox').classList.remove('hidden');
      ydMsg('⚠️ ' + esc(d.note || '该视频直链暂不可用，仅展示元数据。'), 'warn');
      return;
    }

    if (d.media_url && !d.media_unavailable) {
      DY_DIRECT = true;
      $('ydInfoBox').innerHTML = `<div class="yd-info">
        ${d.thumbnail ? `<img src="${assetUrl('/api/img?u=') + encodeURIComponent(d.thumbnail)}" onerror="this.style.display='none'" alt=""/>` : ''}
        <div>
          <div class="t">${esc(d.title || '(无标题)')}</div>
          <div class="ck-meta">
            时长 ${fmtSec(d.duration)} · 来源 ${esc(d.extractor || d.platform || '—')}
            ${d.uploader ? ' · 作者 ' + esc(d.uploader) : ''}${d.likes ? ' · 点赞 ' + esc(fmtNum(d.likes)) : ''}${d.views ? ' · 播放 ' + esc(fmtNum(d.views)) : ''}<br/>
            <span style="color:#43a047;font-weight:600">✅ 视频直链已就绪</span> ${esc(d.note || '将通过浏览器通道直连保存。')}
          </div>
        </div></div>`;
      $('ydInfoBox').classList.remove('hidden');
      $('ydFormats').classList.add('hidden');
      ydMsg(`✅ 抖音视频直链已通过浏览器通道获取，点「开始下载」即直连保存。`, 'ok');
      return;
    }
    $('ydInfoBox').innerHTML = `<div class="yd-info">
      ${d.thumbnail ? `<img src="${assetUrl('/api/img?u=') + encodeURIComponent(d.thumbnail)}" onerror="this.style.display='none'" alt=""/>` : ''}
      <div>
        <div class="t">${esc(d.title || '(无标题)')}</div>
        <div class="ck-meta">
          时长 ${fmtSec(d.duration)} · 来源 ${esc(d.extractor || d.platform || '—')}
          ${d.uploader ? ' · 作者 ' + esc(d.uploader) : ''}${d.likes ? ' · 点赞 ' + esc(fmtNum(d.likes)) : ''}${d.views ? ' · 播放 ' + esc(fmtNum(d.views)) : ''}<br/>
          使用 Cookie：<b>${esc(d.cookie || '无（匿名）')}</b><br/>
          共解析到 <b>${d.count}</b> 个轨道：视频 ${d.video.length} · 音频 ${d.audio.length} · 已合成 ${d.muxed.length}
        </div>
      </div></div>`;
    $('ydInfoBox').classList.remove('hidden');
    pickRecommend();

    FMODE = 'manual';
    document.querySelectorAll('#ydModeSeg button').forEach(x => x.classList.toggle('active', x.dataset.fmode === 'manual'));
    $('ydPresetBox').classList.add('hidden');
    renderFormats();
    ydMsg(`✅ 已列出全部 <b>${d.count}</b> 个音视频品质，已自动选中最佳组合，可自行更改后点「开始下载」。`, 'ok');
  } catch (e2) { ydMsg('解析异常：' + e2.message, 'bad'); }
});

document.addEventListener('change', (e) => { if (e.target && e.target.id === 'ydContainer') updateSum(); });

const PP_TEXT = { started: '正在合并音视频…', processing: '后处理中…', finished: '合并完成' };
function setProg(pct, meta) {
  $('ydProg').classList.remove('hidden');
  if (pct !== null) $('ydProgFill').style.width = Math.max(0, Math.min(100, pct)) + '%';
  if (meta !== undefined) $('ydProgMeta').innerHTML = meta;
}

function handleYdEvent(ev, d) {
  const log = $('ydLog');
  if (ev === 'meta') {
    ydMsg(`<span class="spinner alt"></span>已建连 · Cookie <b>${esc(d.cookie)}</b> · 格式 <b>${esc(d.format)}</b>` +
      (d.merge ? ` · 下载完成后合并为 ${esc((d.container || 'mp4').toUpperCase())}` : '') +
      ` · yt-dlp ${esc(d.yt_dlp)} · ffmpeg ${d.ffmpeg ? '可用' : '缺失'}`, 'info');
    log.textContent = ''; log.classList.remove('hidden');
    setProg(0, '准备中…');
  } else if (ev === 'progress') {
    const pct = parseFloat(String(d.percent).replace('%', '')) || 0;
    const bad = (v) => !v || v === 'NA' || v === 'Unknown' || v === 'Unknown B/s';
    setProg(pct, `<b>${esc(d.percent || pct.toFixed(1) + '%')}</b>` +
      (d.done ? `　${fmtBytes(d.done)}${d.total ? ' / ' + fmtBytes(d.total) : ''}` : '') +
      (bad(d.speed) ? '' : `　⚡ ${esc(d.speed)}`) +
      (bad(d.eta) ? '' : `　⏳ 剩余 ${esc(d.eta)}`));
  } else if (ev === 'stage') {
    setProg(100, `<span class="spinner alt"></span>${esc(PP_TEXT[d.status] || d.status)}`);
    log.textContent += '[postprocess] ' + d.status + '\n'; log.scrollTop = log.scrollHeight;
  } else if (ev === 'log') {
    log.textContent += d.line + '\n'; log.scrollTop = log.scrollHeight;
  } else if (ev === 'done') {
    setProg(100, `<b>100%</b>　已落盘 ${fmtBytes(d.size || 0)}`);
    ydMsg((d.skipped ? `ℹ️ 该品质此前已下载过，直接复用现有文件：` : `✅ 下载完成：`) +
      `<b>${esc(d.filename)}</b>（${fmtBytes(d.size || 0)}）<br/>` +
      `<a class="link" href="${assetUrl('/downloads/') + encodeURIComponent(d.filename)}" ` +
      `download="${escAttr(d.filename)}">⬇ 保存到本地</a>　服务端路径：<code>${esc(d.path)}</code>`,
      d.skipped ? 'warn' : 'ok');
    log.textContent += '\n完成：' + d.path + '\n';
  } else if (ev === 'already') {

    setProg(0, '');
    $('ydProg').classList.add('hidden');
    ydMsg(`ℹ️ <b>${esc(d.title || '该视频')}</b> 已下载过，已跳过重复下载。` +
      `<br/><button class="btn" id="ydGotoDl">📂 前往下载列表查看</button>`, 'info');
    const gb = $('ydGotoDl');
    if (gb) gb.onclick = () => { setMode('downloads'); reloadHistory(); };
  } else if (ev === 'error') {
    $('ydProg').classList.add('hidden');
    ydMsg('❌ ' + esc(d.message), 'bad');
  }
}

$('ydRun').onclick = (e) => withBusy(e.currentTarget, '下载中…', async () => {
  if (DEMO) { loadDemoData(); toast('演示模式：已加载示例；真实下载需本地运行后端', 'warn'); return; }
  const url = $('ydUrl').value.trim();
  if (!url) { ydMsg('请先粘贴视频链接', 'warn'); return; }
  const payload = { url, platform: $('ydPlatform').value };


  payload.title = (FMT && FMT.title) || '';
  payload.author = (FMT && FMT.uploader) || '';
  payload.cover = (FMT && FMT.thumbnail) || '';
  payload.likes = (FMT && FMT.likes) || 0;
  payload.views = (FMT && FMT.views) || 0;
  payload.duration = (FMT && FMT.duration) || 0;
  payload.media_url = (FMT && FMT.media_url) || '';
  if (DY_DIRECT && payload.media_url) {

  } else if (FMODE === 'manual') {
    const fid = currentFormatId();
    if (!fid) { ydMsg('请先解析并在上方选择要下载的音/视频轨。', 'warn'); return; }
    payload.format_id = fid;
    payload.container = $('ydContainer') ? $('ydContainer').value : 'mp4';
    payload.audio_only = (SEL.mode === 'av' && !SEL.v && !!SEL.a);
  } else {
    payload.quality = $('ydQuality').value;
  }
  const log = $('ydLog'); log.textContent = ''; log.classList.remove('hidden');
  setProg(0, '连接中…');
  try {
    const resp = await fetch('/api/download', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    if (!resp.ok) { ydMsg('请求失败：HTTP ' + resp.status, 'bad'); return; }
    const reader = resp.body.getReader(), dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const m = frame.match(/^event: (.+)\ndata: ([\s\S]+)$/);
        if (m) { try { handleYdEvent(m[1], JSON.parse(m[2])); } catch (err) {} }
      }
    }
  } catch (e2) { ydMsg('下载异常：' + e2.message, 'bad'); }
});

$('ydOpenDir').onclick = () => ydMsg('下载的文件保存在服务端项目目录的 <code>downloads/</code> 下；' +
  '完成后可点上方「⬇ 保存到本地」用浏览器再存一份。', 'info');

$('ydUpdate').onclick = checkAndUpdateYtdlp;


let ALL = [], MD = '', MD_FILE = '';

function selectedPlatforms() {
  return Array.from($('platChecks').querySelectorAll('input:checked')).map(c => c.value).join(',');
}

$('runBtn').onclick = (e) => withBusy(e.currentTarget, '采集中…（实时显示结果）', async () => {
  if (DEMO) { loadDemoData(); toast('演示模式：已加载示例检索结果；真实采集需本地运行后端', 'warn'); return; }
  const body = {
    keywords: $('keywords').value.trim(),
    platforms: selectedPlatforms(),
    max_per_keyword: parseInt($('maxPer').value) || 50,
    enrich: $('enrich').checked,
    browser_search: $('browserSearch').checked,
    use_saved_cookies: $('useSaved').checked,
    resolve_text: $('resolveText').value.trim(),
    author: $('author').value.trim()
  };
  ALL = []; render();
  $('notes').classList.add('hidden');
  $('mdCard').classList.add('hidden');
  try {
    const resp = await fetch('/api/collect', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (!resp.ok) { alert('采集失败：HTTP ' + resp.status); return; }
    const reader = resp.body.getReader(), dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const m = frame.match(/^event: (.+)\ndata: ([\s\S]+)$/);
        if (m) { try { handleCollectEvent(m[1], JSON.parse(m[2])); } catch (_) {} }
      }
    }
  } catch (e2) { alert('请求异常：' + e2.message); }
});

const COLLECT_NOTES = [];
function handleCollectEvent(ev, d) {
  if (ev === 'start') {
    ALL = []; COLLECT_NOTES.length = 0;
    $('collectProgress').classList.remove('hidden');
    $('cpFill').style.width = '0%';
    $('cpText').textContent = '开始采集…';
    render();
  } else if (ev === 'result') {
    ALL.push(d);
    render();
  } else if (ev === 'enrich') {
    const it = ALL.find(x => x.id === d.id);
    if (it) Object.assign(it, d.item); else ALL.push(d.item);
    render();
  } else if (ev === 'progress') {
    let pct = 0, txt = '';
    if (d.total) { pct = Math.round((d.searched || 0) / d.total * 100); txt = `已搜索 ${d.searched || 0}/${d.total} 个任务 · 找到 ${d.found || 0} 条`; }
    else if (d.enrich_total) { pct = Math.round((d.enrich_done || 0) / d.enrich_total * 100); txt = `正在补全元数据 ${d.enrich_done || 0}/${d.enrich_total} · 共 ${d.found || 0} 条`; }
    $('cpFill').style.width = pct + '%';
    $('cpText').textContent = txt;
  } else if (ev === 'stage') {
    $('cpFill').style.width = '0%';
    $('cpText').textContent = d.stage === 'enrich' ? `开始补全元数据（${d.total || 0} 条）…` : '采集中…';
  } else if (ev === 'note') {
    COLLECT_NOTES.push(d.text);
  } else if (ev === 'done') {
    $('collectProgress').classList.add('hidden');
    MD = d.md || ''; MD_FILE = d.md_file || '';
    $('mdView').textContent = MD;
    $('mdFile').textContent = '已生成：' + MD_FILE;
    $('mdCard').classList.remove('hidden');
    const ns = (d.notes && d.notes.length) ? d.notes : COLLECT_NOTES;
    if (ns && ns.length) {
      $('notes').innerHTML = ns.map(n => '• ' + esc(n)).join('<br/>');
      $('notes').classList.remove('hidden');
    }
  } else if (ev === 'error') {
    $('collectProgress').classList.add('hidden');
    $('notes').innerHTML = '⚠ ' + esc(d.message || '采集失败');
    $('notes').className = 'notice warn';
    $('notes').classList.remove('hidden');
  }
}


function filtered() {
  const fp = $('filterPlatform').value;
  const ml = parseInt($('minLikes').value) || 0;
  const kw = $('kwFilter').value.trim().toLowerCase();
  let arr = ALL.filter(it => {
    if (fp && it.platform !== fp) return false;
    if (it.likes < ml) return false;
    if (kw && !((it.title || '').toLowerCase().includes(kw) || (it.keyword || '').toLowerCase().includes(kw))) return false;
    return true;
  });
  const field = $('sortField').value, dir = $('sortDir').value === 'asc' ? 1 : -1;
  arr.sort((a, b) => {
    let va = a[field], vb = b[field];
    if (typeof va === 'string') { va = va.toLowerCase(); vb = (vb || '').toLowerCase(); return va < vb ? -dir : va > vb ? dir : 0; }
    return ((va || 0) - (vb || 0)) * dir;
  });
  return arr;
}

function fmtNum(n) { if (!n) return '0'; if (n >= 1e8) return (n/1e8).toFixed(1)+'亿'; if (n >= 1e4) return (n/1e4).toFixed(1)+'万'; return ''+n; }
function fmtDur(s) { if (!s) return '—'; s=+s; const m=Math.floor(s/60), sec=s%60, h=Math.floor(m/60); if (h) return h+':'+String(m%60).padStart(2,'0')+':'+String(sec).padStart(2,'0'); return m+':'+String(sec).padStart(2,'0'); }
const platClass = p => ['bilibili','douyin','kuaishou','weibo','xiaohongshu','youtube'].includes(p) ? p : 'bilibili';

function render() {
  const arr = filtered();
  $('count').textContent = `共 ${arr.length} 条（原始 ${ALL.length} 条）`;
  const tb = $('tbody');
  if (!arr.length) { tb.innerHTML = '<tr><td colspan="8" class="empty">无匹配结果</td></tr>'; return; }
  tb.innerHTML = arr.map((it, i) => `
    <tr>
      <td>${i+1}</td>
      <td><span class="pill ${platClass(it.platform)}">${esc(it.platform)}</span></td>
      <td>${esc(it.title)}</td>
      <td>${esc(it.author || '—')}</td>
      <td>${fmtDur(it.duration)}</td>
      <td>${it.likes ? `<span class="num">${fmtNum(it.likes)}</span>` : '—'}</td>
      <td>${it.views ? `<span class="num">${fmtNum(it.views)}</span>` : '—'}</td>
      <td>
        <span class="row-actions">
          <span class="link" onclick="openLink('${escAttr(it.platform)}','${escAttr(it.url)}')" style="cursor:pointer">打开</span>
          <span class="link copy-row" data-url="${escAttr(it.url)}" onclick="copyText(this.dataset.url, this)" style="cursor:pointer">复制</span>
        </span>
      </td>
    </tr>`).join('');
}

['sortField','sortDir','filterPlatform','minLikes','kwFilter'].forEach(id => $(id).addEventListener('input', render));

function download(filename, content, type) {
  const blob = new Blob([content], { type: type || 'text/plain;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = filename; a.click();
  URL.revokeObjectURL(a.href);
}
$('dlMd').onclick = () => download(MD_FILE || 'links.md', MD, 'text/markdown;charset=utf-8');
$('dlCsv').onclick = () => {
  const head = 'platform,keyword,title,author,duration,likes,views,url\n';
  const rows = ALL.map(i => [i.platform,i.keyword,(i.title||'').replace(/,/g,'，'),i.author||'',i.duration,i.likes,i.views,i.url].join(',')).join('\n');
  download('links.csv', head + rows, 'text/csv;charset=utf-8');
};
$('copyMd').onclick = async () => {
  if (await safeCopy(MD)) { $('copyMd').textContent = '✓ 已复制'; setTimeout(()=>$('copyMd').textContent='⧉ 复制 MD', 1500); }
  else { alert('复制失败（浏览器限制 HTTP 页面访问剪贴板），请手动选择文本复制'); }
};


// HTTP 站点（非安全上下文）下 navigator.clipboard 不可用，回退到 execCommand 传统方案
function fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
  document.body.appendChild(ta);
  ta.focus(); ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  document.body.removeChild(ta);
  return ok;
}
function safeCopy(text) {
  return new Promise(resolve => {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(() => resolve(true)).catch(() => resolve(fallbackCopy(text)));
    } else {
      resolve(fallbackCopy(text));
    }
  });
}

async function copyText(text, btn) {
  if (!text) { toast('没有可复制的链接', 'warn'); return; }
  const ok = await safeCopy(text);
  if (ok) {
    const n = text.split('\n').filter(Boolean).length;
    if (btn) { const old = btn.textContent; btn.textContent = '✓ 已复制'; setTimeout(() => btn.textContent = old, 1500); }
    else toast('已复制 ' + n + ' 条链接');
  } else {
    alert('复制失败（浏览器限制 HTTP 页面访问剪贴板），请手动选择文本复制');
  }
}

$('dlTxt').onclick = () => download('links.txt', ALL.map(i => i.url).join('\n'), 'text/plain;charset=utf-8');
$('copyTxt').onclick = (e) => copyText(ALL.map(i => i.url).join('\n'), e.currentTarget);

$('dlTxtF').onclick = () => {
  const t = filtered().map(i => i.url).join('\n');
  if (!t) { toast('当前筛选条件下没有匹配的链接', 'warn'); return; }
  download('links_filtered.txt', t, 'text/plain;charset=utf-8');
};
$('copyTxtF').onclick = (e) => {
  const t = filtered().map(i => i.url).join('\n');
  if (!t) { toast('当前筛选条件下没有匹配的链接', 'warn'); return; }
  copyText(t, e.currentTarget);
};


let BATCH = [];
const BSEL = new Set();
let DETAIL_IDX = -1;

function batchSelected() { return BATCH.filter(e => BSEL.has(e.id)); }

function renderBatch() {
  const grid = $('batchGrid');
  if (!BATCH.length) { grid.innerHTML = ''; $('batchToolbar').style.display = 'none'; return; }
  $('batchToolbar').style.display = 'flex';
  grid.innerHTML = BATCH.map((e, i) => `
    <div class="batch-card ${BSEL.has(e.id) ? 'selected' : ''}" data-i="${i}">
      <input type="checkbox" class="batch-check" ${BSEL.has(e.id) ? 'checked' : ''} data-i="${i}" />
      ${coverImgGrid(e.thumbnail, e.cover_file, e.id)}
      ${e.duration ? `<span class="batch-dur">${fmtSec(e.duration)}</span>` : ''}
      <div class="batch-meta">
        <div class="batch-title">${esc(e.title)}</div>
      <div class="batch-sub">
        <span class="pill ${platClass(e.platform)}">${esc(e.platform)}</span>
        ${e.media_unavailable ? `<span class="pill" style="background:rgba(250,173,20,.20);color:#b26a00">⚠ 直链不可用</span>` : ''}
        ${e.playlist_type ? `<span class="pill" style="background:rgba(31,209,200,.16);color:#0a8f86">📁 ${esc(e.playlist_type)}</span>` : ''}
        ${e.is_playlist ? `<span class="pill" style="background:rgba(250,173,20,.20);color:#b26a00">整列表 ${e.playlist_count || '?'} 个</span>` : ''}
        ${e.uploader ? `<span>${esc(e.uploader)}</span>` : ''}
      </div>
      </div>
    </div>`).join('');
  grid.querySelectorAll('.batch-card').forEach(card => {
    const i = parseInt(card.dataset.i, 10);
    card.onclick = (ev) => {
      if (ev.target.classList.contains('batch-check')) return;
      openBatchDetail(i);
    };

    bindLongPress(card, () => {
      const id = BATCH[i].id;
      if (BSEL.has(id)) { BSEL.delete(id); card.classList.remove('selected'); }
      else { BSEL.add(id); card.classList.add('selected'); }
      const cb = card.querySelector('.batch-check'); if (cb) cb.checked = BSEL.has(id);
      updateBatchSel();
    });
  });
  grid.querySelectorAll('.batch-check').forEach(c => {
    c.onclick = (ev) => {
      ev.stopPropagation();
      const i = parseInt(c.dataset.i, 10), id = BATCH[i].id;
      if (c.checked) BSEL.add(id); else BSEL.delete(id);
      const card = c.closest('.batch-card');
      if (card) card.classList.toggle('selected', c.checked);
      updateBatchSel();
    };
  });
  updateBatchSel();
}

function updateBatchSel() {
  const n = batchSelected().length;
  $('batchSelCount').textContent = `已选 ${n} / ${BATCH.length}`;
  $('batchDownload').textContent = `⬇ 批量下载（${n}）`;
  $('batchDownload').disabled = n === 0;
}

$('batchParse').onclick = (e) => withBusy(e.currentTarget, '解析中…（播放列表可能稍慢）', async () => {
  if (DEMO) { loadDemoData(); toast('演示模式：已加载示例批量清单；真实解析需本地运行后端', 'warn'); return; }
  const raw = $('batchUrls').value.trim();
  if (!raw) { $('batchNotes').innerHTML = '请先粘贴播放列表链接或至少一个视频链接。'; $('batchNotes').classList.remove('hidden'); return; }
  $('batchNotes').classList.add('hidden');
  try {
    const d = await postJSON('/api/batch/info', { urls: raw, expand_playlist: $('batchExpandPlaylist').checked });
    if (!d.ok) { $('batchNotes').innerHTML = '解析失败：' + esc(d.error || '未知'); $('batchNotes').classList.remove('hidden'); return; }
    BATCH = d.items || [];
    BSEL.clear();
    BATCH.forEach(en => BSEL.add(en.id));
    renderBatch();
    $('batchNotes').innerHTML = `✅ 解析到 <b>${BATCH.length}</b> 个可下载视频。` +
      (d.notes && d.notes.length ? '<br/>• ' + d.notes.map(n => esc(n)).join('<br/>• ') : '');
    $('batchNotes').className = 'notice info';
    $('batchNotes').classList.remove('hidden');

    const pd = (d.playlist_detected || []);
    if (pd.length) {
      const parts = pd.map(p => `🔗 检测到 B 站播放列表（<b>${esc(p.type)}</b>）：共 <b>${p.count}</b> 个视频${p.title ? ' · ' + esc(p.title) : ''}${(p.expanded) ? '（已自动展开为各视频独立卡片）' : '（未展开，仅解析该链接本身）'}`);
      $('batchPlaylistHint').className = 'notice info';
      $('batchPlaylistHint').innerHTML = parts.join('<br/>');
      $('batchPlaylistHint').classList.remove('hidden');
    } else {
      $('batchPlaylistHint').classList.add('hidden');
    }
    if (BATCH.length) $('batchToolbar').scrollIntoView({ behavior: 'smooth' });
  } catch (e2) { $('batchNotes').innerHTML = '请求异常：' + e2.message; $('batchNotes').classList.remove('hidden'); }
});

$('batchClear').onclick = () => { $('batchUrls').value = ''; BATCH = []; BSEL.clear(); renderBatch(); $('batchNotes').classList.add('hidden'); $('batchPlaylistHint').classList.add('hidden'); };


function firstBiliUrl(t){ const m = (t || '').match(/https?:\/\/[^\s]*bilibili\.com[^\s]*/i); return m ? m[0] : ''; }
let _biliDetTimer = null;
$('batchUrls').addEventListener('input', () => {
  clearTimeout(_biliDetTimer);
  _biliDetTimer = setTimeout(async () => {
    const u = firstBiliUrl($('batchUrls').value);
    if (!u) { $('batchPlaylistHint').classList.add('hidden'); return; }
    try {
      const d = await postJSON('/api/batch/detect', { url: u });
      if (d.ok && d.is_playlist) {
        $('batchPlaylistHint').className = 'notice info';
        let msg = `🔗 该 B 站链接疑似 <b>${esc(d.playlist_type)}</b>`;
        if (d.count) msg += `（共 <b>${d.count}</b> 个视频）`;
        if (d.playlist_type === '分P') {
          msg += `。分P（多P）视频会<b>自动展开</b>为各 P 独立卡片（各自封面）；「自动解析 B 站播放列表」开关仅影响合集/收藏夹/番剧。`;
        } else {
          msg += `。「自动解析 B 站播放列表」开启时会逐条展开为独立视频下载；关闭则只解析该链接本身。`;
        }
        $('batchPlaylistHint').innerHTML = msg;
        $('batchPlaylistHint').classList.remove('hidden');
      } else { $('batchPlaylistHint').classList.add('hidden'); }
    } catch (e) {  }
  }, 500);
});

$('batchSelAll').onchange = () => {
  if ($('batchSelAll').checked) BATCH.forEach(en => BSEL.add(en.id));
  else BSEL.clear();
  renderBatch();
};


function openBatchDetail(i) {
  DETAIL_IDX = i;
  const e = BATCH[i];

  const dt = e.cover_file
    ? `/api/files/serve?file=${encodeURIComponent(e.cover_file)}`
    : (e.thumbnail || '');
  $('detailThumb').src = dt;
  $('detailThumb').style.display = dt ? 'block' : 'none';
  $('detailBody').innerHTML = `
    <div class="detail-row"><b>标题</b><span>${esc(e.title)}</span></div>
    <div class="detail-row"><b>来源</b><span>${esc(PLAT_LABEL[e.platform] || e.platform || '未知')}</span></div>
    <div class="detail-row"><b>作者</b><span>${esc(e.uploader || '—')}</span></div>
    <div class="detail-row"><b>点赞</b><span>${e.likes ? `<span class="num">${fmtNum(e.likes)}</span>` : '—'}</span></div>
    <div class="detail-row"><b>播放</b><span>${e.views ? `<span class="num">${fmtNum(e.views)}</span>` : '—'}</span></div>
    <div class="detail-row"><b>时长</b><span>${fmtSec(e.duration)}</span></div>`;
  if (e.media_unavailable && !e.media_url) {
    $('detailBody').insertAdjacentHTML('beforeend',
      `<div class="detail-row" style="grid-column:1/-1"><span style="color:#e8a33d;font-weight:600">⚠ 直链不可用</span> 抖音平台反爬升级，视频直链暂无法下载；下方仅展示已抓回的元数据。</div>`);
    $('detailAddBatch').textContent = '⚠ 直链不可用';
    $('detailAddBatch').disabled = true;
  }
  $('detailOpen').href = e.url;
  $('detailOpen').onclick = (ev) => { ev.preventDefault(); openLink(e.platform, e.url); };
  $('detailFmtList').classList.add('hidden'); $('detailFmtList').innerHTML = '';
  const added = BSEL.has(e.id);
  $('detailAddBatch').textContent = added ? '✓ 已在批量列表' : '➕ 加入批量下载';
  $('detailModal').classList.remove('hidden');
}
$('detailClose').onclick = () => $('detailModal').classList.add('hidden');
$('detailModal').onclick = (ev) => { if (ev.target === $('detailModal')) $('detailModal').classList.add('hidden'); };
$('detailAddBatch').onclick = () => {
  if (DETAIL_IDX < 0) return;
  const id = BATCH[DETAIL_IDX].id;
  if (BSEL.has(id)) { BSEL.delete(id); $('detailAddBatch').textContent = '➕ 加入批量下载'; }
  else { BSEL.add(id); $('detailAddBatch').textContent = '✓ 已在批量列表'; }
  renderBatch();
};
$('detailFormats').onclick = (e) => withBusy(e.currentTarget, '解析中…', async () => {
  if (DETAIL_IDX < 0) return;
  const url = BATCH[DETAIL_IDX].url;
  try {
    const d = await postJSON('/api/download/info', { url });
    if (!d.ok) { $('detailFmtList').classList.remove('hidden'); $('detailFmtList').innerHTML = '<div class="row">解析失败：' + esc(d.error || '') + '</div>'; return; }
    if (d.media_unavailable) { $('detailFmtList').classList.remove('hidden'); $('detailFmtList').innerHTML = '<div class="row">⚠ ' + esc(d.note || '该视频直链暂不可用') + '</div>'; return; }
    if (d.media_url && !d.media_unavailable) {
      $('detailFmtList').classList.remove('hidden');
      $('detailFmtList').innerHTML = '<div class="row">✅ 该抖音视频直链已就绪，点「加入批量下载」后在批量面板直接下载。</div>';
      return;
    }
    const rows = [];
    const push = (arr, cls) => arr.forEach(f => rows.push(`<div class="row"><span class="${cls}">${esc(f.format_id)}</span><span>${esc(f.height ? f.height + 'p' : '')} ${esc(f.vcodec || '')} ${esc(f.acodec || '')} · ${esc(fmtRate(f.tbr))} · ${fmtBytes(f.filesize) || '?'}</span></div>`));
    push(d.video, 'tagv'); push(d.audio, 'taga'); push(d.muxed, 'tagm');
    $('detailFmtList').innerHTML = rows.join('') || '<div class="row">无可读格式</div>';
    $('detailFmtList').classList.remove('hidden');
  } catch (e2) { $('detailFmtList').classList.remove('hidden'); $('detailFmtList').innerHTML = '<div class="row">异常：' + esc(e2.message) + '</div>'; }
});


$('batchDownload').onclick = () => {
  const sel = batchSelected();
  if (!sel.length) return;

  if ($('batchUseGlobal').checked) {
    startBatchDownload({
      quality: $('batchGlobalFormat').value,
      vcodec: $('batchGlobalCodec').value,
      container: 'mp4',
      audio_only: $('batchGlobalFormat').value === 'audio',
      bilibili_delogo: false,
    });
    return;
  }

  $('batchQuality').value = $('batchGlobalFormat').value;
  $('batchCodec').value = $('batchGlobalCodec').value;
  $('batchModalCount').textContent = sel.length;
  $('batchModal').classList.remove('hidden');
};
$('batchModalClose').onclick = () => $('batchModal').classList.add('hidden');
$('batchCancel').onclick = () => $('batchModal').classList.add('hidden');
$('batchModal').onclick = (ev) => { if (ev.target === $('batchModal')) $('batchModal').classList.add('hidden'); };
$('batchConfirm').onclick = (e) => withBusy(e.currentTarget, '准备中…', () => startBatchDownload());

function startBatchDownload(override) {
  let sel = batchSelected();
  if (!sel.length) return;
  $('batchModal').classList.add('hidden');
  const settings = override || {
    quality: $('batchQuality').value,
    vcodec: $('batchCodec').value,
    format_id: $('batchFormatId').value.trim(),
    container: $('batchContainer').value,
    audio_only: $('batchAudioOnly').checked,
    bilibili_delogo: $('batchDelogo').checked,
  };
  const panel = $('batchPanel'); panel.classList.remove('hidden');
  $('batchProgWrap').classList.remove('hidden');
  $('batchProgFill').style.width = '0%';
  $('batchProgMeta').innerHTML = `准备下载 <b>${sel.length}</b> 个视频…`;
  $('batchStatus').innerHTML = '';
  const log = $('batchLog'); log.textContent = ''; log.classList.remove('hidden');

  const statusMap = {};
  const setStatus = (idx, title, kind, text, cover) => {
    statusMap[idx] = { title, kind, text, cover: cover || (statusMap[idx] && statusMap[idx].cover) || '' };
    $('batchStatus').innerHTML = Object.keys(statusMap).sort((a, b) => a - b).map(k => {
      const s = statusMap[k];
      const thumb = s.cover ? `<img class="bs-thumb" src="${assetUrl('/api/img?u=') + encodeURIComponent(s.cover)}" onerror="this.style.display='none'" alt=""/>` : '';
      return `<div class="batch-status ${s.kind}">${thumb}<span class="dot"></span>${esc(s.title)} — ${s.text}</div>`;
    }).join('');
  };
  const _skipDyn = sel.filter(e => e.media_unavailable && !e.media_url);
  if (_skipDyn.length) {
    sel = sel.filter(e => !(e.media_unavailable && !e.media_url));
    setStatus(-1, `抖音直链不可用（已跳过 ${_skipDyn.length} 个）`, '', '图文内容或平台反爬拦截，仅元数据可用');
  }
  if (!sel.length) { $('batchStatus').innerHTML = '<div class="batch-status">抖音直链均因平台反爬暂不可用，无内容可下载。</div>'; return; }
  sel.forEach((e, i) => setStatus(i, e.title || e.url, '', '排队中', e.thumbnail || e.cover));

  fetch('/api/batch/download', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ entries: sel.map(e => ({ url: e.url, title: e.title, platform: e.platform, id: e.id, thumbnail: e.thumbnail || e.cover || '', media_url: e.media_url || '', is_playlist: !!e.is_playlist })), settings })
  }).then(async resp => {
    const reader = resp.body.getReader(), dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const m = frame.match(/^event: (.+)\ndata: ([\s\S]+)$/);
        if (m) { try { handleBatchEvent(m[1], JSON.parse(m[2]), setStatus, sel); } catch (err) {} }
      }
    }
  }).catch(e2 => { $('batchProgMeta').innerHTML = '下载异常：' + e2.message; });
}

function handleBatchEvent(ev, d, setStatus, sel) {
  const log = $('batchLog');
  if (ev === 'batch-start') {
    $('batchProgMeta').innerHTML = `批量下载中… 共 <b>${d.total}</b> 个`;
  } else if (ev === 'entry-start') {
    setStatus(d.index, d.title || d.url, '', `<span class="spinner alt"></span>下载中…`, d.cover || '');
    $('batchProgFill').style.width = Math.round(d.index / d.total * 100) + '%';
    log.textContent += `\n[${d.index + 1}/${d.total}] 开始：${d.title || d.url}\n`;
  } else if (ev === 'progress') {
    const i = d.index || 0;
    setStatus(i, sel[i] ? (sel[i].title || sel[i].url) : '', '', `${esc(d.percent || '')} ${fmtBytes(d.done || 0)}`);
    const total = sel.length || 1;
    const pct = parseFloat(String(d.percent).replace('%', '')) || 0;
    $('batchProgFill').style.width = Math.round((i + pct / 100) / total * 100) + '%';
  } else if (ev === 'stage') {
    setStatus(d.index, sel[d.index] ? sel[d.index].title : '', '', esc(PP_TEXT[d.status] || d.status));
  } else if (ev === 'log') {
    log.textContent += d.line + '\n'; log.scrollTop = log.scrollHeight;
    } else if (ev === 'entry-done') {
      const r = d.result || {};
      if (r.playlist) {
        const n = (r.files && r.files.length) || '?';
        setStatus(d.index, sel[d.index] ? sel[d.index].title : '', 'ok', `完成 ${n} 个文件`, r.cover || '');
        (r.files || []).forEach(f => { if (f.filename) log.textContent += `✅ ${f.filename}\n`; });
      } else {
        setStatus(d.index, sel[d.index] ? sel[d.index].title : '', r.skipped ? 'skip' : 'ok',
                  r.skipped ? '已存在（复用）' : `完成 ${fmtBytes(r.size || 0)}`, r.cover || '');
        if (r.filename) log.textContent += `✅ ${r.filename}\n`;
      }
  } else if (ev === 'entry-skipped') {
    const txt = (d.reason || '跳过') + (d.path ? ` · <span class="link goto-dl">前往下载列表</span>` : '');
    setStatus(d.index, sel[d.index] ? sel[d.index].title : '', 'skip', txt);
  } else if (ev === 'error') {
    if (d.index != null) setStatus(d.index, sel[d.index] ? sel[d.index].title : '', 'err', d.message || '失败');
    else $('batchProgMeta').innerHTML = '❌ ' + esc(d.message || '失败');
    log.textContent += '❌ ' + (d.message || '失败') + '\n';
  } else if (ev === 'batch-done') {
    $('batchProgFill').style.width = '100%';
    $('batchProgMeta').innerHTML = `✅ 批量下载完成（共 ${d.total} 个）`;
  }
  log.scrollTop = log.scrollHeight;
}


const PLAT_LABEL = { bilibili:'Bilibili', douyin:'抖音', kuaishou:'快手', weibo:'微博',
  xiaohongshu:'小红书', youtube:'YouTube' };
let DL_HISTORY = [];
const DL_LIVE = new Map();
const DL_TASKS_UI = new Map();
const DL_BATCH_STATUS = {};
let DL_ES = null, DL_INIT = false;
const DL_SEL = new Set();
let DL_VIEW = localStorage.getItem('dlView') || 'row';
let DL_LIVE_VIEW = localStorage.getItem('dlLiveView') || 'row';
let DL_LIVE_COLLAPSED = localStorage.getItem('dlLiveCollapsed') === '1';
let DL_SESSION_START = Date.now();
let DL_SESSION_ITEMS = [];
let DL_HISTORY_ITEMS = [];

const platBadge = (p) => `<div class="dl-plat"><span class="badge">${esc(PLAT_LABEL[p] || p || '未知')}</span></div>`;
function coverImg(cover_url, cover_file, id) {
  const did = id ? ` data-detail-id="${escAttr(id)}"` : '';
  if (cover_file)
    return `<img class="dl-cover" draggable="false"${did} src="/api/files/serve?file=${encodeURIComponent(cover_file)}" alt="" loading="lazy" onerror="this.classList.add('ph');this.removeAttribute('src');this.textContent='🎞';"/>`;
  if (cover_url && /^https?:\/\//.test(cover_url))
    return `<img class="dl-cover" draggable="false"${did} src="${assetUrl('/api/img?u=') + encodeURIComponent(cover_url)}" alt="" loading="lazy" onerror="this.classList.add('ph');this.removeAttribute('src');this.textContent='🎞';"/>`;
  return `<div class="dl-cover ph"${did}>🎞</div>`;
}

function coverImgGrid(cover_url, cover_file, id) {
  const did = id ? ` data-detail-id="${escAttr(id)}"` : '';
  if (cover_file)
    return `<img class="dl-thumb" draggable="false"${did} src="/api/files/serve?file=${encodeURIComponent(cover_file)}" alt="" loading="lazy" onerror="this.classList.add('ph');this.removeAttribute('src');this.textContent='🎞';"/>`;
  if (cover_url && /^https?:\/\//.test(cover_url))
    return `<img class="dl-thumb" draggable="false"${did} src="${assetUrl('/api/img?u=') + encodeURIComponent(cover_url)}" alt="" loading="lazy" onerror="this.classList.add('ph');this.removeAttribute('src');this.textContent='🎞';"/>`;
  return `<div class="dl-thumb ph"${did}>🎞</div>`;
}
const platBadgeInline = (p) => `<span class="badge">${esc(PLAT_LABEL[p] || p || '未知')}</span>`;
function liveOverall(m) {
  if (m.kind === 'single') return m.pct || 0;
  const n = m.total || 0; if (!n) return 0;
  let s = 0, c = 0; for (const k in m.entries) { s += (m.entries[k] || 0); c++; }
  return c ? Math.round(s / c) : 0;
}
const parsePct = (s) => { if (!s) return 0; const v = parseFloat(String(s).replace('%','').trim()); return isNaN(v) ? 0 : v; };

function newLive(tid) {
  const m = { task_id: tid, kind: 'single', title: '', author: '', cover: '', platform: '',
              likes: 0, total: 0, entries: {}, pct: 0, statusText: '准备中…', el: null, err: false };
  DL_LIVE.set(tid, m); return m;
}
function liveRowInner(m) {
  const overall = liveOverall(m);
  const sub = `<div class="dl-sub">
      <span>作者：<b>${esc(m.author || '—')}</b></span>
      ${m.likes ? `<span>赞：<b>${fmtNum(m.likes)}</b></span>` : ''}
      <span>${m.kind === 'batch' ? '批量下载' : '单文件下载'}</span>
    </div>`;
  const bar = `<div class="dl-bar"><i style="width:${overall}%"></i></div>`;
  const right = `<div class="dl-right"><span class="dl-status live">${esc(m.statusText || '下载中…')}</span></div>`;
  return `<div class="dl-cb"></div>${coverImg(m.cover, m.cover_file || '')}${platBadge(m.platform)}<div class="dl-main"><div class="dl-title">${esc(m.title || '下载中…')}</div>${sub}${bar}</div>${right}`;
}
function histRowInner(it) {
  const st = it.status || 'done';
  const stText = ({ done: '已完成', skipped: '已存在', error: '失败' })[st] || st;
  const stCls = st === 'skipped' ? 'skipped' : (st === 'error' ? 'skipped' : 'done');
  const sub = `<div class="dl-sub">
      <span>作者：<b>${esc(it.author || '—')}</b></span>
      ${it.likes ? `<span>赞：<b>${fmtNum(it.likes)}</b></span>` : ''}
      <span>大小：<b>${fmtBytes(it.size)}</b></span>
      ${it.finished_at ? `<span>完成：${esc(it.finished_at)}</span>` : ''}
    </div>`;
  const bar = (st === 'done' || st === 'skipped') ? '' : `<div class="dl-bar"><i style="width:${it.progress || 0}%"></i></div>`;
  const right = `<div class="dl-right">
      <span class="dl-status ${stCls}">${esc(stText)}</span>
      <div style="display:flex;gap:6px">
        <button class="btn" data-action="open" data-path="${escAttr(it.path)}">📂 打开</button>
        <button class="btn" data-action="saveone" data-path="${escAttr(it.path)}">💾 保存</button>
        <button class="btn" data-action="delone" data-id="${escAttr(it.id)}">🗑</button>
      </div>
    </div>`;
  const checked = DL_SEL.has(it.id) ? 'checked' : '';
  return `<label class="dl-cb"><input type="checkbox" data-id="${escAttr(it.id)}" data-path="${escAttr(it.path)}" ${checked}/></label>${coverImg(it.cover, it.cover_file || '', it.id)}${platBadge(it.platform)}<div class="dl-main"><div class="dl-title">${esc(it.title || it.filename || '—')}</div>${sub}${bar}</div>${right}`;
}

function histCardInner(it) {
  const st = it.status || 'done';
  const stText = ({ done: '已完成', skipped: '已存在', error: '失败' })[st] || st;
  const checked = DL_SEL.has(it.id) ? 'checked' : '';
  return `<label class="dl-cb"><input type="checkbox" data-id="${escAttr(it.id)}" data-path="${escAttr(it.path)}" ${checked}/></label>
    ${coverImgGrid(it.cover, it.cover_file || '', it.id)}
    <div class="dl-card-title">${esc(it.title || it.filename || '—')}</div>
    <div class="dl-card-meta">
      ${platBadgeInline(it.platform)}
      ${it.author ? `<span>作者：<b>${esc(it.author)}</b></span>` : ''}
      ${it.likes ? `<span>赞：<b>${fmtNum(it.likes)}</b></span>` : ''}
    </div>
    <div class="dl-card-actions">
      <button class="btn" data-action="open" data-path="${escAttr(it.path)}">📂 打开</button>
      <button class="btn" data-action="saveone" data-path="${escAttr(it.path)}">💾 保存</button>
      <button class="btn" data-action="delone" data-id="${escAttr(it.id)}">🗑</button>
    </div>`;
}

function renderDownloads() {
  const plat = $('dlPlat').value, status = $('dlStatus').value, q = $('dlSearch').value.trim().toLowerCase();
  const liveWrap = $('dlLive'); liveWrap.innerHTML = '';
  let liveCount = 0;

  liveCount += renderLiveTasksInto(liveWrap);

  const hist = DL_HISTORY.filter(it => {
    if (plat && it.platform !== plat) return false;
    if (status) { if (status === 'downloading') return false; if (status === 'error') return it.status === 'error'; if ((it.status || 'done') !== status) return false; }
    if (q) { const hay = ((it.title || '') + ' ' + (it.author || '')).toLowerCase(); if (!hay.includes(q)) return false; }
    return true;
  });
  const isSession = (it) => {
    const fa = it.finished_at;
    if (!fa) return false;
    const t = Date.parse(fa);
    return !isNaN(t) && t >= DL_SESSION_START;
  };
  const sessionItems = hist.filter(isSession);
  const historyItems = hist.filter(it => !isSession(it));
  DL_SESSION_ITEMS = sessionItems; DL_HISTORY_ITEMS = historyItems;

  const gridCls = (DL_VIEW === 'grid') ? 'dl-grid' : 'dl-hist';


  const sessionWrap = $('dlSessionHist'); sessionWrap.className = 'dl-sec-body ' + gridCls; sessionWrap.innerHTML = '';
  const histWrap = $('dlHist'); histWrap.className = 'dl-sec-body ' + gridCls; histWrap.innerHTML = '';

  const paint = (wrap, items) => {
    for (const it of items) {
      const el = document.createElement('div');
      if (DL_VIEW === 'grid') {
        el.className = 'dl-card' + (DL_SEL.has(it.id) ? ' sel' : '');
        el.innerHTML = histCardInner(it);
      } else {
        el.className = 'dl-row' + (DL_SEL.has(it.id) ? ' sel' : '');
        el.innerHTML = histRowInner(it);
      }
      el.dataset.histId = it.id;
      wrap.appendChild(el);
    }
  };
  if (!sessionItems.length) {
    sessionWrap.innerHTML = liveCount ? '' : `<div class="dl-empty">本次会话还没有下载记录</div>`;
  } else {
    paint(sessionWrap, sessionItems);
  }
  if (!historyItems.length) {
    histWrap.innerHTML = `<div class="dl-empty">没有更早的下载记录</div>`;
  } else {
    paint(histWrap, historyItems);
  }
  $('dlSessionCount').textContent = `${sessionItems.length} 条` + (liveCount ? ` · ${liveCount} 进行中` : '');
  $('dlHistCount').textContent = `${historyItems.length} 条`;
  $('dlCount').textContent = `共 ${hist.length} 条历史` + (liveCount ? ` · ${liveCount} 个进行中` : '');
  syncSecSelAll();
  syncSelInfo();
}

function syncSecSelAll() {
  const mk = (sec, items) => {
    const cb = document.querySelector(`.dlSecSelAll[data-sec="${sec}"]`);
    if (!cb) return;
    cb.checked = items.length > 0 && items.every(it => DL_SEL.has(it.id));
    cb.indeterminate = !cb.checked && items.some(it => DL_SEL.has(it.id));
  };
  mk('session', DL_SESSION_ITEMS);
  mk('hist', DL_HISTORY_ITEMS);
}
function syncSelInfo() { $('dlSelInfo').textContent = `已选 ${DL_SEL.size} 项`; syncSecSelAll(); }


function toggleDlSec(sec) {
  const el = sec === 'session' ? $('dlSecSession') : $('dlSecHist');
  if (!el) return;
  el.classList.toggle('collapsed');
  localStorage.setItem('dlSec_' + sec, el.classList.contains('collapsed') ? '1' : '0');
}


function toggleSel(id, rowEl) {
  const cb = rowEl.querySelector('input[type=checkbox][data-id]');
  if (DL_SEL.has(id)) { DL_SEL.delete(id); if (cb) cb.checked = false; }
  else { DL_SEL.add(id); if (cb) cb.checked = true; }
  rowEl.classList.toggle('sel', DL_SEL.has(id));
  syncSelInfo();
}

function openDetail(id) {
  const it = DL_HISTORY.find(x => x.id === id); if (!it) return;
  $('dlDetailBody').innerHTML = detailInner(it);
  $('dlDetail').classList.remove('hidden');


  const _miss = (v) => v === undefined || v === null;
  if (it.url && (_miss(it.author) || _miss(it.likes) || _miss(it.views) || _miss(it.duration))) {
    fetch('/api/download/info', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: it.url, platform: it.platform }),
    }).then(r => r.json()).then(d => {
      if (!d || !d.ok) return;
      let changed = false;
      if (!it.author && d.uploader) { it.author = d.uploader; changed = true; }
      if (!it.likes && d.likes) { it.likes = d.likes; changed = true; }
      if (!it.views && d.views) { it.views = d.views; changed = true; }
      if (!it.duration && d.duration) { it.duration = d.duration; changed = true; }
      if (!it.cover && d.thumbnail) { it.cover = d.thumbnail; changed = true; }
      if (changed && !$('dlDetail').classList.contains('hidden')) {
        $('dlDetailBody').innerHTML = detailInner(it);
      }
    }).catch(() => {});
  }
}
function detailInner(it) {
  const stText = ({ done: '已完成', skipped: '已存在', error: '失败' })[it.status || 'done'] || it.status;
  const cover = it.cover_file
    ? `/api/files/serve?file=${encodeURIComponent(it.cover_file)}`
    : (it.cover && /^https?:\/\//.test(it.cover) ? assetUrl('/api/img?u=') + encodeURIComponent(it.cover) : '');
  const coverHtml = cover
    ? `<img class="dd-cover" src="${escAttr(cover)}" alt="" onerror="this.onerror=null;this.replaceWith(Object.assign(document.createElement('div'),{className:'dd-cover ph',textContent:'🎞'}))"/>`
    : `<div class="dd-cover ph">🎞</div>`;
  const linkHtml = it.url
    ? `<a class="dd-link" href="${escAttr(it.url)}" target="_blank" rel="noopener">${esc(it.url)}</a> <button class="btn" data-action="copy-text" data-text="${escAttr(it.url)}">复制</button>`
    : '—';
  const row = (k, v) => `<div class="k">${k}</div><div class="v">${v}</div>`;
  return `
    ${coverHtml}
    <div class="dd-title">${esc(it.title || it.filename || '—')}</div>
    <div class="dd-grid">
      ${row('平台', esc(PLAT_LABEL[it.platform] || it.platform || '未知'))}
      ${row('作者', esc(it.author || '—'))}
      ${row('点赞', (it.likes != null && it.likes !== '') ? fmtNum(it.likes) : '—')}
      ${row('播放', (it.views != null && it.views !== '') ? fmtNum(it.views) : '—')}
      ${row('时长', it.duration ? fmtDur(it.duration) : '—')}
      ${row('大小', fmtBytes(it.size))}
      ${row('状态', esc(stText))}
      ${row('完成时间', esc(it.finished_at || '—'))}
      <div class="k">链接</div><div class="v">${linkHtml}</div>
      <div class="k">本地路径</div><div class="v">${esc(it.path || '—')} <button class="btn" data-action="copy-text" data-text="${escAttr(it.path || '')}">复制</button></div>
    </div>
    <div class="dd-actions">
      <button class="btn primary" data-action="saveone" data-path="${escAttr(it.path)}">💾 保存到下载</button>
      <button class="btn" data-action="delone" data-id="${escAttr(it.id)}">🗑 删除</button>
    </div>`;
}
function setDlView(v) {
  DL_VIEW = v;
  localStorage.setItem('dlView', v);
  document.querySelectorAll('#dlViewSwitch .vs-btn').forEach(b => b.classList.toggle('active', b.dataset.view === v));
  renderDownloads();
}

function handleDlAction(btn) {
  const a = btn.dataset.action;

  if (btn.dataset.global) { dlBatchAction(a, btn.dataset.taskId); return; }
  if (btn.dataset.taskKey) { dlTaskAction(a, btn.dataset.taskKey); return; }
  if (a === 'open') openFolder(btn.dataset.path);
  else if (a === 'saveone') nativeDownloadAll([btn.dataset.path]);
  else if (a === 'delone') {
    if (!confirm('确认删除该条下载记录？（将同时删除本地文件，不可恢复）')) return;
    deleteItems([btn.dataset.id]);
  }
}

function openFolder(path) {
  const fn = basename(path);
  if (!fn) { toast('路径为空'); return; }
  fetch(assetUrl('/api/files/open?file=') + encodeURIComponent(fn))
    .then(r => r.json())
    .then(r => { if (r && r.ok) toast('已打开所在文件夹'); else toast('打开失败：' + ((r && r.error) || '未知错误')); })
    .catch(e => toast('打开失败：' + e));
}


function taskStatusCls(s) {
  return ({ queued: 'queued', downloading: 'live', paused: 'paused', stopped: 'stopped',
            done: 'done', error: 'skipped', skipped: 'skipped' })[s] || 'queued';
}
const TASK_STATUS_TXT = { queued: '排队中', downloading: '下载中', paused: '已暂停',
  stopped: '已停止', done: '已完成', error: '失败', skipped: '已跳过' };

function taskKeyOf(d) {
  if (d.task_key) return d.task_key;
  if (d.task_id == null) return null;
  const idx = (d.index != null) ? d.index : (d.tag !== undefined && d.tag !== '' ? d.tag : 0);
  return d.task_id + ':' + idx;
}
function upsertTask(tk, patch) {
  const t = DL_TASKS_UI.get(tk) || { task_key: tk, task_id: patch.task_id, index: null, pct: 0, status: 'queued' };
  Object.assign(t, patch);
  DL_TASKS_UI.set(tk, t);
  return t;
}
function taskRowInner(t) {
  const st = t.status || 'queued';
  const pct = t.pct || 0;
  const stTxt = TASK_STATUS_TXT[st] || st;
  const bar = `<div class="dl-bar"><i style="width:${pct}%"></i></div>`;
  let ctrls = '';
  if (st === 'downloading' || st === 'queued') {
    ctrls = `<button class="btn dl-ctl" data-action="pause" data-task-key="${escAttr(t.task_key)}">⏸ 暂停</button>`
          + `<button class="btn dl-ctl" data-action="stop" data-task-key="${escAttr(t.task_key)}">⏹ 停止</button>`
          + `<button class="btn dl-ctl" data-action="delete" data-task-key="${escAttr(t.task_key)}">🗑 删除</button>`;
  } else if (st === 'paused' || st === 'stopped' || st === 'error') {
    ctrls = `<button class="btn dl-ctl" data-action="resume" data-task-key="${escAttr(t.task_key)}">▶ 继续</button>`
          + `<button class="btn dl-ctl" data-action="delete" data-task-key="${escAttr(t.task_key)}">🗑 删除</button>`;
  } else {
    ctrls = `<button class="btn dl-ctl" data-action="delete" data-task-key="${escAttr(t.task_key)}">🗑 删除</button>`;
  }
  const sub = `<div class="dl-sub"><span>作者：<b>${esc(t.author || '—')}</b></span></div>`;
  const right = `<div class="dl-right"><span class="dl-status ${taskStatusCls(st)}">${esc(stTxt)}</span>`
              + `<div style="display:flex;gap:6px;flex-wrap:wrap">${ctrls}</div></div>`;
  return `${coverImg(t.cover, t.cover_file || '', t.task_key)}${platBadge(t.platform)}<div class="dl-main"><div class="dl-title">${esc(t.title || '下载中…')}</div>${sub}${bar}</div>${right}`;
}

function renderLiveTasksInto(liveWrap) {
  let n = 0;
  const batchIds = [...new Set([...DL_TASKS_UI.values()].map(t => t.task_id))].filter(Boolean);
  if (batchIds.length && DL_TASKS_UI.size) {
    const bar = document.createElement('div'); bar.className = 'dl-task-global';
    let ghtml = `<span class="dl-task-global-title">本批下载 · ${DL_TASKS_UI.size} 个任务</span>`;
    for (const id of batchIds) {
      const paused = DL_BATCH_STATUS[id] === 'paused';
      ghtml += `<button class="btn" data-global="1" data-action="${paused ? 'resume' : 'pause'}" data-task-id="${escAttr(id)}">${paused ? '▶ 继续本批' : '⏸ 暂停本批'}</button>`;
    }
    ghtml += `<button class="btn dl-ctl" data-global="1" data-action="clear" data-task-id="${escAttr(batchIds.join(','))}">🧹 清空已完成</button>`;

    ghtml += `<button class="btn" data-action="toggle-collapse">${DL_LIVE_COLLAPSED ? '▸ 展开列表' : '▾ 收起列表'}</button>`;
    ghtml += `<span class="dl-view-switch" id="dlLiveViewSwitch">`
           + `<button type="button" class="vs-btn ${DL_LIVE_VIEW === 'row' ? 'active' : ''}" data-liveview="row">☰ 单行</button>`
           + `<button type="button" class="vs-btn ${DL_LIVE_VIEW === 'grid' ? 'active' : ''}" data-liveview="grid">▦ 网格</button>`
           + `</span>`;
    bar.innerHTML = ghtml;
    liveWrap.appendChild(bar); n++;
  }
  const rows = document.createElement('div');
  rows.className = 'dl-live-rows'
    + (DL_LIVE_VIEW === 'grid' ? ' dl-live-grid' : '')
    + (DL_LIVE_COLLAPSED ? ' dl-live-hidden' : '');
  if (DL_TASKS_UI.size) {
    for (const t of DL_TASKS_UI.values()) {
      const el = document.createElement('div'); el.className = 'dl-row task'; el.dataset.taskKey = t.task_key;
      el.innerHTML = taskRowInner(t);
      rows.appendChild(el); n++;
    }
  } else {
    rows.innerHTML = `<div class="dl-empty">暂无进行中的下载任务</div>`;
  }
  liveWrap.appendChild(rows);
  return n;
}
function setDlLiveView(v) {
  DL_LIVE_VIEW = (v === 'grid') ? 'grid' : 'row';
  localStorage.setItem('dlLiveView', DL_LIVE_VIEW);
  renderLiveTasks();
}
function renderLiveTasks() {
  const liveWrap = $('dlLive'); if (!liveWrap) return 0;
  liveWrap.innerHTML = '';
  return renderLiveTasksInto(liveWrap);
}
async function dlTaskAction(action, taskKey) {
  try { await postJSON('/api/dl/control', { scope: 'task', action, task_key: taskKey }); }
  catch (e) { toast('操作失败：' + (e.message || e)); }
}
async function dlBatchAction(action, taskId) {
  if (action === 'clear') {
    for (const t of [...DL_TASKS_UI.values()]) {
      if (['done', 'error', 'skipped'].includes(t.status)) DL_TASKS_UI.delete(t.task_key);
    }
    renderLiveTasks(); return;
  }
  try { await postJSON('/api/dl/control', { scope: 'batch', action, task_id: taskId }); }
  catch (e) { toast('操作失败：' + (e.message || e)); }
}

function patchLive(m) {
  if (!m.el) return;
  const bar = m.el.querySelector('.dl-bar > i');
  const st = m.el.querySelector('.dl-status');
  if (bar) bar.style.width = liveOverall(m) + '%';
  if (st) st.textContent = m.statusText;
}
function renderLiveRow(m) {
  const el = document.createElement('div'); el.className = 'dl-row live'; el.innerHTML = liveRowInner(m);
  if (m.el && m.el.parentNode) m.el.parentNode.replaceChild(el, m.el);
  else $('dlLive').appendChild(el);
  m.el = el;
}
function finishLive(tid) {
  const m = DL_LIVE.get(tid);
  if (m && m.el && m.el.parentNode) m.el.parentNode.removeChild(m.el);
  DL_LIVE.delete(tid);
  reloadHistory();
}

async function reloadHistory() {
  try { const r = await (await fetch('/api/downloads/history')).json(); DL_HISTORY = r.items || r || []; }
  catch (e) { DL_HISTORY = []; }
  renderDownloads();
}

function onLiveEvent(ev, d) {
  const tid = d.task_id;
  if (!tid) return;
  const tk = taskKeyOf(d);

  if (ev === 'task-update') {
    if (d.task_key) {
      upsertTask(d.task_key, {
        task_id: d.task_id || tid, status: d.status || 'queued',
        title: d.title, platform: d.platform, url: d.url, cover: d.cover, cover_file: d.cover_file,
      });
      renderLiveTasks();
    }
    return;
  }
  if (ev === 'batch-update') {
    DL_BATCH_STATUS[d.task_id] = d.status;
    renderLiveTasks();
    return;
  }
  if (ev === 'entry-start') {
    const key = tid + ':' + (d.index != null ? d.index : 0);
    upsertTask(key, { task_id: tid, index: d.index, status: 'queued',
      title: d.title, platform: d.platform, url: d.url, cover: d.cover });
    renderLiveTasks();
    return;
  }
  if (ev === 'progress') {
    const key = tid + ':' + (d.index != null ? d.index : 0);
    const t = DL_TASKS_UI.get(key);
    if (t) { t.pct = parsePct(d.percent) || t.pct; renderLiveTasks(); }
    return;
  }
  if (ev === 'meta') {

    if (tk && DL_TASKS_UI.has(tk)) {
      const t = DL_TASKS_UI.get(tk);
      if (d.cover) { t.cover = d.cover; t.cover_file = t.cover_file || d.cover_file || ''; }
      if (d.title && !t.title) t.title = d.title;
      renderLiveTasks();
    }
    return;
  }
  if (ev === 'batch-start') {
    DL_BATCH_STATUS[tid] = 'running';
    renderLiveTasks();
    return;
  }
  if (ev === 'stage') {

    if (tk && DL_TASKS_UI.has(tk)) { DL_TASKS_UI.get(tk).status = 'downloading'; renderLiveTasks(); }
    return;
  }
  if (ev === 'batch-progress') {

    return;
  }
  if (ev === 'done' || ev === 'batch-done') {

    reloadHistory();
    return;
  }
  if (ev === 'error') {
    if (tk && DL_TASKS_UI.has(tk)) { DL_TASKS_UI.get(tk).status = 'error'; renderLiveTasks(); }
    reloadHistory();
    return;
  }
  if (ev === 'task-stopped') {
    if (tk && DL_TASKS_UI.has(tk)) renderLiveTasks();
    return;
  }
}

function loadDownloads() {
  if (!DL_INIT) {

    ['session', 'hist'].forEach(s => {
      if (localStorage.getItem('dlSec_' + s) === '1') {
        const el = s === 'session' ? $('dlSecSession') : $('dlSecHist');
        if (el) el.classList.add('collapsed');
      }
    });
    $('dlPlat').addEventListener('change', renderDownloads);      $('dlStatus').addEventListener('change', renderDownloads);
    $('dlSearch').addEventListener('input', renderDownloads);
    $('dlRefresh').addEventListener('click', reloadHistory);

    $('dlList').addEventListener('click', (e) => {
      const cov = e.target.closest('[data-detail-id]');
      if (cov) { e.stopPropagation(); openDetail(cov.dataset.detailId); return; }

      const lv = e.target.closest('[data-liveview]');
      if (lv) { setDlLiveView(lv.dataset.liveview); return; }
      const btn = e.target.closest('[data-action]');
      if (btn) {
        if (btn.dataset.action === 'toggle-collapse') {
          DL_LIVE_COLLAPSED = !DL_LIVE_COLLAPSED;
          localStorage.setItem('dlLiveCollapsed', DL_LIVE_COLLAPSED ? '1' : '0');
          renderLiveTasks(); return;
        }
        handleDlAction(btn); return;
      }
      if (e.target.closest('.dl-cb')) return;

      const card = e.target.closest('.dl-card, .dl-row');
      if (card && card.dataset.histId) toggleSel(card.dataset.histId, card);
    });
    _dlDragInit();
    $('dlList').addEventListener('change', (e) => {
      const cb = e.target.closest('input[type=checkbox][data-id]'); if (!cb) return;
      const row = cb.closest('.dl-card, .dl-row');
      if (cb.checked) { DL_SEL.add(cb.dataset.id); if (row) row.classList.add('sel'); }
      else { DL_SEL.delete(cb.dataset.id); if (row) row.classList.remove('sel'); }
      syncSelInfo();
    });

    $('dlList').addEventListener('click', (e) => {
      const head = e.target.closest('.dl-sec-head');
      if (!head) return;

      if (e.target.closest('.dl-sec-sel')) return;
      toggleDlSec(head.dataset.sec);
    });

    document.querySelectorAll('.dlSecSelAll').forEach(cb => {
      cb.addEventListener('change', () => {
        const items = cb.dataset.sec === 'session' ? DL_SESSION_ITEMS : DL_HISTORY_ITEMS;
        const v = cb.checked;
        items.forEach(it => { if (v) DL_SEL.add(it.id); else DL_SEL.delete(it.id); });
        renderDownloads();
      });
    });
    $('dlSave').addEventListener('click', () => {
      const paths = DL_HISTORY.filter(it => DL_SEL.has(it.id)).map(it => it.path).filter(Boolean);
      if (!paths.length) { toast('请先勾选要移动的文件'); return; }
      batchSave(paths);
    });

    $('dlDownloadBtn').addEventListener('click', () => {
      const paths = DL_HISTORY.filter(it => DL_SEL.has(it.id)).map(it => it.path).filter(Boolean);
      if (!paths.length) { toast('请先勾选要下载的文件'); return; }
      $('dlDownloadModal').classList.remove('hidden');
    });
    $('dlDlClose').addEventListener('click', () => $('dlDownloadModal').classList.add('hidden'));
    $('dlDownloadModal').addEventListener('click', (e) => {
      if (e.target === $('dlDownloadModal')) $('dlDownloadModal').classList.add('hidden');
    });
    $('dlDlSingle').addEventListener('click', () => {
      const paths = DL_HISTORY.filter(it => DL_SEL.has(it.id)).map(it => it.path).filter(Boolean);
      $('dlDownloadModal').classList.add('hidden');
      nativeDownloadAll(paths);
    });
    $('dlDlZip').addEventListener('click', () => {
      const paths = DL_HISTORY.filter(it => DL_SEL.has(it.id)).map(it => it.path).filter(Boolean);
      $('dlDownloadModal').classList.add('hidden');
      downloadZip(paths);
    });
    $('dlDelete').addEventListener('click', () => {
      if (!DL_SEL.size) { toast('请先勾选要删除的记录'); return; }
      const ids = [...DL_SEL];
      if (!confirm(`确认删除 ${ids.length} 条下载记录？（将同时删除本地文件，不可恢复）`)) return;
      deleteItems(ids);
    });

    $('dlDetailClose').addEventListener('click', () => $('dlDetail').classList.add('hidden'));
    $('dlDetail').addEventListener('click', (e) => {
      if (e.target === $('dlDetail')) { $('dlDetail').classList.add('hidden'); return; }
      const btn = e.target.closest('[data-action]'); if (!btn) return;
      const a = btn.dataset.action;
      if (a === 'copy-text') safeCopy(btn.dataset.text).then(ok => { if (ok) toast('已复制'); else alert('复制失败，请手动选择文本复制'); });
      else if (a === 'saveone') nativeDownloadAll([btn.dataset.path]);
      else if (a === 'delone') { if (!confirm('确认删除该条下载记录？（将同时删除本地文件，不可恢复）')) return; $('dlDetail').classList.add('hidden'); deleteItems([btn.dataset.id]); }
    });

    document.querySelectorAll('#dlViewSwitch .vs-btn').forEach(b => b.addEventListener('click', () => setDlView(b.dataset.view)));
    setDlView(DL_VIEW);
    DL_INIT = true;
  }
  if (!DL_ES && !DEMO) {
    DL_ES = new EventSource('/api/downloads/stream');
    DL_ES.addEventListener('snapshot', (e) => { try { DL_HISTORY = (JSON.parse(e.data).items) || []; } catch (_) {} renderDownloads(); });
    ['meta','batch-start','entry-start','progress','stage','batch-progress','done','batch-done','error','task-update','batch-update','task-stopped']
      .forEach(ev => DL_ES.addEventListener(ev, (e) => { try { onLiveEvent(ev, JSON.parse(e.data)); } catch (_) {} }));
  }
  reloadHistory();
}

const basename = (p) => String(p || '').split(/[\\/]/).pop();

let DL_DIR_HANDLE = null;




async function writeFilesToDir(dir, paths) {
  let ok = 0, fail = 0;
  for (const p of paths) {
    const fn = basename(p);
    try {
      const resp = await fetch(assetUrl('/downloads/') + encodeURIComponent(fn));
      if (!resp.ok) { fail++; continue; }
      const blob = await resp.blob();
      const fh = await dir.getFileHandle(fn, { create: true });
      const w = await fh.createWritable();
      await w.write(blob); await w.close();
      ok++;
    } catch (e) { fail++; }
  }
  toast(`已写入本地文件夹 ${ok}/${paths.length} 个文件` + (fail ? `（${fail} 个失败）` : ''));
}



function downloadZip(paths) {


  const qs = paths.map(p => 'f=' + encodeURIComponent(p)).join('&');
  const url = '/api/files/zip?' + qs;

  fetch(url, { method: 'HEAD' }).then(r => {
    if (!r.ok) {
      r.json().then(j => toast('压缩包下载失败：' + ((j && j.error) || '文件不存在'))).catch(() => toast('压缩包下载失败：文件不存在'));
      return;
    }
    const a = document.createElement('a');
    a.href = url;
    a.download = 'videos_' + Date.now() + '.zip';
    document.body.appendChild(a); a.click(); a.remove();
    toast('已打包为单个压缩包，浏览器将提示一次保存位置');
  }).catch(() => {

    const a = document.createElement('a');
    a.href = url; a.download = 'videos_' + Date.now() + '.zip';
    document.body.appendChild(a); a.click(); a.remove();
    toast('已打包为单个压缩包，浏览器将提示一次保存位置');
  });
}



async function batchSave(paths) {
  if (!paths || !paths.length) { toast('请先勾选要移动的文件'); return; }
  if (!('showDirectoryPicker' in window)) {
    toast('当前浏览器不支持「移动到文件夹」，请使用 Chrome/Edge，或点「批量下载」选择压缩包下载');
    return;
  }
  try {
    let dir = DL_DIR_HANDLE;
    if (dir) {
      try { if ((await dir.queryPermission({ mode: 'readwrite' })) !== 'granted') dir = null; }
      catch (_) { dir = null; }
    }
    if (!dir) { dir = await window.showDirectoryPicker({ mode: 'readwrite' }); DL_DIR_HANDLE = dir; }
    await writeFilesToDir(dir, paths);
  } catch (e) {
    if (e && e.name === 'AbortError') return;
    toast('移动到文件夹失败：' + (e && e.message ? e.message : e));
  }
}


function nativeDownloadAll(paths) {
  let i = 0;
  const step = () => {
    if (i >= paths.length) {
      toast(`已触发 ${paths.length} 个文件下载（可在浏览器下载管理中查看）`);
      return;
    }
    const fn = basename(paths[i++]);
    const a = document.createElement('a');
    a.href = '/downloads/' + encodeURIComponent(fn);
    a.download = fn;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(step, 400);
  };
  step();
}
function deleteItems(ids) {
  postJSON('/api/downloads/delete', { ids }).then(r => {
    if (r.ok) { toast(`已删除 ${r.removed_ids} 条记录`); DL_SEL.clear(); reloadHistory(); }
    else toast('删除失败：' + (r.error || ''));
  });
}


setMode(localStorage.getItem('mode') || 'search');
loadCookies();


// 页面加载后：若已有进行中的登录任务（其他标签页/外部触发/刷新前启动），自动接续轮询并展示实时浏览器窗口
(async () => {
  try {
    const s = await (await fetch('/api/cookies/login_status')).json();
    if (s && s.running && !loginTimer) {
      loginTimer = setInterval(async () => {
        try {
          const st = await (await fetch('/api/cookies/login_status')).json();
          const shotEl = ckShotWrap, shotImg = ckLoginShot;
          if (shotEl && shotImg) {
            if (st.running && st.screenshot) {
              shotImg.src = 'data:image/jpeg;base64,' + st.screenshot;
              shotEl.classList.remove('hidden');
              const PL = { bilibili: 'Bilibili', douyin: '抖音', kuaishou: '快手', weibo: '微博', xiaohongshu: '小红书', youtube: 'YouTube' };
              const pl = ckShotPlat; if (pl) pl.textContent = PL[st.platform] || st.platform || '';
            } else if (!st.running) {
              shotEl.classList.add('hidden');
            }
          }
          const stb = ckStopAll;
          if (stb) { if (st.running) stb.classList.remove('hidden'); else stb.classList.add('hidden'); }
          if (!st.running) {
            clearInterval(loginTimer); loginTimer = null;
            const cap = ckCaptureNow; if (cap) cap.classList.add('hidden');
            loadCookies();
          }
        } catch (e) { clearInterval(loginTimer); loginTimer = null; }
      }, 1500);
    }
  } catch (e) { /* 非后端环境（如 GitHub Pages 演示模式）无需轮询 */ }
})();
