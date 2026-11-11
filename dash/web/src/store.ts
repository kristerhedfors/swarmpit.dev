// Minimal pub/sub store.
export class Store<T extends object> {
  private subs = new Set<(s: T, prev: T) => void>();
  constructor(private state: T) {}
  get(): T { return this.state; }
  set(patch: Partial<T>) {
    const prev = this.state;
    this.state = { ...prev, ...patch };
    this.subs.forEach((f) => f(this.state, prev));
  }
  sub(f: (s: T, prev: T) => void): () => void { this.subs.add(f); return () => this.subs.delete(f); }
}

export interface AppState {
  theme: 'auto' | 'light' | 'dark';
  liveConnected: boolean;
  liveRuns: string[];
}
export const app = new Store<AppState>({
  theme: (localStorage.getItem('obs.theme') as AppState['theme']) || 'auto',
  liveConnected: false,
  liveRuns: [],
});
