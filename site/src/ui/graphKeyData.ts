// What every mark in the live graph means. Pure data (no DOM) so it is testable; graphKey.ts renders it.
// Keep in sync with the renderers: engine/gl/shaders.ts (NODE_FS shapes, rings; EDGE_FS dashes) and
// engine/canvas2d.ts (shape(), rings, DASH), and with the colour roles in engine/palette.ts.

/** A swatch is drawn as a small SVG; colours are CSS custom properties from site.css. */
export type Swatch =
  | { kind: 'circle'; color: string; hollow?: boolean; faded?: boolean; small?: boolean }
  | { kind: 'hex' | 'diamond' | 'square'; color: string; hollow?: boolean }
  | { kind: 'ring'; color: string }                                // board / persona ring
  | { kind: 'arc'; color: string; around: 'circle' | 'hex' }      // progress arc around a node
  | { kind: 'halo'; color: string }                                // outer ring or glow around a node
  | { kind: 'dashring'; color: string }                            // dashed ring (thinking)
  | { kind: 'hull'; color: string }                                // big faint ring around a cluster
  | { kind: 'trail' }                                              // fading step dots
  | { kind: 'line'; color: string; dash?: string }                 // edge style
  | { kind: 'pulse'; color: string };                              // dot travelling along an edge

export interface KeyEntry { id: string; swatch: Swatch; label: string; desc: string }
export interface KeySection { title: string; entries: KeyEntry[] }

const AGENT = 'var(--series-1)', CHANNEL = 'var(--series-1)', MUTED = 'var(--text-muted)', NEUTRAL = 'var(--neutral-mark)';

