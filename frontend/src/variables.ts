import { store } from './store';
import client from './api';
import type { components } from './api-schema';
type State = components["schemas"]["State"];
type VarChange = components["schemas"]["VarChange"];
type Variable = components["schemas"]["Variable"];
type ValueChild = components["schemas"]["ValueChild"];

let container: HTMLElement;
let currentDiff: Map<string, VarChange> = new Map();

export function initVariables() {
  container = document.getElementById('variables-content')!;
  
  store.subscribe(async () => {
    if (!store.sessionId) return;
    
    const signal = store.signal;

    try {
      // Fetch state and diff in parallel
      const [stateRes, diffRes] = await Promise.all([
        client.GET('/api/sessions/{session_id}/state', {
          params: { path: { session_id: store.sessionId }, query: { seq: store.currentSeq } },
          signal
        }),
        client.GET('/api/sessions/{session_id}/diff', {
          params: { path: { session_id: store.sessionId }, query: { seq: store.currentSeq } },
          signal
        })
      ]);

      if (stateRes.data && diffRes.data) {
        currentDiff.clear();
        for (const change of diffRes.data.changes) {
          // Key by frame_id + name to match accurately
          currentDiff.set(`${change.frame_id}:${change.name}`, change);
        }
        renderVariables(stateRes.data);
      }
    } catch (e: unknown) {
      if (e instanceof Error && e.name === 'AbortError') return;
      console.error(e);
    }
  });
}

function renderVariables(state: State) {
  if (state.frames.length === 0) {
    container.innerHTML = '<div style="color:var(--text-muted)">No active frames</div>';
    return;
  }

  // The executing frame -- its locals are the scope you are paused in and the ones that change
  // as you scrub. (frames[0] is the OUTERMOST frame, i.e. <module>, whose globals barely move.)
  const frame = state.frames.find(f => f.frame_id === state.current_frame_id)
    ?? state.frames.at(-1)
    ?? state.frames[0];
  if (!frame) {
    container.innerHTML = '<div style="color:var(--text-muted)">No active frames</div>';
    return;
  }
  const locals = frame.variables;
  
  const root = document.createElement('div');
  root.className = 'var-tree';

  if (locals.length === 0) {
    root.innerHTML = '<div style="color:var(--text-muted);font-style:italic">No local variables</div>';
  } else {
    for (const v of locals) {
      root.appendChild(createVarNode(v, frame.frame_id));
    }
  }

  container.innerHTML = '';
  container.appendChild(root);
}

function createVarNode(v: Variable | ValueChild, frameId: number, indent: number = 0): HTMLElement {
  const row = document.createElement('div');
  row.className = 'var-row';
  row.style.paddingLeft = `${indent * 16}px`;
  
  const keyName = 'name' in v ? v.name : v.key;
  // E2E test requires data-testid="var-row" and data-name="<name>"
  row.setAttribute('data-testid', 'var-row');
  row.setAttribute('data-name', keyName);

  const diffKey = `${frameId}:${keyName}`;
  const change = currentDiff.get(diffKey);

  let badgeHtml = '';
  if (change) {
    badgeHtml = `<span class="badge ${change.kind}" title="${change.old || 'none'} &rarr; ${change.new || 'none'}">${change.kind}</span>`;
  }

  // Lossy capture badges parsed from preview
  const preview = v.preview;
  let customBadges = '';
  const lossyTags = ['<redacted>', '<budget>', '<depth>', '<cycle>'];
  for (const tag of lossyTags) {
    if (preview.includes(tag)) {
      customBadges += `<span class="badge" style="background:#475569;color:#f8fafc">${tag}</span>`;
    }
  }
  if ('truncated' in v && v.truncated) {
    customBadges += `<span class="badge" style="background:#475569;color:#f8fafc">truncated</span>`;
  }
  if ('obj_id' in v && v.obj_id !== null && v.obj_id !== undefined) {
    customBadges += `<span class="badge" style="background:#3b82f6;color:#f8fafc" title="Object Identity">#${v.obj_id}</span>`;
  }

  const caret = v.has_children ? '&#9656; ' : '&nbsp;&nbsp;';

  row.innerHTML = `
    <span style="color:var(--text-muted);width:16px;display:inline-block;cursor:pointer;" class="expander">${caret}</span>
    <span class="var-name">${escapeHtml(keyName)}</span>
    <span class="var-preview">${escapeHtml(preview)}</span>
    ${badgeHtml}
    ${customBadges}
  `;

  // Context menu (right-click) for query
  row.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    if (store.sessionId) {
      window.dispatchEvent(new CustomEvent('query:var-writes', { detail: { name: keyName } }));
    }
  });

  const wrapper = document.createElement('div');
  wrapper.appendChild(row);
  
  const childrenContainer = document.createElement('div');
  childrenContainer.style.display = 'none';
  wrapper.appendChild(childrenContainer);

  let expanded = false;
  let loaded = false;

  const expander = row.querySelector('.expander') as HTMLElement;
  row.addEventListener('click', async () => {
    // Only expand if it has children
    if (!v.has_children) return;

    expanded = !expanded;
    expander.innerHTML = expanded ? '&#9662; ' : '&#9656; ';
    childrenContainer.style.display = expanded ? 'block' : 'none';

    if (expanded && !loaded && v.ref !== null && store.sessionId) {
      loaded = true;
      childrenContainer.innerHTML = `<div style="padding-left:${(indent+1)*16}px;color:var(--text-muted);">Loading...</div>`;
      
      const { data } = await client.GET('/api/sessions/{session_id}/value', {
        params: { path: { session_id: store.sessionId }, query: { ref: v.ref } }
      });

      childrenContainer.innerHTML = '';
      if (data && data.children) {
        for (const child of data.children) {
          childrenContainer.appendChild(createVarNode(child, frameId, indent + 1));
        }
      } else {
        childrenContainer.innerHTML = `<div style="padding-left:${(indent+1)*16}px;color:var(--text-muted);">No children</div>`;
      }
    }
  });

  return wrapper;
}

function escapeHtml(unsafe: string) {
  return unsafe
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
