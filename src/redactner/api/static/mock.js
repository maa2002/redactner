/*
 * RedactNER mock backend.
 *
 * Loaded on every page but only USED when the URL contains ?mock=1: app.js then sends every API
 * call to window.RedactNERMock.fetch instead of window.fetch. It implements the /api/v1 contract
 * (token header, raw-body upload, analyze, redact, download, delete) with a sample CV that has a
 * title, headings, a bulleted list, bold/italic runs, a key/value table, a text box with a hidden
 * fallback duplicate, a footnote, header/footer and two comments by different authors.
 * Offsets are Python code points (like the real server). ?mock=1&residual=2 → amber residual scan.
 */
(function () {
  'use strict';

  var API = '/api/v1';
  var DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
  var SAMPLE_NAME = 'Jane_Citizen_CV.docx';
  var P_DOC = 'word/document.xml';
  var P_HDR = 'word/header1.xml';
  var P_FTR = 'word/footer1.xml';
  var P_FN = 'word/footnotes.xml';
  var P_CMT = 'word/comments.xml';

  var LABELS = [
    ['PERSON', '#e6194b', 'Names of people, including initials'],
    ['EMAIL', '#3cb44b', 'E-mail addresses'],
    ['PHONE', '#4363d8', 'Phone, mobile and fax numbers'],
    ['ADDRESS', '#f58231', 'Street addresses, unit/building numbers, postcodes'],
    ['LOCATION', '#911eb4', 'Cities, suburbs, states, coordinates'],
    ['DATE', '#0e9fb8', 'Dates of birth and other personal dates'],
    ['ID_NUMBER', '#f032e6', 'Government, tax, medical, employee, customer and vehicle identifiers'],
    ['FINANCIAL', '#7aa31c', 'Card numbers, bank accounts, BSB, IBAN, SWIFT/BIC'],
    ['USERNAME', '#d6568f', 'User names and handles'],
    ['CREDENTIAL', '#469990', 'Passwords, PINs, CVVs, API keys'],
    ['IP_ADDRESS', '#8a6fd1', 'IPv4, IPv6 and MAC addresses'],
    ['URL', '#9a6324', 'Personal URLs and profile links'],
    ['ORGANIZATION', '#808000', 'Employers and company names (quasi-identifier)']
  ];
  var LABEL_NAMES = LABELS.map(function (l) { return l[0]; });
  var PRESETS = {
    strict: LABEL_NAMES.filter(function (n) { return n !== 'ORGANIZATION'; }),
    direct: ['PERSON', 'EMAIL', 'PHONE', 'ADDRESS', 'ID_NUMBER', 'FINANCIAL', 'USERNAME', 'CREDENTIAL', 'IP_ADDRESS', 'URL']
  };

  // ---------- sample document ----------
  // seg(location, part, text, opts); opts: style, align, list, table, marks [[substring, 'b'|'i'|'u'|'bi'...]],
  // hidden, author, ents [[surface, label, score, source]].
  function seg(location, part, text, opts) {
    opts = opts || {};
    return {
      location: location, part: part, text: text,
      style: opts.style || null, align: opts.align || null, list: !!opts.list, table: opts.table || null,
      markSpecs: opts.marks || [], hidden: !!opts.hidden, author: opts.author || null, ents: opts.ents || []
    };
  }
  function cell(r, c, text, opts) {
    opts = opts || {};
    opts.table = { t: 0, r: r, c: c, rows: 5, cols: 2 };
    return seg('table', P_DOC, text, opts);
  }
  var KEY = { marks: [['*', 'b']] }; // whole-cell bold

  var TEXTBOX = 'Available from 1 December. Call Jane on 0400 123 456.';
  var TEXTBOX_ENTS = [['Jane', 'PERSON', 0.9, 'propagated'], ['0400 123 456', 'PHONE', 1.0, 'rule'], ['1 December', 'DATE', 0.58, 'model']];

  var DOC = [
    seg('header', P_HDR, 'Jane Citizen · Curriculum Vitae', { align: 'right', ents: [['Jane Citizen', 'PERSON', 0.9, 'propagated']] }),
    seg('body', P_DOC, 'Jane Citizen', { style: 'Title', align: 'center', ents: [['Jane Citizen', 'PERSON', 0.998, 'model']] }),
    seg('body', P_DOC, 'Senior Data Analyst', { style: 'Subtitle', align: 'center' }),
    seg('body', P_DOC, '📧 jane.citizen@example.com · 📞 +61 412 345 678 · linkedin.com/in/janecitizen', {
      align: 'center',
      ents: [['jane.citizen@example.com', 'EMAIL', 1.0, 'rule'], ['+61 412 345 678', 'PHONE', 1.0, 'rule'], ['linkedin.com/in/janecitizen', 'URL', 0.97, 'rule']]
    }),
    seg('body', P_DOC, 'Profile', { style: 'Heading1' }),
    seg('body', P_DOC, 'Data analyst with five years of experience at Atlassian and the Commonwealth Bank. Jane enjoys turning messy spreadsheets into clear decisions.', {
      align: 'both', marks: [['five years', 'b'], ['clear decisions', 'i']],
      ents: [['Atlassian', 'ORGANIZATION', 0.88, 'model'], ['Commonwealth Bank', 'ORGANIZATION', 0.81, 'model'], ['Jane', 'PERSON', 0.9, 'propagated']]
    }),
    seg('body', P_DOC, 'Experience', { style: 'Heading1' }),
    seg('body', P_DOC, 'Senior Data Analyst, Atlassian, Sydney', {
      style: 'Heading2',
      ents: [['Atlassian', 'ORGANIZATION', 0.86, 'model'], ['Sydney', 'LOCATION', 0.74, 'model']]
    }),
    seg('body', P_DOC, 'Built the reporting platform with Priya Raman; code on GitHub as @janecodes.', {
      style: 'ListParagraph', list: true, marks: [['reporting platform', 'b']],
      ents: [['Priya Raman', 'PERSON', 0.96, 'model'], ['@janecodes', 'USERNAME', 0.62, 'model']]
    }),
    seg('body', P_DOC, 'Admin access to the reporting server 10.24.8.17 (password: Tr0ub4dor&3) was handed over in March.', {
      style: 'ListParagraph', list: true, marks: [['Tr0ub4dor&3', 'u']],
      ents: [['10.24.8.17', 'IP_ADDRESS', 1.0, 'rule'], ['Tr0ub4dor&3', 'CREDENTIAL', 0.71, 'model'], ['March', 'DATE', 0.22, 'model']]
    }),
    seg('body', P_DOC, 'Mentored two graduates and ran the internal analytics guild.', { style: 'ListParagraph', list: true }),
    seg('body', P_DOC, 'Personal Details', { style: 'Heading1' }),
    cell(0, 0, 'Address', KEY),
    cell(0, 1, '42 Wallaby Way, Sydney NSW 2000', { ents: [['42 Wallaby Way, Sydney NSW 2000', 'ADDRESS', 0.93, 'model']] }),
    cell(1, 0, 'Date of Birth', KEY),
    cell(1, 1, '14/03/1996', { ents: [['14/03/1996', 'DATE', 0.97, 'model']] }),
    cell(2, 0, 'Bank Account', KEY),
    cell(2, 1, 'BSB 062-000, account 1234 5678', { ents: [['062-000', 'FINANCIAL', 0.99, 'rule'], ['1234 5678', 'FINANCIAL', 0.91, 'model']] }),
    cell(3, 0, 'Tax File Number', KEY),
    cell(3, 1, '123 456 782', { ents: [['123 456 782', 'ID_NUMBER', 1.0, 'rule']] }),
    cell(4, 0, 'Driver Licence', KEY), // (4, 1) intentionally missing → the UI fills the empty cell
    seg('body', P_DOC, 'Referees', { style: 'Heading1' }),
    seg('body', P_DOC, 'Dr Michael Okafor, University of Technology Sydney · m.okafor@uts.edu.au · (02) 9514 2000', {
      marks: [['Dr Michael Okafor', 'b']],
      ents: [['Michael Okafor', 'PERSON', 0.97, 'model'], ['University of Technology Sydney', 'ORGANIZATION', 0.66, 'model'],
        ['m.okafor@uts.edu.au', 'EMAIL', 1.0, 'rule'], ['(02) 9514 2000', 'PHONE', 1.0, 'rule']]
    }),
    seg('textbox', P_DOC, TEXTBOX, { marks: [['Available from 1 December.', 'b']], ents: TEXTBOX_ENTS }),
    seg('textbox', P_DOC, TEXTBOX, { hidden: true, ents: TEXTBOX_ENTS }), // VML fallback copy of the same text box
    seg('footnote', P_FN, 'Referee details shared with Michael Okafor’s consent on 02/10/2026.', {
      ents: [['Michael Okafor', 'PERSON', 0.9, 'propagated'], ['02/10/2026', 'DATE', 0.55, 'model']]
    }),
    seg('footer', P_FTR, 'Jane Citizen · Curriculum Vitae · Confidential', {
      align: 'center', ents: [['Jane Citizen', 'PERSON', 0.9, 'propagated']]
    }),
    seg('comment', P_CMT, 'Michael, can you confirm Jane’s start date at Atlassian? Call me on 0400 123 456.', {
      author: 'Sam Lee',
      ents: [['Michael', 'PERSON', 0.9, 'propagated'], ['Jane', 'PERSON', 0.9, 'propagated'], ['Atlassian', 'ORGANIZATION', 0.84, 'model'], ['0400 123 456', 'PHONE', 1.0, 'rule']]
    }),
    seg('comment', P_CMT, 'Please remove my surname before this goes to the agency.', { author: 'Priya Raman' })
  ];

  var docs = Object.create(null);
  var docSeq = 0;

  // ---------- helpers ----------
  function delay(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }
  function jitter(base) { return base + Math.round(Math.random() * base * 0.5); }
  function json(status, obj) {
    return new Response(JSON.stringify(obj), { status: status, headers: { 'Content-Type': 'application/json' } });
  }
  function fail(status, detail) { return json(status, { detail: detail }); }
  function header(init, name) {
    var h = init.headers || {};
    if (typeof Headers !== 'undefined' && h instanceof Headers) return h.get(name);
    var want = name.toLowerCase();
    var keys = Object.keys(h);
    for (var i = 0; i < keys.length; i++) if (keys[i].toLowerCase() === want) return h[keys[i]];
    return null;
  }
  function cpLength(s) { return Array.from(s).length; }
  function toCp(text, unitIndex) { return cpLength(text.slice(0, unitIndex)); } // UTF-16 → code points
  function escapeRegExp(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
  function segIndexById(id) {
    var m = /^s(\d+)$/.exec(String(id));
    var i = m ? Number(m[1]) : -1;
    return i >= 0 && i < DOC.length ? i : -1;
  }

  function segmentsPayload() {
    return DOC.map(function (s, i) {
      var marks = s.markSpecs.map(function (spec) {
        var u = spec[0] === '*' ? 0 : s.text.indexOf(spec[0]);
        var len = spec[0] === '*' ? s.text.length : spec[0].length;
        return { s: toCp(s.text, u), e: toCp(s.text, u + len), b: spec[1].indexOf('b') >= 0, i: spec[1].indexOf('i') >= 0, u: spec[1].indexOf('u') >= 0 };
      });
      return {
        id: 's' + i, location: s.location, text: s.text, part: s.part, style: s.style, align: s.align,
        list: s.list, table: s.table, marks: marks, hidden: s.hidden, author: s.author
      };
    });
  }

  async function parseJson(init) {
    var ctype = (header(init, 'Content-Type') || '').split(';')[0].trim().toLowerCase();
    if (ctype !== 'application/json') return { error: fail(415, 'Expected Content-Type: application/json') };
    try { return { body: JSON.parse(init.body) }; } catch (e) { return { error: fail(422, 'Request body is not valid JSON') }; }
  }

  // ---------- endpoints ----------
  function config() {
    return {
      labels: LABELS.map(function (l) {
        return { name: l[0], color: l[1], description: l[2], default_on: PRESETS.strict.indexOf(l[0]) >= 0 };
      }),
      presets: PRESETS,
      default_preset: 'strict',
      modes: [{ id: 'label', name: 'Labels ([PERSON_1])' }, { id: 'mask', name: 'Black-out (█████)' }],
      detectors: [
        { id: 'hybrid', name: 'RedactNER (model + rules)' },
        { id: 'model', name: 'Model only' },
        { id: 'rules', name: 'Rules only' }
      ],
      default_detector: 'hybrid',
      default_threshold: 0.5,
      max_upload_mb: 20,
      model: { name: 'modernbert-base', device: 'cuda' }
    };
  }

  async function upload(init) {
    var ctype = (header(init, 'Content-Type') || '').split(';')[0].trim();
    if (ctype !== DOCX_MIME) return fail(415, 'Expected Content-Type: ' + DOCX_MIME);
    var rawName = header(init, 'X-Filename');
    if (!rawName) return fail(400, 'Missing X-Filename header');
    var filename;
    try { filename = decodeURIComponent(rawName); } catch (e) { return fail(400, 'X-Filename is not valid percent-encoding'); }
    var body = init.body;
    if (!body || typeof body.size !== 'number') return fail(400, 'Request body must be the raw .docx bytes');
    await delay(jitter(400));
    if (body.size === 0) return fail(400, 'Empty upload. Send the .docx file as the request body.');
    if (body.size > 20 * 1024 * 1024) return fail(413, 'file larger than 20 MB');
    var magic = new Uint8Array(await body.slice(0, 2).arrayBuffer());
    if (magic[0] !== 0x50 || magic[1] !== 0x4b || /corrupt/i.test(filename)) {
      return fail(422, 'not a readable .docx file: the file is not a ZIP package');
    }
    var id = 'mock-' + (++docSeq) + '-' + Date.now().toString(36);
    docs[id] = { filename: filename, analysis: null, redaction: null };
    var warnings = ['Accepted 2 tracked changes so deleted text cannot be recovered.'];
    if (filename !== SAMPLE_NAME) warnings.unshift('Mock backend: showing the sample CV instead of your file.');
    return json(200, {
      doc_id: id,
      filename: filename,
      stats: { paragraphs: 24, tables: 1, headers: 1, footers: 1, comments: 2, images: 0, hyperlinks: 1, tracked_changes_accepted: 2 },
      warnings: warnings
    });
  }

  function detectAll(req) {
    var wanted = new Set(req.labels || []);
    var detector = req.detector || 'hybrid';
    var threshold = req.threshold === undefined || req.threshold === null ? 0.5 : Number(req.threshold);
    var allow = new Set((req.allow_terms || []).map(function (t) { return String(t).trim().toLowerCase(); }).filter(Boolean));
    var candidates = [];

    DOC.forEach(function (s, segIdx) {
      var seen = Object.create(null);
      s.ents.forEach(function (spec) {
        var surface = spec[0], label = spec[1], score = spec[2], source = spec[3];
        var n = seen[surface] || 0;
        seen[surface] = n + 1;
        if (!wanted.has(label)) return;
        if (detector === 'rules' && source !== 'rule') return;
        if (detector === 'model' && source !== 'model') return;
        if (source !== 'rule' && score < threshold) return;
        if (allow.has(surface.toLowerCase())) return;
        var u = -1;
        for (var k = 0; k <= n; k++) { u = s.text.indexOf(surface, u + 1); if (u < 0) return; }
        candidates.push({ seg: segIdx, u: u, uEnd: u + surface.length, label: label, score: score, source: source, prio: 0 });
      });
    });

    (req.deny_terms || []).forEach(function (term) {
      term = String(term).trim();
      if (!term) return;
      var re = new RegExp('(?<!\\w)' + escapeRegExp(term) + '(?!\\w)', 'gi');
      DOC.forEach(function (s, segIdx) {
        var m;
        re.lastIndex = 0;
        while ((m = re.exec(s.text)) !== null) {
          if (m[0].length === 0) { re.lastIndex++; continue; }
          if (allow.has(m[0].toLowerCase())) continue;
          candidates.push({ seg: segIdx, u: m.index, uEnd: m.index + m[0].length, label: 'CUSTOM', score: 1.0, source: 'deny', prio: 1 });
        }
      });
    });

    // Resolve overlaps: deny-list wins, then longer spans, then earlier spans.
    candidates.sort(function (a, b) { return b.prio - a.prio || (b.uEnd - b.u) - (a.uEnd - a.u) || a.seg - b.seg || a.u - b.u; });
    var kept = [];
    candidates.forEach(function (c) {
      var clash = kept.some(function (k) { return k.seg === c.seg && c.u < k.uEnd && k.u < c.uEnd; });
      if (!clash) kept.push(c);
    });
    kept.sort(function (a, b) { return a.seg - b.seg || a.u - b.u; });
    return kept.map(function (c, i) {
      var text = DOC[c.seg].text;
      return {
        id: 'e' + i, segment_id: 's' + c.seg, start: toCp(text, c.u), end: toCp(text, c.uEnd),
        label: c.label, score: c.score, source: c.source, text: text.slice(c.u, c.uEnd)
      };
    });
  }

  async function analyze(docId, init) {
    var doc = docs[docId];
    if (!doc) return fail(404, 'Document not found or expired.');
    var parsed = await parseJson(init);
    if (parsed.error) return parsed.error;
    var req = parsed.body || {};
    if (req.labels !== undefined && req.labels !== null && !Array.isArray(req.labels)) return fail(422, 'invalid request fields: labels');
    var unknown = (req.labels || []).filter(function (l) { return LABEL_NAMES.indexOf(l) < 0; });
    if (unknown.length) return fail(422, 'unknown PII type(s): ' + unknown.join(', '));
    if (req.detector && ['hybrid', 'model', 'rules'].indexOf(req.detector) < 0) return fail(422, 'unknown detector: ' + req.detector);
    if (req.threshold !== undefined && req.threshold !== null && !(req.threshold >= 0.01 && req.threshold <= 0.99)) {
      return fail(422, 'invalid request fields: threshold');
    }
    var started = performance.now();
    await delay(jitter(380));
    // Test hook: an "Always Redact" term of __fail__ simulates a server error (exercises Retry).
    if ((req.deny_terms || []).some(function (t) { return String(t).trim().toLowerCase() === '__fail__'; })) {
      return fail(500, 'simulated analysis failure (mock)');
    }
    var entities = detectAll(req);
    var byLabel = {}, byLocation = {};
    entities.forEach(function (e) {
      byLabel[e.label] = (byLabel[e.label] || 0) + 1;
      var loc = DOC[segIndexById(e.segment_id)].location;
      byLocation[loc] = (byLocation[loc] || 0) + 1;
    });
    doc.analysis = { entities: entities };
    doc.redaction = null;
    return json(200, {
      segments: segmentsPayload(),
      entities: entities,
      summary: { by_label: byLabel, by_location: byLocation },
      elapsed_ms: Math.round(performance.now() - started)
    });
  }

  async function redact(docId, init) {
    var doc = docs[docId];
    if (!doc) return fail(404, 'Document not found or expired.');
    if (!doc.analysis) return fail(409, 'Analyse the document first.');
    var parsed = await parseJson(init);
    if (parsed.error) return parsed.error;
    var req = parsed.body || {};
    if (['label', 'mask'].indexOf(req.mode) < 0) return fail(422, 'invalid request fields: mode');
    if (!Array.isArray(req.exclude_ids) || !Array.isArray(req.extra_spans)) return fail(422, 'invalid request fields: body');
    for (var i = 0; i < req.extra_spans.length; i++) {
      var x = req.extra_spans[i];
      var si = segIndexById(x && x.segment_id);
      if (si < 0) return fail(422, 'extra_spans[' + i + ']: unknown segment ' + (x && x.segment_id));
      var len = cpLength(DOC[si].text);
      if (!Number.isInteger(x.start) || !Number.isInteger(x.end) || x.start < 0 || x.end <= x.start || x.end > len) {
        return fail(422, 'extra_spans[' + i + ']: invalid offsets ' + x.start + '..' + x.end + ' (segment length ' + len + ')');
      }
      if (LABEL_NAMES.indexOf(x.label) < 0 && x.label !== 'CUSTOM') return fail(422, 'extra_spans[' + i + ']: unknown label ' + x.label);
    }
    var started = performance.now();
    await delay(jitter(450));
    var excluded = new Set(req.exclude_ids);
    var chosen = doc.analysis.entities.filter(function (e) { return !excluded.has(e.id); })
      .map(function (e) { return { label: e.label, segIdx: segIndexById(e.segment_id) }; });
    req.extra_spans.forEach(function (x) { chosen.push({ label: x.label, segIdx: segIndexById(x.segment_id) }); });
    var byLabel = {}, byLocation = {};
    chosen.forEach(function (c) {
      byLabel[c.label] = (byLabel[c.label] || 0) + 1;
      var loc = DOC[c.segIdx].location;
      byLocation[loc] = (byLocation[loc] || 0) + 1;
    });
    var residual = Math.max(0, parseInt(new URLSearchParams(window.location.search).get('residual') || '0', 10) || 0);
    var urlKept = doc.analysis.entities.some(function (e) { return e.label === 'URL' && !excluded.has(e.id); });
    var warnings = ['Accepted 2 tracked changes so deleted text cannot be recovered.'];
    if (residual) warnings.push('The residual scan replaced ' + residual + ' leftover occurrence' + (residual === 1 ? '' : 's') + ' of redacted values.');
    doc.redaction = { mode: req.mode, count: chosen.length };
    return json(200, {
      report: {
        replacements_applied: chosen.length + residual,
        counts_by_label: byLabel,
        counts_by_location: byLocation,
        hyperlinks_unlinked: urlKept ? 1 : 0,
        fields_scrubbed: 0,
        metadata_cleared: ['dc:creator', 'cp:lastModifiedBy', 'cp:revision', 'app:Company'],
        comment_authors_anonymised: 2,
        thumbnail_removed: false,
        tracked_changes_accepted: 2,
        residual_fixes: residual,
        warnings: warnings,
        elapsed_ms: Math.round(performance.now() - started)
      },
      download_url: API + '/documents/' + encodeURIComponent(docId) + '/download'
    });
  }

  async function download(docId) {
    var doc = docs[docId];
    if (!doc) return fail(404, 'Document not found or expired.');
    if (!doc.redaction) return fail(409, 'Nothing has been redacted yet.');
    await delay(jitter(120));
    var text = 'RedactNER mock output for "' + doc.filename + '" (' + doc.redaction.count + ' replacements, mode "' +
      doc.redaction.mode + '").\nThis placeholder comes from the mock backend; it is not a real Word document.\n';
    return new Response(new Blob([text], { type: DOCX_MIME }), {
      status: 200,
      headers: { 'Content-Type': DOCX_MIME, 'Content-Disposition': 'attachment; filename="redacted.docx"' }
    });
  }

  async function remove(docId) {
    await delay(60);
    var existed = !!docs[docId];
    delete docs[docId];
    return json(200, { deleted: existed });
  }

  async function route(method, path, init) {
    if (!header(init, 'X-RedactNER-Token')) return fail(401, 'missing or invalid API token');
    var m;
    if (method === 'GET' && path === API + '/config') { await delay(100); return json(200, config()); }
    if (method === 'POST' && path === API + '/documents') return upload(init);
    if ((m = /^\/api\/v1\/documents\/([^/]+)\/analyze$/.exec(path)) && method === 'POST') return analyze(decodeURIComponent(m[1]), init);
    if ((m = /^\/api\/v1\/documents\/([^/]+)\/redact$/.exec(path)) && method === 'POST') return redact(decodeURIComponent(m[1]), init);
    if ((m = /^\/api\/v1\/documents\/([^/]+)\/download$/.exec(path)) && method === 'GET') return download(decodeURIComponent(m[1]));
    if ((m = /^\/api\/v1\/documents\/([^/]+)$/.exec(path)) && method === 'DELETE') return remove(decodeURIComponent(m[1]));
    return fail(404, 'Not Found');
  }

  window.RedactNERMock = {
    sampleName: SAMPLE_NAME,
    /** A tiny fake .docx (starts with the ZIP signature) for "Load Sample Document". */
    sampleFile: function () {
      var bytes = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x14, 0x00, 0x06, 0x00]);
      return new File([bytes, 'mock RedactNER sample'], SAMPLE_NAME, { type: DOCX_MIME });
    },
    /** Drop-in replacement for window.fetch(url, init) covering the /api/v1 contract. */
    fetch: function (input, init) {
      init = init || {};
      var url = new URL(typeof input === 'string' ? input : input.url, window.location.href);
      var method = String(init.method || 'GET').toUpperCase();
      return route(method, url.pathname, init).catch(function (err) {
        return fail(500, 'Mock backend error: ' + (err && err.message ? err.message : String(err)));
      });
    }
  };
})();
