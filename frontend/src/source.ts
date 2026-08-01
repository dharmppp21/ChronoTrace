import { store } from './store';
import client from './api';
import type { components } from './api-schema';
type Source = components["schemas"]["Source"];
type State = components["schemas"]["State"];

let container: HTMLElement;
let currentFile: string | null = null;
let currentSource: Source | null = null;

export function initSource() {
  container = document.getElementById('source-content')!;
  
  store.subscribe(async () => {
    if (!store.sessionId) return;
    
    // Fetch state for current seq, cancelable
    const signal = store.signal;
    let state: State | null = null;
    try {
      const { data } = await client.GET('/api/sessions/{session_id}/state', {
        params: { path: { session_id: store.sessionId }, query: { seq: store.currentSeq } },
        signal
      });
      if (data) state = data;
    } catch (e: unknown) {
      if (e instanceof Error && e.name === 'AbortError') return;
      console.error(e);
      return;
    }

    if (!state || state.frames.length === 0) return;

    // The currently executing frame -- matched by current_frame_id, NOT frames[0] (which is the
    // outermost <module> frame). This is the file/line the playhead is actually sitting on.
    const frame = state.frames.find(f => f.frame_id === state.current_frame_id)
      ?? state.frames.at(-1)
      ?? state.frames[0];
    if (!frame) return;
    const file = frame.file;
    const lineno = frame.lineno;

    if (file !== currentFile) {
      currentFile = file;
      const { data: srcData } = await client.GET('/api/sessions/{session_id}/source', {
        params: { path: { session_id: store.sessionId }, query: { file } }
      });
      
      if (srcData) {
        currentSource = srcData;
        renderSource(lineno);
      } else {
        container.innerHTML = `<div style="color:var(--text-muted)">Source unavailable for ${file}</div>`;
      }
    } else {
      // Just update the highlight if file hasn't changed
      renderSource(lineno);
    }
  });
}

function renderSource(activeLine: number) {
  if (!currentSource) return;
  
  const { lines, heatmap, available } = currentSource;
  
  // Calculate log-scaled heatmap max
  const maxHits = Math.max(1, ...heatmap.map(h => h.count));
  const logMax = Math.log(maxHits + 1);

  const heatmapMap = new Map<number, number>();
  for (const h of heatmap) {
    heatmapMap.set(h.lineno, h.count);
  }

  let html = '';
  if (!available) {
    html += `<div style="background:var(--color-modified);color:#000;padding:4px 8px;margin-bottom:8px;border-radius:4px;font-size:0.8rem;font-weight:bold;">Source changed since recording. Heatmap hidden.</div>`;
  }

  // Build the lines
  for (let i = 0; i < lines.length; i++) {
    const lineNum = i + 1;
    const isTarget = lineNum === activeLine;
    const count = heatmapMap.get(lineNum) || 0;
    
    let heatWidth = 0;
    if (available && count > 0) {
      // log scale: ln(count+1) / ln(maxHits+1)
      heatWidth = (Math.log(count + 1) / logMax) * 100;
    }

    html += `<div class="source-line ${isTarget ? 'active' : ''}">
      <div class="source-gutter" data-line="${lineNum}" title="Click to add retroactive breakpoint">${lineNum}</div>
      <div class="source-heatmap" title="${count} executions">
        ${available && count > 0 ? `<div class="source-heatmap-bar" style="width: ${heatWidth}%"></div>` : ''}
      </div>
      <div class="source-text">${escapeHtml(lines[i] || '')}</div>
    </div>`;
  }

  container.innerHTML = html;

  // Add gutter click handlers
  const gutters = container.querySelectorAll('.source-gutter');
  gutters.forEach(g => {
    g.addEventListener('click', async (e) => {
      const line = parseInt((e.target as HTMLElement).getAttribute('data-line') || '0', 10);
      if (line > 0 && store.sessionId && currentFile) {
        // We'll dispatch a custom event that query.ts can listen to
        window.dispatchEvent(new CustomEvent('query:break', { 
          detail: { file: currentFile, lineno: line } 
        }));
      }
    });
  });

  // Scroll active line into view
  const activeEl = container.querySelector('.source-line.active');
  if (activeEl) {
    activeEl.scrollIntoView({ block: 'center', behavior: 'auto' });
  }
}

function escapeHtml(unsafe: string) {
  return unsafe
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
