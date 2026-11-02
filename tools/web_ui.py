#!/usr/bin/env python3
"""Serve the local composition as a web chat window on this machine.

The page and its API listen on 127.0.0.1 only, and every call is relayed
unchanged to the composition's UI Unix socket inside the VM through
`lima_ui_bridge.py`. The socket's permissions and the UI service's capability
checks stay authoritative: this server adds a transport, not authority. It
holds a few bridge connections open so that a long `conversation.wait` does
not block the other calls, and it never retries a request: a failed send may
have completed.

  python3 tools/web_ui.py                       # http://127.0.0.1:8765/
  python3 tools/web_ui.py --port 9000
  python3 tools/web_ui.py --socket /home/<user>/open-math-run/chat/ui/ui.sock

Without `--socket` it asks `local_chat_start.py --status` for the socket. The
page reads a project's sessions and history, says something, shows the turn's
tool calls and the pending approvals, and decides them.
"""

import argparse
import json
import queue
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
MAX_CONNECTIONS = 4


class Connection:
    """One bridge process: a JSON line out, a JSON line back, in order."""

    def __init__(self, vm, socket_path):
        self.process = subprocess.Popen(
            [sys.executable, str(HERE / 'lima_ui_bridge.py'), '--vm', vm, socket_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            for line in self.process.stdout:
                self.lines.put(line)
        finally:
            self.lines.put(None)

    def send(self, request):
        self.process.stdin.write(json.dumps(request, ensure_ascii=False) + '\n')
        self.process.stdin.flush()

    def read(self, timeout):
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise TimeoutError('the composition did not answer in time')
        if line is None:
            raise ConnectionError('the bridge closed with the request outstanding')
        return json.loads(line)

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()


class Bridge:
    """A small pool of bridge connections; one request in flight on each."""

    def __init__(self, vm, socket_path):
        self.vm = vm
        self.socket_path = socket_path
        self.condition = threading.Condition()
        self.idle = []
        self.open = 0
        self.counter = 0

    def warm(self, count=1):
        """Open connections ahead of the first call: each one costs an SSH round trip."""
        def open_some():
            for _ in range(count):
                try:
                    connection = self._acquire()
                except Exception:
                    return
                self._release(connection)
        threading.Thread(target=open_some, daemon=True).start()

    def call(self, payload, timeout=60):
        with self.condition:
            self.counter += 1
            request_id = self.counter
        request = dict(payload, id=request_id)
        connection = self._acquire()
        try:
            connection.send(request)
            response = connection.read(timeout)
            if response.get('id') != request_id:
                raise ConnectionError('the answer names another request')
        except Exception:
            self._drop(connection)
            raise
        self._release(connection)
        return response

    def _acquire(self):
        with self.condition:
            while True:
                if self.idle:
                    return self.idle.pop()
                if self.open < MAX_CONNECTIONS:
                    self.open += 1
                    try:
                        return Connection(self.vm, self.socket_path)
                    except Exception:
                        self.open -= 1
                        raise
                self.condition.wait(timeout=60)
                if not self.idle and self.open >= MAX_CONNECTIONS:
                    raise TimeoutError('every bridge connection is busy')

    def _release(self, connection):
        with self.condition:
            self.idle.append(connection)
            self.condition.notify()

    def _drop(self, connection):
        connection.close()
        with self.condition:
            self.open -= 1
            self.condition.notify()


def socket_from_status(vm):
    result = subprocess.run([sys.executable, str(HERE / 'local_chat_start.py'), '--vm', vm, '--status'],
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    try:
        status = json.loads(result.stdout)
    except ValueError:
        raise SystemExit(result.stderr.strip() or 'local_chat_start.py --status printed no state')
    if not status.get('socket_ready'):
        raise SystemExit(status.get('socket') or 'the composition is not running')
    return status['socket']


def ensure_session(bridge, project, session):
    """Start the session when the UI does not list it live; leave a live one alone."""
    listed = bridge.call({'op': 'sessions.list'})
    if 'ok' not in listed:
        return listed
    for live in listed['ok'].get('sessions', []):
        if live.get('name') == session and live.get('project') == project:
            return None
    return bridge.call({'op': 'sessions.create', 'project': project, 'session': session}, timeout=120)


class Handler(BaseHTTPRequestHandler):
    server_version = 'open-math-web'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    def reply(self, status, body, content_type='application/json; charset=utf-8'):
        data = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def failure(self, status, code, message):
        self.reply(status, json.dumps({'error': {'code': code, 'message': message}}))

    def do_GET(self):
        parts = urlsplit(self.path)
        query = {key: values[-1] for key, values in parse_qs(parts.query).items()}
        if parts.path == '/':
            return self.reply(200, PAGE, 'text/html; charset=utf-8')
        bridge = self.server.bridge
        try:
            if parts.path == '/api/projects':
                return self.reply(200, json.dumps(bridge.call({'op': 'projects.list'})))
            if parts.path == '/api/sessions':
                return self.reply(200, json.dumps(bridge.call(
                    {'op': 'conversation.list', 'project': query.get('project', '')})))
            if parts.path == '/api/approvals':
                return self.reply(200, json.dumps(bridge.call({'op': 'approvals.list'})))
            if parts.path in ('/api/history', '/api/wait'):
                op = 'conversation.history' if parts.path == '/api/history' else 'conversation.wait'
                payload = {'op': op, 'project': query.get('project', ''), 'session': query.get('session', '')}
                if query.get('after_seq'):
                    payload['after_seq'] = int(query['after_seq'])
                if op == 'conversation.history':
                    return self.reply(200, json.dumps(bridge.call(payload, timeout=60)))
                # A page that navigates away leaves its wait behind. Cap it and answer
                # "nothing new" instead, so the connection returns to the pool; the page
                # asks again, and the closed socket cancels the abandoned wait.
                try:
                    return self.reply(200, json.dumps(bridge.call(payload, timeout=25)))
                except TimeoutError:
                    return self.reply(200, json.dumps({'ok': {'changed': False}}))
        except (TimeoutError, ConnectionError, OSError) as exc:
            return self.failure(502, 'bridge_failed', str(exc))
        return self.failure(404, 'not_found', parts.path)

    def do_POST(self):
        parts = urlsplit(self.path)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            body = json.loads(self.rfile.read(length) or b'{}')
        except (ValueError, OSError):
            return self.failure(400, 'invalid_input', 'the body must be one JSON object')
        bridge = self.server.bridge
        try:
            if parts.path == '/api/say':
                project, session = body.get('project'), body.get('session')
                text = (body.get('text') or '').strip()
                if not project or not session or not text:
                    return self.failure(400, 'invalid_input', 'project, session and text are required')
                started = ensure_session(bridge, project, session)
                if started is not None and 'error' in started:
                    return self.reply(200, json.dumps(started))
                answer = bridge.call({'op': 'conversation.say', 'project': project, 'session': session,
                                      'message_id': uuid.uuid4().hex, 'text': text})
                return self.reply(200, json.dumps(answer))
            if parts.path == '/api/decide':
                answer = bridge.call({'op': 'approvals.decide', 'approval': int(body['approval']),
                                      'digest': body['digest'], 'allow': bool(body.get('allow'))})
                return self.reply(200, json.dumps(answer))
            if parts.path == '/api/session':
                project, session = body.get('project'), body.get('session')
                if not project or not session:
                    return self.failure(400, 'invalid_input', 'project and session are required')
                started = ensure_session(bridge, project, session)
                return self.reply(200, json.dumps(started or {'ok': {}}))
        except (TimeoutError, ConnectionError, OSError) as exc:
            return self.failure(502, 'bridge_failed', str(exc))
        except (KeyError, TypeError, ValueError) as exc:
            return self.failure(400, 'invalid_input', str(exc))
        return self.failure(404, 'not_found', parts.path)


PAGE = r'''<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Open Math</title>
<style>
  :root {
    --bg: #f6f6f3; --panel: #ffffff; --text: #1b1b19; --muted: #6d6d66;
    --line: #e2e2dc; --accent: #2f6b4f; --accent-soft: #eaf1ec; --warn: #f4e6c8;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.55 ui-sans-serif, -apple-system, "Helvetica Neue", Arial, sans-serif;
    display: flex; flex-direction: column;
  }
  header {
    display: flex; gap: 10px; align-items: center; padding: 10px 18px;
    border-bottom: 1px solid var(--line); background: var(--panel); flex-wrap: wrap;
  }
  header .brand { font-weight: 600; letter-spacing: .01em; margin-right: 6px; }
  header .dot { width: 7px; height: 7px; border-radius: 50%; background: #b9b9b2; }
  header .dot.on { background: var(--accent); }
  header .status { color: var(--muted); font-size: 12px; }
  select, button, input, textarea {
    font: inherit; color: inherit; background: var(--panel);
    border: 1px solid var(--line); border-radius: 8px; padding: 6px 10px;
  }
  select { max-width: 220px; }
  button { cursor: pointer; }
  button:hover { border-color: #c9c9c1; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.primary:disabled { opacity: .45; cursor: default; }
  main { flex: 1; overflow-y: auto; }
  .thread { max-width: 780px; margin: 0 auto; padding: 26px 20px 30px; }
  .msg { margin: 0 0 18px; }
  .msg.user { display: flex; justify-content: flex-end; }
  .msg.user .bubble {
    background: var(--accent-soft); border: 1px solid #dbe7de; border-radius: 14px 14px 4px 14px;
    padding: 10px 14px; max-width: 82%; white-space: pre-wrap; overflow-wrap: anywhere;
  }
  .msg.agent .who { font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); margin-bottom: 4px; }
  .msg.agent .bubble { overflow-wrap: anywhere; }
  .msg.agent .bubble ul, .msg.agent .bubble ol { margin: 6px 0; padding-left: 24px; }
  .msg.agent .bubble li { margin: 2px 0; }
  .tool {
    margin: 0 0 6px; padding: 3px 0 3px 10px; border-left: 2px solid var(--line);
    font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--muted);
    overflow-wrap: anywhere;
  }
  .tool.bad { border-left-color: #c98b7a; color: #8d4a3a; }
  .thinking { color: var(--muted); font-size: 13px; margin: 0 0 18px; }
  .thinking .pulse { display: inline-block; animation: fade 1.2s ease-in-out infinite; }
  @keyframes fade { 0%, 100% { opacity: .35 } 50% { opacity: 1 } }
  .approvals { max-width: 780px; margin: 16px auto 0; padding: 0 20px; }
  .approval {
    background: var(--warn); border: 1px solid #e6d3a8; border-radius: 10px;
    padding: 10px 14px; margin-bottom: 10px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap;
  }
  .approval .what { flex: 1; min-width: 240px; font-size: 13px; }
  .approval code { font: 12px ui-monospace, Menlo, monospace; }
  footer { border-top: 1px solid var(--line); background: var(--panel); padding: 12px 20px; }
  .composer { max-width: 780px; margin: 0 auto; display: flex; gap: 10px; align-items: flex-end; }
  .composer textarea {
    flex: 1; resize: none; min-height: 44px; max-height: 200px; padding: 10px 12px; line-height: 1.45;
  }
  .hint { max-width: 780px; margin: 6px auto 0; color: var(--muted); font-size: 11px; }
  pre { background: #f2f2ee; border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; overflow-x: auto; }
  pre code { font: 12.5px/1.5 ui-monospace, Menlo, monospace; }
  .code-inline { font: 13px ui-monospace, Menlo, monospace; background: #f0f0eb; padding: 1px 4px; border-radius: 4px; }
  a { color: var(--accent); }
</style>
<header>
  <span class="brand">Open Math</span>
  <span class="dot" id="dot"></span><span class="status" id="status">connecting</span>
  <span style="flex:1"></span>
  project <select id="project"></select>
  session <select id="session"></select>
  <button id="newSession" title="Start a session with a new name">+</button>
</header>
<main id="scroll">
  <div class="approvals" id="approvals"></div>
  <div class="thread" id="thread"></div>
</main>
<footer>
  <div class="composer">
    <textarea id="text" placeholder="Message the agent…  (Enter to send, Shift+Enter for a new line)"></textarea>
    <button class="primary" id="send">Send</button>
  </div>
  <div class="hint" id="hint"></div>
</footer>
<script>
const $ = (s) => document.querySelector(s);
const state = { project: '', session: '', records: [], lastSeq: -1, loading: false };
const KEEP = new Set(['user', 'assistant', 'tool_call', 'tool_result', 'turn_complete']);
const toolNames = {};

function esc(text) {
  return String(text == null ? '' : text).replace(/[&<>"']/g, (c) => (
    {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

function inline(text) {
  return esc(text)
    .replace(/`([^`\n]+)`/g, '<span class="code-inline">$1</span>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
}

function prose(text) {
  const lines = String(text).split('\n');
  let out = '';
  let list = null;
  const close = () => { if (list) { out += '</' + list + '>'; list = null; } };
  for (const line of lines) {
    let match;
    if ((match = line.match(/^\s*[-*]\s+(.*)$/))) {
      if (list !== 'ul') { close(); out += '<ul>'; list = 'ul'; }
      out += '<li>' + inline(match[1]) + '</li>';
      continue;
    }
    if ((match = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
      if (list !== 'ol') { close(); out += '<ol>'; list = 'ol'; }
      out += '<li>' + inline(match[1]) + '</li>';
      continue;
    }
    close();
    out += inline(line) + '<br>';
  }
  close();
  return out;
}

function md(text) {
  const parts = String(text || '').split(/```/);
  return parts.map((part, i) => {
    if (i % 2 === 1) {
      const body = part.replace(/^[a-zA-Z0-9_+-]*\n/, '');
      return '<pre><code>' + esc(body.replace(/\n$/, '')) + '</code></pre>';
    }
    return prose(part);
  }).join('');
}

async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({ error: { message: 'bad answer' } }));
  if (body.error) throw new Error(body.error.message || body.error.code);
  return body.ok !== undefined ? body.ok : body;
}

async function loadProjects() {
  const ok = await api('/api/projects');
  const items = ok.projects || [];
  const select = $('#project');
  select.innerHTML = '';
  for (const item of items) {
    const option = document.createElement('option');
    option.value = option.textContent = item.name;
    select.append(option);
  }
  const stored = localStorage.getItem('openmath.project');
  const chosen = items.find((i) => i.name === stored) ? stored : (items.find((i) => i.name === 'demo') || items[0] || {}).name;
  if (chosen) { select.value = chosen; await switchProject(chosen); }
}

async function loadSessions() {
  const ok = await api('/api/sessions?project=' + encodeURIComponent(state.project));
  const items = ok.sessions || [];
  const select = $('#session');
  select.innerHTML = '';
  for (const item of items) {
    const option = document.createElement('option');
    option.value = option.textContent = item.name;
    select.append(option);
  }
  const stored = localStorage.getItem('openmath.session');
  const chosen = items.find((i) => i.name === stored) ? stored : (items.find((i) => i.name === 'default') || items[0] || {}).name;
  if (chosen) { select.value = chosen; await switchSession(chosen); }
  else { state.session = ''; state.records = []; state.lastSeq = -1; render(); }
}

async function switchProject(name) {
  state.project = name;
  localStorage.setItem('openmath.project', name);
  await loadSessions();
}

async function switchSession(name) {
  state.session = name;
  localStorage.setItem('openmath.session', name);
  state.records = [];
  state.lastSeq = -1;
  await loadHistory(true);
  render();
}

async function loadHistory(reset) {
  if (!state.project || !state.session) return;
  const query = '/api/history?project=' + encodeURIComponent(state.project) +
    '&session=' + encodeURIComponent(state.session) +
    (reset || state.lastSeq < 0 ? '' : '&after_seq=' + state.lastSeq);
  const ok = await api(query);
  for (const record of ok.records || []) {
    if (record.seq > state.lastSeq) {
      state.lastSeq = record.seq;
      state.records.push(record);
    }
  }
  if (state.records.length > 800) state.records = state.records.slice(-800);
}

function describe(record) {
  const args = record.arguments || {};
  const bits = Object.entries(args).map(([key, value]) => {
    let text = typeof value === 'string' ? value : JSON.stringify(value);
    if (text == null) text = String(value);
    if (text.length > 90) text = text.slice(0, 90) + '…';
    return key + '=' + text;
  });
  return (record.name || 'tool') + (bits.length ? '(' + bits.join(', ') + ')' : '');
}

function render() {
  const thread = $('#thread');
  const atBottom = $('#scroll').scrollHeight - $('#scroll').scrollTop - $('#scroll').clientHeight < 120;
  thread.innerHTML = '';
  let lastUser = -1;
  state.records.forEach((record, index) => { if (record.kind === 'user') lastUser = index; });
  let finished = false;
  for (let i = lastUser + 1; i < state.records.length; i++) {
    if (state.records[i].kind === 'turn_complete') finished = true;
  }
  for (const record of state.records) {
    if (!KEEP.has(record.kind)) continue;
    if (record.kind === 'tool_call') {
      const line = document.createElement('div');
      line.className = 'tool';
      line.textContent = describe(record);
      thread.append(line);
    } else if (record.kind === 'tool_result' && record.error_code) {
      const line = document.createElement('div');
      line.className = 'tool bad';
      line.textContent = record.error_code + ': ' + (record.summary || '');
      thread.append(line);
    } else if (record.kind === 'user') {
      const wrap = document.createElement('div');
      wrap.className = 'msg user';
      const bubble = document.createElement('div');
      bubble.className = 'bubble';
      bubble.textContent = record.text || '';
      wrap.append(bubble);
      thread.append(wrap);
    } else if (record.kind === 'assistant') {
      const wrap = document.createElement('div');
      wrap.className = 'msg agent';
      wrap.innerHTML = '<div class="who">agent</div><div class="bubble">' + md(record.text) + '</div>';
      thread.append(wrap);
    }
  }
  if (lastUser >= 0 && !finished) {
    const line = document.createElement('div');
    line.className = 'thinking';
    line.innerHTML = '<span class="pulse">thinking…</span>';
    thread.append(line);
  }
  $('#dot').className = 'dot on';
  $('#status').textContent = state.project + ' / ' + (state.session || 'no session');
  if (atBottom) $('#scroll').scrollTop = $('#scroll').scrollHeight;
}

async function loadApprovals() {
  try {
    const ok = await api('/api/approvals');
    const box = $('#approvals');
    box.innerHTML = '';
    for (const item of ok.pending || []) {
      const card = document.createElement('div');
      card.className = 'approval';
      const what = document.createElement('div');
      what.className = 'what';
      what.innerHTML = '<strong>Approval needed</strong> — <code>' + esc(String(item.action || '') + ' ' + String(item.resource || '')) + '</code><br>' + esc(item.reason || '');
      const allow = document.createElement('button');
      allow.className = 'primary';
      allow.textContent = 'Allow';
      allow.onclick = () => decide(item, true);
      const deny = document.createElement('button');
      deny.textContent = 'Deny';
      deny.onclick = () => decide(item, false);
      card.append(what, allow, deny);
      box.append(card);
    }
  } catch (error) { /* the poll loop reports connection state */ }
}

async function decide(item, allow) {
  try {
    await api('/api/decide', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approval: item.id, digest: item.digest, allow }) });
  } catch (error) { $('#hint').textContent = error.message; }
  await loadApprovals();
}

async function send() {
  const text = $('#text').value.trim();
  if (!text || !state.project || !state.session) return;
  $('#send').disabled = true;
  $('#text').value = '';
  try {
    await api('/api/say', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project: state.project, session: state.session, text }) });
    await loadHistory(false);
    render();
  } catch (error) {
    $('#hint').textContent = error.message;
    $('#text').value = text;
  } finally {
    $('#send').disabled = false;
  }
}

async function poll() {
  for (;;) {
    if (!state.project || !state.session) { await sleep(500); continue; }
    try {
      const after = state.lastSeq >= 0 ? '&after_seq=' + state.lastSeq : '';
      const ok = await api('/api/wait?project=' + encodeURIComponent(state.project) +
        '&session=' + encodeURIComponent(state.session) + after);
      if (ok.changed) { await loadHistory(false); render(); }
      else await sleep(400);
    } catch (error) {
      $('#status').textContent = 'disconnected: ' + error.message;
      $('#dot').className = 'dot';
      await sleep(2000);
    }
  }
}

function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

$('#send').onclick = send;
$('#text').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(); }
});
$('#project').onchange = (event) => switchProject(event.target.value);
$('#session').onchange = (event) => switchSession(event.target.value);
$('#newSession').onclick = async () => {
  const name = prompt('New session name');
  if (!name) return;
  try {
    await api('/api/session', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project: state.project, session: name }) });
  } catch (error) { $('#hint').textContent = error.message; }
  await loadSessions();
  $('#session').value = name;
  await switchSession(name);
};

loadProjects().catch((error) => { $('#status').textContent = 'error: ' + error.message; });
setInterval(loadApprovals, 5000);
loadApprovals();
poll();
</script>
</html>
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vm', default='open-math')
    parser.add_argument('--socket', help='absolute UI socket path inside the VM')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    socket_path = args.socket or socket_from_status(args.vm)
    if not socket_path.startswith('/'):
        parser.error('socket path must be absolute')
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.bridge = Bridge(args.vm, socket_path)
    server.bridge.warm()
    print(f'http://{args.host}:{args.port}/  ->  {args.vm}:{socket_path}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
