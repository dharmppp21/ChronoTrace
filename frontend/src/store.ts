import { components } from "./api-schema";
import client from "./api";

export type SessionMeta = components["schemas"]["SessionMeta"];
export type State = components["schemas"]["State"];
export type Timeline = components["schemas"]["Timeline"];
export type Source = components["schemas"]["Source"];
export type CallTree = components["schemas"]["CallTree"];
export type Diff = components["schemas"]["Diff"];
export type Problem = components["schemas"]["HTTPValidationError"] | { code: string; detail: string };

type Subscriber = () => void;

class Store {
  public sessionId: string | null = null;
  public currentSeq: number = 0;
  
  // Basic metadata to know bounds
  public sessionMeta: SessionMeta | null = null;
  public totalEvents: number = 0;
  public isTruncated: boolean = false;

  private subscribers: Set<Subscriber> = new Set();
  private abortController = new AbortController();

  subscribe(callback: Subscriber) {
    this.subscribers.add(callback);
    return () => this.subscribers.delete(callback);
  }

  notify() {
    for (const sub of this.subscribers) {
      sub();
    }
  }

  async setSessionId(id: string) {
    this.sessionId = id;
    this.currentSeq = 0;

    // Fetch meta
    const { data } = await client.GET("/api/sessions/{session_id}", {
      params: { path: { session_id: id } }
    });
    if (data) {
      this.sessionMeta = data;
      this.totalEvents = data.event_count;
      this.isTruncated = data.truncated;
      this.currentSeq = Math.max(0, this.totalEvents - 1);
    }
    this.newGeneration();
    this.notify();
  }

  setSeq(seq: number) {
    const clamped = Math.max(0, Math.min(seq, this.totalEvents - 1));
    if (this.currentSeq === clamped) return;
    this.currentSeq = clamped;
    this.newGeneration();
    this.notify();
  }

  // One abort generation per seq CHANGE. Every panel fetching the current instant shares this
  // one signal, so they never cancel each other; it aborts only when the instant changes, which
  // supersedes the now-stale in-flight requests for the old seq (ADR-0010: cancel on the server).
  get signal(): AbortSignal {
    return this.abortController.signal;
  }

  private newGeneration() {
    this.abortController.abort();
    this.abortController = new AbortController();
  }
}

export const store = new Store();
