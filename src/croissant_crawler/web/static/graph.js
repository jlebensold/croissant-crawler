/* Knowledge-graph explorer (Cytoscape). Overview of hubs, focus + expand on demand. */
(function () {
  const css = getComputedStyle(document.documentElement);
  const v = (name) => css.getPropertyValue(name).trim();
  const TYPE_COLORS = {
    dataset: v('--accent'), organization: '#db2777', person: '#f472b6', license: '#65a30d', task: '#ea580c',
    modality: '#0ea5e9', domain: '#8b5cf6', paper: '#64748b', source: '#0f766e', external_dataset: '#a8a29e',
  };
  const SOURCE_COLORS = { huggingface: v('--src-huggingface'), openml: v('--src-openml'), kaggle: v('--src-kaggle'),
    dataverse: v('--src-dataverse'), web: v('--src-web') };
  const EDGE_COLORS = { SAME_AS: '#dc2626', DERIVED_FROM: '#7c3aed', SIMILAR_TO: '#0ea5e9', CO_OCCURS: v('--border-strong') };
  const hidden = new Set(['person']);
  const el = document.getElementById('cy');

  const cy = cytoscape({
    container: el,
    wheelSensitivity: 0.25,
    minZoom: 0.1, maxZoom: 3,
    style: [
      { selector: 'node', style: {
        'background-color': (n) => n.data('type') === 'dataset' ? (SOURCE_COLORS[n.data('props')?.source] || TYPE_COLORS.dataset) : (TYPE_COLORS[n.data('type')] || '#999'),
        'label': 'data(short)', 'font-size': 10, 'color': v('--text'), 'text-valign': 'bottom', 'text-margin-y': 3,
        'text-outline-color': v('--surface'), 'text-outline-width': 2, 'width': 'data(size)', 'height': 'data(size)',
        'border-width': 0 } },
      { selector: 'node[type = "dataset"]', style: { 'shape': 'round-rectangle' } },
      { selector: 'node[type = "license"]', style: { 'shape': 'diamond' } },
      { selector: 'node[type = "paper"]', style: { 'shape': 'tag' } },
      { selector: 'node:selected', style: { 'border-width': 3, 'border-color': v('--text') } },
      { selector: 'node.center', style: { 'border-width': 3, 'border-color': v('--accent'), 'font-weight': 700, 'font-size': 12 } },
      { selector: 'edge', style: {
        'width': 'data(w)', 'line-color': (e) => EDGE_COLORS[e.data('type')] || v('--border-strong'),
        'opacity': 0.55, 'curve-style': 'haystack' } },
      { selector: 'edge[type = "DERIVED_FROM"]', style: { 'curve-style': 'bezier', 'target-arrow-shape': 'triangle',
        'target-arrow-color': EDGE_COLORS.DERIVED_FROM, 'opacity': .9 } },
      { selector: '.hidden', style: { 'display': 'none' } },
    ],
  });

  const short = (s) => (s.length > 28 ? s.slice(0, 26) + '…' : s);
  function nodeData(n, degree) {
    const d = degree ?? n.degree ?? 1;
    const size = n.type === 'dataset' ? 14 + Math.min(16, Math.sqrt(n.props?.popularity || 0) / 60) : 16 + Math.min(44, Math.sqrt(d) * 3);
    return { id: n.id, label: n.label, short: short(n.label), type: n.type, props: n.props || {}, size, degree: d };
  }

  function applyHidden() {
    cy.nodes().forEach((n) => n.toggleClass('hidden', hidden.has(n.data('type')) && !n.hasClass('center')));
  }

  function runLayout(name) {
    cy.layout(name === 'concentric'
      ? { name: 'concentric', animate: false, minNodeSpacing: 18, concentric: (n) => (n.hasClass('center') ? 10 : n.data('type') === 'dataset' ? 1 : 3), levelWidth: () => 1 }
      : { name: 'cose', animate: false, nodeRepulsion: () => 22000, idealEdgeLength: () => 120, edgeElasticity: () => 60,
          nodeOverlap: 20, gravity: 0.6, numIter: 2000, randomize: true, padding: 30 }
    ).run();
  }

  async function overview() {
    const r = await fetch('/api/graph/overview?per_type=12');
    const g = await r.json();
    cy.elements().remove();
    const linked = new Set(g.edges.flatMap((e) => [e.src, e.dst]));
    cy.add(g.nodes.filter((n) => linked.has(n.id)).map((n) => ({ group: 'nodes', data: nodeData(n, n.degree) })));
    const maxW = Math.max(1, ...g.edges.map((e) => e.weight));
    cy.add(g.edges.map((e) => ({ group: 'edges', data: { id: `${e.src}|${e.dst}`, source: e.src, target: e.dst, type: 'CO_OCCURS', weight: e.weight, w: 0.5 + 5 * (e.weight / maxW) } })));
    applyHidden();
    runLayout('cose');
    detail(null);
  }

  async function focus(id, merge = false) {
    const r = await fetch('/api/graph/node?limit=80&id=' + encodeURIComponent(id));
    const g = await r.json();
    if (!g.nodes.length) {
      document.getElementById('node-detail').innerHTML = `<p class="small" style="margin:0">Node <code>${escapeHtml(id)}</code> is not in the graph yet. It appears after the next graph rebuild.</p>`;
      if (!cy.nodes().length) overview();
      return;
    }
    if (!merge) cy.elements().remove();
    cy.nodes().removeClass('center');
    for (const n of g.nodes) {
      if (cy.getElementById(n.id).empty()) cy.add({ group: 'nodes', data: nodeData(n) });
    }
    for (const e of g.edges) {
      const eid = `${e.src}|${e.dst}|${e.type}`;
      if (cy.getElementById(eid).empty()) cy.add({ group: 'edges', data: { id: eid, source: e.src, target: e.dst, type: e.type, w: e.type === 'SIMILAR_TO' ? 1 + 3 * e.weight : 1.2 } });
    }
    const c = cy.getElementById(id);
    c.addClass('center').data('degree', g.degree);
    applyHidden();
    runLayout(merge ? 'cose' : 'concentric');
    cy.center(c);
    detail(c);
  }

  function linkFor(d) {
    const p = d.props || {};
    switch (d.type) {
      case 'dataset': return `<a class="btn small" href="/datasets/${d.id.split(':')[1]}">Open dataset page</a>`;
      case 'task': return `<a class="btn small" href="/?tasks=${encodeURIComponent(d.label)}">Search datasets</a>`;
      case 'modality': return `<a class="btn small" href="/?modalities=${encodeURIComponent(d.label)}">Search datasets</a>`;
      case 'domain': return `<a class="btn small" href="/?domains=${encodeURIComponent(d.label)}">Search datasets</a>`;
      case 'license': return `<a class="btn small" href="/?license=${encodeURIComponent(d.label)}">Search datasets</a>`;
      case 'source': return `<a class="btn small" href="/?source=${encodeURIComponent(d.label)}">Search datasets</a>`;
      case 'paper': return p.url ? `<a class="btn small" href="${p.url}" target="_blank" rel="noopener">Open paper ↗</a>` : '';
      default: return `<a class="btn small" href="/?q=${encodeURIComponent(d.label)}">Search “${escapeHtml(short(d.label))}”</a>`;
    }
  }

  function escapeHtml(s) { return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

  function detail(node) {
    const box = document.getElementById('node-detail');
    if (!node) { box.innerHTML = '<p class="muted small" style="margin:0">Overview: the most connected tasks, modalities, domains, licenses, organisations and sources. Edge thickness = number of datasets sharing both. Click a hub to explore.</p>'; return; }
    const d = node.data();
    const p = d.props || {};
    const rows = [];
    if (d.type === 'dataset') {
      rows.push(`<div class="row"><span class="badge src ${escapeHtml(p.source)}">${escapeHtml(p.source)}</span><span class="tier ${escapeHtml(p.tier)}">${escapeHtml(p.tier)}</span><span class="small">${Math.round(p.quality)}/100</span></div>`);
      rows.push(`<div class="mono small faint">${escapeHtml((p.external_id || '').split('|').pop())}</div>`);
    }
    rows.push(`<div class="small muted">${escapeHtml(d.type)} · ${d.degree ?? node.degree()} connections</div>`);
    box.innerHTML = `<h3 style="overflow-wrap:anywhere">${escapeHtml(d.label)}</h3><div class="stack" style="--gap:.4rem">${rows.join('')}<div class="row">${linkFor(d)}<button class="btn small" id="btn-expand">Expand</button></div></div>`;
    document.getElementById('btn-expand').onclick = () => focus(d.id, true);
  }

  cy.on('tap', 'node', (evt) => {
    const n = evt.target;
    if (n.data('degree') && cy.edges().filter((e) => e.data('type') === 'CO_OCCURS').length) { focus(n.id()); return; }
    detail(n);
  });
  cy.on('dbltap', 'node', (evt) => focus(evt.target.id(), true));

  // legend
  const legend = document.getElementById('legend');
  for (const [type, color] of Object.entries(TYPE_COLORS)) {
    const id = 'lg-' + type;
    legend.insertAdjacentHTML('beforeend', `<label for="${id}"><input type="checkbox" id="${id}" ${hidden.has(type) ? '' : 'checked'}><span class="sw" style="background:${color}"></span>${type.replace('_', ' ')}</label>`);
    document.getElementById(id).addEventListener('change', (e) => { e.target.checked ? hidden.delete(type) : hidden.add(type); applyHidden(); });
  }

  // typeahead
  const input = document.getElementById('node-search');
  const list = document.getElementById('node-results');
  let timer;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { list.hidden = true; return; }
    timer = setTimeout(async () => {
      const r = await fetch('/api/graph/search?q=' + encodeURIComponent(q));
      const items = await r.json();
      list.innerHTML = items.map((it) => `<li data-id="${escapeHtml(it.id)}"><span>${escapeHtml(short(it.label))}</span><span class="faint small">${escapeHtml(it.type)} · ${it.degree}</span></li>`).join('') || '<li class="muted">No matches</li>';
      list.hidden = false;
    }, 200);
  });
  list.addEventListener('click', (e) => {
    const li = e.target.closest('li[data-id]');
    if (!li) return;
    list.hidden = true;
    input.value = '';
    focus(li.dataset.id);
  });
  document.addEventListener('click', (e) => { if (!e.target.closest('.typeahead')) list.hidden = true; });
  document.getElementById('btn-overview').addEventListener('click', overview);

  const initial = el.dataset.focus;
  initial ? focus(initial) : overview();
})();
