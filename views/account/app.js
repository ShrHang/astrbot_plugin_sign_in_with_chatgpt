"use strict";
(() => {
  const bridge = window.AstrBotPluginView;
  const el = (id) => document.getElementById(id);
  const phases = { preparing: "正在准备登录", waiting_browser: "等待浏览器授权", validating: "正在验证授权" };
  const accounts = { unbound: "未绑定", ready: "已连接", login_required: "需要重新登录", configuration_error: "配置异常" };
  const errors = { configuration_error: "授权客户端配置异常，请检查插件配置。", login_required: "ChatGPT 授权已失效，请重新授权。", limited: "请前往 ChatGPT 设置查看适用的用量限制。", temporary: "ChatGPT 服务暂不可用，请稍后重试。", permission: "当前账号或工作区不允许此操作，请检查权限。", request: "请求未能完成，请检查配置后重试。", browser: "系统浏览器未能打开，请检查默认浏览器设置。", timeout: "操作已超时，请重试。", closed: "管理服务已停止，请重新打开本页。" };
  let snapshot = null, timer = null, stopped = false, fetching = false, pendingAction = false;
  let catalogContext = null, catalogEpoch = 0, modelsLoading = false, revision = 0, welcomeSaving = false;
  const number = (value) => Number.isFinite(value) && value >= 0 ? value : 0;
  const count = (value) => new Intl.NumberFormat("zh-CN").format(number(value));
  const duration = (value) => typeof value !== "number" || !Number.isFinite(value) ? "—" : value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(1)} s`;
  const text = (id, value) => { el(id).textContent = value; };
  function notice(value) { text("notice", value || ""); }
  function errorText(error) { return errors[error?.category] || "操作未完成，请稍后重试。"; }
  function isAuthorizing(attempt) { return Boolean(attempt && Object.hasOwn(phases, attempt.phase)); }
  function node(tag, value, className) { const element = document.createElement(tag); if (value !== undefined) element.textContent = value; if (className) element.className = className; return element; }
  function empty(id, value) { el(id).replaceChildren(node("li", value, "empty")); }
  function stopSession() {
    stopped = true; catalogEpoch++; clearTimeout(timer);
    document.querySelectorAll("button").forEach((button) => { button.disabled = true; });
    notice("管理登录已失效，请重新登录 AstrBot 后打开本页。");
    if (el("welcome").open) text("welcome-notice", "管理登录已失效，请重新登录 AstrBot 后打开本页。");
  }
  async function api(path, body) {
    try {
      const data = body === undefined ? await bridge.apiGet(path) : await bridge.apiPost(path, body);
      if (!data.ok) { const error = new Error("管理操作未完成"); error.category = data.error?.category; throw error; }
      return data.result;
    } catch (error) {
      if (error.category === "unauthorized" || /\b401\b|unauthorized|管理认证失效/i.test(String(error.message))) stopSession();
      else notice(errorText(error));
      throw error;
    }
  }
  function renderHealth(health) {
    el("health").hidden = !health;
    const limited = health?.category === "limited";
    el("login").classList.toggle("secondary", limited || isAuthorizing(snapshot?.attempt));
    if (!health) return;
    text("health-title", limited ? "用量已达上限" : "账号提示");
    text("health-help", errorText(health));
    el("health-usage").hidden = !limited;
  }
  function render(data) {
    snapshot = data;
    const { account, attempt, logout } = data;
    const active = isAuthorizing(attempt);
    text("account-state", account.logging_out ? "正在解绑" : active ? "登录中" : accounts[account.state] || "未知状态");
    el("account-state").dataset.ready = String(account.connected && !active && !account.logging_out);
    text("account-email", account.connected ? account.email || "已连接的 ChatGPT 账号" : "尚未连接 ChatGPT 账号");
    let help = account.connected ? "账号已连接，可在 AstrBot 中使用。" : account.state === "configuration_error" ? errors.configuration_error : account.state === "login_required" ? errors.login_required : "使用 ChatGPT 登录以连接你的账号。";
    if (active) help = phases[attempt.phase];
    else if (attempt?.phase === "expired") help = "登录已超时，请重新发起授权。";
    else if (attempt?.phase === "failed") help = `登录未完成：${errorText(attempt.error)}`;
    else if (attempt?.phase === "cancelled") help = account.connected ? "本次授权已取消，原账号仍已连接。" : "登录已取消，可重新登录。";
    else if (attempt?.phase === "succeeded" && account.connected) help = "登录成功，账号已连接。";
    if (logout?.local_cleared) help = "账号已解绑。";
    if (account.logging_out) help = "正在解除账号绑定…";
    text("account-help", help);
    text("plan-status", account.connected ? "Using ChatGPT plan · 使用 ChatGPT 计划" : "登录后，符合条件的 AI 请求将使用你的 ChatGPT 计划。");
    text("models-plan", account.connected ? "Using ChatGPT plan · 使用 ChatGPT 计划" : "登录后查看 ChatGPT 计划可用模型");
    text("countdown", active ? `剩余约 ${Math.max(0, Math.ceil(attempt.deadline - Date.now() / 1000))} 秒` : "");
    text("login-label", active ? "取消授权" : account.connected ? "重新授权" : "Continue with ChatGPT");
    el("login").dataset.reconnect = String(account.connected);
    el("login").disabled = Boolean(stopped || pendingAction || account.logging_out || account.state === "configuration_error");
    el("logout").disabled = Boolean(stopped || pendingAction || account.logging_out || (!account.connected && !account.registered && !account.registration_pending && !active));
    renderHealth(account.health);
    if (logout?.error && !logout.local_cleared) notice(errorText(logout.error));
    const context = account.connected && !active && !account.logging_out ? `${account.email || "connected"}|${attempt?.phase === "succeeded" ? attempt.attempt_id : ""}` : null;
    if (context !== catalogContext) {
      catalogContext = context; catalogEpoch++; modelsLoading = false;
      empty("models", context ? "正在读取模型目录…" : active ? "授权完成后更新模型目录" : "登录后可查看模型列表");
      if (context && !stopped) loadModels(false);
    }
    el("models-refresh").disabled = Boolean(stopped || pendingAction || active || account.logging_out || !account.connected || modelsLoading);
    renderUsage(data.usage);
    const showWelcome = account.connected && account.welcome_pending && !active && !account.logging_out;
    if (showWelcome && !el("welcome").open && !stopped) el("welcome").showModal();
    else if (!showWelcome && el("welcome").open && !welcomeSaving) el("welcome").close();
  }
  function renderUsage(usage) {
    const totals = usage?.totals;
    const requests = number(totals?.requests), reported = number(totals?.usage_reported_requests);
    const coverage = (field) => Object.hasOwn(totals || {}, `${field}_reported_requests`) ? number(totals[`${field}_reported_requests`]) : reported;
    const inputReported = coverage("input"), outputReported = coverage("output"), cachedReported = coverage("cached");
    text("total-requests", totals ? count(requests) : "—");
    text("request-detail", totals ? `${count(totals.succeeded)} 成功 · ${count(totals.failed)} 失败 · ${count(totals.cancelled)} 取消` : "统计暂不可用");
    text("input-tokens", inputReported ? count(totals.input_tokens) : "—");
    text("output-tokens", outputReported ? count(totals.output_tokens) : "—");
    text("cached-detail", cachedReported ? `其中缓存输入 ${count(totals.cached_tokens)}` : "缓存输入未知");
    text("coverage-detail", totals ? `${count(reported)} / ${count(requests)} 次请求回报用量字段` : "用量回报未知");
    el("coverage-detail").title = `字段回报请求数：输入 ${count(inputReported)}，输出 ${count(outputReported)}，缓存输入 ${count(cachedReported)}；未回报部分未知。`;
    text("average-duration", requests ? duration(number(totals.total_duration_ms) / requests) : "—");
    text("usage-warning", usage?.storage_error ? "本地统计暂未能可靠保存，记录可能不完整。" : "");
    const rows = [];
    if (usage) {
      const known = new Map((usage.daily || []).map((row) => [row.date, row]));
      const start = Number.isFinite(usage.since) ? new Date(usage.since * 1000) : new Date();
      start.setUTCHours(0, 0, 0, 0);
      if (!Number.isFinite(usage.since)) start.setUTCDate(start.getUTCDate() - 29);
      for (let i = 0; i < 30; i++) {
        const date = new Date(start); date.setUTCDate(date.getUTCDate() + i);
        const day = date.toISOString().slice(0, 10);
        rows.push({ date: day, ...(known.get(day) || {}) });
      }
    }
    const max = Math.max(1, ...rows.map((row) => number(row.requests)));
    el("daily-chart").replaceChildren(); el("daily-table").replaceChildren();
    for (const row of rows) {
      const column = node("div", undefined, "chart-column");
      column.title = `${row.date} · ${count(row.requests)} 次请求`;
      const bar = node("div", undefined, "chart-bar");
      bar.style.height = `${number(row.requests) / max * 100}%`;
      column.append(bar); el("daily-chart").append(column);
      const tr = node("tr");
      for (const value of [row.date, count(row.requests), count(row.succeeded), count(row.failed), count(row.cancelled)]) tr.append(node("td", value));
      el("daily-table").append(tr);
    }
    text("chart-start", rows[0]?.date.slice(5).replace("-", "/") || "—");
    text("chart-end", rows.at(-1)?.date.slice(5).replace("-", "/") || "—");
    text("chart-empty", !usage ? "本地统计暂不可用" : requests ? "" : "此期间尚无本地请求记录");
    el("model-usage").replaceChildren();
    const models = [...(usage?.models || [])].sort((a, b) => number(b.requests) - number(a.requests));
    for (const model of models) {
      const li = node("li"), row = node("div", undefined, "model-row");
      row.append(node("span", model.model), node("span", count(model.requests)));
      const track = node("div", undefined, "model-track"), fill = node("div", undefined, "model-fill");
      track.setAttribute("aria-hidden", "true");
      fill.style.width = `${requests ? Math.min(100, number(model.requests) / requests * 100) : 0}%`;
      track.append(fill); li.append(row, track); el("model-usage").append(li);
    }
    if (!models.length) empty("model-usage", usage ? "有请求记录后显示模型分布" : "本地统计暂不可用");
    el("recent-requests").replaceChildren();
    for (const request of (usage?.recent || [])) {
      const tr = node("tr");
      const date = new Date(request.at * 1000);
      tr.append(node("td", Number.isFinite(date.getTime()) ? date.toISOString().slice(5, 19).replace("T", " ") : "—"));
      const model = node("td", request.model); model.append(node("span", request.kind === "search" ? "搜索" : "对话", "kind-label")); tr.append(model);
      const result = node("td"), pill = node("span", ({ succeeded: "成功", failed: "失败", cancelled: "已取消" })[request.outcome] || "未知", "outcome");
      pill.dataset.outcome = request.outcome; result.append(pill); tr.append(result);
      const tokens = node("td", request.usage_reported ? `${request.input_tokens == null ? "未知" : count(request.input_tokens)} / ${request.output_tokens == null ? "未知" : count(request.output_tokens)}` : "未知");
      tokens.title = request.usage_reported ? `缓存输入：${request.cached_tokens == null ? "未知" : count(request.cached_tokens)}` : "本次请求未回报 token 用量";
      tr.append(tokens, node("td", duration(request.duration_ms))); el("recent-requests").append(tr);
    }
    if (!el("recent-requests").children.length) {
      const tr = node("tr"), td = node("td", usage ? "尚无请求记录。通过此插件使用模型后，记录会显示在这里。" : "本地统计暂不可用", "empty");
      td.colSpan = 5; tr.append(td); el("recent-requests").append(tr);
    }
  }
  function schedule() {
    clearTimeout(timer);
    if (stopped || document.hidden) return;
    const active = snapshot && (isAuthorizing(snapshot.attempt) || snapshot.account.logging_out);
    timer = setTimeout(refresh, active ? 1000 : 5000);
  }
  async function refresh() {
    if (fetching || stopped || document.hidden || pendingAction || welcomeSaving) { schedule(); return; }
    fetching = true; const version = revision;
    try { const data = await api("status"); if (version === revision && !stopped) render(data); }
    catch (_) {} finally { fetching = false; schedule(); }
  }
  async function action(path, body) {
    if (stopped || pendingAction) return;
    pendingAction = true; revision++;
    if (snapshot) render(snapshot);
    try { notice(""); const data = await api(path, body); if (!stopped) render(data); }
    catch (_) {} finally { pendingAction = false; if (snapshot && !stopped) render(snapshot); schedule(); }
  }
  async function loadModels(force = true) {
    if (stopped || modelsLoading || !catalogContext) return;
    modelsLoading = true; const epoch = catalogEpoch;
    el("models-refresh").disabled = true;
    try {
      const data = await api(force ? "models/refresh" : "models", force ? {} : undefined);
      if (stopped || epoch !== catalogEpoch) return;
      el("models").replaceChildren();
      const catalog = Array.isArray(data.catalog) ? data.catalog : (data.models || []).map((slug) => ({ slug }));
      for (const model of catalog) {
        const li = node("li", model.display_name || model.slug);
        if (model.display_name && model.display_name !== model.slug) li.append(node("span", model.slug, "model-slug"));
        el("models").append(li);
      }
      if (!catalog.length) empty("models", "当前账号没有可用模型");
    } catch (_) { if (epoch === catalogEpoch) empty("models", "模型目录读取失败，可点击“刷新模型”重试。"); }
    finally { if (epoch === catalogEpoch) { modelsLoading = false; if (snapshot && !stopped) render(snapshot); } }
  }
  async function acknowledgeWelcome() {
    if (stopped || welcomeSaving) return;
    welcomeSaving = true; revision++; el("welcome-ack").disabled = true; text("welcome-notice", "");
    try { const data = await api("plan/acknowledge", {}); if (!stopped) { render(data); if (!data.account.welcome_pending) el("welcome").close(); } }
    catch (_) { if (!stopped) text("welcome-notice", "确认暂未保存，请重试。"); }
    finally { welcomeSaving = false; el("welcome-ack").disabled = stopped; schedule(); }
  }
  el("login").addEventListener("click", () => {
    if (isAuthorizing(snapshot?.attempt)) action("auth/cancel", { attempt_id: snapshot.attempt.attempt_id });
    else action("auth/start", {});
  });
  el("logout").addEventListener("click", () => { if (window.confirm("确定解绑当前 ChatGPT 账号？")) action("auth/logout", {}); });
  el("models-refresh").addEventListener("click", () => loadModels(true));
  el("welcome-ack").addEventListener("click", acknowledgeWelcome);
  el("welcome").addEventListener("cancel", (event) => { event.preventDefault(); acknowledgeWelcome(); });
  document.addEventListener("visibilitychange", () => { clearTimeout(timer); if (!document.hidden) refresh(); });
  if (!bridge) { notice("请从 AstrBot 插件页面打开本页。"); return; }
  bridge.ready().then(refresh).catch(() => notice("无法连接 AstrBot，请重新打开插件页面。"));
})();
