import type { LiveMessage } from '../lib/types';
import type { Route } from '../router';

export interface View {
  el: HTMLElement;
  mount(route: Route): void;
  /** Return true if the route change was handled in place (no re-mount). */
  update?(route: Route): boolean;
  destroy(): void;
  /** Global SSE messages (all runs). */
  onLive?(m: LiveMessage): void;
  /** Theme changed: repaint DOM/SVG colours in place (canvas components listen to onThemeChange
   *  themselves). Views without it are re-mounted. */
  onTheme?(): void;
}
