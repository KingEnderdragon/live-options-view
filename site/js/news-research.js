// News research view (MediaFlow P1): renders the sanitized research projection written by
// `python -m market_data.news research-view` (market_data/news/research.py, schema
// news-research-view/v1). site/index.html loads it before news-panel.js; loadNewsPanel (the News tab's
// first open) also calls loadNewsResearch, which mounts it under the news views from the one public key
// RESEARCH_KEY (news/v2/research/view.json) on the same base as the panel (/browser-data on the
// production origin, the public R2 origin otherwise). The fetcher requests that exact key only, never
// the private ledger; a 404 shows "No research view published", anything else that fails shows a
// failure. The tests-only harness (tests/news_panel/research.html) still renders a projection directly.
// Text goes through textContent only; links are re-checked with new URL() and must be http(s).
// What the view keeps apart, and this page shows apart:
//   - how we know: evidence kind, the source chain (who published, who they cite, the origin) and each
//     hop's access result (read, http_403, login_required, not attempted...);
//   - what is reported: one subject and one action (with its literal status) per claim, quantities each
//     with what they are OF and when they were measured (often "unknown");
//   - context figures, which are not pressure quantities;
//   - a hypothesis, labelled CONTEMPORANEOUS only if it was made and committed before its horizon,
//     otherwise RETROSPECTIVE (never a prediction);
//   - every time with its kind and precision: publication / EDGAR acceptance / sent, our access, the
//     capture commit, the annotation time and its commit; and each record's revision provenance.
// The view is research records, not proof that any market claim or thesis is true; it says so.
(function (root) {
  'use strict';

  var VIEW_SCHEMA = 'news-research-view/v1';
  var RESEARCH_KEY = 'news/v2/research/view.json';
  var DEADLINE_MS = 20000;
  var MODES = ['collector', 'archive', 'publication', 'research'];
  var EVIDENCE = {
    primary_disclosure: 'primary disclosure', attributed_report: 'attributed report',
    model_estimate: 'model estimate', mechanism_hypothesis: 'mechanism hypothesis',
    thesis_assertion: 'thesis assertion'
  };
  var ACCESS = {
    read: 'read', http_401: 'HTTP 401 (login)', http_403: 'HTTP 403', bot_blocked: 'bot-blocked',
    login_required: 'login required', not_attempted: 'not attempted', not_available: 'not available to us'
  };
  var CSS = [
    '#news-research { max-width: 1100px; display: flex; flex-direction: column; gap: 0.6rem; padding: 0.75rem 1rem;',
    '  font-size: 0.85rem; line-height: 1.4; }',
    '#news-research .nr-box { background: var(--panel, #161b22); border: 1px solid var(--border, #30363d);',
    '  border-radius: 6px; padding: 0.6rem 0.75rem; min-width: 0; }',
    '#news-research .nr-head { display: flex; flex-wrap: wrap; gap: 0.4rem; align-items: baseline; }',
    '#news-research .nr-badge { display: inline-block; font-size: 0.7rem; font-weight: 600; padding: 1px 6px;',
    '  border: 1px solid var(--border, #30363d); border-radius: 10px; color: var(--fg, #c9d1d9); white-space: nowrap; }',
    '#news-research .nr-research { color: #4a90d9; border-color: #4a90d9; }',
    '#news-research .nr-retro { color: #ffaa88; border-color: #881100; }',
    '#news-research .nr-contemp { color: #00cc55; border-color: #00cc55; }',
    '#news-research .nr-synthetic { color: #d29922; border-color: #d29922; }',
    '#news-research .nr-warn { color: #d29922; border-color: #d29922; }',
    '#news-research .nr-bad { color: #ee3300; border-color: #ee3300; }',
    '#news-research .nr-dim, #news-research .nr-note { color: var(--dim, #6e7681); }',
    '#news-research .nr-note { font-size: 0.75rem; margin: 0.2rem 0; }',
    '#news-research .nr-title { color: var(--bright, #f0f6fc); font-weight: 600; overflow-wrap: anywhere; }',
    '#news-research a { color: #58a6ff; }',
    '#news-research a.nr-title { color: var(--bright, #f0f6fc); }',
    '#news-research a:focus-visible { outline: 2px solid #4a90d9; outline-offset: 1px; }',
    '#news-research .nr-times { list-style: none; padding: 0; margin: 0.3rem 0; font-size: 0.75rem;',
    '  display: flex; flex-direction: column; gap: 1px; }',
    '#news-research .nr-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; overflow-wrap: anywhere; }',
    '#news-research .nr-ann { border-left: 2px solid var(--border, #30363d); padding-left: 0.6rem; margin-top: 0.5rem; }',
    '#news-research .nr-claim { margin-top: 0.45rem; }',
    '#news-research .nr-claim h4 { font-size: 0.8rem; color: var(--bright, #f0f6fc); margin: 0;',
    '  display: flex; flex-wrap: wrap; gap: 0.35rem; align-items: baseline; }',
    '#news-research ul.nr-list { margin: 0.2rem 0 0 1rem; padding: 0; font-size: 0.75rem; }',
    '#news-research .nr-hyp { margin-top: 0.45rem; padding: 0.4rem 0.5rem; border: 1px dashed var(--border, #30363d);',
    '  border-radius: 4px; }',
    '#news-research .nr-empty { color: var(--dim, #6e7681); font-style: italic; }',
    '@media (max-width: 600px) { #news-research { padding: 0.75rem 16px; } }'
  ].join('\n');

  function el(doc, tag, cls, text) {
    var e = doc.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function add(parent) {
    for (var i = 1; i < arguments.length; i++) if (arguments[i]) parent.appendChild(arguments[i]);
    return parent;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function badge(doc, text, kind) { return el(doc, 'span', 'nr-badge' + (kind ? ' nr-' + kind : ''), text); }
  function short(id) { return id ? String(id).slice(0, 12) : 'none'; }

  function safeUrl(u) {
    if (typeof u !== 'string') return null;
    try {
      var p = new URL(u);
      return (p.protocol === 'https:' || p.protocol === 'http:') ? p.href : null;
    } catch (e) { return null; }
  }

  function ensureStyle(doc) {
    if (doc.getElementById && doc.getElementById('news-research-style')) return;
    var s = el(doc, 'style');
    s.id = 'news-research-style';
    s.textContent = CSS;
    (doc.head || doc.body).appendChild(s);
  }

  // A projection the page will render, or a list of problems (the page then shows a failure).
  function viewProblems(v) {
    var p = [];
    if (!v || typeof v !== 'object') return ['not an object'];
    if (v.schema_version !== VIEW_SCHEMA) p.push('schema_version is not ' + VIEW_SCHEMA);
    if (MODES.indexOf(v.mode) < 0) p.push('unknown mode');
    if (typeof v.cutoff !== 'string') p.push('cutoff missing');
    if (!Array.isArray(v.items)) p.push('items missing');
    else v.items.forEach(function (it, i) {
      if (!it || !it.source || !Array.isArray(it.annotations)) p.push('items[' + i + '] malformed');
      else it.annotations.forEach(function (a, j) {
        if (!a || !Array.isArray(a.claims) || typeof a.annotated_at !== 'string') p.push('items[' + i + '].annotations[' + j + '] malformed');
      });
    });
    return p;
  }

  function timeText(tv, fallback) {
    if (!tv || tv.value === 'unknown' || tv.value === null || tv.value === undefined) return fallback || 'unknown';
    return tv.value + (tv.precision && tv.precision !== 'second' ? ' (' + tv.precision + ' precision)' : '');
  }

  function pubLabel(kind) {
    return kind === 'edgar_acceptance' ? 'EDGAR acceptance' : kind === 'sent' ? 'sent' : 'published';
  }

  function accessText(acc) {
    var s = ACCESS[acc.result] || acc.result;
    if (acc.at && acc.at.value && acc.at.value !== 'unknown') s += ' at ' + timeText(acc.at);
    if (acc.attempts && acc.attempts.length) {
      s += ' (attempts: ' + acc.attempts.map(function (x) { return (ACCESS[x.result] || x.result) + ' ' + x.requested_at; }).join('; ') + ')';
    }
    return s;
  }

  function linkOrText(doc, text, url, cls) {
    var u = safeUrl(url);
    if (!u) return el(doc, 'span', cls, text);
    var a = el(doc, 'a', cls, text);
    a.href = u;
    a.rel = 'noopener noreferrer';
    a.target = '_blank';
    return a;
  }

  function renderSource(doc, it) {
    var s = it.source;
    var box = el(doc, 'div', 'nr-box');
    box.setAttribute('data-kind', s.kind);
    var head = add(el(doc, 'div', 'nr-head'),
      badge(doc, s.kind === 'research_capture' ? 'research capture' : 'collected', s.kind === 'research_capture' ? 'research' : null),
      linkOrText(doc, s.title || '(untitled)', s.url, 'nr-title'),
      el(doc, 'span', 'nr-dim', s.party || ''));
    var times = el(doc, 'ul', 'nr-times');
    add(times, el(doc, 'li', null, pubLabel(s.published && s.published.kind) + ' ' + timeText(s.published) +
      (s.published && s.published.basis ? ' · ' + s.published.basis : '')));
    if (s.updated) add(times, el(doc, 'li', null, 'updated ' + timeText(s.updated)));
    if (s.kind === 'research_capture') {
      add(times, el(doc, 'li', null, 'accessed ' + timeText(s.access.at) + ' — ' + accessText({ result: s.access.result, attempts: s.access.attempts }) +
        (s.access.via ? ' · via ' + s.access.via : '')));
      if (s.access.at && s.access.at.basis) add(times, el(doc, 'li', 'nr-dim', 'access time basis: ' + s.access.at.basis));
      add(times, el(doc, 'li', null, 'capture committed ' + (s.committed_at || 'not yet committed') + ' · no collector known-at'));
    } else {
      add(times, el(doc, 'li', null, 'observed by the collector ' + s.observed_at + ' · archived ' + (s.committed_at || 'not yet')));
    }
    add(times, el(doc, 'li', 'nr-mono nr-dim', (s.kind === 'research_capture' ? 'capture revision ' : 'revision ') +
      it.revision_count + ' · ' + short(s.revision_id) + (s.previous_revision_id ? ' ← ' + short(s.previous_revision_id) : ' (first)') +
      ' · item ' + short(s.item_id) + ' · run ' + (s.run_id || 'none')));
    add(box, head, times);
    if (s.note) add(box, el(doc, 'p', 'nr-note', s.note));
    if (!it.annotations.length) add(box, el(doc, 'p', 'nr-empty', 'no annotation visible at this cutoff'));
    it.annotations.forEach(function (a) { add(box, renderAnnotation(doc, a)); });
    if (it.annotations_on_other_revisions && it.annotations_on_other_revisions.length) {
      add(box, el(doc, 'p', 'nr-note', 'annotations made on an earlier revision: ' + it.annotations_on_other_revisions.join(', ') +
        ' (not carried over: a changed source needs a new annotation)'));
    }
    return box;
  }

  function renderAnnotation(doc, a) {
    var box = el(doc, 'div', 'nr-ann');
    box.setAttribute('data-lineage', a.lineage);
    var head = add(el(doc, 'div', 'nr-head'),
      el(doc, 'strong', null, 'annotation ' + a.lineage),
      badge(doc, a.method + ' ' + a.version),
      a.revision_count > 1 ? badge(doc, 'revision ' + a.revision_count + ' of ' + a.revision_count) : null,
      a.made_after_cutoff ? badge(doc, 'made after cutoff', 'retro') : null);
    var times = add(el(doc, 'ul', 'nr-times'),
      el(doc, 'li', null, 'made ' + a.annotated_at + ' · committed ' + (a.committed_at || 'not yet committed')),
      el(doc, 'li', 'nr-mono nr-dim', 'annotation ' + short(a.annotation_id) +
        (a.previous && a.previous.length ? ' ← ' + a.previous.map(short).join(' ← ') : '') +
        ' · on revision ' + short(a.input_revision_id) + ' · run ' + a.run_id));
    add(box, head, times);
    a.claims.forEach(function (c) { add(box, renderClaim(doc, c)); });
    if (a.context && a.context.length) {
      var ctx = add(el(doc, 'div', 'nr-claim'), el(doc, 'h4', null, 'Context (stated figures, not pressure quantities)'));
      add(ctx, list(doc, a.context.map(quantityText)));
      add(box, ctx);
    }
    if (a.hypothesis) add(box, renderHypothesis(doc, a.hypothesis));
    if (a.hypothesis_withheld) add(box, el(doc, 'p', 'nr-note', 'hypothesis withheld: ' + a.hypothesis_withheld));
    if (a.note) add(box, el(doc, 'p', 'nr-note', a.note));
    return box;
  }

  function quantityText(q) {
    return String(q.value) + ' ' + q.unit + ' — of ' + q.of + ' · measured ' + q.measured_at + ' · ' + q.basis;
  }

  function list(doc, lines) {
    var ul = el(doc, 'ul', 'nr-list');
    lines.forEach(function (l) { add(ul, el(doc, 'li', null, l)); });
    return ul;
  }

  function renderClaim(doc, c) {
    var box = el(doc, 'div', 'nr-claim');
    box.setAttribute('data-claim', c.id);
    var status = c.action.status;
    add(box, add(el(doc, 'h4'),
      el(doc, 'span', null, c.id),
      badge(doc, EVIDENCE[c.evidence_kind] || c.evidence_kind,
        c.evidence_kind === 'thesis_assertion' || c.evidence_kind === 'mechanism_hypothesis' ? 'warn' : null),
      el(doc, 'span', null, (c.subject.party || c.subject.population) + (c.subject.population ? ' (population)' : '')),
      el(doc, 'span', 'nr-dim', '·'),
      el(doc, 'span', null, c.action.kind),
      badge(doc, status, status === 'unverified' || status === 'contradicted' ? 'bad' : status === 'offered' ? 'warn' : null)));
    var lines = [];
    if (c.action.detail) lines.push('action: ' + c.action.detail);
    if (c.exposure) {
      lines.push('exposure: ' + c.exposure.direction + (c.exposure.to ? ' · ' + c.exposure.to : '') +
        (c.exposure.instrument_ids && c.exposure.instrument_ids.length ? ' · ' + c.exposure.instrument_ids.join(', ') : ''));
    }
    c.quantities.forEach(function (q) { lines.push('quantity: ' + quantityText(q)); });
    if (!c.quantities.length) lines.push('quantity: none stated');
    c.dates.forEach(function (d) { lines.push('date: ' + d.kind + ' ' + timeText(d.at) + ' (not a measurement time)'); });
    if (lines.length) add(box, list(doc, lines));
    var chain = el(doc, 'ul', 'nr-list');
    c.chain.forEach(function (h) {
      var li = el(doc, 'li');
      add(li, el(doc, 'span', null, h.role + ': '), linkOrText(doc, h.party, h.url),
        el(doc, 'span', 'nr-dim', ' — ' + accessText(h.access) + (h.revision ? ' · captured revision ' + short(h.revision.revision_id) : ' · not captured')),
        h.note ? el(doc, 'span', 'nr-dim', ' · ' + h.note) : null);
      add(chain, li);
    });
    add(box, el(doc, 'div', 'nr-note', 'source chain (what we read → who they cite → origin):'), chain);
    if (c.note) add(box, el(doc, 'p', 'nr-note', c.note));
    return box;
  }

  function renderHypothesis(doc, h) {
    var box = el(doc, 'div', 'nr-hyp');
    var retro = h.timing !== 'contemporaneous';
    add(box, add(el(doc, 'div', 'nr-head'),
      badge(doc, retro ? 'RETROSPECTIVE hypothesis' : 'CONTEMPORANEOUS hypothesis', retro ? 'retro' : 'contemp'),
      el(doc, 'span', 'nr-dim', retro ? 'made after its horizon began: not a prediction'
        : 'made and committed before its horizon began')));
    add(box, el(doc, 'p', null, h.statement));
    add(box, list(doc, [
      'horizon ' + timeText(h.horizon.start) + ' → ' + timeText(h.horizon.end) +
        (h.instruments && h.instruments.length ? ' · ' + h.instruments.join(', ') : ''),
      'disconfirmed by: ' + h.disconfirmation
    ].concat((h.missing_evidence || []).map(function (m) { return 'missing evidence: ' + m; }))));
    return box;
  }

  // Render a projection into container. Returns the data-state set on the container.
  function render(doc, container, view) {
    ensureStyle(doc);
    clear(container);
    var problems = viewProblems(view);
    if (problems.length) {
      container.setAttribute('data-state', 'failed');
      add(container, el(doc, 'p', 'nr-box nr-bad', 'Could not show research records: ' + problems.slice(0, 3).join('; ')));
      return 'failed';
    }
    var head = el(doc, 'div', 'nr-box');
    add(head, add(el(doc, 'div', 'nr-head'),
      badge(doc, view.mode === 'research' ? 'RESEARCH' : view.mode === 'publication' ? 'RETROSPECTIVE' : view.mode,
        view.mode === 'publication' ? 'retro' : 'research'),
      /SYNTHETIC/.test(view.data_label || '') ? badge(doc, 'SYNTHETIC', 'synthetic') : null,
      /PREPARED/.test(view.data_label || '') ? badge(doc, 'PREPARED', 'warn') : null,
      el(doc, 'span', null, view.label),
      el(doc, 'span', 'nr-dim', 'cutoff ' + view.cutoff)));
    add(head, el(doc, 'p', 'nr-note', view.data_label || ''));
    add(head, el(doc, 'p', 'nr-note', view.not_proof || 'Research records, not proof.'));
    var ex = view.excluded || {};
    if (ex.no_publication_time || ex.not_yet_visible) {
      add(head, el(doc, 'p', 'nr-note', 'not shown: ' + (ex.not_yet_visible || 0) + ' not yet visible at this cutoff, ' +
        (ex.no_publication_time || 0) + ' with no publication time (never guessed)'));
    }
    add(container, head);
    if (!view.items.length) {
      add(container, el(doc, 'p', 'nr-box nr-empty', 'No research records visible at this cutoff in ' + view.mode + ' mode.'));
    }
    view.items.forEach(function (it) { add(container, renderSource(doc, it)); });
    container.setAttribute('data-state', 'ready');
    container.setAttribute('data-items', String(view.items.length));
    return 'ready';
  }

  function researchError(kind, message) {
    var e = new Error(message);
    e.researchKind = kind;  // refused | absent | deadline | http | network | json
    return e;
  }

  // Returns fetchView() -> Promise<object>: exactly RESEARCH_KEY on base, no cached copy, no credentials,
  // the existing ?_= query, and a deadline (aborted when AbortController exists).
  function makeFetcher(base, fetchImpl, now, deadlineMs, timers) {
    var root = String(base).replace(/\/+$/, '');
    var limit = deadlineMs || DEADLINE_MS;
    var T = timers || { set: function (f, ms) { return setTimeout(f, ms); }, clear: function (h) { clearTimeout(h); } };
    return function fetchView(key) {
      if (key !== undefined && key !== RESEARCH_KEY) {
        return Promise.reject(researchError('refused', 'refused: only the research view key is requested'));
      }
      var url = root + '/' + RESEARCH_KEY + '?_=' + Math.floor(now());
      var ctrl = typeof AbortController === 'function' ? new AbortController() : null;
      var timer = null;
      var expired = new Promise(function (_, reject) {
        timer = T.set(function () {
          if (ctrl) { try { ctrl.abort(); } catch (e) { /* settled */ } }
          reject(researchError('deadline', 'no complete response within ' + Math.round(limit / 1000) + ' s'));
        }, limit);
      });
      var work = Promise.resolve().then(function () {
        var init = { credentials: 'omit', cache: 'no-store' };
        if (ctrl) init.signal = ctrl.signal;
        return fetchImpl(url, init);
      }).then(function (r) {
        if (r.status === 404) throw researchError('absent', 'No research view published');
        if (!r.ok) throw researchError('http', 'HTTP ' + r.status);
        return r.text().then(function (body) {
          try { return JSON.parse(body); } catch (e) { throw researchError('json', 'response is not valid JSON'); }
        });
      }, function (e) {
        throw e && e.researchKind ? e : researchError('network', 'network error');
      });
      return Promise.race([work, expired]).then(function (v) { T.clear(timer); return v; },
        function (e) { T.clear(timer); throw e; });
    };
  }

  // Mount once: loading, then ready / absent / failed. Never throws.
  function mount(doc, container, fetchView) {
    ensureStyle(doc);
    clear(container);
    container.setAttribute('data-state', 'loading');
    add(container, el(doc, 'p', 'nr-box nr-dim', 'Loading research records…'));
    return fetchView().then(function (view) {
      return render(doc, container, view);
    }, function (e) {
      clear(container);
      var absent = e && e.researchKind === 'absent';
      container.setAttribute('data-state', absent ? 'absent' : 'failed');
      add(container, el(doc, 'p', 'nr-box ' + (absent ? 'nr-empty' : 'nr-bad'),
        absent ? 'No research view published' : 'Could not load research records: ' + (e && e.message || 'unknown error')));
      return container.getAttribute('data-state');
    });
  }

  var api = { VIEW_SCHEMA: VIEW_SCHEMA, RESEARCH_KEY: RESEARCH_KEY, CSS: CSS, render: render, viewProblems: viewProblems,
              safeUrl: safeUrl, makeFetcher: makeFetcher, mount: mount };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.NewsResearch = api;
})(typeof window !== 'undefined' ? window : this);

// Called by loadNewsPanel (js/news-panel.js) on the News tab's first open. BROWSER_DATA is the page's
// data base (index.html). Absent container (the harness, other pages): nothing happens.
function loadNewsResearch() {
  var box = typeof document !== 'undefined' ? document.getElementById('news-research') : null;
  if (!box) return null;
  return NewsResearch.mount(document, box,
    NewsResearch.makeFetcher(BROWSER_DATA, function (url, opts) { return fetch(url, opts); }, Date.now));
}
