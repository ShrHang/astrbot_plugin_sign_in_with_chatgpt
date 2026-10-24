"use strict";
(() => {
  const bridge = window.AstrBotPluginView;
  const el = (id) => document.getElementById(id);
  const phases = { preparing: "正在准备登录", waiting_browser: "等待浏览器授权", validating: "正在验证授权" };
  const accounts = { unbound: "未绑定", ready: "已绑定", login_required: "登录已失效", configuration_error: "客户端配置异常" };
  let snapshot = null, timer = null, stopped = false, fetching = false, modeledAttempt = null, pendingAction = false;
  function notice(value) { el("notice").textContent = value || ""; }
  function errorText(error) { return error?.message || "操作未完成，请重试。"; }
  function isAuthorizing(attempt) { return Boolean(attempt && Object.hasOwn(phases, attempt.phase)); }
  async function api(path, body) {
    try {
      const data = body === undefined ? await bridge.apiGet(path) : await bridge.apiPost(path, body);
      if (!data.ok) throw new Error(errorText(data.error));
      return data.result;
    } catch (error) {
      const message = String(error.message || "管理操作失败");
      if (/401|unauthorized|未授权|认证失效|token.*(expired|invalid)|token.*(过期|无效)/i.test(message)) {
        stopped = true;
        clearTimeout(timer);
        document.querySelectorAll("button").forEach((button) => { button.disabled = true; });
        notice("管理登录已失效，请重新登录 AstrBot 后打开本页。");
      } else notice(message);
      throw error;
    }
  }
  function render(data) {
    snapshot = data;
    const { account, attempt, logout } = data;
    const active = isAuthorizing(attempt);
    el("account-state").textContent = account.logging_out ? "正在解绑" : active ? "登录中" : accounts[account.state] || "未知状态";
    let help = account.connected ? "账号已绑定。" : account.state === "configuration_error"
      ? "授权客户端配置异常，请检查注册配置。" : account.state === "login_required"
      ? "登录已失效，请重新登录。" : "尚未绑定账号，请点击“Continue with ChatGPT”登录。";
    if (active) help = phases[attempt.phase];
    else if (attempt?.phase === "expired") help = `登录已超时，请点击“${account.connected ? "重新授权" : "Continue with ChatGPT"}”重新登录。`;
    else if (attempt?.phase === "failed") help = `登录未完成：${errorText(attempt.error)}`;
    else if (attempt?.phase === "cancelled") help = account.connected ? "本次登录已取消，账号仍已绑定。" : "登录已取消，可重新登录。";
    else if (attempt?.phase === "succeeded" && account.connected) help = "登录成功，账号已绑定。";
    if (logout?.local_cleared) help = "账号已解绑。";
    if (account.logging_out) help = "正在解除账号绑定…";
    el("account-help").textContent = help;
    el("countdown").textContent = active ? `剩余约 ${Math.max(0, Math.ceil(attempt.deadline - Date.now() / 1000))} 秒` : "";
    el("login").textContent = active ? "取消授权" : account.connected ? "重新授权" : "Continue with ChatGPT";
    el("login").classList.toggle("secondary", active);
    el("login").disabled = Boolean(pendingAction || account.logging_out || account.state === "configuration_error");
    el("logout").disabled = Boolean(pendingAction || account.logging_out);
    el("models-refresh").disabled = Boolean(pendingAction || active || account.logging_out || !account.connected);
    if (logout?.error && !logout.local_cleared) notice(errorText(logout.error));
    if (!account.connected) {
      const li = document.createElement("li"); li.textContent = "登录后可查看模型列表"; el("models").replaceChildren(li);
    }
    if (attempt?.phase === "succeeded" && attempt.attempt_id !== modeledAttempt && account.connected) {
      modeledAttempt = attempt.attempt_id;
      loadModels();
    }
  }
  function schedule() {
    clearTimeout(timer);
    if (stopped || document.hidden) return;
    const active = snapshot && (snapshot.account.authorizing || snapshot.account.logging_out);
    timer = setTimeout(refresh, active ? 1000 : 5000);
  }
  async function refresh() {
    if (fetching || stopped || document.hidden) return;
    fetching = true;
    try { render(await api("status")); } catch (_) {} finally { fetching = false; schedule(); }
  }
  async function action(path, body) {
    if (stopped || pendingAction) return;
    pendingAction = true;
    if (snapshot) render(snapshot);
    try { notice(""); render(await api(path, body)); } catch (_) {} finally {
      pendingAction = false;
      if (snapshot && !stopped) render(snapshot);
      schedule();
    }
  }
  el("login").addEventListener("click", () => {
    if (isAuthorizing(snapshot?.attempt)) action("auth/cancel", { attempt_id: snapshot.attempt.attempt_id });
    else action("auth/start", {});
  });
  el("logout").addEventListener("click", () => { if (window.confirm("确定解绑当前账号？")) action("auth/logout", {}); });
  async function loadModels() {
    try {
      const data = await api("models/refresh", {}); el("models").replaceChildren();
      for (const name of data.models) { const li = document.createElement("li"); li.textContent = name; el("models").append(li); }
      if (!data.models.length) { const li = document.createElement("li"); li.textContent = "当前账号没有可用模型"; el("models").append(li); }
    } catch (_) {}
  }
  el("models-refresh").addEventListener("click", loadModels);
  document.addEventListener("visibilitychange", () => { clearTimeout(timer); if (!document.hidden) refresh(); });
  if (!bridge) { notice("请从 AstrBot 插件页面打开本页。"); return; }
  bridge.ready().then(refresh).catch(() => notice("无法连接 AstrBot，请重新打开插件页面。"));
})();