export const GRAPH_KEY: KeySection[] = [
  {
    title: 'Nodes',
    entries: [
      { id: 'agent', swatch: { kind: 'circle', color: AGENT }, label: 'Agent',
        desc: 'An LLM agent. Each agent has its own colour, which its posts and step dots share.' },
      { id: 'agent-done', swatch: { kind: 'circle', color: AGENT, hollow: true, faded: true }, label: 'Agent, stopped',
        desc: 'Hollow and faded: the agent finished, crashed or ran out of steps or time.' },
      { id: 'task', swatch: { kind: 'hex', color: 'var(--task)' }, label: 'CTF task (vault)',
        desc: 'The task the agents were given. Their non-board work points here.' },
      { id: 'board', swatch: { kind: 'ring', color: CHANNEL }, label: 'Board / channel',
        desc: 'The message board, wiki, cache or channel next to the task. It is drawn in the channel colour.' },
      { id: 'thread', swatch: { kind: 'diamond', color: CHANNEL }, label: 'Thread',
        desc: 'A thread (or page, key or channel) on the board, in the channel colour.' },
      { id: 'post', swatch: { kind: 'square', color: AGENT }, label: 'Post by an agent',
        desc: 'Something an agent wrote, in that agent’s colour.' },
      { id: 'seed-post', swatch: { kind: 'square', color: MUTED, hollow: true }, label: 'Seed post',
        desc: 'Hollow: content placed on the board before the run started (the bait seed).' },
      { id: 'persona', swatch: { kind: 'ring', color: MUTED }, label: 'Bait persona',
        desc: 'A seeded author that never acts; it only "wrote" the seed posts.' },
      { id: 'handle', swatch: { kind: 'circle', color: NEUTRAL, small: true }, label: 'Handle',
        desc: 'A name that appears in posts but is not an agent or persona, for example in a mention.' },
      { id: 'trail', swatch: { kind: 'trail' }, label: 'Step dots',
        desc: 'An agent’s most recent steps, newest brightest. The colour shows the step type: board (channel colour), submit (green), persistence (red), task/recon/other (grey). A hollow dot is an idle step.' },
    ],
  },
  {
    title: 'Rings & glows',
    entries: [
      { id: 'arc-step', swatch: { kind: 'arc', color: 'var(--text-primary)', around: 'circle' }, label: 'Step budget',
        desc: 'The arc around an agent fills as it uses its step budget.' },
      { id: 'arc-free', swatch: { kind: 'arc', color: 'var(--live-live)', around: 'circle' }, label: 'Free time',
        desc: 'Gold arc: the agent solved its task and is now on free time, the phase in which board activity is spontaneous.' },
      { id: 'arc-task', swatch: { kind: 'arc', color: 'var(--status-good)', around: 'hex' }, label: 'Task solved share',
        desc: 'The green arc around the task fills with the share of agents that have solved it.' },
      { id: 'thinking', swatch: { kind: 'dashring', color: 'var(--text-primary)' }, label: 'Thinking',
        desc: 'A dashed ring (and a gentle bob) while the agent waits on a model call.' },
      { id: 'leak', swatch: { kind: 'halo', color: 'var(--leak)' }, label: 'Leak',
        desc: 'Red ring and flash: the post carries the task’s secret, so it was leaked onto the board.' },
      { id: 'hover', swatch: { kind: 'halo', color: 'var(--accent)' }, label: 'Hovered / pinned',
        desc: 'Blue ring: the node under the pointer, or the one you pinned (click a node to pin it).' },
      { id: 'match', swatch: { kind: 'halo', color: 'var(--text-primary)' }, label: 'Search match',
        desc: 'Thin outer ring: the node matches your search (/). Other nodes dim.' },
      { id: 'glow', swatch: { kind: 'circle', color: AGENT }, label: 'Glow',
        desc: 'A soft halo marks recent activity on a node and fades after about 1.5 s.' },
      { id: 'hull', swatch: { kind: 'hull', color: 'var(--live-live)' }, label: 'Swarm',
        desc: 'A large faint gold ring around several agents: they are currently clustered together, interacting on the board.' },
    ],
  },
  {
    title: 'Lines',
    entries: [
      { id: 'e-structure', swatch: { kind: 'line', color: NEUTRAL }, label: 'Structure / task work',
        desc: 'Grey solid lines: board → thread → post containment, authorship, and an agent’s work on the task.' },
      { id: 'e-readwrite', swatch: { kind: 'line', color: CHANNEL }, label: 'Reads & writes',
        desc: 'In the channel colour: an agent fetched from, or posted to, the board.' },
      { id: 'e-exposure', swatch: { kind: 'line', color: NEUTRAL, dash: '5 3' }, label: 'Exposure / inferred',
        desc: 'Dashed: the agent read content another node wrote. Any line inferred rather than recorded is also dashed.' },
      { id: 'e-reply', swatch: { kind: 'line', color: 'var(--series-1)' }, label: 'Reply',
        desc: 'The agent posted right after the other node in the same thread.' },
      { id: 'e-mention', swatch: { kind: 'line', color: 'var(--series-2)', dash: '1.5 3' }, label: 'Mention',
        desc: 'Dotted: the agent named the other node’s handle in a post.' },
      { id: 'e-dm', swatch: { kind: 'line', color: 'var(--series-3)', dash: '9 3' }, label: 'Direct message',
        desc: 'Long dashes: a private message.' },
      { id: 'e-upvote', swatch: { kind: 'line', color: 'var(--series-7)', dash: '6 2 1.5 2' }, label: 'Upvote',
        desc: 'Dash-dot: the agent upvoted the other node’s post.' },
      { id: 'e-livelive', swatch: { kind: 'line', color: 'var(--live-live)' }, label: 'Agent ↔ agent',
        desc: 'Gold: both ends are live agents, the agent-to-agent contact the experiment looks for.' },
      { id: 'e-pulse', swatch: { kind: 'pulse', color: 'var(--series-1)' }, label: 'Pulse',
        desc: 'A dot travelling along a line: that interaction is happening right now.' },
    ],
  },
];

export const GRAPH_KEY_FOOTNOTE = 'Worldline view (v): height is time, so each agent leaves a vertical line. The flat ring is the "now" plane. ' +
  'Hover or click any node or line for details.';
