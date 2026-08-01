import { store } from './store';
import client from './api';
import type { components } from './api-schema';
type TimelineBucket = components["schemas"]["TimelineBucket"];

// Density resolution requested from the backend, independent of pixel width: draw() scales these
// across whatever width the canvas ends up with. Tying the count to canvas.width failed when the
// canvas had 0 width at init (before layout), which asked the API for zero buckets.
const TIMELINE_BUCKETS = 256;

let canvas: HTMLCanvasElement;
let ctx: CanvasRenderingContext2D;
let buckets: TimelineBucket[] = [];
let dragging = false;
let rafId: number | null = null;

export function initTimeline() {
  canvas = document.getElementById('timeline') as HTMLCanvasElement;
  ctx = canvas.getContext('2d')!;

  // A ResizeObserver, not a one-time resize(): the canvas often has 0 width when init() runs
  // (layout not done yet), and the observer fires again once it is laid out, and on any resize.
  const resize = () => {
    const parent = canvas.parentElement;
    if (parent && parent.clientWidth > 0) {
      canvas.width = parent.clientWidth;
      canvas.height = parent.clientHeight;
      draw();
    }
  };
  if (canvas.parentElement) new ResizeObserver(resize).observe(canvas.parentElement);
  resize();

  // Mouse handlers
  canvas.addEventListener('mousedown', (e) => {
    dragging = true;
    handleScrub(e);
  });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    if (rafId) cancelAnimationFrame(rafId);
    rafId = requestAnimationFrame(() => handleScrub(e));
  });
  window.addEventListener('mouseup', () => {
    dragging = false;
  });

  store.subscribe(() => {
    if (store.sessionId && buckets.length === 0) {
      fetchTimeline();
    }
    draw();
  });
}

async function fetchTimeline() {
  if (!store.sessionId) return;
  const { data } = await client.GET('/api/sessions/{session_id}/timeline', {
    params: { path: { session_id: store.sessionId }, query: { buckets: TIMELINE_BUCKETS } }
  });
  if (data) {
    buckets = data.buckets;
    draw();
  }
}

function handleScrub(e: MouseEvent) {
  if (!store.sessionId || store.totalEvents === 0) return;
  const rect = canvas.getBoundingClientRect();
  const x = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
  const fraction = x / rect.width;
  const seq = Math.floor(fraction * store.totalEvents);
  store.setSeq(seq);
}

function draw() {
  if (!ctx || !canvas) return;
  const w = canvas.width;
  const h = canvas.height;
  
  ctx.clearRect(0, 0, w, h);
  
  // Draw density band
  if (buckets.length > 0 && store.totalEvents > 0) {
    const maxCount = Math.max(...buckets.map(b => b.event_count));
    ctx.fillStyle = 'rgba(59, 130, 246, 0.3)'; // blue accent
    
    const bucketWidth = w / buckets.length;
    for (let i = 0; i < buckets.length; i++) {
      const b = buckets[i];
      if (!b || b.event_count === 0) continue;
      const height = (b.event_count / maxCount) * h;
      ctx.fillRect(i * bucketWidth, h - height, bucketWidth + 1, height);
    }
  }

  // Draw truncation boundary if truncated
  if (store.isTruncated) {
    ctx.fillStyle = 'rgba(239, 68, 68, 0.2)'; // red area for lost tail
    // The truncated portion is at the very end. Actually, the totalEvents reflects the surviving events.
    // So the timeline is only the surviving events. We can draw a red border on the right.
    ctx.fillRect(w - 4, 0, 4, h);
  }

  // Draw playhead
  if (store.totalEvents > 0) {
    const fraction = store.currentSeq / Math.max(1, store.totalEvents - 1);
    const x = fraction * w;
    
    ctx.strokeStyle = '#f8fafc'; // white
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
    ctx.stroke();
  }
}
