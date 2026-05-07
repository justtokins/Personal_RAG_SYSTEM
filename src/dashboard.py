"""
dashboard.py — Server-side rendered HTML dashboard.

Functional, not pretty. Shows everything needed to monitor the system:
    - Corpus stats (docs, chunks)
    - Ingestion queue by status
    - Recent documents with tag filter
    - Failed files with error messages
    - Manual search
    - Trigger scan / backup buttons

No JS framework. Vanilla JS fetches from /api/* endpoints.
Tailwind CDN for minimal styling (loaded from CDN — no build step).
The bearer token is read from localStorage on first visit and stored.
"""

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>RAGBase Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    body { background: #0f172a; color: #e2e8f0; font-family: 'Courier New', monospace; }
    .card { background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 16px; }
    .badge { display:inline-block; padding:2px 8px; border-radius:12px; font-size:0.7rem; font-weight:600; }
    .badge-complete  { background:#064e3b; color:#6ee7b7; }
    .badge-failed    { background:#450a0a; color:#fca5a5; }
    .badge-queued    { background:#1e3a5f; color:#93c5fd; }
    .badge-processing{ background:#3d2b00; color:#fcd34d; }
    .badge-skipped   { background:#1f2937; color:#9ca3af; }
    .btn { padding:6px 14px; border-radius:6px; font-size:0.8rem; font-weight:600;
           cursor:pointer; border:none; }
    .btn-primary { background:#3b82f6; color:#fff; }
    .btn-primary:hover { background:#2563eb; }
    .btn-danger  { background:#dc2626; color:#fff; }
    .result-item { border-left:3px solid #3b82f6; padding:8px 12px; margin:6px 0;
                   background:#0f172a; border-radius:0 6px 6px 0; }
    .tag-chip { display:inline-block; background:#1e3a5f; color:#93c5fd;
                border-radius:10px; padding:1px 8px; font-size:0.7rem; margin:2px; }
    input[type=text], input[type=password] {
      background:#0f172a; border:1px solid #334155; color:#e2e8f0;
      border-radius:6px; padding:6px 10px; width:100%; font-family:inherit;
    }
    input[type=text]:focus, input[type=password]:focus {
      outline:none; border-color:#3b82f6;
    }
    table { width:100%; border-collapse:collapse; font-size:0.8rem; }
    th { text-align:left; padding:6px 8px; color:#64748b;
         border-bottom:1px solid #334155; font-weight:500; }
    td { padding:6px 8px; border-bottom:1px solid #1e293b; }
    tr:hover td { background:#1e293b; }
    .stat-num { font-size:1.6rem; font-weight:700; color:#38bdf8; }
    .stat-label { font-size:0.7rem; color:#64748b; text-transform:uppercase; letter-spacing:.08em; }
    #toast { position:fixed; bottom:20px; right:20px; padding:10px 18px;
             border-radius:8px; font-size:0.8rem; display:none; z-index:999; }
  </style>
</head>
<body class="min-h-screen p-6">

<div id="toast"></div>

<!-- Token modal -->
<div id="token-modal"
     style="display:none; position:fixed; inset:0; background:#000a; z-index:100;
            display:flex; align-items:center; justify-content:center;">
  <div class="card" style="width:380px">
    <h2 class="text-lg font-bold mb-4">Enter Bearer Token</h2>
    <input type="password" id="token-input" placeholder="Your MCP_BEARER_TOKEN" class="mb-3">
    <button class="btn btn-primary w-full" onclick="saveToken()">Save & Continue</button>
  </div>
</div>

<!-- Header -->
<div class="flex items-center justify-between mb-6">
  <div>
    <h1 class="text-2xl font-bold text-white">🗄 RAGBase</h1>
    <p class="text-slate-400 text-sm">Personal Knowledge Base</p>
  </div>
  <div class="flex gap-2">
    <button class="btn btn-primary" onclick="triggerScan()">↻ Scan Now</button>
    <button class="btn" style="background:#7c3aed;color:#fff" onclick="triggerBackup()">💾 Backup Now</button>
    <button class="btn" style="background:#334155;color:#e2e8f0" onclick="clearToken()">⚙ Token</button>
  </div>
</div>

<!-- Stats row -->
<div class="grid grid-cols-2 md:grid-cols-5 gap-4 mb-6" id="stats-row">
  <div class="card text-center"><div class="stat-num" id="s-docs">—</div><div class="stat-label">Documents</div></div>
  <div class="card text-center"><div class="stat-num" id="s-chunks">—</div><div class="stat-label">Chunks</div></div>
  <div class="card text-center"><div class="stat-num text-green-400" id="s-complete">—</div><div class="stat-label">Complete</div></div>
  <div class="card text-center"><div class="stat-num text-red-400" id="s-failed">—</div><div class="stat-label">Failed</div></div>
  <div class="card text-center"><div class="stat-num text-yellow-400" id="s-queued">—</div><div class="stat-label">Queued</div></div>
</div>

<!-- Main grid -->
<div class="grid grid-cols-1 lg:grid-cols-2 gap-6">

  <!-- Search panel -->
  <div class="card lg:col-span-2">
    <h2 class="font-bold mb-3 text-slate-200">🔍 Search Knowledge Base</h2>
    <div class="flex gap-2 mb-2">
      <input type="text" id="search-q" placeholder="Ask anything..." style="flex:1">
      <select id="search-type"
        style="background:#0f172a;border:1px solid #334155;color:#e2e8f0;border-radius:6px;padding:6px 8px;font-family:inherit;font-size:0.85rem">
        <option value="">All types</option>
        <option value="pdf">PDF</option>
        <option value="word">Word</option>
        <option value="email">Email</option>
        <option value="image">Image</option>
        <option value="video">Video</option>
        <option value="text">Text/MD</option>
      </select>
      <button class="btn btn-primary" onclick="doSearch()">Search</button>
    </div>
    <div id="search-results" class="mt-3 text-slate-400 text-sm">Results will appear here.</div>
  </div>

  <!-- Recent documents -->
  <div class="card">
    <div class="flex items-center justify-between mb-3">
      <h2 class="font-bold text-slate-200">📄 Recent Documents</h2>
      <select id="doc-type-filter"
        style="background:#0f172a;border:1px solid #334155;color:#e2e8f0;border-radius:6px;padding:4px 6px;font-size:0.75rem"
        onchange="loadDocuments()">
        <option value="">All types</option>
        <option value="pdf">PDF</option>
        <option value="word">Word</option>
        <option value="email">Email</option>
        <option value="image">Image</option>
        <option value="video">Video</option>
        <option value="text">Text/MD</option>
      </select>
    </div>
    <table>
      <thead><tr><th>File</th><th>Type</th><th>Tags</th><th>Date</th></tr></thead>
      <tbody id="docs-table"></tbody>
    </table>
  </div>

  <!-- Failed files -->
  <div class="card">
    <h2 class="font-bold text-slate-200 mb-3">❌ Failed Files</h2>
    <table>
      <thead><tr><th>File</th><th>Error</th><th>Time</th></tr></thead>
      <tbody id="failed-table"></tbody>
    </table>
    <p id="no-failed" class="text-slate-500 text-sm mt-2" style="display:none">No failed files ✓</p>
  </div>

  <!-- Processing queue -->
  <div class="card lg:col-span-2">
    <h2 class="font-bold text-slate-200 mb-3">⏳ Ingestion Queue</h2>
    <table>
      <thead><tr><th>File</th><th>Status</th><th>Size</th><th>Queued</th><th>Processed</th></tr></thead>
      <tbody id="queue-table"></tbody>
    </table>
  </div>

</div>

<!-- Footer -->
<div class="mt-6 text-center text-slate-600 text-xs">
  RAGBase • Auto-refreshes every 30s •
  <span id="last-refresh">Never</span>
</div>

<script>
// ── Token management ───────────────────────────────────────────────
function getToken() { return localStorage.getItem('ragbase_token') || ''; }
function saveToken() {
  const t = document.getElementById('token-input').value.trim();
  if (!t) return;
  localStorage.setItem('ragbase_token', t);
  document.getElementById('token-modal').style.display = 'none';
  loadAll();
}
function clearToken() {
  localStorage.removeItem('ragbase_token');
  document.getElementById('token-modal').style.display = 'flex';
}

// ── API calls ──────────────────────────────────────────────────────
async function api(path, method='GET', body=null) {
  const opts = {
    method,
    headers: {
      'Authorization': 'Bearer ' + getToken(),
      'Content-Type': 'application/json',
    }
  };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  if (r.status === 401) { clearToken(); return null; }
  return r.json();
}

// ── Toast ──────────────────────────────────────────────────────────
function toast(msg, color='#065f46') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.background = color;
  el.style.display = 'block';
  setTimeout(() => el.style.display = 'none', 3000);
}

// ── Stats ──────────────────────────────────────────────────────────
async function loadStats() {
  const d = await api('/api/stats');
  if (!d) return;
  document.getElementById('s-docs').textContent    = d.total_documents.toLocaleString();
  document.getElementById('s-chunks').textContent  = d.total_chunks.toLocaleString();
  document.getElementById('s-complete').textContent = d.complete || 0;
  document.getElementById('s-failed').textContent   = d.failed || 0;
  document.getElementById('s-queued').textContent   = (d.queued || 0) + (d.processing || 0);
  document.getElementById('last-refresh').textContent = new Date().toLocaleTimeString();
}

// ── Documents ──────────────────────────────────────────────────────
async function loadDocuments() {
  const ft = document.getElementById('doc-type-filter').value;
  const d  = await api('/api/documents?limit=20' + (ft ? '&file_type='+ft : ''));
  if (!d) return;
  const tbody = document.getElementById('docs-table');
  tbody.innerHTML = d.documents.map(doc => `
    <tr>
      <td class="text-slate-200" title="${esc(doc.path)}">${esc(doc.filename)}</td>
      <td><span class="badge badge-queued">${doc.file_type}</span></td>
      <td>${(doc.tags||[]).map(t=>'<span class="tag-chip">'+t+'</span>').join('')}</td>
      <td class="text-slate-500">${doc.ingested_at.slice(0,10)}</td>
    </tr>
  `).join('');
}

// ── Queue ──────────────────────────────────────────────────────────
async function loadQueue() {
  const d = await api('/api/queue?limit=50');
  if (!d) return;
  const tbody  = document.getElementById('queue-table');
  const failed = document.getElementById('failed-table');
  const noFail = document.getElementById('no-failed');

  const queueItems  = d.items.filter(i => i.status !== 'failed');
  const failedItems = d.items.filter(i => i.status === 'failed');

  tbody.innerHTML = queueItems.map(i => `
    <tr>
      <td class="text-slate-200">${esc(i.filename)}</td>
      <td><span class="badge badge-${i.status}">${i.status}</span></td>
      <td class="text-slate-500">${formatBytes(i.file_size)}</td>
      <td class="text-slate-500">${i.queued_at.slice(0,16).replace('T',' ')}</td>
      <td class="text-slate-500">${i.processed_at ? i.processed_at.slice(0,16).replace('T',' ') : '—'}</td>
    </tr>
  `).join('') || '<tr><td colspan="5" class="text-slate-600">No items</td></tr>';

  if (failedItems.length === 0) {
    noFail.style.display = 'block';
    failed.innerHTML = '';
  } else {
    noFail.style.display = 'none';
    failed.innerHTML = failedItems.map(i => `
      <tr>
        <td class="text-red-300">${esc(i.filename)}</td>
        <td class="text-red-500 text-xs">${esc(i.error || '—')}</td>
        <td class="text-slate-500">${i.processed_at ? i.processed_at.slice(0,16).replace('T',' ') : '—'}</td>
      </tr>
    `).join('');
  }
}

// ── Search ─────────────────────────────────────────────────────────
async function doSearch() {
  const q  = document.getElementById('search-q').value.trim();
  const ft = document.getElementById('search-type').value;
  if (!q) return;

  const el = document.getElementById('search-results');
  el.innerHTML = '<span class="text-slate-400">Searching...</span>';

  const url = `/api/search?q=${encodeURIComponent(q)}&top_k=8${ft ? '&file_type='+ft : ''}`;
  const d   = await api(url);
  if (!d || !d.results.length) {
    el.innerHTML = '<span class="text-slate-500">No results found.</span>';
    return;
  }

  el.innerHTML = d.results.map(r => {
    let cite = r.filename;
    if (r.page_number) cite += ` p.${r.page_number}`;
    if (r.timestamp_start != null) {
      const ts = Math.floor(r.timestamp_start);
      cite += ` ${Math.floor(ts/60)}:${String(ts%60).padStart(2,'0')}`;
    }
    return `
      <div class="result-item">
        <div class="text-xs text-blue-400 mb-1">
          📎 ${cite} &nbsp;
          <span class="text-slate-500">score: ${r.score.toFixed(3)}</span>
        </div>
        <div class="text-slate-300 text-sm">${esc(r.content.slice(0, 400))}${r.content.length > 400 ? '...' : ''}</div>
      </div>
    `;
  }).join('');
}

// ── Actions ────────────────────────────────────────────────────────
async function triggerScan() {
  toast('Scanning Dropbox folder...', '#1d4ed8');
  const d = await api('/api/scan', 'POST');
  if (d) {
    toast(`Scan complete: ${d.complete || 0} ingested, ${d.failed || 0} failed`, '#065f46');
    loadAll();
  }
}

async function triggerBackup() {
  toast('Starting backup...', '#6d28d9');
  const d = await api('/api/backup', 'POST');
  if (d) {
    if (d.status === 'complete') {
      toast(`Backup complete: ${d.backup_file}`, '#065f46');
    } else {
      toast(`Backup failed: ${d.error}`, '#7f1d1d');
    }
  }
}

// ── Helpers ────────────────────────────────────────────────────────
function formatBytes(b) {
  if (!b) return '—';
  if (b < 1024) return b + 'B';
  if (b < 1024*1024) return (b/1024).toFixed(1) + 'KB';
  return (b/1024/1024).toFixed(1) + 'MB';
}

// FIX: Escape user-controlled content before injecting into innerHTML.
// A filename like <script>alert(1)</script>.pdf would execute without this.
// Personal tool, low severity, but wrong practice — always escape.
function esc(str) {
  const d = document.createElement('div');
  d.appendChild(document.createTextNode(String(str || '')));
  return d.innerHTML;
}

// ── Init ───────────────────────────────────────────────────────────
async function loadAll() {
  await Promise.all([loadStats(), loadDocuments(), loadQueue()]);
}

document.addEventListener('keydown', e => {
  if (e.key === 'Enter' && document.activeElement.id === 'search-q') doSearch();
  if (e.key === 'Enter' && document.activeElement.id === 'token-input') saveToken();
});

// Show token modal if no token saved
if (!getToken()) {
  document.getElementById('token-modal').style.display = 'flex';
} else {
  loadAll();
  setInterval(loadAll, 30000);
}
</script>
</body>
</html>"""


def render_dashboard() -> str:
    return _DASHBOARD_HTML