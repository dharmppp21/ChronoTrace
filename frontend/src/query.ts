import { store } from './store';
import client from './api';
import type { components } from './api-schema';
type QueryDescriptor = components["schemas"]["QueryDescriptor"];
type QueryHit = components["schemas"]["QueryHit"];

let container: HTMLElement;
let descriptors: QueryDescriptor[] = [];
let currentCursor: number | null = null;
let currentQueryName: string | null = null;
let currentQueryArgs: Record<string, any> = {};

export function initQuery() {
  container = document.getElementById('query-content')!;
  
  // Create UI skeleton
  container.innerHTML = `
    <div style="margin-bottom: 12px;">
      <select id="query-select" style="width:100%; padding:4px; background:var(--bg-color); color:var(--text-main); border:1px solid var(--border-color);"></select>
    </div>
    <div id="query-form" style="display:flex; flex-direction:column; gap:8px;"></div>
    <div style="margin-top: 12px;">
      <button id="btn-run-query" style="width:100%; padding:6px; background:var(--accent); color:#fff; border:none; border-radius:4px; cursor:pointer;">Run Query</button>
    </div>
    <div id="query-error" style="color:var(--color-removed); margin-top:8px; font-size:0.8rem; font-weight:bold;"></div>
    <div id="query-results" style="margin-top: 16px; display:flex; flex-direction:column; gap:4px;"></div>
    <button id="btn-load-more" class="hidden" style="margin-top:8px; padding:4px; width:100%; background:var(--bg-color); color:var(--text-main); border:1px solid var(--border-color);">Load More</button>
  `;

  fetchQueries();

  const select = document.getElementById('query-select') as HTMLSelectElement;
  select.addEventListener('change', () => {
    renderForm(select.value);
  });

  document.getElementById('btn-run-query')?.addEventListener('click', () => {
    runQuery(false);
  });

  document.getElementById('btn-load-more')?.addEventListener('click', () => {
    runQuery(true);
  });

  // Listen for custom events triggered from other panels
  window.addEventListener('query:break', (e: any) => {
    const { file, lineno } = e.detail;
    select.value = 'break';
    renderForm('break');
    const fileInput = document.getElementById('query-arg-file') as HTMLInputElement;
    const lineInput = document.getElementById('query-arg-lineno') as HTMLInputElement;
    if (fileInput) fileInput.value = file;
    if (lineInput) lineInput.value = lineno.toString();
    runQuery(false);
  });

  window.addEventListener('query:var-writes', (e: any) => {
    const { name } = e.detail;
    select.value = 'var-writes';
    renderForm('var-writes');
    const nameInput = document.getElementById('query-arg-name') as HTMLInputElement;
    if (nameInput) nameInput.value = name;
    runQuery(false);
  });
}

async function fetchQueries() {
  const { data } = await client.GET('/api/queries');
  if (data) {
    descriptors = data;
    const select = document.getElementById('query-select') as HTMLSelectElement;
    select.innerHTML = descriptors.map(d => `<option value="${d.name}">${d.name} - ${d.summary}</option>`).join('');
    if (descriptors.length > 0) {
      renderForm(descriptors[0]!.name);
    }
  }
}

function renderForm(queryName: string) {
  const desc = descriptors.find(d => d.name === queryName);
  if (!desc) return;

  const form = document.getElementById('query-form')!;
  form.innerHTML = '';
  document.getElementById('query-results')!.innerHTML = '';
  document.getElementById('query-error')!.textContent = '';
  document.getElementById('btn-load-more')?.classList.add('hidden');

  for (const arg of desc.args) {
    const wrapper = document.createElement('div');
    wrapper.innerHTML = `
      <label style="font-size:0.75rem; color:var(--text-muted); display:block; margin-bottom:2px;">
        ${arg.name} ${arg.required ? '*' : ''}
      </label>
      <input type="text" id="query-arg-${arg.name}" style="width:100%; padding:4px; background:var(--bg-color); color:var(--text-main); border:1px solid var(--border-color); border-radius:2px;" />
    `;
    form.appendChild(wrapper);
  }
}

