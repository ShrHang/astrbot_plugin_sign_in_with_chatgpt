// Run: node --test tests/test_dashboard.cjs. Isolated DOM/bridge fixture; no network or account.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.dataset = {}; this.style = {}; this.listeners = {}; this.disabled = false; this.open = false; this.value = ''; this.classes = new Set(); this.classList = { toggle: (name, active) => active ? this.classes.add(name) : this.classes.delete(name) }; }
  set textContent(value) { this.value = String(value); this.children = []; }
  get textContent() { return this.value + this.children.map((child) => child.textContent).join(''); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this.value = ''; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  dispatch(name, event = {}) { if (name === 'click' && this.disabled) return; for (const callback of this.listeners[name] || []) callback(event); }
  showModal() { this.open = true; }
  close() { this.open = false; }
}
const counts = { requests: 3, succeeded: 1, failed: 1, cancelled: 1, input_tokens: 120, cached_tokens: 40, output_tokens: 20, usage_reported_requests: 1, total_duration_ms: 4500 };
function status(overrides = {}) { return { account: { state: 'ready', connected: true, email: 'person@example.test', ...overrides }, attempt: null, logout: null, usage: { days: 30, timezone: 'UTC', since: Date.UTC(2026, 9, 1) / 1000, totals: { ...counts }, daily: [{ date: '2026-10-01', ...counts }], models: [{ model: 'test-model', ...counts }], recent: [{ at: Date.UTC(2026, 9, 1) / 1000, model: 'test-model', kind: 'chat', outcome: 'failed', duration_ms: 1500, usage_reported: false, input_tokens: null, cached_tokens: null, output_tokens: null }], storage_error: false } }; }
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
async function fixture(initial = status()) {
  const elements = new Map();
  const html = fs.readFileSync(path.join(root, 'views/account/index.html'), 'utf8');
  for (const match of html.matchAll(/<([a-z]+)[^>]*\bid="([^"]+)"/g)) elements.set(match[2], new Element(match[1]));
  const document = new Element(); document.hidden = false; document.getElementById = (id) => elements.get(id); document.createElement = (tag) => new Element(tag); document.querySelectorAll = () => [...elements.values()].filter((element) => element.tagName === 'button');
  const state = { snapshot: initial, calls: [], responses: {} };
  const request = async (route, body) => { state.calls.push([route, body]); const response = state.responses[route]; if (response) return typeof response === 'function' ? response(body) : response; return { ok: true, result: route.startsWith('models') ? { catalog: [{ slug: 'test-model', display_name: 'Test Model' }] } : state.snapshot }; };
  const context = { document, window: { AstrBotPluginView: { ready: async () => {}, apiGet: request, apiPost: request }, confirm: () => true }, setTimeout: () => 1, clearTimeout: () => {}, console };
  vm.runInNewContext(fs.readFileSync(path.join(root, 'views/account/app.js'), 'utf8'), context);
  await flush();
  return { state, elements, get: (id) => elements.get(id), async refresh(next) { state.snapshot = next; document.dispatch('visibilitychange'); await flush(); } };
}
test('initial connected account loads named catalog and shows reported usage with unknown recent tokens', async () => {
  const f = await fixture();
  assert.equal(f.state.calls.filter(([route]) => route === 'models').length, 1);
  assert.match(f.get('models').textContent, /Test Model/);
  assert.equal(f.get('input-tokens').textContent, '120');
  assert.match(f.get('cached-detail').textContent, /40/);
  assert.match(f.get('coverage-detail').textContent, /1 \/ 3/);
  assert.match(f.get('recent-requests').textContent, /未知/);
  assert.equal(f.get('daily-chart').children.length, 30);
  assert.equal(f.get('daily-table').children.length, 30);
  await f.refresh(status());
  assert.equal(f.state.calls.filter(([route]) => route === 'models').length, 1);
});
test('no reported token usage remains unknown and a usage limit keeps ChatGPT identity with primary manage action', async () => {
  const data = status({ health: { category: 'limited', message: 'raw diagnostic should stay hidden', diagnostics: { request_id: 'sensitive' } } });
  data.usage.totals.usage_reported_requests = 0;
  const f = await fixture(data);
  assert.equal(f.get('input-tokens').textContent, '—'); assert.equal(f.get('output-tokens').textContent, '—');
  assert.equal(f.get('health').hidden, false); assert.equal(f.get('health-usage').hidden, false);
  assert.match(f.get('account-email').textContent, /person/); assert.equal(f.get('login').classes.has('secondary'), true);
  assert.doesNotMatch(f.get('health-help').textContent, /raw|sensitive/);
  await f.refresh(status()); assert.equal(f.get('login').classes.has('secondary'), false);
});
test('welcome Escape persists acknowledgement and failure keeps dialog available for retry', async () => {
  const f = await fixture(status({ welcome_pending: true }));
  assert.equal(f.get('welcome').open, true);
  f.state.responses['plan/acknowledge'] = { ok: false, error: { category: 'temporary' } };
  let prevented = false; f.get('welcome').dispatch('cancel', { preventDefault() { prevented = true; } }); await flush();
  assert.equal(prevented, true); assert.equal(f.get('welcome').open, true); assert.match(f.get('welcome-notice').textContent, /重试/);
  f.state.responses['plan/acknowledge'] = { ok: true, result: status({ welcome_pending: false }) };
  f.get('welcome-ack').dispatch('click'); await flush(); assert.equal(f.get('welcome').open, false);
  await f.refresh(status({ welcome_pending: false })); assert.equal(f.get('welcome').open, false);
});
test('late model refresh cannot restore catalog after disconnect and reconnect reloads it', async () => {
  const f = await fixture(); let resolve;
  f.state.responses['models/refresh'] = () => new Promise((done) => { resolve = done; });
  f.get('models-refresh').dispatch('click'); await flush();
  await f.refresh(status({ state: 'unbound', connected: false, email: null }));
  assert.match(f.get('models').textContent, /登录后/);
  resolve({ ok: true, result: { catalog: [{ slug: 'stale-secret-model' }] } }); await flush();
  assert.doesNotMatch(f.get('models').textContent, /stale/);
  await f.refresh(status({ email: 'new@example.test' }));
  assert.equal(f.state.calls.filter(([route]) => route === 'models').length, 2);
});
test('authorization cancellation uses current attempt and unauthorized manager stops all actions', async () => {
  const data = status(); data.attempt = { phase: 'waiting_browser', attempt_id: 'current-attempt', deadline: Date.now() / 1000 + 120 };
  const f = await fixture(data); assert.match(f.get('login-label').textContent, /取消/);
  f.state.responses['auth/cancel'] = { ok: true, result: status() };
  f.get('login').dispatch('click'); await flush();
  const call = f.state.calls.find(([route]) => route === 'auth/cancel'); assert.equal(call[1].attempt_id, 'current-attempt');
  f.state.responses.status = { ok: false, error: { category: 'unauthorized' } };
  await f.refresh(status());
  assert.equal(f.get('login').disabled, true); assert.equal(f.get('logout').disabled, true); assert.equal(f.get('models-refresh').disabled, true);
  assert.match(f.get('notice').textContent, /AstrBot/);
});
test('every external usage link protects opener and no remote fonts or assets are introduced', () => {
  const html = fs.readFileSync(path.join(root, 'views/account/index.html'), 'utf8');
  const links = [...html.matchAll(/<a\b[^>]+>/g)].map((match) => match[0]);
  assert.equal(links.length, 3);
  for (const link of links) { assert.match(link, /href="https:\/\/chatgpt.com\/settings\/usage"/); assert.match(link, /target="_blank"/); assert.match(link, /rel="noopener noreferrer"/); }
  assert.doesNotMatch(html, /(?:src|href)="https?:\/\/(?!chatgpt.com\/settings\/usage)/);
});

test('retained invalid registration can be unbound while configuration prevents login', async () => {
  const f = await fixture(status({ state: 'configuration_error', connected: false, registered: true }));
  assert.equal(f.get('login').disabled, true);
  assert.equal(f.get('logout').disabled, false);
  f.state.responses['auth/logout'] = { ok: true, result: status({ state: 'unbound', connected: false, registered: false }) };
  f.get('logout').dispatch('click'); await flush();
  assert.ok(f.state.calls.some(([route]) => route === 'auth/logout'));
});
test('status started before authorization cannot overwrite the newer authorization attempt', async () => {
  const f = await fixture(); let resolve;
  f.state.responses.status = () => new Promise((done) => { resolve = done; });
  const poll = f.refresh(status()); await flush();
  const authorizing = status(); authorizing.attempt = { phase: 'preparing', attempt_id: 'new', deadline: Date.now() / 1000 + 120 };
  f.state.responses['auth/start'] = { ok: true, result: authorizing };
  f.get('login').dispatch('click'); await flush();
  assert.match(f.get('login-label').textContent, /取消/);
  resolve({ ok: true, result: status() }); await poll; await flush();
  assert.match(f.get('login-label').textContent, /取消/);
});
test('output-only usage keeps missing input and cache totals unknown', async () => {
  const data = status();
  Object.assign(data.usage.totals, { input_tokens: 0, cached_tokens: 0, output_tokens: 20, input_reported_requests: 0, cached_reported_requests: 0, output_reported_requests: 1 });
  const f = await fixture(data);
  assert.equal(f.get('input-tokens').textContent, '—');
  assert.equal(f.get('output-tokens').textContent, '20');
  assert.equal(f.get('cached-detail').textContent, '缓存输入未知');
  assert.match(f.get('coverage-detail').title, /输入 0，输出 1/);
  Object.assign(data.usage.totals, { input_tokens: 0, cached_tokens: 0, input_reported_requests: 1, cached_reported_requests: 1 });
  await f.refresh(data);
  assert.equal(f.get('input-tokens').textContent, '0');
  assert.match(f.get('cached-detail').textContent, /缓存输入 0/);
});
