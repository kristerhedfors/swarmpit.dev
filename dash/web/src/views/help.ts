// Help: shortcuts, colour legend, metric glossary (from metric_defs via /api/facets), data caveats.
import { api } from '../api';
import { h, replace } from '../dom';
import { agentColor, EDGE_KINDS, edgeColor, IFACES, ifaceColor, popColor, STEP_CLASSES, stepColor } from '../lib/colors';
import { SHORTCUTS } from '../keys';
import type { View } from './view';

export function helpView(): View {
  const root = h('div');
  const gloss = h('div', null, h('div', { class: 'empty' }, 'loading metric registry…'));
  return {
    el: root,
    mount: () => {
      replace(root,
        h('div', { class: 'view-title' }, h('h1', null, 'Help')),
        h('div', { class: 'help-grid' },
          h('div', { class: 'card' }, h('header', null, h('h2', null, 'Keyboard shortcuts')),
            h('table', null, ...SHORTCUTS.map(([k, d]) => h('tr', null, h('td', null, h('kbd', null, k)), h('td', { class: 'secondary' }, d))))),
          h('div', { class: 'card' }, h('header', null, h('h2', null, 'Colour roles')),
            h('div', { class: 'sec muted' }, 'Step category (lanes)'),
            h('div', { class: 'legend' }, ...STEP_CLASSES.map((c) => h('span', { class: 'it' }, c.id === 'idle' ? h('i', { class: 'hollow' }) : h('i', { class: 'sw', style: { background: stepColor(c.id) } }), c.label))),
            h('p', { class: 'muted' }, 'Red outline = persistence attempt or secret leak. Board lane glyphs: ○ read, ● post, ▲ upvote, ◆ DM, ✕ delete — coloured by agent.'),
            h('div', { class: 'sec muted' }, 'Agents'),
            h('div', { class: 'legend' }, ...Array.from({ length: 8 }, (_, i) => h('span', { class: 'it' }, h('i', { class: 'sw', style: { background: agentColor(i), borderRadius: '50%' } }), 'a' + i))),
            h('p', { class: 'muted' }, 'a8–a15 reuse these hues with dashed chart lines (a16+ dotted). In runs with more than 8 agents, selecting an agent mutes all others (highlight-on-select). Digit keys pick lanes by position; type two digits quickly for lanes 10+.'),
            h('div', { class: 'sec muted' }, 'Interfaces / population'),
            h('div', { class: 'legend' }, ...IFACES.map((f) => h('span', { class: 'it' }, h('i', { class: 'sw', style: { background: ifaceColor(f) } }), f)),
              h('span', { class: 'it' }, h('i', { class: 'sw', style: { background: popColor('solo') } }), 'solo'), h('span', { class: 'it' }, h('i', { class: 'sw', style: { background: popColor('multi') } }), 'multi')),
            h('div', { class: 'sec muted' }, 'Interaction edges (src attends to dst)'),
            h('div', { class: 'legend' }, ...EDGE_KINDS.map((k) => h('span', { class: 'it' }, h('i', { class: 'ln', style: { background: edgeColor(k) } }), k)))),
          h('div', { class: 'card' }, h('header', null, h('h2', null, 'Known data caveats')),
            h('ul', { class: 'secondary', style: { paddingLeft: '18px', margin: 0 } },
              h('li', null, 'The first IRC pass (before lab commit 6034d34) lost agent lines ending in a bare LF; those runs are archived as *-invalid_irc_lf (tagged, so excluded by default). Any run where an agent sent bytes to the IRC board but the board parsed no command from them gets an irc_inbound_broken badge and is excluded from cell comparisons unless "include irc_inbound_broken runs" is ticked. A read-only probe (connect, read, close) is not flagged.'),
              h('li', null, 'Cross mentions (run metric) follows lab/analyze.py: live posts whose body contains another live author name as a plain substring, so it matches analysis.json. Graph-based mention edges are reported separately as mention_edges_live.'),
              h('li', null, 'Seed provenance: runs carry no seed.json, so the seed is read from the lab checkout that owns the runs dir at ingest time (seed_inferred). Derive checks it against the board numbering and flags seed_mismatch when they disagree.'),
              h('li', null, 'code_version is inferred from git history when run.json has none; a "+dirty?" suffix means the lab tree had uncommitted changes older than the run start.'),
              h('li', null, 'Smoke runs use an older harness (code_version pre-987afa9-inferred, voluntary_stop endings).'),
              h('li', null, 'Board attribution, reply and exposure edges are heuristic; edge evidence says how. Listings and blind replies get lower weight.'),
              h('li', null, 'Agents in multi runs share a board, so CIs are bootstrapped by run, never by agent.'),
              h('li', null, 'Prompts / nudges the model saw are not logged; the inspector marks the reconstructed output truncation.'),
              h('li', null, 'The observatory is read-only in v1; /api/control/* returns 501.')))),
        h('div', { class: 'card', style: { marginTop: '12px' } }, h('header', null, h('h2', null, 'Metric glossary'), h('span', { class: 'sub' }, 'from metric_defs')), gloss));
      api.facets().then((f) => replace(gloss, h('table', { class: 'tbl' },
        h('thead', null, h('tr', null, h('th', null, 'metric'), h('th', null, 'id'), h('th', null, 'unit'), h('th', null, 'grain'), h('th', null, 'agg'), h('th', null, 'direction'), h('th', null, 'description'))),
        h('tbody', null, ...f.metrics.map((m) => h('tr', null, h('td', null, m.label), h('td', { class: 'mono' }, m.metric_id), h('td', null, m.unit), h('td', null, m.grain), h('td', null, m.agg), h('td', null, m.higher_is), h('td', { class: 'wrap secondary' }, m.description)))))))
        .catch((e) => replace(gloss, h('div', { class: 'err' }, String(e))));
    },
    destroy: () => {},
  };
}
