import { describe, it, expect, vi } from 'vitest';
import { store } from './store';

describe('Store', () => {
  it('initializes correctly', () => {
    expect(store.currentSeq).toBe(0);
    expect(store.sessionId).toBe(null);
  });

  it('subscribes and notifies', () => {
    const fn = vi.fn();
    const unsub = store.subscribe(fn);
    
    store.notify();
    expect(fn).toHaveBeenCalledTimes(1);

    unsub();
    store.notify();
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('clamps seq to totalEvents', () => {
    store.totalEvents = 10;
    store.setSeq(15);
    expect(store.currentSeq).toBe(9);

    store.setSeq(-5);
    expect(store.currentSeq).toBe(0);
  });

  it('supersedes in-flight requests only when the seq changes', () => {
    store.totalEvents = 100;
    store.setSeq(10);
    const first = store.signal;
    expect(first.aborted).toBe(false);

    // same seq -> same generation: panels for the current instant must not cancel each other
    store.setSeq(10);
    expect(store.signal).toBe(first);
    expect(first.aborted).toBe(false);

    // a new seq -> the old generation aborts (stale requests), a fresh signal takes over
    store.setSeq(20);
    expect(first.aborted).toBe(true);
    expect(store.signal.aborted).toBe(false);
  });
});
