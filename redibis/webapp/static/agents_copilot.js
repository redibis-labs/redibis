/* Optional CopilotKit / AG-UI chat panel — streams from /api/copilotkit/agent */
(function () {
  'use strict';

  if (!window.REDIBIS_COPILOTKIT_ENABLED) return;

  const threadId = localStorage.getItem('redibis_copilot_thread') || crypto.randomUUID();
  localStorage.setItem('redibis_copilot_thread', threadId);

  const panel = document.createElement('aside');
  panel.id = 'agCopilot';
  panel.className = 'ag-copilot';
  panel.innerHTML = `
    <header class="ag-copilot-head">
      <span>Governance copilot</span>
      <button type="button" class="ag-btn" id="agCopilotToggle" aria-label="Close">×</button>
    </header>
    <div class="ag-copilot-msgs" id="agCopilotMsgs"></div>
    <form class="ag-copilot-form" id="agCopilotForm">
      <input type="text" id="agCopilotInput" placeholder="Ask about pipelines, PII, classification…" autocomplete="off"/>
      <button type="submit" class="ag-btn primary">Send</button>
    </form>`;
  document.body.appendChild(panel);

  const toggleBtn = document.createElement('button');
  toggleBtn.type = 'button';
  toggleBtn.className = 'ag-copilot-fab ag-btn primary';
  toggleBtn.textContent = 'Copilot';
  toggleBtn.id = 'agCopilotFab';
  document.body.appendChild(toggleBtn);

  const msgs = document.getElementById('agCopilotMsgs');
  const form = document.getElementById('agCopilotForm');
  const input = document.getElementById('agCopilotInput');
  let open = false;
  let history = [];

  function setOpen(on) {
    open = on;
    panel.classList.toggle('open', on);
  }

  toggleBtn.onclick = () => setOpen(!open);
  document.getElementById('agCopilotToggle').onclick = () => setOpen(false);

  function appendMsg(role, text) {
    const el = document.createElement('div');
    el.className = 'ag-copilot-msg ' + role;
    el.textContent = text;
    msgs.appendChild(el);
    msgs.scrollTop = msgs.scrollHeight;
  }

  function parseSseText(raw) {
    const parts = [];
    raw.split('\n').forEach((line) => {
      if (!line.startsWith('data:')) return;
      try {
        const ev = JSON.parse(line.slice(5).trim());
        if (ev.type === 'TEXT_MESSAGE_CONTENT' && ev.delta) parts.push(ev.delta);
        if (ev.type === 'RAW' && ev.event && ev.event.event === 'on_chain_stream') {
          const chunk = ev.event.data && ev.event.data.chunk;
          const messages = chunk && chunk.messages;
          if (messages && messages.length) {
            const c = messages[messages.length - 1].content;
            if (c) parts.push(String(c));
          }
        }
      } catch (_) { /* ignore */ }
    });
    return parts.join('').trim();
  }

  function extractPipelineFence(text) {
    const m = text.match(/```redibis-pipeline\s*([\s\S]*?)```/);
    if (!m) return null;
    try {
      return JSON.parse(m[1].trim());
    } catch (_) {
      return null;
    }
  }

  function displayReply(pending, reply) {
    const pipeline = extractPipelineFence(reply);
    const visible = pipeline
      ? reply.replace(/```redibis-pipeline[\s\S]*?```/, '').trim()
      : reply;
    pending.textContent = visible || reply;
    if (pipeline && window.REDIBIS_AGENTS && window.REDIBIS_AGENTS.loadPipeline) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'ag-btn primary ag-copilot-load';
      btn.textContent = 'Load on board';
      btn.onclick = () => {
        window.REDIBIS_AGENTS.loadPipeline(pipeline);
        setOpen(false);
      };
      pending.appendChild(document.createElement('br'));
      pending.appendChild(btn);
    }
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const text = (input.value || '').trim();
    if (!text) return;
    input.value = '';
    appendMsg('user', text);
    history.push({ id: crypto.randomUUID(), role: 'user', content: text });
    appendMsg('assistant', '…');
    const pending = msgs.lastChild;

    try {
      const res = await fetch('/api/copilotkit/agent', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
        },
        body: JSON.stringify({
          threadId,
          runId: crypto.randomUUID(),
          forwardedProps: {},
          messages: history,
          tools: [],
          state: {},
          context: [],
        }),
      });
      const raw = await res.text();
      if (!res.ok) throw new Error(raw || res.statusText);
      const reply = parseSseText(raw) || '(no response)';
      displayReply(pending, reply);
      history.push({ id: crypto.randomUUID(), role: 'assistant', content: reply });
    } catch (err) {
      pending.textContent = 'Error: ' + (err.message || err);
    }
  });
})();
