/*
 * RedactNER: Word-style live redaction editor (vanilla JS, no framework, no build step).
 *
 * Security: document text is untrusted. Every document-derived node is built with
 * createElement/textContent (never innerHTML); colours go through CSSOM (style.setProperty),
 * so the page runs under the server's strict CSP. Replacement tags are rendered with CSS
 * `content: attr(data-rep)`, so the DOM text of a paragraph always equals the segment text;
 * which keeps selection offsets exact in every view.
 *
 * Offsets: the server counts Python code points; the DOM counts UTF-16 units. Segments carry
 * conversion maps (built only when the text contains astral characters such as emoji).
 */
(function () {
  'use strict';

  // ===================================================================== constants
  const API = '/api/v1';
  const DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
  const TOKEN_PLACEHOLDER = '__REDACTNER_TOKEN__';
  const MASK = '█████'; // must match operators.MASK
  const DEBOUNCE_MS = 500;
  const TAB_KEY = 'redactner.inspectorTab'; // per-viewer convenience only
  const FALLBACK_COLORS = { CUSTOM: '#64748b' };
  const FALLBACK_COLOR = '#8a8f98';
  const AUTHOR_COLORS = ['#5b8cff', '#e0613a', '#2fa36b', '#a35bd6', '#c9921f', '#d6568f'];
  const DEVICE_NAMES = { cuda: 'GPU', mps: 'GPU', gpu: 'GPU', cpu: 'CPU' };
  const MODE_INFO = {
    label: ['Labels', 'Replaces each item with a numbered tag such as [PERSON_1], so the document stays readable.'],
    mask: ['Black-Out', 'Replaces each item with a fixed-width block so its length isn’t revealed.'],
  };
  const SOURCE_TEXT = {
    model: 'Detected by the model',
    deny: 'On your Always Redact list',
    user: 'Added by you',
  };
  const STAT_NAMES = {
    paragraphs: ['Paragraph', 'Paragraphs'],
    tables: ['Table', 'Tables'],
    headers: ['Header', 'Headers'],
    footers: ['Footer', 'Footers'],
    comments: ['Comment', 'Comments'],
    images: ['Image', 'Images'],
    hyperlinks: ['Hyperlink', 'Hyperlinks'],
    tracked_changes_accepted: ['Tracked Change Accepted', 'Tracked Changes Accepted'],
  };

  // ===================================================================== state
  /** The single source of truth for the page. */
  const state = {
    mock: false,                 // ?mock=1 → window.RedactNERMock instead of fetch
    token: '',                   // X-RedactNER-Token from <meta name="redactner-token">
    config: null,                // GET /config (normalised)
    labelInfo: new Map(),        // label -> {name, color, description}
    options: { preset: 'custom', labels: new Set(), threshold: null, mode: 'label' }, // null = tuned thresholds
    view: 'markup',              // markup | redacted | original
    changesOnly: false,
    zoom: 1,
    inspectorOpen: true,
    inspectorTab: 'changes',     // changes | settings (remembered)
    doc: null,                   // {doc_id, filename, stats, warnings, size}
    analysis: null,              // normalised analyze response (normalizeAnalysis)
    analysisKey: '',             // JSON of the request behind `analysis` (staleness)
    analysisPending: false,      // debounce timer running
    analysisQueued: false,       // options changed while a request was in flight
    excluded: new Set(),         // entity ids the user keeps (not redacted)
    userSpans: [],               // spans added by selection (see addUserSpan)
    userSeq: 0,
    lastUserLabel: '',
    replacements: new Map(),     // span id -> live replacement text ([PERSON_1] / █████)
    report: null,                // last redact report
    reportStale: false,          // review/options changed after the report
    downloadUrl: '',
    busy: { upload: false, analyze: false, redact: false, download: false },
    pendingSelection: null,      // {segId, uStart, uEnd} behind "Redact Selection As"
  };

  const ui = {};
  let transport = null;
  let markEls = new Map();       // span id -> .rd element on the page
  let paraEls = new Map();       // segment id -> paragraph element
  const spanOfEl = new WeakMap();
  const jumpIndex = new Map();
  let analysisTimer = 0;
  let pointerDown = false;
  let selectionTimer = 0;
  const tip = { timer: 0, target: null };
  let openPop = null;

  // ===================================================================== helpers
  const $ = (id) => document.getElementById(id);
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(v, hi));
  const round2 = (v) => Math.round(v * 100) / 100;
  const fmtInt = (n) => Number(n || 0).toLocaleString();
  const fmtScore = (s) => (Number.isFinite(Number(s)) ? Number(s).toFixed(2) : 'n/a');
  const plural = (n, one, many) => `${fmtInt(n)} ${Number(n) === 1 ? one : many}`;
  const titleCase = (s) => String(s).replace(/[_-]+/g, ' ').toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
  const humanLabel = (label) => String(label).replace(/_/g, ' ');
  const isNarrow = () => window.matchMedia('(max-width: 1100px)').matches;
  const scrollBehavior = () => (window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth');

  function fmtMs(ms) {
    const n = Number(ms);
    if (!Number.isFinite(n)) return 'n/a';
    return n >= 1000 ? `${(n / 1000).toFixed(1)} s` : `${Math.round(n)} ms`;
  }

  function fmtBytes(n) {
    if (!Number.isFinite(n)) return '';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  }

  /** Create an element. props: text, class, dataset, or any attribute (true → empty attribute). */
  function h(tag, props, children) {
    const node = document.createElement(tag);
    if (props) {
      for (const [key, value] of Object.entries(props)) {
        if (value === undefined || value === null || value === false) continue;
        if (key === 'text') node.textContent = String(value);
        else if (key === 'class') node.className = value;
        else if (key === 'dataset') Object.assign(node.dataset, value);
        else node.setAttribute(key, value === true ? '' : String(value));
      }
    }
    if (children !== undefined && children !== null) {
      for (const child of [].concat(children)) if (child !== undefined && child !== null) node.append(child);
    }
    return node;
  }

  function fillSelect(select, pairs) {
    select.replaceChildren(...pairs.map(([value, text]) => h('option', { value, text })));
  }

  function parseTerms(text) {
    const seen = new Set();
    const out = [];
    for (const line of String(text || '').split(/\r?\n/)) {
      const term = line.trim();
      if (term && !seen.has(term)) { seen.add(term); out.push(term); }
    }
    return out;
  }

  /** Label names in config order; unknown labels (e.g. CUSTOM) last. */
  function orderedLabelNames(names) {
    const set = new Set(names);
    const known = state.config ? state.config.labels.map((l) => l.name).filter((n) => set.has(n)) : [];
    return known.concat([...set].filter((n) => !state.labelInfo.has(n)).sort());
  }

  function bump(map, key) { map.set(key, (map.get(key) || 0) + 1); }

  // ===================================================================== colours
  function hexToRgb(hex) {
    const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(String(hex || '').trim());
    if (!m) return null;
    let v = m[1];
    if (v.length === 3) v = v.split('').map((c) => c + c).join('');
    const n = parseInt(v, 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  function labelColor(name) {
    const info = state.labelInfo.get(name);
    return (info && info.color) || FALLBACK_COLORS[name] || FALLBACK_COLOR;
  }

  /** Expose a label colour to CSS as --c-rgb (+ readable --c-fg). CSSOM is CSP-safe. */
  function paintLabel(node, name) {
    const rgb = hexToRgb(labelColor(name)) || hexToRgb(FALLBACK_COLOR);
    const luminance = (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255;
    node.style.setProperty('--c-rgb', rgb.join(', '));
    node.style.setProperty('--c-fg', luminance > 0.45 ? '#111111' : '#ffffff');
  }

  // ===================================================================== offsets (code points <-> UTF-16)
  function buildOffsetMaps(text) {
    if (!/[\uD800-\uDFFF]/.test(text)) return null;
    const cpToUnit = [];
    const unitToCp = new Array(text.length + 1);
    let cp = 0;
    let u = 0;
    while (u < text.length) {
      const c = text.charCodeAt(u);
      const pair = c >= 0xd800 && c <= 0xdbff && u + 1 < text.length && (text.charCodeAt(u + 1) & 0xfc00) === 0xdc00;
      cpToUnit.push(u);
      unitToCp[u] = cp;
      if (pair) unitToCp[u + 1] = cp + 1; // mid-pair: never used as a boundary (selections are snapped)
      u += pair ? 2 : 1;
      cp += 1;
    }
    cpToUnit.push(u);
    unitToCp[u] = cp;
    return { cpToUnit, unitToCp };
  }

  function cpToUnits(seg, cp) {
    const n = clamp(Math.trunc(Number(cp)) || 0, 0, seg.cpLength);
    return seg.maps ? seg.maps.cpToUnit[n] : n;
  }

  function unitsToCp(seg, u) {
    const n = clamp(u, 0, seg.text.length);
    return seg.maps ? seg.maps.unitToCp[n] : n;
  }

  function isLowSurrogateAt(text, u) {
    if (u <= 0 || u >= text.length) return false;
    const c = text.charCodeAt(u);
    return c >= 0xdc00 && c <= 0xdfff && (text.charCodeAt(u - 1) & 0xfc00) === 0xd800;
  }

  // ===================================================================== toasts
  /** action: optional {label, run} rendered as a button (e.g. Retry). */
  function toast(message, type = 'info', timeout, action) {
    const box = h('div', { class: `toast toast-${type}`, role: type === 'error' ? 'alert' : 'status' });
    const close = h('button', { type: 'button', class: 'toast-close', 'aria-label': 'Dismiss Notification', text: '×' });
    close.addEventListener('click', () => box.remove());
    box.append(h('div', { class: 'toast-msg', text: message }));
    if (action) {
      const btn = h('button', { type: 'button', class: 'toast-action', text: action.label });
      btn.addEventListener('click', () => { box.remove(); action.run(); });
      box.append(btn);
    }
    box.append(close);
    ui.toasts.append(box);
    while (ui.toasts.children.length > 4) ui.toasts.firstElementChild.remove();
    const ms = timeout !== undefined ? timeout : type === 'error' ? 10000 : 4500;
    if (ms > 0) setTimeout(() => box.remove(), ms);
  }

  function toastError(err, prefix) {
    const msg = err && err.message ? err.message : String(err);
    toast(prefix ? `${prefix}: ${msg}` : msg, 'error');
    if (!(err instanceof ApiError)) console.error(err);
  }

  // ===================================================================== API
  class ApiError extends Error {
    constructor(status, message) { super(message); this.name = 'ApiError'; this.status = status; }
  }

  async function errorDetail(res) {
    let text = '';
    try { text = await res.text(); } catch (_) { /* ignore */ }
    if (text) {
      try {
        const detail = JSON.parse(text).detail;
        if (typeof detail === 'string' && detail) return detail;
        if (Array.isArray(detail) && detail.length) {
          return detail.map((d) => {
            if (!d || !d.msg) return JSON.stringify(d);
            const where = Array.isArray(d.loc) ? d.loc.filter((p) => p !== 'body').join('.') : '';
            return where ? `${where}: ${d.msg}` : d.msg;
          }).join('; ');
        }
        if (detail) return JSON.stringify(detail);
      } catch (_) { /* not JSON */ }
    }
    return `Request failed (HTTP ${res.status}${res.statusText ? ` ${res.statusText}` : ''})`;
  }

  async function apiRequest(url, { method = 'GET', json, body, headers = {}, raw = false } = {}) {
    const init = { method, cache: 'no-store', headers: Object.assign({ 'X-RedactNER-Token': state.token }, headers) };
    if (json !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(json);
    } else if (body !== undefined) {
      init.body = body;
    }
    let res;
    try {
      res = await transport(url, init);
    } catch (err) {
      throw new ApiError(0, state.mock
        ? `Mock backend failed: ${err && err.message ? err.message : err}`
        : 'Cannot reach the RedactNER server. Is it still running?');
    }
    if (!res.ok) throw new ApiError(res.status, await errorDetail(res));
    if (raw) return res;
    if (res.status === 204) return null;
    const text = await res.text();
    if (!text) return null;
    try { return JSON.parse(text); } catch (_) { throw new ApiError(res.status, 'The server sent an unexpected (non-JSON) response.'); }
  }

  const docUrl = (id, suffix = '') => `${API}/documents/${encodeURIComponent(id)}${suffix}`;
  const api = {
    config: () => apiRequest(`${API}/config`),
    upload: (file) => apiRequest(`${API}/documents`, {
      method: 'POST',
      body: file, // raw bytes, not multipart
      headers: { 'Content-Type': DOCX_MIME, 'X-Filename': encodeURIComponent(file.name) },
    }),
    analyze: (id, req) => apiRequest(docUrl(id, '/analyze'), { method: 'POST', json: req }),
    redact: (id, req) => apiRequest(docUrl(id, '/redact'), { method: 'POST', json: req }),
    download: (url) => apiRequest(url, { raw: true }),
    remove: (id) => apiRequest(docUrl(id), { method: 'DELETE' }),
  };

  // ===================================================================== replacement preview (mirrors operators.py)
  /** operators.normalise_value: strip+lower; digit-heavy → digits only, else collapse whitespace. */
  function normaliseValue(value) {
    const v = value.trim().toLowerCase();
    const length = Array.from(v).length;
    const digits = (v.match(/\p{Nd}/gu) || []).length;
    if (digits >= Math.max(4, Math.floor(length / 2))) return v.replace(/\P{Nd}/gu, '');
    return v.replace(/\s+/g, ' ');
  }

  /** detector._priority: (user decision, score, length). */
  function comparePriority(a, b) {
    const va = ['deny', 'user'].includes(a.source) ? 1 : 0;
    const vb = ['deny', 'user'].includes(b.source) ? 1 : 0;
    return (va - vb) || (a.score - b.score) || ((a.end - a.start) - (b.end - b.start));
  }

  /** detector.resolve_overlaps: union of overlapping spans, label from the strongest member. */
  function resolveOverlaps(spans) {
    const ordered = [...spans].sort((a, b) => a.uStart - b.uStart || b.uEnd - a.uEnd);
    const clusters = [];
    for (const sp of ordered) {
      const last = clusters[clusters.length - 1];
      if (last && sp.uStart < last.uEnd) {
        last.members.push(sp);
        last.uEnd = Math.max(last.uEnd, sp.uEnd);
      } else {
        clusters.push({ uStart: sp.uStart, uEnd: sp.uEnd, members: [sp] });
      }
    }
    for (const c of clusters) {
      let best = c.members[0];
      for (let i = 1; i < c.members.length; i += 1) if (comparePriority(c.members[i], best) > 0) best = c.members[i];
      c.label = best.label;
    }
    return clusters;
  }

  /** Live replacement text for every active span, numbered exactly like ReplacementBuilder. */
  function computeReplacements() {
    const reps = new Map();
    if (!state.analysis) return reps;
    const mask = state.options.mode === 'mask';
    const numbers = new Map();
    const counters = new Map();
    const superseded = supersededIds();
    for (const seg of state.analysis.segments) { // document order, like pipeline.redact
      const spans = activeSpansForSegment(seg, superseded);
      if (!spans.length) continue;
      for (const cluster of resolveOverlaps(spans)) {
        let rep = MASK;
        if (!mask) {
          const tag = cluster.label === 'CUSTOM' ? 'REDACTED' : cluster.label;
          const key = `${tag}\u0000${normaliseValue(seg.text.slice(cluster.uStart, cluster.uEnd))}`;
          let n = numbers.get(key);
          if (n === undefined) {
            n = (counters.get(tag) || 0) + 1;
            counters.set(tag, n);
            numbers.set(key, n);
          }
          rep = `[${tag}_${n}]`;
        }
        for (const m of cluster.members) reps.set(m.id, rep);
      }
    }
    return reps;
  }

  // ===================================================================== config & options
  function normalizeConfig(cfg) {
    const c = Object.assign({}, cfg || {});
    c.labels = (Array.isArray(c.labels) ? c.labels : []).filter((l) => l && l.name).map((l) => Object.assign({}, l, { name: String(l.name) }));
    c.presets = c.presets && typeof c.presets === 'object' ? c.presets : {};
    c.modes = Array.isArray(c.modes) && c.modes.length ? c.modes : [{ id: 'label', name: 'Labels' }];
    return c;
  }

  function presetLabels(name) {
    const list = state.config && state.config.presets[name];
    return Array.isArray(list) ? list.filter((n) => state.labelInfo.has(n)) : null;
  }

  function presetFor(labels) {
    for (const name of Object.keys(state.config.presets)) {
      const list = presetLabels(name);
      if (list && list.length === labels.size && list.every((n) => labels.has(n))) return name;
    }
    return 'custom';
  }

  function modeTitle(mode) {
    if (MODE_INFO[mode.id]) return MODE_INFO[mode.id][0];
    return titleCase(String(mode.name || mode.id).replace(/\s*\(.*\)\s*$/, ''));
  }

  function initOptions() {
    const cfg = state.config;
    state.labelInfo = new Map(cfg.labels.map((l) => [l.name, l]));

    const presets = Object.keys(cfg.presets).filter((n) => presetLabels(n));
    fillSelect(ui.preset, presets.map((n) => [n, titleCase(n)]).concat([['custom', 'Custom']]));
    state.options.labels = new Set(presetLabels(cfg.default_preset) || cfg.labels.filter((l) => l.default_on).map((l) => l.name));
    state.options.preset = presetFor(state.options.labels);
    ui.preset.value = state.options.preset;
    renderTypesGrid();

    state.options.threshold = null;
    ui.confidence.value = String(defaultConfidence());
    showConfidence();

    renderModes(cfg.modes);
    fillSelect(ui.selLabel, cfg.labels.map((l) => [l.name, humanLabel(l.name)]));
    if (cfg.max_upload_mb) ui.maxMb.textContent = String(cfg.max_upload_mb);
    for (const control of [ui.preset, ui.confidence]) control.disabled = false;
  }

  function renderTypesGrid() {
    ui.typesGrid.replaceChildren();
    for (const info of state.config.labels) {
      const input = h('input', { type: 'checkbox', value: info.name });
      input.checked = state.options.labels.has(info.name);
      const toggle = h('label', {
        class: `type-chip${input.checked ? ' is-on' : ''}`,
        'data-tip-title': humanLabel(info.name),
        'data-tip': info.description || '',
      }, [input, h('span', { class: 'chip-dot', 'aria-hidden': 'true' }), h('span', { text: humanLabel(info.name) })]);
      paintLabel(toggle, info.name);
      ui.typesGrid.append(toggle);
    }
    updateTypesCount();
  }

  function syncTypeStates() {
    for (const input of ui.typesGrid.querySelectorAll('input[type="checkbox"]')) {
      input.checked = state.options.labels.has(input.value);
      input.closest('.type-chip').classList.toggle('is-on', input.checked);
    }
    updateTypesCount();
  }

  function updateTypesCount() {
    ui.typesCount.textContent = `${fmtInt(state.options.labels.size)} Selected`;
  }

  function renderModes(modes) {
    if (!modes.some((m) => String(m.id) === state.options.mode)) state.options.mode = String(modes[0].id);
    ui.modeGroup.replaceChildren();
    for (const mode of modes) {
      const id = String(mode.id);
      const input = h('input', { type: 'radio', name: 'mode', value: id });
      input.checked = id === state.options.mode;
      const info = MODE_INFO[id];
      ui.modeGroup.append(h('label', {
        class: 'seg-opt',
        'data-tip-title': modeTitle(mode),
        'data-tip': info ? info[1] : String(mode.name || id),
      }, [input, h('span', { text: modeTitle(mode) })]));
    }
  }

  function buildAnalyzeRequest() {
    return {
      labels: orderedLabelNames(state.options.labels),
      threshold: state.options.threshold,
      deny_terms: parseTerms(ui.denyTerms.value),
      allow_terms: parseTerms(ui.allowTerms.value),
    };
  }

  function isStale() {
    return !!state.analysis && JSON.stringify(buildAnalyzeRequest()) !== state.analysisKey;
  }

  /** Any detection option changed: refresh the settings and re-analyse after a short pause. */
  function optionsChanged() {
    updateTypesCount();
    updateTermCounts();
    if (state.doc) scheduleAnalysis(false);
    updateUI();
  }

  function updateTermCounts() {
    const deny = parseTerms(ui.denyTerms.value).length;
    const allow = parseTerms(ui.allowTerms.value).length;
    ui.denyCount.textContent = plural(deny, 'Term', 'Terms');
    ui.denyCount.hidden = deny === 0;
    ui.allowCount.textContent = plural(allow, 'Term', 'Terms');
    ui.allowCount.hidden = allow === 0;
  }

  function setLabels(names) {
    state.options.labels = new Set(names);
    syncTypeStates();
    state.options.preset = presetFor(state.options.labels);
    ui.preset.value = state.options.preset;
    optionsChanged();
  }

  function onPresetChange() {
    state.options.preset = ui.preset.value;
    const list = presetLabels(ui.preset.value);
    if (list) { state.options.labels = new Set(list); syncTypeStates(); }
    optionsChanged();
  }

  function onTypeToggle(event) {
    const input = event.target;
    if (!input || input.type !== 'checkbox') return;
    if (input.checked) state.options.labels.add(input.value);
    else state.options.labels.delete(input.value);
    input.closest('.type-chip').classList.toggle('is-on', input.checked);
    state.options.preset = presetFor(state.options.labels); // edits → Custom unless they equal a preset
    ui.preset.value = state.options.preset;
    optionsChanged();
  }

  function defaultConfidence() {
    return clamp(round2(Number(state.config && state.config.default_threshold) || 0.5), 0.01, 0.95);
  }

  /** The slider shows "Tuned" until it is moved: the model then applies the thresholds tuned on held-out data. */
  function showConfidence() {
    const tuned = state.options.threshold === null;
    ui.confidenceValue.textContent = tuned ? 'Tuned' : state.options.threshold.toFixed(2);
    ui.confidenceReset.hidden = tuned;
  }

  function onConfidenceInput() {
    state.options.threshold = round2(Number(ui.confidence.value));
    showConfidence();
    optionsChanged();
  }

  function onConfidenceReset() {
    state.options.threshold = null;
    ui.confidence.value = String(defaultConfidence());
    showConfidence();
    optionsChanged();
  }

  function onModeChange(event) {
    if (!event.target || event.target.name !== 'mode') return;
    state.options.mode = event.target.value;
    if (state.analysis) {
      refreshReplacements(true); // instant: no server call
      if (state.report) state.reportStale = true;
      renderReview();
    }
    updateUI();
  }

  // ===================================================================== view controls
  function setView(view) {
    state.view = view;
    ui.pages.classList.remove('view-markup', 'view-redacted', 'view-original');
    ui.pages.classList.add(`view-${view}`);
    for (const input of ui.viewRadios) input.checked = input.value === view; // segmented switch
    ui.viewSelect.value = view; // narrow-screen dropdown
    hideTip();
  }

  function toggleChangesOnly() {
    state.changesOnly = !state.changesOnly;
    ui.btnChangesOnly.setAttribute('aria-checked', state.changesOnly ? 'true' : 'false');
    ui.pages.classList.toggle('changes-only', state.changesOnly);
  }

  /** Show/hide the inspector; `tab` (changes | settings) is remembered for next time. */
  function setInspector(open, tab) {
    if (tab && tab !== state.inspectorTab) {
      state.inspectorTab = tab;
      try { window.localStorage.setItem(TAB_KEY, tab); } catch (_) { /* storage unavailable */ }
    }
    state.inspectorOpen = open;
    const settings = state.inspectorTab === 'settings';
    ui.inspector.classList.toggle('is-collapsed', !open);
    ui.tabChanges.setAttribute('aria-selected', settings ? 'false' : 'true');
    ui.tabSettings.setAttribute('aria-selected', settings ? 'true' : 'false');
    ui.tabChanges.tabIndex = settings ? -1 : 0;
    ui.tabSettings.tabIndex = settings ? 0 : -1;
    ui.panelChanges.hidden = settings;
    ui.panelSettings.hidden = !settings;
    ui.btnSettings.setAttribute('aria-expanded', open ? 'true' : 'false');
    ui.btnSettings.setAttribute('aria-pressed', open && settings ? 'true' : 'false');
  }

  /** Gear: closed → open on Settings; on Settings → close; on Changes → switch to Settings. */
  function onSettingsButton() {
    if (state.inspectorOpen && state.inspectorTab === 'settings') setInspector(false);
    else setInspector(true, 'settings');
  }

  function onTabKeydown(event) {
    const keys = { ArrowLeft: 'changes', ArrowRight: 'settings', Home: 'changes', End: 'settings' };
    const tab = keys[event.key];
    if (!tab) return;
    event.preventDefault();
    setInspector(true, tab);
    (tab === 'settings' ? ui.tabSettings : ui.tabChanges).focus();
  }

  // ===================================================================== upload / start over
  function hasFiles(event) {
    return !!(event.dataTransfer && Array.from(event.dataTransfer.types || []).includes('Files'));
  }

  function validateFile(file) {
    if (!/\.docx$/i.test(file.name)) return `“${file.name}” is not a Word .docx file. Re-save older .doc files as .docx first.`;
    if (file.size === 0) return `“${file.name}” is empty.`;
    const maxMb = Number(state.config.max_upload_mb) || 20;
    if (file.size > maxMb * 1024 * 1024) return `“${file.name}” is ${fmtBytes(file.size)}; the limit is ${maxMb} MB.`;
    return null;
  }

  function openFilePicker() {
    if (!state.busy.upload && !state.busy.redact) ui.fileInput.click();
  }

  async function handleFile(file) {
    if (!file) return;
    if (state.busy.upload || state.busy.redact) { toast('Please wait for the current step to finish.', 'warn'); return; }
    if (!state.config) { toast('Still connecting to the RedactNER server…', 'warn'); return; }
    const problem = validateFile(file);
    if (problem) { toast(problem, 'error'); return; }

    setBusy('upload', true);
    let res = null;
    try {
      res = await api.upload(file);
    } catch (err) {
      toastError(err, 'Could Not Open Document');
    } finally {
      state.busy.upload = false;
      ui.fileInput.value = '';
    }
    if (res) {
      const previous = state.doc;
      resetDocument();
      state.doc = {
        doc_id: String(res.doc_id),
        filename: res.filename || file.name,
        stats: res.stats || {},
        warnings: Array.isArray(res.warnings) ? res.warnings : [],
        size: file.size,
      };
      if (previous && previous.doc_id !== state.doc.doc_id) api.remove(previous.doc_id).catch(() => {});
      renderDocMeta();
      renderReview();
      scheduleAnalysis(true);
    }
    updateUI();
  }

  function resetDocument() {
    clearTimeout(analysisTimer);
    Object.assign(state, {
      doc: null, analysis: null, analysisKey: '', analysisPending: false, analysisQueued: false,
      excluded: new Set(), userSpans: [], replacements: new Map(),
      report: null, reportStale: false, downloadUrl: '', pendingSelection: null,
    });
    jumpIndex.clear();
    renderDocument();
    renderDocMeta();
    renderReview();
    updateUI();
  }

  async function startOver() {
    if (isBusy()) return;
    const unsaved = (state.excluded.size > 0 || state.userSpans.length > 0) && !state.report;
    if (unsaved && !window.confirm('Close this document and discard your review changes?')) return;
    const doc = state.doc;
    resetDocument();
    ui.canvas.scrollTop = 0;
    ui.btnFile.focus({ preventScroll: true });
    if (!doc) return;
    try {
      await api.remove(doc.doc_id);
      toast('Document closed and deleted from the local server.', 'success', 3500);
    } catch (err) {
      if (err.status !== 404) toastError(err, 'Could Not Delete The Document');
    }
  }

  function renderDocMeta() {
    const doc = state.doc;
    ui.docStats.replaceChildren();
    ui.docWarnings.replaceChildren();
    if (!doc) return;
    for (const [key, value] of Object.entries(doc.stats)) {
      const n = Number(value) || 0;
      if (n === 0) continue;
      const names = STAT_NAMES[key] || [titleCase(key), titleCase(key)];
      ui.docStats.append(h('li', { class: 'chip' }, [h('b', { text: fmtInt(n) }), ` ${n === 1 ? names[0] : names[1]}`]));
    }
    ui.docWarnings.replaceChildren(...doc.warnings.map((w) => h('li', { text: String(w) })));
  }

  // ===================================================================== analysis
  function normalizeTable(t) {
    if (!t || typeof t !== 'object') return null;
    const r = Math.trunc(Number(t.r));
    const c = Math.trunc(Number(t.c));
    if (!Number.isFinite(r) || !Number.isFinite(c) || r < 0 || c < 0 || r > 2000 || c > 64) return null;
    const rows = clamp(Math.trunc(Number(t.rows)) || 0, r + 1, 2000);
    const cols = clamp(Math.trunc(Number(t.cols)) || 0, c + 1, 64);
    return { t: String(t.t === undefined || t.t === null ? '0' : t.t), r, c, rows, cols };
  }

  function normalizeAnalysis(res) {
    const segments = [];
    const segById = new Map();
    for (const raw of (res && Array.isArray(res.segments) ? res.segments : [])) {
      if (!raw || raw.id === undefined || raw.id === null) continue;
      const text = typeof raw.text === 'string' ? raw.text : String(raw.text === undefined || raw.text === null ? '' : raw.text);
      const maps = buildOffsetMaps(text);
      const seg = {
        id: String(raw.id),
        location: String(raw.location || 'body'),
        part: String(raw.part || ''),
        text,
        maps,
        cpLength: maps ? maps.cpToUnit.length - 1 : text.length,
        style: raw.style ? String(raw.style) : null,
        align: raw.align ? String(raw.align) : null,
        list: !!raw.list,
        table: normalizeTable(raw.table),
        hidden: !!raw.hidden,
        author: raw.author === undefined || raw.author === null ? null : String(raw.author),
        fmt: [],
        fmtBounds: [],
      };
      for (const m of Array.isArray(raw.marks) ? raw.marks : []) {
        if (!m || !(m.b || m.i || m.u)) continue;
        const uStart = cpToUnits(seg, m.s);
        const uEnd = cpToUnits(seg, m.e);
        if (uEnd > uStart) seg.fmt.push({ uStart, uEnd, b: !!m.b, i: !!m.i, u: !!m.u });
      }
      seg.fmtBounds = [...new Set(seg.fmt.flatMap((f) => [f.uStart, f.uEnd]))].sort((a, b) => a - b);
      segments.push(seg);
      segById.set(seg.id, seg);
    }

    const entities = [];
    const entityById = new Map();
    const entitiesBySeg = new Map();
    for (const raw of (res && Array.isArray(res.entities) ? res.entities : [])) {
      const seg = raw && segById.get(String(raw.segment_id));
      if (!seg || raw.id === undefined || raw.id === null) continue;
      const uStart = cpToUnits(seg, raw.start);
      const uEnd = cpToUnits(seg, raw.end);
      if (!(uEnd > uStart)) continue;
      const score = Number(raw.score);
      const ent = {
        kind: 'entity', id: String(raw.id), segment_id: seg.id,
        start: unitsToCp(seg, uStart), end: unitsToCp(seg, uEnd), uStart, uEnd,
        label: String(raw.label || 'CUSTOM'), score: Number.isFinite(score) ? score : 1, source: String(raw.source || 'model'),
      };
      entities.push(ent);
      entityById.set(ent.id, ent);
      if (!entitiesBySeg.has(seg.id)) entitiesBySeg.set(seg.id, []);
      entitiesBySeg.get(seg.id).push(ent);
    }

    // Hidden segments are duplicate text-box copies: pair each with its visible twin (same text) so
    // the user's decisions on the visible copy are mirrored into the hidden one.
    const twinOf = new Map();
    const hiddenTwins = new Map();
    segments.forEach((seg, i) => {
      if (!seg.hidden) return;
      let twin = null;
      for (let j = i - 1; j >= 0 && j >= i - 300 && !twin; j -= 1) if (!segments[j].hidden && segments[j].text === seg.text) twin = segments[j];
      for (let j = i + 1; j < segments.length && j <= i + 300 && !twin; j += 1) if (!segments[j].hidden && segments[j].text === seg.text) twin = segments[j];
      if (!twin) return;
      twinOf.set(seg.id, twin.id);
      if (!hiddenTwins.has(twin.id)) hiddenTwins.set(twin.id, []);
      hiddenTwins.get(twin.id).push(seg.id);
    });
    const twinEntity = new Map();
    for (const [hiddenId, visibleId] of twinOf) {
      const visible = entitiesBySeg.get(visibleId) || [];
      for (const e of entitiesBySeg.get(hiddenId) || []) {
        const match = visible.find((v) => v.start === e.start && v.end === e.end && v.label === e.label);
        if (match) twinEntity.set(e.id, match);
      }
    }
    return { segments, segById, entities, entityById, entitiesBySeg, twinOf, hiddenTwins, twinEntity, elapsed_ms: res ? res.elapsed_ms : undefined };
  }

  const spanKey = (e) => `${e.segment_id}|${e.start}|${e.end}|${e.label}`;

  /** Install a new analysis, carrying over the user's kept items and added spans. */
  function applyAnalysis(res, req) {
    const prev = state.analysis;
    const keptKeys = new Set();
    if (prev) {
      for (const id of state.excluded) {
        const e = prev.entityById.get(id);
        if (e) keptKeys.add(spanKey(e));
      }
    }
    const prevUserSpans = state.userSpans;
    state.analysis = normalizeAnalysis(res);
    state.analysisKey = JSON.stringify(req);
    state.excluded = new Set(state.analysis.entities.filter((e) => keptKeys.has(spanKey(e))).map((e) => e.id));
    state.userSpans = [];
    for (const u of prevUserSpans) {
      const seg = state.analysis.segById.get(u.segment_id);
      if (seg && !seg.hidden && seg.text.slice(u.uStart, u.uEnd) === u.text) addUserSpan(seg.id, u.uStart, u.uEnd, u.label);
    }
    if (state.report) state.reportStale = true;
    jumpIndex.clear();
  }

  function scheduleAnalysis(immediate) {
    clearTimeout(analysisTimer);
    if (!state.doc) return;
    state.analysisPending = true;
    analysisTimer = setTimeout(() => {
      state.analysisPending = false;
      startAnalysis(false);
    }, immediate ? 0 : DEBOUNCE_MS);
    updateUI();
  }

  async function startAnalysis(force) {
    if (!state.doc) return;
    if (state.busy.analyze || state.busy.upload || state.busy.redact || state.busy.download) {
      state.analysisQueued = true; // one request at a time keeps the server's stored analysis in step
      return;
    }
    if (!force && state.analysis && !isStale()) { updateUI(); return; }
    clearTimeout(analysisTimer);
    state.analysisPending = false;
    const req = buildAnalyzeRequest();
    const docId = state.doc.doc_id;
    setBusy('analyze', true);
    let res = null;
    try {
      res = await api.analyze(docId, req);
    } catch (err) {
      const msg = err && err.message ? err.message : String(err);
      toast(`Analysis Failed: ${msg}`, 'error', 15000, { label: 'Retry', run: () => startAnalysis(true) });
    } finally {
      state.busy.analyze = false;
    }
    if (res && state.doc && state.doc.doc_id === docId) {
      const first = !state.analysis;
      applyAnalysis(res, req);
      renderDocument();
      renderReview();
      if (first) ui.canvas.scrollTop = 0;
    }
    updateUI();
    runQueuedAnalysis();
  }

  function runQueuedAnalysis() {
    if (!state.analysisQueued || isBusy()) return;
    state.analysisQueued = false;
    if (state.doc && (!state.analysis || isStale())) startAnalysis(false);
  }

  // ===================================================================== spans (exclusions, user additions)
  function supersededIds() {
    const ids = new Set();
    for (const u of state.userSpans) for (const id of u.supersedes) ids.add(id);
    return ids;
  }

  /** Whether an entity will be redacted. Hidden copies follow their visible twin. */
  function isEntityActive(e, superseded) {
    const twin = state.analysis.twinEntity.get(e.id);
    if (twin) return isEntityActive(twin, superseded);
    const seg = state.analysis.segById.get(e.segment_id);
    if (seg && seg.hidden) return true;
    return !superseded.has(e.id) && !state.excluded.has(e.id);
  }

  /** Spans that will be replaced in a segment (hidden copies get mirrored user spans). */
  function activeSpansForSegment(seg, superseded) {
    const out = (state.analysis.entitiesBySeg.get(seg.id) || []).filter((e) => isEntityActive(e, superseded));
    const source = seg.hidden ? state.analysis.twinOf.get(seg.id) : seg.id;
    if (!source) return out;
    for (const u of state.userSpans) {
      if (u.segment_id !== source) continue;
      out.push(seg.hidden ? Object.assign({}, u, { id: `${u.id}@${seg.id}`, segment_id: seg.id }) : u);
    }
    return out;
  }

  /** Spans drawn in a visible paragraph: entities not covered by a user span, plus user spans. */
  function visibleSpans(segId, superseded = supersededIds()) {
    const entities = (state.analysis.entitiesBySeg.get(segId) || []).filter((e) => !superseded.has(e.id));
    const users = state.userSpans.filter((u) => u.segment_id === segId);
    return entities.concat(users).sort((a, b) => a.uStart - b.uStart || b.uEnd - a.uEnd);
  }

  /**
   * Add a user span. Overlapping highlights are absorbed: the span grows to their union, covered
   * entities are excluded and covered user spans merged, so highlights never overlap.
   */
  function addUserSpan(segId, uStart, uEnd, label) {
    const seg = state.analysis.segById.get(segId);
    if (!seg || !(uEnd > uStart)) return null;
    let s = uStart;
    let e = uEnd;
    const candidates = visibleSpans(segId);
    const hit = new Set();
    let grew = true;
    while (grew) {
      grew = false;
      for (const c of candidates) {
        if (hit.has(c) || !(c.uStart < e && s < c.uEnd)) continue;
        hit.add(c);
        if (c.uStart < s) { s = c.uStart; grew = true; }
        if (c.uEnd > e) { e = c.uEnd; grew = true; }
      }
    }
    const supersedes = [];
    for (const c of hit) {
      if (c.kind === 'user') {
        supersedes.push(...c.supersedes);
        state.userSpans = state.userSpans.filter((u) => u !== c);
      } else {
        supersedes.push(c.id);
      }
    }
    const span = {
      kind: 'user', id: `u${++state.userSeq}`, segment_id: segId,
      uStart: s, uEnd: e, start: unitsToCp(seg, s), end: unitsToCp(seg, e),
      label, score: 1, source: 'user', text: seg.text.slice(s, e), supersedes,
    };
    state.userSpans.push(span);
    return span;
  }

  function removeUserSpan(id) {
    state.userSpans = state.userSpans.filter((u) => u.id !== id);
  }

  /** Counts over what the user can see (hidden duplicate copies excluded). */
  function computeCounts() {
    const counts = { total: new Map(), active: new Map(), redactions: 0, kept: 0, added: state.userSpans.length };
    if (!state.analysis) return counts;
    const superseded = supersededIds();
    for (const e of state.analysis.entities) {
      const seg = state.analysis.segById.get(e.segment_id);
      if (!seg || seg.hidden || superseded.has(e.id)) continue;
      bump(counts.total, e.label);
      if (state.excluded.has(e.id)) { counts.kept += 1; continue; }
      bump(counts.active, e.label);
      counts.redactions += 1;
    }
    for (const u of state.userSpans) {
      bump(counts.total, u.label);
      bump(counts.active, u.label);
      counts.redactions += 1;
    }
    return counts;
  }

  // ===================================================================== document rendering
  function paraClass(seg) {
    const cls = ['para'];
    const style = seg.style ? seg.style.toLowerCase().replace(/[\s_-]+/g, '') : '';
    if (style === 'title') cls.push('p-title');
    else if (style === 'subtitle') cls.push('p-subtitle');
    else if (style === 'heading1') cls.push('p-h1');
    else if (style === 'heading2') cls.push('p-h2');
    else if (/^heading[3-9]$/.test(style)) cls.push('p-h3');
    else if (style === 'quote' || style === 'intensequote') cls.push('p-quote');
    else if (style === 'caption') cls.push('p-caption');
    if (seg.list) cls.push('is-list');
    if (seg.align === 'center' || seg.align === 'right' || seg.align === 'both') cls.push(`al-${seg.align}`);
    return cls.join(' ');
  }

  /** Append seg.text[a:b] with its bold/italic/underline runs. */
  function appendFormatted(parent, seg, a, b) {
    if (b <= a) return;
    if (!seg.fmt.length) { parent.append(document.createTextNode(seg.text.slice(a, b))); return; }
    const cuts = [a];
    for (const x of seg.fmtBounds) if (x > a && x < b) cuts.push(x);
    cuts.push(b);
    for (let i = 0; i < cuts.length - 1; i += 1) {
      const s = cuts[i];
      const e = cuts[i + 1];
      let bold = false;
      let italic = false;
      let under = false;
      for (const f of seg.fmt) {
        if (f.uStart <= s && f.uEnd >= e) { bold = bold || f.b; italic = italic || f.i; under = under || f.u; }
      }
      const node = document.createTextNode(seg.text.slice(s, e));
      if (bold || italic || under) {
        const classes = [bold ? 'fb' : '', italic ? 'fi' : '', under ? 'fu' : ''].filter(Boolean).join(' ');
        parent.append(h('span', { class: classes }, node));
      } else {
        parent.append(node);
      }
    }
  }

  function applyReplacement(el, sp) {
    const isUser = sp.kind === 'user';
    const kept = !isUser && state.excluded.has(sp.id);
    const rep = kept ? '' : state.replacements.get(sp.id) || '';
    if (rep) {
      if (el.dataset.rep !== rep) el.dataset.rep = rep;
    } else if ('rep' in el.dataset) {
      delete el.dataset.rep;
    }
    el.classList.toggle('is-kept', kept);
    const what = `${humanLabel(sp.label)}: ${el.textContent}`;
    el.setAttribute('aria-label', kept
      ? `${what}. Kept. Press to redact it.`
      : `${what}. Replaced with ${rep}. ${isUser ? 'Added by you. Press to remove.' : 'Press to keep it.'}`);
  }

  function createChange(seg, sp) {
    const el = h('span', {
      class: `rd${sp.kind === 'user' ? ' is-user' : ''}`,
      tabindex: '0',
      role: 'button',
      dataset: { id: sp.id, kind: sp.kind, label: sp.label },
    });
    paintLabel(el, sp.label);
    const orig = h('span', { class: 'orig' });
    appendFormatted(orig, seg, sp.uStart, sp.uEnd);
    el.append(orig);
    spanOfEl.set(el, sp);
    markEls.set(sp.id, el);
    applyReplacement(el, sp);
    return el;
  }

  function fillParagraph(p, seg, superseded = supersededIds()) {
    for (const el of p.querySelectorAll('.rd')) if (markEls.get(el.dataset.id) === el) markEls.delete(el.dataset.id);
    const frag = document.createDocumentFragment();
    let pos = 0;
    let count = 0;
    for (const sp of visibleSpans(seg.id, superseded)) {
      if (sp.uStart < pos) continue; // defensive: never nest highlights
      appendFormatted(frag, seg, pos, sp.uStart);
      frag.append(createChange(seg, sp));
      count += 1;
      pos = sp.uEnd;
    }
    appendFormatted(frag, seg, pos, seg.text.length);
    p.replaceChildren(frag);
    p.classList.toggle('has-changes', count > 0);
  }

  function renderParagraph(seg, superseded) {
    const p = h('p', { class: paraClass(seg), dataset: { segId: seg.id } });
    fillParagraph(p, seg, superseded);
    paraEls.set(seg.id, p);
    return p;
  }

  /** Flow builder for one zone: paragraphs, tables (grouped by table.t, placed by r/c) and text boxes. */
  function makeFlow(container, superseded) {
    const tables = new Map();
    let box = null;

    function ensureGrid(tbl, rows, cols) {
      tbl.cols = Math.max(tbl.cols, cols);
      while (tbl.rows.length < rows) {
        const tr = h('tr');
        tbl.tbody.append(tr);
        tbl.rows.push({ tr, cells: [] });
      }
      for (const row of tbl.rows) {
        while (row.cells.length < tbl.cols) {
          const td = h('td');
          row.tr.append(td);
          row.cells.push(td);
        }
      }
    }

    function cellFor(t) {
      let tbl = tables.get(t.t);
      if (!tbl) {
        const tbody = h('tbody');
        container.append(h('div', { class: 'table-wrap' }, h('table', { class: 'doc-table' }, tbody)));
        tbl = { tbody, rows: [], cols: 0 };
        tables.set(t.t, tbl);
      }
      ensureGrid(tbl, Math.max(t.rows, t.r + 1), Math.max(t.cols, t.c + 1)); // fills empty cells too
      return tbl.rows[t.r].cells[t.c];
    }

    return {
      add(seg) {
        const p = renderParagraph(seg, superseded);
        if (seg.table) {
          box = null;
          cellFor(seg.table).append(p);
        } else if (seg.location === 'textbox') {
          if (!box) {
            box = h('div', { class: 'textbox', role: 'group', 'aria-label': 'Text Box' });
            container.append(box);
          }
          box.append(p);
        } else {
          box = null;
          container.append(p);
        }
      },
    };
  }

  function createBalloon(author, authorNumbers) {
    if (author && !authorNumbers.has(author)) authorNumbers.set(author, authorNumbers.size + 1);
    const n = author ? authorNumbers.get(author) : 0;
    const balloon = h('div', { class: 'balloon', role: 'note' });
    balloon.style.setProperty('--a-rgb', hexToRgb(AUTHOR_COLORS[Math.max(0, n - 1) % AUTHOR_COLORS.length]).join(', '));
    const name = h('span', { class: 'balloon-author author', dataset: author ? { rep: `Reviewer ${n}` } : undefined });
    name.append(h('span', { class: 'orig', text: author || 'Comment' }));
    balloon.append(name);
    return balloon;
  }

  function refreshFlags(scope) {
    for (const c of scope.querySelectorAll('tr, .table-wrap, .textbox, li, .balloon, .zone')) {
      c.classList.toggle('has-changes', !!c.querySelector('.para.has-changes'));
    }
  }

  function updateFlagsFor(p) {
    for (const selector of ['tr', '.table-wrap', '.textbox', 'li', '.balloon', '.zone']) {
      const c = p.closest(selector);
      if (c) c.classList.toggle('has-changes', !!c.querySelector('.para.has-changes'));
    }
  }

  /** Full render of the document as a Word page (only on a new analysis). */
  function renderDocument() {
    hideTip();
    hideSelToolbar();
    markEls = new Map();
    paraEls = new Map();
    ui.pages.replaceChildren();
    const a = state.analysis;
    if (!a) return;
    state.replacements = computeReplacements();
    const superseded = supersededIds();

    const page = h('article', { class: 'page', 'aria-label': 'Document Page' });
    const header = h('div', { class: 'zone page-header' });
    const body = h('div', { class: 'zone page-body' });
    const footer = h('div', { class: 'zone page-footer' });
    const footnotes = h('ol', { class: 'footnotes' });
    const endnotes = h('ol', { class: 'endnotes' });
    const rail = h('aside', { class: 'comment-rail', 'aria-label': 'Comments' });
    const flows = { header: makeFlow(header, superseded), body: makeFlow(body, superseded), footer: makeFlow(footer, superseded) };
    const lastPart = { header: null, footer: null };
    const authorNumbers = new Map();
    let balloon = null;
    let balloonAuthor = null;

    for (const seg of a.segments) {
      if (seg.hidden) continue; // duplicate text-box copy: never shown
      const loc = seg.location;
      if (loc === 'comment') {
        const author = seg.author || '';
        if (!balloon || author !== balloonAuthor) {
          balloon = createBalloon(author, authorNumbers);
          balloonAuthor = author;
          rail.append(balloon);
        }
        balloon.append(renderParagraph(seg, superseded));
        continue;
      }
      balloon = null;
      if (loc === 'footnote' || loc === 'endnote') {
        (loc === 'footnote' ? footnotes : endnotes).append(h('li', null, renderParagraph(seg, superseded)));
      } else if (loc === 'header' || loc === 'footer') {
        if (lastPart[loc] !== null && lastPart[loc] !== seg.part) (loc === 'header' ? header : footer).append(h('hr', { class: 'zone-divider' }));
        lastPart[loc] = seg.part;
        flows[loc].add(seg);
      } else {
        flows.body.add(seg);
      }
    }

    if (header.childNodes.length) page.append(header);
    page.append(body);
    if (footnotes.childNodes.length || endnotes.childNodes.length) {
      const notes = h('div', { class: 'zone page-notes' }, h('hr', { class: 'note-rule' }));
      if (footnotes.childNodes.length) notes.append(footnotes);
      if (endnotes.childNodes.length) notes.append(endnotes);
      page.append(notes);
    }
    if (footer.childNodes.length) page.append(footer);
    const sheet = h('div', { class: 'sheet' }, page);
    if (rail.childNodes.length) sheet.append(rail);
    ui.pages.append(sheet);
    refreshFlags(ui.pages);
  }

  /** Recompute numbering and push it to every change on the page (attribute updates only). */
  function refreshReplacements(recompute) {
    if (recompute) state.replacements = computeReplacements();
    for (const [id, el] of markEls) {
      const sp = spanOfEl.get(el);
      if (!sp || !el.isConnected) { markEls.delete(id); continue; }
      applyReplacement(el, sp);
    }
  }

  /** After a keep/redact/add/remove: re-render only the touched paragraphs. */
  function afterReviewChange(segIds, focusId) {
    hideTip();
    state.replacements = computeReplacements();
    const superseded = supersededIds();
    for (const segId of new Set(segIds)) {
      const p = paraEls.get(segId);
      const seg = state.analysis.segById.get(segId);
      if (p && seg) { fillParagraph(p, seg, superseded); updateFlagsFor(p); }
    }
    refreshReplacements(false);
    if (focusId !== undefined) {
      const target = focusId ? markEls.get(focusId) : null;
      if (target) target.focus({ preventScroll: true });
    }
    if (state.report) state.reportStale = true;
    renderReview();
    updateUI();
  }

  function activateChange(el) {
    const p = el.closest('.para');
    const sp = spanOfEl.get(el);
    if (!p || !sp) return;
    const hadFocus = document.activeElement === el;
    let focusId = sp.id;
    if (sp.kind === 'user') {
      removeUserSpan(sp.id);
      focusId = null;
    } else if (state.excluded.has(sp.id)) {
      state.excluded.delete(sp.id);
    } else {
      state.excluded.add(sp.id);
    }
    afterReviewChange([p.dataset.segId], hadFocus ? focusId : undefined);
  }

  function onPagesClick(event) {
    const el = event.target.closest ? event.target.closest('.rd') : null;
    if (!el || !ui.pages.contains(el)) return;
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed && el.closest('.para').contains(sel.anchorNode)) return; // a drag-select
    activateChange(el);
  }

  function onPagesKeydown(event) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const el = event.target.closest ? event.target.closest('.rd') : null;
    if (!el) return;
    event.preventDefault();
    activateChange(el);
  }

  function resetReview() {
    state.excluded = new Set();
    state.userSpans = [];
    if (state.report) state.reportStale = true;
    renderDocument();
    renderReview();
    updateUI();
  }

  function jumpTo(label) {
    const els = [...ui.pages.querySelectorAll('.rd')].filter((el) => el.dataset.label === label);
    if (!els.length) return;
    const next = ((jumpIndex.has(label) ? jumpIndex.get(label) : -1) + 1) % els.length;
    jumpIndex.set(label, next);
    const el = els[next];
    if (isNarrow()) setInspector(false);
    el.scrollIntoView({ behavior: scrollBehavior(), block: 'center' });
    el.classList.remove('flash');
    void el.offsetWidth; // restart the animation
    el.classList.add('flash');
    el.addEventListener('animationend', () => el.classList.remove('flash'), { once: true });
  }

  // ===================================================================== selection → "Redact Selection As"
  function paraOf(node) {
    const el = node && (node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement);
    const p = el ? el.closest('.para') : null;
    return p && ui.pages.contains(p) ? p : null;
  }

  /** UTF-16 offset of the DOM boundary point (node, offset) within root's text (TreeWalker). */
  function boundaryOffset(root, node, offset) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let total = 0;
    if (node.nodeType === Node.TEXT_NODE) {
      for (let t = walker.nextNode(); t; t = walker.nextNode()) {
        if (t === node) return total + offset;
        total += t.data.length;
      }
      return total;
    }
    const point = document.createRange();
    point.setStart(node, offset);
    point.collapse(true);
    for (let t = walker.nextNode(); t; t = walker.nextNode()) {
      if (point.comparePoint(t, t.data.length) <= 0) total += t.data.length; // text ends before the point
      else break;
    }
    return total;
  }

  function readSegmentSelection() {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
    const range = sel.getRangeAt(0);
    const p = paraOf(range.startContainer);
    if (!p) return null;
    const seg = state.analysis && state.analysis.segById.get(p.dataset.segId);
    if (!seg) return null;
    let uStart = boundaryOffset(p, range.startContainer, range.startOffset);
    let uEnd;
    if (paraOf(range.endContainer) === p) {
      uEnd = boundaryOffset(p, range.endContainer, range.endOffset);
    } else {
      // e.g. triple-click: the range ends at the next block. Accept only if nothing but whitespace follows.
      const tail = document.createRange();
      tail.setStart(p, p.childNodes.length);
      tail.setEnd(range.endContainer, range.endOffset);
      if (tail.toString().trim() !== '') return null;
      uEnd = seg.text.length;
    }
    const text = seg.text;
    while (uStart < uEnd && /\s/.test(text[uStart])) uStart += 1;
    while (uEnd > uStart && /\s/.test(text[uEnd - 1])) uEnd -= 1;
    if (isLowSurrogateAt(text, uStart)) uStart -= 1;
    if (isLowSurrogateAt(text, uEnd)) uEnd += 1;
    if (uEnd <= uStart) return null;
    return { segId: seg.id, uStart, uEnd, range };
  }

  function defaultLabelFor(info) {
    const overlap = visibleSpans(info.segId).find((s) => s.uStart < info.uEnd && info.uStart < s.uEnd && state.labelInfo.has(s.label));
    if (overlap) return overlap.label;
    if (state.lastUserLabel && state.labelInfo.has(state.lastUserLabel)) return state.lastUserLabel;
    const enabled = orderedLabelNames(state.options.labels).filter((n) => state.labelInfo.has(n));
    return enabled[0] || (state.config.labels[0] ? state.config.labels[0].name : '');
  }

  function showSelToolbar(info) {
    state.pendingSelection = { segId: info.segId, uStart: info.uStart, uEnd: info.uEnd };
    ui.selLabel.value = defaultLabelFor(info);
    ui.selToolbar.hidden = false;
    const rects = info.range.getClientRects();
    const last = rects.length ? rects[rects.length - 1] : info.range.getBoundingClientRect();
    const first = rects.length ? rects[0] : last;
    const box = ui.selToolbar.getBoundingClientRect();
    const left = clamp(last.left, 8, document.documentElement.clientWidth - box.width - 8);
    let top = last.bottom + 8;
    if (top + box.height > window.innerHeight - 36) top = first.top - box.height - 8;
    ui.selToolbar.style.left = `${Math.round(left)}px`;
    ui.selToolbar.style.top = `${Math.round(Math.max(8, top))}px`;
  }

  function hideSelToolbar() {
    if (ui.selToolbar) ui.selToolbar.hidden = true;
    state.pendingSelection = null;
  }

  function scheduleSelectionCheck() {
    clearTimeout(selectionTimer);
    selectionTimer = setTimeout(checkSelection, 40);
  }

  function checkSelection() {
    if (ui.selToolbar.contains(document.activeElement)) return;
    if (!state.analysis || ui.pages.hidden || state.busy.redact) { hideSelToolbar(); return; }
    const info = readSegmentSelection();
    if (info) showSelToolbar(info);
    else hideSelToolbar();
  }

  function addFromSelection() {
    const pending = state.pendingSelection;
    const label = ui.selLabel.value;
    if (!pending || !label || !state.analysis) { hideSelToolbar(); return; }
    state.lastUserLabel = label;
    const span = addUserSpan(pending.segId, pending.uStart, pending.uEnd, label);
    const sel = window.getSelection();
    if (sel) sel.removeAllRanges();
    hideSelToolbar();
    if (span) afterReviewChange([pending.segId], span.id);
  }

  // ===================================================================== tooltips
  function tipContent(target) {
    if (target.classList.contains('rd')) {
      const sp = spanOfEl.get(target);
      if (!sp) return null;
      const isUser = sp.kind === 'user';
      const kept = !isUser && state.excluded.has(sp.id);
      const rep = state.replacements.get(sp.id);
      const title = isUser ? humanLabel(sp.label) : `${humanLabel(sp.label)} · ${fmtScore(sp.score)}`;
      const source = SOURCE_TEXT[sp.source] || 'Detected';
      let text;
      if (isUser) text = `${source}. Becomes ${rep || 'redacted'}. Click to remove.`;
      else if (kept) text = `${source}. Kept as is. Click to redact it.`;
      else text = `${source}. Becomes ${rep || 'redacted'}. Click to keep it.`;
      return { title, text };
    }
    const title = target.dataset.tipTitle;
    return title ? { title, text: target.dataset.tip || '' } : null;
  }

  function showTip(target) {
    if (!target.isConnected) return;
    const content = tipContent(target);
    if (!content) return;
    ui.tooltipTitle.textContent = content.title;
    ui.tooltipText.textContent = content.text;
    ui.tooltip.hidden = false;
    const r = target.getBoundingClientRect();
    const t = ui.tooltip.getBoundingClientRect();
    const isChange = target.classList.contains('rd');
    let top = isChange ? r.top - t.height - 8 : r.bottom + 8;
    if (isChange && top < 8) top = r.bottom + 8;
    if (!isChange && top + t.height > window.innerHeight - 8) top = r.top - t.height - 8;
    const left = clamp(r.left + r.width / 2 - t.width / 2, 8, document.documentElement.clientWidth - t.width - 8);
    ui.tooltip.style.top = `${Math.round(top)}px`;
    ui.tooltip.style.left = `${Math.round(left)}px`;
    target.setAttribute('aria-describedby', 'tooltip');
  }

  function hideTip() {
    clearTimeout(tip.timer);
    if (tip.target && tip.target.getAttribute('aria-describedby') === 'tooltip') tip.target.removeAttribute('aria-describedby');
    tip.target = null;
    if (ui.tooltip) ui.tooltip.hidden = true;
  }

  function tipTargetOf(node) {
    if (!node || !node.closest) return null;
    return node.closest('.rd, [data-tip-title]');
  }

  function scheduleTip(target, delay) {
    if (tip.target === target) return;
    hideTip();
    tip.target = target;
    tip.timer = setTimeout(() => { if (tip.target === target) showTip(target); }, delay);
  }

  function onTipOver(event) {
    if (openPop && openPop.pop.contains(event.target)) return;
    const target = tipTargetOf(event.target);
    if (target) scheduleTip(target, target.classList.contains('rd') ? 180 : 300);
  }

  function onTipOut(event) {
    if (!tip.target) return;
    if (event.relatedTarget && tip.target.contains(event.relatedTarget)) return;
    hideTip();
  }

  // ===================================================================== popovers
  function menuItems() {
    return [...ui.fileMenu.querySelectorAll('.menu-item')].filter((item) => !item.disabled);
  }

  function openMenu() {
    closeMenu(false);
    hideTip();
    ui.menuStartOver.disabled = !state.doc || isBusy();
    ui.menuOpen.disabled = state.busy.upload || state.busy.redact;
    ui.fileMenu.hidden = false;
    ui.btnFile.setAttribute('aria-expanded', 'true');
    openPop = { pop: ui.fileMenu, anchor: ui.btnFile };
    positionMenu();
    const first = menuItems()[0];
    if (first) first.focus({ preventScroll: true });
  }

  function positionMenu() {
    if (!openPop) return;
    const { pop, anchor } = openPop;
    const r = anchor.getBoundingClientRect();
    const width = pop.offsetWidth;
    const left = clamp(r.right - width, 8, document.documentElement.clientWidth - width - 8); // right-aligned
    pop.style.left = `${Math.round(left)}px`;
    pop.style.top = `${Math.round(r.bottom + 6)}px`;
  }

  function closeMenu(restoreFocus) {
    if (!openPop) return;
    const { pop, anchor } = openPop;
    openPop = null;
    pop.hidden = true;
    anchor.setAttribute('aria-expanded', 'false');
    if (restoreFocus) anchor.focus({ preventScroll: true });
  }

  function onMenuKeydown(event) {
    const items = menuItems();
    if (!items.length) return;
    const i = items.indexOf(document.activeElement);
    let next = null;
    if (event.key === 'ArrowDown') next = items[(i + 1) % items.length];
    else if (event.key === 'ArrowUp') next = items[(i - 1 + items.length) % items.length];
    else if (event.key === 'Home') next = items[0];
    else if (event.key === 'End') next = items[items.length - 1];
    else if (event.key === 'Tab') { closeMenu(false); return; }
    if (next) { event.preventDefault(); next.focus(); }
  }

  // ===================================================================== review pane
  function renderReview() {
    const a = state.analysis;
    ui.paneEmpty.hidden = !!state.doc;
    ui.paneSummary.hidden = !a;
    ui.paneDoc.hidden = !state.doc || (!ui.docStats.childNodes.length && !ui.docWarnings.childNodes.length);
    if (a) {
      const counts = computeCounts();
      ui.tileRedactions.textContent = fmtInt(counts.redactions);
      ui.tileKept.textContent = fmtInt(counts.kept);
      ui.tileAdded.textContent = fmtInt(counts.added);
      ui.typeList.replaceChildren();
      const names = orderedLabelNames(counts.total.keys());
      if (!names.length) ui.typeList.append(h('li', { class: 'type-empty', text: 'No Personal Information Found' }));
      for (const name of names) {
        const active = counts.active.get(name) || 0;
        const total = counts.total.get(name) || 0;
        const count = h('span', { class: 'type-count', text: fmtInt(active) });
        if (active !== total) count.append(' ', h('span', { class: 'type-of', text: `of ${fmtInt(total)}` }));
        const row = h('button', {
          type: 'button', class: 'type-row', dataset: { label: name },
          'data-tip-title': `Show Next ${humanLabel(name)}`,
          'data-tip': (state.labelInfo.get(name) || {}).description || (name === 'CUSTOM' ? 'Terms from your Always Redact list.' : ''),
        }, [h('span', { class: 'type-dot', 'aria-hidden': 'true' }), h('span', { class: 'type-name', text: humanLabel(name) }), count]);
        paintLabel(row, name);
        ui.typeList.append(h('li', null, row));
      }
      ui.btnResetReview.hidden = !(state.excluded.size || state.userSpans.length);
    }
    renderReport();
  }

  function renderReport() {
    const r = state.report;
    ui.paneReport.hidden = !r;
    if (!r) return;
    const fixes = Number(r.residual_fixes) || 0;
    ui.residual.classList.toggle('is-warn', fixes > 0);
    ui.residualValue.textContent = fixes > 0 ? plural(fixes, 'Safety-Net Fix', 'Safety-Net Fixes') : '0 Leaks';
    ui.reportStale.hidden = !state.reportStale;

    const metadata = Array.isArray(r.metadata_cleared) ? r.metadata_cleared : [];
    const stats = [
      ['Replacements', r.replacements_applied, fmtInt],
      ['Hyperlinks Unlinked', r.hyperlinks_unlinked, fmtInt],
      ['Comment Authors Anonymised', r.comment_authors_anonymised, fmtInt],
      ['Metadata Fields Cleared', metadata.length, fmtInt],
      ['Fields Scrubbed', r.fields_scrubbed, fmtInt],
      ['Tracked Changes Accepted', r.tracked_changes_accepted, fmtInt],
      ['Thumbnail Removed', r.thumbnail_removed, (v) => (v ? 'Yes' : 'No')],
      ['Time Taken', r.elapsed_ms, fmtMs],
    ];
    ui.reportStats.replaceChildren();
    for (const [title, value, format] of stats) {
      if (value === undefined || value === null) continue;
      ui.reportStats.append(h('dt', { text: title }), h('dd', { text: format(value) }));
    }
    ui.reportMeta.replaceChildren(...(metadata.length
      ? metadata.map((m) => h('li', { class: 'chip chip-mono', text: String(m) }))
      : [h('li', { class: 'chip is-zero', text: 'None' })]));
    const warnings = Array.isArray(r.warnings) ? r.warnings : [];
    ui.reportWarnings.replaceChildren(...warnings.map((w) => h('li', { text: String(w) })));
    ui.reportWarnWrap.hidden = warnings.length === 0;
  }

  // ===================================================================== redact & download
  function buildRedactRequest() {
    const superseded = supersededIds();
    const exclude = state.analysis.entities.filter((e) => !isEntityActive(e, superseded)).map((e) => e.id);
    const extra = [];
    for (const u of state.userSpans) {
      extra.push({ segment_id: u.segment_id, start: u.start, end: u.end, label: u.label });
      for (const hiddenId of state.analysis.hiddenTwins.get(u.segment_id) || []) {
        extra.push({ segment_id: hiddenId, start: u.start, end: u.end, label: u.label }); // same text → same offsets
      }
    }
    return { mode: state.options.mode, exclude_ids: exclude, extra_spans: extra };
  }

  async function runRedact() {
    if (!state.doc || !state.analysis || isBusy() || state.analysisPending || isStale()) return;
    const req = buildRedactRequest();
    const docId = state.doc.doc_id;
    setBusy('redact', true);
    let res = null;
    try {
      res = await api.redact(docId, req);
    } catch (err) {
      toastError(err, 'Redaction Failed');
    } finally {
      state.busy.redact = false;
    }
    if (res && state.doc && state.doc.doc_id === docId) {
      state.report = res.report || {};
      state.reportStale = false;
      state.downloadUrl = res.download_url || docUrl(docId, '/download');
      if (!isNarrow()) setInspector(true, 'changes'); // show the report
      renderReview();
      updateUI();
      await downloadRedacted();
    } else {
      updateUI();
    }
    runQueuedAnalysis();
  }

  async function downloadRedacted() {
    if (!state.downloadUrl || state.busy.download) return false;
    setBusy('download', true);
    let ok = false;
    try {
      const res = await api.download(state.downloadUrl);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const link = h('a', { href: url, download: 'redacted.docx', class: 'visually-hidden' });
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      toast('Saved redacted.docx to your downloads.', 'success', 3500);
      ok = true;
    } catch (err) {
      toastError(err, 'Download Failed');
    } finally {
      setBusy('download', false);
    }
    runQueuedAnalysis();
    return ok;
  }

  // ===================================================================== global UI state
  function isBusy() {
    return state.busy.upload || state.busy.analyze || state.busy.redact || state.busy.download;
  }

  function setBusy(key, on) {
    state.busy[key] = on;
    updateUI();
  }

  function setButton(btn, busy, text) {
    btn.querySelector('.spinner').hidden = !busy;
    btn.querySelector('.btn-text').textContent = text;
    btn.setAttribute('aria-label', text); // keeps a name when the label is hidden on phones
    btn.setAttribute('aria-busy', busy ? 'true' : 'false');
  }

  function modelText() {
    const model = (state.config && state.config.model) || {};
    const device = model.device ? DEVICE_NAMES[String(model.device).toLowerCase()] || String(model.device).toUpperCase() : '';
    return [model.name || '', device].filter(Boolean).join(' · ');
  }

  function renderStatus(counts) {
    let text = 'Ready';
    if (state.busy.upload) text = 'Opening Document…';
    else if (state.doc && !state.analysis) text = state.busy.analyze || state.analysisPending ? 'Analysing Document…' : 'Not Analysed';
    else if (state.analysis) {
      text = [
        plural(counts.redactions, 'Redaction', 'Redactions'),
        `${fmtInt(counts.kept)} Kept`,
        `${fmtInt(counts.added)} Added`,
        `Analysed In ${fmtMs(state.analysis.elapsed_ms)}`,
      ].join(' · ');
    }
    ui.statusText.textContent = text;
    ui.statusCounts.disabled = !state.analysis;
    const r = state.report;
    ui.statusResidual.hidden = !r;
    if (r) {
      const fixes = Number(r.residual_fixes) || 0;
      ui.statusResidual.className = `status-residual ${fixes > 0 ? 'is-warn' : 'is-ok'}`;
      ui.statusResidual.textContent = fixes > 0 ? `Residual Scan: ${plural(fixes, 'Safety-Net Fix', 'Safety-Net Fixes')}` : 'Residual Scan: 0 Leaks';
    }
  }

  function setModelStatus(kind, text) {
    ui.statusModel.dataset.state = kind;
    ui.statusModelText.textContent = text;
  }

  /** Recompute visibility, disabled states and labels of every control from `state`. */
  function updateUI() {
    const busy = isBusy();
    const hasDoc = !!state.doc;
    const hasAnalysis = !!state.analysis;
    const stale = isStale();
    const updating = state.busy.analyze || state.analysisPending;
    const counts = computeCounts();

    ui.docTitle.textContent = hasDoc ? state.doc.filename : 'No Document';
    ui.docTitle.title = hasDoc ? state.doc.filename : '';
    ui.docTitle.classList.toggle('is-empty', !hasDoc);
    ui.tbUpdating.hidden = !(hasDoc && updating);

    ui.btnRedact.disabled = !hasAnalysis || busy || updating || stale;
    setButton(ui.btnRedact, state.busy.redact || state.busy.download, state.busy.redact ? 'Redacting…' : 'Redact & Download');
    ui.emptySample.disabled = busy || !state.config;
    if (openPop) {
      ui.menuStartOver.disabled = !hasDoc || busy;
      ui.menuOpen.disabled = state.busy.upload || state.busy.redact;
    }

    ui.emptyState.hidden = hasDoc || state.busy.upload;
    const loading = state.busy.upload || (hasDoc && !hasAnalysis);
    ui.loadingState.hidden = !loading;
    if (loading) {
      const working = state.busy.upload || updating;
      ui.loadingSpinner.hidden = !working;
      ui.loadingRetry.hidden = working;
      ui.loadingText.textContent = state.busy.upload ? 'Reading Document…'
        : working ? 'Finding Personal Information…' : 'Analysis Did Not Complete';
    }
    ui.pages.hidden = !hasAnalysis;
    ui.progress.hidden = !(hasAnalysis && (updating || state.busy.redact));

    ui.changesCount.hidden = !hasAnalysis;
    ui.changesCount.textContent = fmtInt(counts.redactions);
    renderStatus(counts);
  }

  // ===================================================================== wiring
  function cacheElements() {
    const ids = {
      docTitle: 'doc-title', tbUpdating: 'tb-updating', mockBadge: 'mock-badge', viewSelect: 'view-select',
      btnFile: 'btn-file', btnSettings: 'btn-settings', btnRedact: 'btn-redact',
      canvas: 'canvas', progress: 'progress', emptyState: 'empty-state', dropTarget: 'drop-target',
      emptyOpen: 'empty-open', emptySample: 'empty-sample', maxMb: 'max-mb',
      loadingState: 'loading-state', loadingText: 'loading-text', loadingRetry: 'loading-retry', pages: 'pages',
      inspector: 'inspector', tabChanges: 'tab-changes', tabSettings: 'tab-settings', changesCount: 'changes-count',
      btnInspClose: 'btn-insp-close', panelChanges: 'panel-changes', panelSettings: 'panel-settings',
      paneEmpty: 'pane-empty', paneSummary: 'pane-summary',
      tileRedactions: 'tile-redactions', tileKept: 'tile-kept', tileAdded: 'tile-added',
      btnChangesOnly: 'btn-changes-only', typeList: 'type-list', btnResetReview: 'btn-reset-review',
      paneReport: 'pane-report', residual: 'residual', residualValue: 'residual-value', reportStale: 'report-stale',
      reportStats: 'report-stats', reportMeta: 'report-meta', reportWarnWrap: 'report-warn-wrap', reportWarnings: 'report-warnings',
      btnDownloadAgain: 'btn-download-again', paneDoc: 'pane-doc', docStats: 'doc-stats', docWarnings: 'doc-warnings',
      settingsForm: 'settings-form', preset: 'preset',
      typesCount: 'types-count', typesAll: 'types-all', typesNone: 'types-none', typesGrid: 'types-grid',
      confidence: 'confidence', confidenceValue: 'confidence-value', confidenceReset: 'confidence-reset', modeGroup: 'mode-group',
      denyTerms: 'deny-terms', denyCount: 'deny-count', allowTerms: 'allow-terms', allowCount: 'allow-count',
      statusCounts: 'status-counts', statusText: 'status-text', statusResidual: 'status-residual',
      statusModel: 'status-model', statusModelText: 'status-model-text', zoom: 'zoom',
      fileMenu: 'file-menu', menuOpen: 'menu-open', menuStartOver: 'menu-start-over',
      selToolbar: 'sel-toolbar', selAdd: 'sel-add', selLabel: 'sel-label',
      tooltip: 'tooltip', tooltipTitle: 'tooltip-title', tooltipText: 'tooltip-text', toasts: 'toasts', fileInput: 'file-input',
    };
    const missing = [];
    for (const [key, id] of Object.entries(ids)) {
      ui[key] = $(id);
      if (!ui[key]) missing.push(id);
    }
    if (missing.length) throw new Error(`RedactNER UI: missing elements #${missing.join(', #')}`);
    ui.loadingSpinner = ui.loadingState.querySelector('.spinner');
    ui.viewRadios = [...document.querySelectorAll('input[name="view"]')];
  }

  function bindEvents() {
    // file menu + opening documents
    ui.btnFile.addEventListener('click', () => { if (openPop) closeMenu(true); else openMenu(); });
    ui.btnFile.addEventListener('keydown', (e) => {
      if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && !openPop) { e.preventDefault(); openMenu(); }
    });
    ui.fileMenu.addEventListener('keydown', onMenuKeydown);
    ui.menuOpen.addEventListener('click', () => { closeMenu(false); openFilePicker(); });
    ui.menuStartOver.addEventListener('click', () => { closeMenu(false); startOver(); });
    ui.emptyOpen.addEventListener('click', (e) => { e.stopPropagation(); openFilePicker(); });
    ui.dropTarget.addEventListener('click', openFilePicker);
    ui.dropTarget.addEventListener('keydown', (e) => {
      if (e.target === ui.dropTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openFilePicker(); }
    });
    ui.emptySample.addEventListener('click', (e) => {
      e.stopPropagation();
      if (window.RedactNERMock) handleFile(window.RedactNERMock.sampleFile());
    });
    ui.loadingRetry.addEventListener('click', () => startAnalysis(true));
    ui.fileInput.addEventListener('change', () => handleFile(ui.fileInput.files && ui.fileInput.files[0]));
    ui.canvas.addEventListener('dragenter', (e) => { if (hasFiles(e)) ui.canvas.classList.add('is-dragover'); });
    ui.canvas.addEventListener('dragleave', (e) => { if (!ui.canvas.contains(e.relatedTarget)) ui.canvas.classList.remove('is-dragover'); });
    window.addEventListener('dragover', (e) => { if (hasFiles(e)) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } });
    window.addEventListener('drop', (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      ui.canvas.classList.remove('is-dragover');
      const files = e.dataTransfer.files;
      if (files.length > 1) toast('One document at a time: opening the first file.', 'warn');
      handleFile(files[0]);
    });

    // settings (every change re-analyses automatically)
    ui.settingsForm.addEventListener('submit', (e) => e.preventDefault());
    ui.preset.addEventListener('change', onPresetChange);
    ui.typesGrid.addEventListener('change', onTypeToggle);
    ui.typesAll.addEventListener('click', () => setLabels(state.config ? state.config.labels.map((l) => l.name) : []));
    ui.typesNone.addEventListener('click', () => setLabels([]));
    ui.confidence.addEventListener('input', onConfidenceInput);
    ui.confidenceReset.addEventListener('click', onConfidenceReset);
    ui.modeGroup.addEventListener('change', onModeChange);
    ui.denyTerms.addEventListener('input', optionsChanged);
    ui.allowTerms.addEventListener('input', optionsChanged);

    // view
    for (const input of ui.viewRadios) input.addEventListener('change', () => { if (input.checked) setView(input.value); });
    ui.viewSelect.addEventListener('change', () => setView(ui.viewSelect.value));
    ui.btnChangesOnly.addEventListener('click', toggleChangesOnly);
    ui.zoom.addEventListener('change', () => {
      state.zoom = Number(ui.zoom.value) || 1;
      ui.pages.style.setProperty('--zoom', String(state.zoom));
      hideSelToolbar();
    });

    // actions
    ui.btnRedact.addEventListener('click', runRedact);
    ui.btnDownloadAgain.addEventListener('click', downloadRedacted);

    // inspector
    ui.btnSettings.addEventListener('click', onSettingsButton);
    ui.tabChanges.addEventListener('click', () => setInspector(true, 'changes'));
    ui.tabSettings.addEventListener('click', () => setInspector(true, 'settings'));
    ui.tabChanges.addEventListener('keydown', onTabKeydown);
    ui.tabSettings.addEventListener('keydown', onTabKeydown);
    ui.btnInspClose.addEventListener('click', () => { setInspector(false); ui.btnSettings.focus({ preventScroll: true }); });
    ui.statusCounts.addEventListener('click', () => setInspector(true, 'changes'));
    ui.typeList.addEventListener('click', (e) => {
      const row = e.target.closest ? e.target.closest('.type-row') : null;
      if (row) jumpTo(row.dataset.label);
    });
    ui.btnResetReview.addEventListener('click', resetReview);

    // document page
    ui.pages.addEventListener('click', onPagesClick);
    ui.pages.addEventListener('keydown', onPagesKeydown);

    // tooltips
    document.addEventListener('pointerover', onTipOver);
    document.addEventListener('pointerout', onTipOut);
    document.addEventListener('focusin', (e) => {
      const target = tipTargetOf(e.target);
      if (target) scheduleTip(target, 300);
    });
    document.addEventListener('focusout', hideTip);
    document.addEventListener('scroll', () => {
      hideTip();
      if (!ui.selToolbar.hidden) checkSelection();
    }, true);

    // selection toolbar + menu light-dismiss
    document.addEventListener('mousedown', (e) => {
      pointerDown = true;
      hideTip();
      if (!ui.selToolbar.contains(e.target)) hideSelToolbar();
      if (openPop && !openPop.pop.contains(e.target) && !openPop.anchor.contains(e.target)) closeMenu(false);
    });
    document.addEventListener('mouseup', () => { pointerDown = false; scheduleSelectionCheck(); });
    document.addEventListener('selectionchange', () => { if (!pointerDown) scheduleSelectionCheck(); });
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      if (openPop) { closeMenu(true); return; }
      if (!ui.selToolbar.hidden || !ui.tooltip.hidden) { hideSelToolbar(); hideTip(); return; }
      if (state.inspectorOpen && isNarrow()) { setInspector(false); ui.btnSettings.focus({ preventScroll: true }); }
    });
    ui.selAdd.addEventListener('click', addFromSelection);
    let wasNarrow = isNarrow();
    window.addEventListener('resize', () => {
      hideTip();
      hideSelToolbar();
      positionMenu();
      const narrow = isNarrow();
      if (narrow !== wasNarrow) setInspector(!narrow); // drawer on small screens, docked on large
      wasNarrow = narrow;
    });
  }

  function readToken() {
    const meta = document.querySelector('meta[name="redactner-token"]');
    return meta ? meta.getAttribute('content') || '' : '';
  }

  async function loadConfig() {
    setModelStatus('pending', 'Connecting…');
    try {
      state.config = normalizeConfig(await api.config());
      initOptions();
      setModelStatus('ok', modelText() || 'Model Ready');
    } catch (err) {
      setModelStatus('error', 'Server Unavailable');
      const msg = err && err.message ? err.message : String(err);
      toast(`Could Not Load Settings: ${msg}`, 'error', 0, { label: 'Retry', run: loadConfig });
    }
    updateTermCounts();
    updateUI();
  }

  async function boot() {
    cacheElements();
    state.mock = new URLSearchParams(window.location.search).get('mock') === '1';
    if (state.mock && !window.RedactNERMock) {
      state.mock = false;
      toast('Mock mode was requested but mock.js did not load; using the real server.', 'error', 0);
    }
    transport = state.mock ? window.RedactNERMock.fetch : window.fetch.bind(window);
    state.token = readToken();
    ui.mockBadge.hidden = !state.mock;
    ui.emptySample.hidden = !state.mock;
    try {
      const saved = window.localStorage.getItem(TAB_KEY);
      if (saved === 'changes' || saved === 'settings') state.inspectorTab = saved;
    } catch (_) { /* storage unavailable: default tab */ }
    setInspector(!isNarrow());
    setView('markup');
    bindEvents();
    renderReview();
    updateUI();
    if (!state.mock && (!state.token || state.token === TOKEN_PLACEHOLDER)) {
      toast('This page has no session token, so the server will refuse requests. Open the address printed by the RedactNER server, or add ?mock=1 for a demo.', 'warn', 0);
    }
    await loadConfig();
    // Screenshot/demo mode: ?mock=1&demo=1 opens the bundled sample (optionally &view=redacted|original).
    const params = new URLSearchParams(window.location.search);
    if (state.mock && params.get('demo') === '1') {
      ui.mockBadge.hidden = true;
      const view = params.get('view');
      if (view === 'redacted' || view === 'original') setView(view);
      handleFile(window.RedactNERMock.sampleFile());
    }
  }

  function start() {
    boot().catch((err) => {
      console.error(err);
      if (ui.toasts) toast(`RedactNER failed to start: ${err.message}`, 'error', 0);
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
