import { store } from './store';
import client from './api';
import { initTimeline } from './timeline';
import { initSource } from './source';
import { initVariables } from './variables';
import { initCallTree } from './calltree';
import { initQuery } from './query';

async function fetchSessions() {
  const { data } = await client.GET('/api/sessions');
  if (data && data.length > 0) {
    // Auto-open the first session
    store.setSessionId(data[0]!.id);
  }
}

function setupControls() {
  document.getElementById('btn-first')?.addEventListener('click', () => store.setSeq(0));
  document.getElementById('btn-prev')?.addEventListener('click', () => store.setSeq(store.currentSeq - 1));
  document.getElementById('btn-next')?.addEventListener('click', () => store.setSeq(store.currentSeq + 1));
  document.getElementById('btn-last')?.addEventListener('click', () => store.setSeq(store.totalEvents - 1));

  document.getElementById('btn-help')?.addEventListener('click', toggleHelp);
  document.getElementById('error-close')?.addEventListener('click', () => {
    document.getElementById('error-dialog')?.classList.add('hidden');
  });
}

function toggleHelp() {
  const overlay = document.getElementById('help-overlay');
  if (overlay) overlay.classList.toggle('hidden');
}

function setupKeyboardShortcuts() {
  window.addEventListener('keydown', async (e) => {
    if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) {
      return;
    }
    switch (e.key) {
      case 'ArrowLeft':
        store.setSeq(store.currentSeq - 1);
        break;
      case 'ArrowRight':
        store.setSeq(store.currentSeq + 1);
        break;
      case 'Home':
        store.setSeq(0);
        break;
      case 'End':
        store.setSeq(store.totalEvents - 1);
        break;
      case 'n': // step into forward
      case 'p': // step into backward
      case 'o': // step over forward
      case 'O': // step over backward
      case 'f': // run to exit
      case 'F': // back to call
        if (!store.sessionId) return;
        const dir = (e.key === 'p' || e.key === 'O' || e.key === 'F') ? 'backward' : 'forward';
        const mode = (e.key === 'n' || e.key === 'p') ? 'into' : (e.key === 'o' || e.key === 'O') ? 'over' : 'out';
        const { data } = await client.GET('/api/sessions/{session_id}/step', {
          params: { path: { session_id: store.sessionId }, query: { seq: store.currentSeq, dir, mode } }
        });
        if (data && data.seq !== null) {
          store.setSeq(data.seq);
        }
        break;
      case '/':
        e.preventDefault();
        // focus query box (to be implemented in query.ts)
        document.getElementById('query-input')?.focus();
        break;
      case '?':
        toggleHelp();
        break;
    }
  });
}

function updateUI() {
  const emptyState = document.getElementById('empty-state');
  if (!store.sessionId) {
    emptyState?.classList.remove('hidden');
    return;
  }
  emptyState?.classList.add('hidden');

  const stepDisplay = document.getElementById('step-display');
  if (stepDisplay) {
    stepDisplay.textContent = `Step ${store.currentSeq}`;
  }
}

export function showError(title: string, detail: string) {
  const dialog = document.getElementById('error-dialog');
  const t = document.getElementById('error-title');
  const d = document.getElementById('error-detail');
  if (dialog && t && d) {
    t.textContent = title;
    d.textContent = detail;
    dialog.classList.remove('hidden');
  }
}

async function init() {
  setupControls();
  setupKeyboardShortcuts();

  store.subscribe(updateUI);

  initTimeline();
  initSource();
  initVariables();
  initCallTree();
  initQuery();

  await fetchSessions();
  
  if (!store.sessionId) {
    // Show first run tour / empty state
    document.getElementById('empty-state')?.classList.remove('hidden');
  }
}

init();