async function runQuery(loadMore: boolean) {
  if (!store.sessionId) return;
  const select = document.getElementById('query-select') as HTMLSelectElement;
  const queryName = select.value;
  const desc = descriptors.find(d => d.name === queryName);
  if (!desc) return;
  const args: Record<string, any> = {};
  for (const arg of desc.args) {
    const input = document.getElementById(`query-arg-${arg.name}`) as HTMLInputElement;
    if (input && input.value) {
      // Basic type parsing
      if (arg.type === 'integer') {
        args[arg.name] = parseInt(input.value, 10);
      } else if (arg.type === 'boolean') {
        args[arg.name] = input.value.toLowerCase() === 'true';
      } else {
        args[arg.name] = input.value;
      }
    }
  }

  if (!loadMore) {
    currentCursor = null;
    currentQueryName = queryName;
    currentQueryArgs = args;
    document.getElementById('query-results')!.innerHTML = '<div style="color:var(--text-muted);">Running...</div>';
  }

  const errEl = document.getElementById('query-error')!;
  errEl.textContent = '';

  try {
    const { data, error } = await client.POST('/api/sessions/{session_id}/query', {
      params: { path: { session_id: store.sessionId } },
      body: {
        name: currentQueryName!,
        args: currentQueryArgs,
        cursor: currentCursor,
        limit: 100
      }
    });

    if (error) {
      if ('detail' in error) {
        if (typeof error.detail === 'string') {
          errEl.textContent = error.detail;
        } else if (Array.isArray(error.detail)) {
          errEl.textContent = error.detail.map((e: unknown) => (e as any).msg).join(', ');
        } else {
          errEl.textContent = JSON.stringify(error.detail);
        }
      } else {
         errEl.textContent = 'An error occurred';
      }
      if (!loadMore) document.getElementById('query-results')!.innerHTML = '';
      return;
    }

    if (data) {
      renderResults(data.hits, loadMore, data.partial);
      currentCursor = data.next_cursor ?? null;
      
      const btnMore = document.getElementById('btn-load-more')!;
      if (currentCursor !== null) {
        btnMore.classList.remove('hidden');
      } else {
        btnMore.classList.add('hidden');
      }
    }
  } catch (e: unknown) {
    errEl.textContent = (e instanceof Error ? e.message : 'Unknown error');
    if (!loadMore) document.getElementById('query-results')!.innerHTML = '';
  }
}

function renderResults(hits: QueryHit[], append: boolean, partial: boolean) {
  const container = document.getElementById('query-results')!;
  if (!append) container.innerHTML = '';

  if (hits.length === 0 && !append) {
    container.innerHTML = '<div style="color:var(--text-muted);">No results found.</div>';
    if (partial) {
      container.innerHTML += '<div style="color:var(--color-modified);font-size:0.8rem;margin-top:4px;">Note: Recording was crash-truncated.</div>';
    }
    return;
  }

  for (const hit of hits) {
    const row = document.createElement('div');
    row.style.padding = '4px';
    row.style.border = '1px solid var(--border-color)';
    row.style.borderRadius = '4px';
    row.style.cursor = 'pointer';
    row.style.background = 'var(--bg-color)';
    row.style.fontSize = '0.8rem';
    
    row.innerHTML = `
      <div style="color:var(--accent);font-weight:600;">Seq: ${hit.seq}</div>
      ${hit.file ? `<div style="color:var(--text-muted);">${hit.file.split('/').pop()}:${hit.lineno} in ${hit.function}</div>` : ''}
      ${hit.value_preview ? `<div style="color:#a7f3d0;font-family:var(--code-font);">${escapeHtml(hit.value_preview)}</div>` : ''}
      ${hit.note ? `<div style="color:var(--color-modified);">${escapeHtml(hit.note)}</div>` : ''}
    `;

    row.addEventListener('click', () => {
      store.setSeq(hit.seq);
    });

    row.addEventListener('mouseenter', async () => {
      if (!store.sessionId) return;
      // Pre-fetch state without committing to it
      // In a real app we'd display a tooltip or a preview panel
      // For now we just log or do a small peek
      try {
        await client.GET('/api/sessions/{session_id}/state', {
          params: { path: { session_id: store.sessionId }, query: { seq: hit.seq } }
        });
      } catch (e) {}
    });

    container.appendChild(row);
  }
}

function escapeHtml(unsafe: string) {
  return unsafe.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}
