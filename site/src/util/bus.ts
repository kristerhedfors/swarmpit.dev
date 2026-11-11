import type { Emitter as IEmitter } from '../types.js';

export class Emitter<E extends object> implements IEmitter<E> {
  private m = new Map<keyof E, Set<(e: never) => void>>();
  on<K extends keyof E>(k: K, f: (e: E[K]) => void): () => void {
    let s = this.m.get(k);
    if (!s) this.m.set(k, (s = new Set()));
    s.add(f as (e: never) => void);
    return () => { s!.delete(f as (e: never) => void); };
  }
  emit<K extends keyof E>(k: K, e: E[K]): void {
    const s = this.m.get(k);
    if (s) for (const f of [...s]) (f as (e: E[K]) => void)(e);
  }
}
