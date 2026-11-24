// Vanilla JS front end for the planner API.

const $ = (sel) => document.querySelector(sel);
let currentPlan = null;
let openSeq = 0;
let ME = {};

// ---------- utils ----------

// ---------- auth (tokens from core_auth-style /api/auth endpoints) ----------

const tokens = {
  get access() { try { return localStorage.getItem("access_token") || ""; } catch { return ""; } },
  get refresh() { try { return localStorage.getItem("refresh_token") || ""; } catch { return ""; } },
  set(a, r) { try { localStorage.setItem("access_token", a); localStorage.setItem("refresh_token", r); } catch {} },
  clear() { try { localStorage.removeItem("access_token"); localStorage.removeItem("refresh_token"); } catch {} },
};

async function rawApi(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (tokens.access) headers.Authorization = `Bearer ${tokens.access}`;
  const res = await fetch(path, { headers, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  const data = await res.json().catch(() => ({}));
  return { res, data };
}

// Refresh tokens are single-use: concurrent 401s must share one refresh call.
let refreshing = null;
function tryRefresh() {
  if (!tokens.refresh) return Promise.resolve(false);
  if (!refreshing) {
    const used = tokens.refresh;
    const doRefresh = async () => {
      // another tab may have refreshed while this one waited: its new tokens are already stored
      if (tokens.refresh && tokens.refresh !== used) return true;
      const { res, data } = await rawApi("/api/auth/refresh", { method: "POST", body: { refresh_token: tokens.refresh } });
      if (!res.ok) return false;
      tokens.set(data.access_token, data.refresh_token);
      return true;
    };
    // one refresh at a time across every open tab (refresh tokens are single-use)
    refreshing = (navigator.locks ? navigator.locks.request("ads-planner-refresh", doRefresh) : doRefresh())
      .finally(() => setTimeout(() => (refreshing = null), 0));
  }
  return refreshing;
}

// Multipart upload with the same auth/refresh handling as api().
async function upload(path, file) {
  const send = () => {
    const fd = new FormData(); fd.append("file", file);
    return fetch(path, { method: "POST", headers: { Authorization: `Bearer ${tokens.access}` }, body: fd });
  };
  let res = await send();
  if (res.status === 401) {
    if (await tryRefresh()) res = await send();
    if (res.status === 401) { showLogin(); throw new Error("Please sign in"); }
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Upload failed (${res.status})`);
  return data;
}

// API timestamps are UTC without a zone suffix.
const utc = (s) => new Date(/[zZ]|[+-]\d\d:\d\d$/.test(s) ? s : s + "Z");

async function api(path, opts = {}) {
  let { res, data } = await rawApi(path, opts);
  if (res.status === 401 && !path.startsWith("/api/auth/")) {
    if (await tryRefresh()) ({ res, data } = await rawApi(path, opts));
    else { showLogin(); throw new Error("Please sign in"); }
    if (res.status === 401) { showLogin(); throw new Error("Please sign in"); }
  }
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || res.statusText));
  return data;
}

// Sign-in only: there is no signup. The owner is created on the server; the owner adds others.
async function showLogin() {
  const { data } = await rawApi("/api/auth/setup-status");
  $("#login-title").textContent = "Sign in";
  $("#login-hint").textContent = data.has_users === false ? "No account exists yet. Ask the administrator." : "";
  $("#login-error").textContent = "";
  $("#login-screen").hidden = false;
}

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = $("#login-form");
  if (!form.elements.email.value.trim() || !form.elements.password.value) {
    $("#login-error").textContent = "Enter your email and password.";
    return (form.elements.email.value.trim() ? form.elements.password : form.elements.email).focus();
  }
  if (!form.elements.email.checkValidity()) {
    $("#login-error").textContent = "That email address does not look right.";
    return form.elements.email.focus();
  }
  const body = Object.fromEntries(new FormData(form).entries());
  const btn = $("#login-btn");
  btn.disabled = true;
  try {
    const { res, data } = await rawApi("/api/auth/login", { method: "POST", body });
    if (!res.ok) throw new Error(res.status === 422 ? "Enter a valid email and password" : (typeof data.detail === "string" ? data.detail : "Login failed"));
    tokens.set(data.access_token, data.refresh_token);
    $("#login-screen").hidden = true;
    $("#login-form").reset();
    $("#login-form").elements.password.type = "password"; $("#pw-toggle").textContent = "Show";
    init().catch((e) => toast(e.message, true));
  } catch (e) { $("#login-error").textContent = e.message; }
  finally { btn.disabled = false; }
});

$("#btn-logout").addEventListener("click", async () => {
  try { await rawApi("/api/auth/logout", { method: "POST", body: { refresh_token: tokens.refresh } }); } catch {}
  tokens.clear();
  location.reload();
});

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = isError ? "error" : "";
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), 4000);
}

// In-app confirmation: ask("Title", "Text", "Do it") resolves true or false.
function ask(title, text, okLabel = "OK", danger = false) {
  const d = $("#dialog");
  $("#dialog-title").textContent = title;
  $("#dialog-text").textContent = text;
  $("#dialog-ok").textContent = okLabel;
  $("#dialog-ok").className = `btn ${danger ? "danger" : "primary"}`;
  d.returnValue = "";
  return new Promise((resolve) => {
    d.addEventListener("close", () => resolve(d.returnValue === "ok"), { once: true });
    d.showModal();
    $("#dialog-ok").focus();
  });
}

const fmt = {
  usd: (n) => `${Number(n || 0) < 0 ? "−" : ""}$${Math.abs(Number(n || 0)).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`,
  pkr: (n) => `PKR ${Math.round(n || 0).toLocaleString()}`,
  pct: (n) => `${((n || 0) * 100).toFixed(2)}%`,
  int: (n) => Math.round(n || 0).toLocaleString(),
  label: (s) => String(s || "").replace(/_/g, " "),
};

const icon = (name) => `<svg class="icon"><use href="#i-${name}"/></svg>`;

// Signature: the range bar. low..high band, expected tick, optional actual marker.
function rangeBar(low, high, expected, actual = null, { compact = false } = {}) {
  const max = Math.max(high, actual ?? 0, expected, 1) * 1.15;
  const x = (v) => `${Math.min(100, (v / max) * 100).toFixed(2)}%`;
  return `<div class="range${compact ? " compact" : ""}" role="img" aria-label="Estimate ${low} to ${high}, expected ${expected}${actual != null ? `, actual ${actual}` : ""}">
    <div class="track"></div>
    <div class="band" style="left:${x(low)};width:calc(${x(high)} - ${x(low)})"></div>
    <div class="tick" style="left:${x(expected)}"></div>
    ${actual != null ? `<div class="actual" style="left:${x(actual)}" title="Actual ${actual}"></div>` : ""}
    ${compact ? "" : `<div class="scale"><span>0</span><span>${Math.round(max)}</span></div>`}
  </div>`;
}

function formInputs() {
  const fd = new FormData($("#plan-form"));
  return {
    budget_pkr: +fd.get("budget_pkr"),
    country: fd.get("country"),
    product: fd.get("product"),
    niche: fd.get("niche"),
    category_note: (fd.get("category_note") || "").trim(),
    brand_id: +fd.get("brand_id") || 0,
    price_usd: +fd.get("price_usd"),
    duration_days: +fd.get("duration_days"),
    unit_cost_usd: +fd.get("unit_cost_usd") || 0,
    shipping_usd: +fd.get("shipping_usd") || 0,
    offer: (fd.get("offer") || "").trim(),
    usp: (fd.get("usp") || "").trim(),
    delivery_days: +fd.get("delivery_days") || 0,
    returns: (fd.get("returns") || "").trim(),
    social_proof: (fd.get("social_proof") || "").trim(),
    cod: fd.get("country") === "PK" && !!$("#plan-form").elements.cod.checked,
  };
}

function fillForm(inputs) {
  const f = $("#plan-form");
  f.elements.unit_cost_usd.value = ""; f.elements.shipping_usd.value = ""; f.elements.category_note.value = "";
  for (const k of ["offer", "usp", "delivery_days", "returns", "social_proof"]) f.elements[k].value = "";
  f.elements.price_usd.dataset.touched = "1";  // a saved plan's price is a real choice
  f.elements.cod.checked = false;
  for (const [k, v] of Object.entries(inputs)) {
    if (!f.elements[k]) continue;
    if (k === "cod") f.elements.cod.checked = !!v;
    else f.elements[k].value = (k === "delivery_days" && !v) ? "" : (v ?? "");
  }
  updateNoteField();
  updateCodLine();
}

// ---------- views ----------

const PAGES = {
  today: ["Today", "What needs your attention now, ranked"],
  plan: ["New plan", "Tell the app what you sell and where; it shows the orders to expect and writes the full plan"],
  history: ["My plans", "Every plan with its estimate and, once launched, the real result. Click one to open it"],
  rules: ["Learning", "How estimates compare with results, and the rules the planner now follows"],
  monitor: ["Campaigns", "Your live campaigns, checked against their plan every few hours, with what to do about problems"],
  meta: ["Connections", "Meta ad account, email alerts and image generation"],
  activity: ["Activity", "Everything the app and its users did, newest first"],
  billing: ["Payments", "AI cost against the monthly budget, and what the Meta campaigns have spent"],
  account: ["Account", "Your password, your team and the backups"],
};

function go(view) {
  // pushState does not fire hashchange, so the view renders exactly once, right now
  if (location.hash.slice(1) !== view) history.pushState(null, "", `#${view}`);
  showView(view);
}
document.querySelectorAll(".nav [data-view]").forEach((b) =>
  b.addEventListener("click", () => {
    if (b.dataset.view === "plan") newPlan();
    go(b.dataset.view);
  })
);
$("#btn-new-plan").addEventListener("click", () => { newPlan(); go("plan"); window.scrollTo({ top: 0 }); });

// A blank New plan page (keeps the country and niche the user picked last).
function newPlan() {
  currentPlan = null;
  openSeq++;  // a plan still loading must not replace the blank form
  clearTimeout(estTimer); estSeq++;
  $("#est-updating").textContent = "";
  const f = $("#plan-form");
  f.elements.product.value = "";
  f.elements.category_note.value = "";
  delete f.elements.price_usd.dataset.touched;  // a new plan gets the category's usual price again
  $("#plan-card").hidden = true;
  $("#estimate-card").hidden = true;
  $("#estimate-empty").hidden = false;
  setFormLocked(false);
  updateConversions();
  if (f.elements.country.options.length) liveEstimate();  // the defaults already make an estimate
  for (const k of ["offer", "usp", "delivery_days", "returns", "social_proof"]) f.elements[k].value = "";
  setTimeout(prefillBrief, 0);
}

// Open a saved plan on the plan page, with the page title naming it.
async function openPlan(id) {
  const seq = ++openSeq;
  const plan = await api(`/api/plans/${id}`);
  if (seq !== openSeq) return;  // a later click won
  clearTimeout(estTimer); estSeq++;
  currentPlan = plan;
  go("plan");
  fillForm(plan.inputs);
  updateConversions();
  renderEstimate(plan.estimate);
  renderPlan(plan);
  requestAnimationFrame(() => $("#plan-card").scrollIntoView({ behavior: "smooth", block: "start" }));
}
$("#menu-btn").addEventListener("click", () => $("#sidebar").classList.toggle("open"));

function showView(name) {
  if (name !== "plan") openSeq++;  // leaving the plan page cancels a plan still loading
  document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== `view-${name}`));
  document.querySelectorAll(".nav [data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  $("#page-title").textContent = PAGES[name][0];
  $("#page-sub").textContent = PAGES[name][1];
  if (name === "plan" && currentPlan) setPlanTitle(currentPlan);
  $("#btn-new-plan").hidden = !(name === "plan" && currentPlan);
  $("#sidebar").classList.remove("open");
  const loaders = { history: loadHistory, rules: loadRules, meta: loadMeta, monitor: loadMonitor, activity: loadActivity,
                    today: loadToday, billing: loadBilling, account: loadAccount };
  if (loaders[name] && tokens.access) Promise.resolve().then(() => loaders[name]()).catch((e) => toast(e.message, true));
}

// ---------- init ----------

const VIEWS = ["today", "plan", "history", "rules", "monitor", "meta", "activity", "billing", "account"];
function viewFromHash() {
  const v = location.hash.slice(1);
  showView(VIEWS.includes(v) ? v : "today");
}
window.addEventListener("hashchange", viewFromHash);

async function init() {
  if (!tokens.access && !(await tryRefresh())) { showLogin(); return; }
  let me = await rawApi("/api/users/me");
  if (me.res.status === 401 && (await tryRefresh())) me = await rawApi("/api/users/me");
  if (!me.res.ok) { tokens.clear(); showLogin(); return; }
  $("#who").textContent = me.data.email || "";
  ME = me.data;
  document.body.classList.toggle("team", !ME.is_superuser);
  const meta = await api("/api/meta");
  $("#country").innerHTML = Object.entries(meta.countries)
    .map(([code, name]) => `<option value="${code}" ${code === "US" ? "selected" : ""}>${name}</option>`)
    .join("");
  CATEGORIES = meta.categories || Object.entries(meta.niches).map(([key, label]) => ({ key, label, group: "Products" }));
  const groups = [...new Set(CATEGORIES.map((c) => c.group))];
  $("#niche").innerHTML = groups.map((g) => `<optgroup label="${esc(g)}">${CATEGORIES.filter((c) => c.group === g)
    .map((c) => `<option value="${esc(c.key)}" ${c.key === meta.default_niche ? "selected" : ""}>${esc(c.label)}</option>`).join("")}</optgroup>`).join("");
  updateNoteField();
  const fxi = meta.fx || {};
  const when = fxi.updated ? new Date(fxi.updated).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
  $("#rate").textContent = fxi.source === "live" ? `Today's rate: 1 USD = ${(+meta.pkr_per_usd).toFixed(2)} PKR · updated ${when}`
    : fxi.source === "saved" ? `1 USD = ${(+meta.pkr_per_usd).toFixed(2)} PKR · last known rate, ${when} (the live rate could not be reached)`
    : `1 USD = ${meta.pkr_per_usd} PKR (fixed rate from settings)`;
  PKR_PER_USD = meta.pkr_per_usd;
  await loadBrandOptions().catch(() => {});
  if (currentPlan) fillForm(currentPlan.inputs);
  updateConversions();
  viewFromHash();
  if (location.hash.slice(1) === "plan" && !currentPlan) liveEstimate();
  if (!currentPlan) prefillBrief();
  const ms = await api("/api/meta/settings");
  $("#meta-dot").classList.toggle("on", ms.connected);
  refreshAlertBadge();
  if (!init.badgeTimer) init.badgeTimer = setInterval(refreshAlertBadge, 5 * 60 * 1000);  // once, not per login
}

async function refreshAlertBadge() {
  if (!tokens.access || !$("#login-screen").hidden) return;
  try {
    const n = (await api("/api/alerts?status=open")).length;
    const b = $("#alert-count");
    b.textContent = n; b.hidden = n === 0;
  } catch { /* ignore */ }
}
init().catch((e) => toast(e.message, true));

// ---------- estimate ----------

// The estimate follows the form: no button to press.
let estTimer, estSeq = 0;
$("#plan-form").addEventListener("input", () => {
  if (currentPlan) startFromCurrentInputs();
  updateConversions();
  clearTimeout(estTimer);
  estTimer = setTimeout(liveEstimate, 500);
});
// cash on delivery is a Pakistan thing: the box shows only for PK plans
function updateCodLine() {
  const f = $("#plan-form"), pk = f.elements.country.value === "PK";
  $("#cod-line").hidden = !pk;
  if (!pk) f.elements.cod.checked = false;
}
$("#plan-form").addEventListener("change", (ev) => {
  if (ev.target.name === "niche") onCategoryChange();
  if (ev.target.name === "country") updateCodLine();
  clearTimeout(estTimer); estTimer = setTimeout(liveEstimate, 150);
});
$("#plan-form").elements.price_usd.addEventListener("input", (ev) => { ev.target.dataset.touched = "1"; });

let CATEGORIES = [];
// "Other" needs the user's words; for the rest they are an optional extra for the AI.
function updateNoteField() {
  const other = $("#plan-form").elements.niche.value === "other";
  const input = $("#plan-form").elements.category_note;
  input.required = other;
  $("#note-label").innerHTML = other ? "What is it? <em class=\"opt req\">needed for Other</em>" : "What is it exactly? <em class=\"opt\">optional</em>";
  input.placeholder = other ? "e.g. prayer mats, phone cases, car accessories" : "e.g. stainless steel cutlery for weddings and gifts";
}
// A new category suggests its usual price, unless the user already typed one.
function onCategoryChange() {
  updateNoteField();
  const f = $("#plan-form");
  const c = CATEGORIES.find((x) => x.key === f.elements.niche.value);
  if (c && c.typical_price && !f.elements.price_usd.dataset.touched) {
    f.elements.price_usd.value = c.typical_price;
    updateConversions();
  }
}

function setFormLocked(locked) {
  [...$("#plan-form").elements].forEach((el) => (el.disabled = locked));
  $("#plan-form").title = locked ? "Save or cancel the plan edit first" : "";
}

function startFromCurrentInputs() {
  currentPlan = null;
  $("#plan-card").hidden = true;
  $("#btn-new-plan").hidden = true;
  $("#page-title").textContent = PAGES.plan[0];
  $("#page-sub").textContent = PAGES.plan[1];
}

async function liveEstimate() {
  const f = $("#plan-form");
  const ok = ["budget_pkr", "price_usd", "duration_days", "category_note"].every((k) => f.elements[k].checkValidity());
  if (!ok || !f.elements.country.value) return;
  const seq = ++estSeq;
  $("#est-updating").textContent = "Updating";
  try {
    const e = await api("/api/estimate", { method: "POST", body: { ...formInputs(), product: formInputs().product || "Jacket" } });
    if (seq === estSeq) renderEstimate(e);
  } catch { /* the form shows its own validation */ }
  finally { if (seq === estSeq) $("#est-updating").textContent = ""; }
}

let PKR_PER_USD = 280;
function updateConversions() {
  const f = $("#plan-form");
  const b = +f.elements.budget_pkr.value, p = +f.elements.price_usd.value, d = +f.elements.duration_days.value;
  $("#conv-budget").textContent = b > 0 ? `About ${fmt.usd(b / PKR_PER_USD)}${d > 0 ? `, or ${fmt.pkr(b / d)} a day` : ""}` : "";
  $("#conv-price").textContent = p > 0 ? `About ${fmt.pkr(p * PKR_PER_USD)}` : "";
}

function renderEstimate(e) {
  $("#estimate-card").hidden = false;
  $("#estimate-empty").hidden = true;
  $("#est-low").textContent = e.orders_low;
  $("#est-high").textContent = e.orders_high;
  $("#est-expected").textContent = e.orders_expected;
  $("#est-range").innerHTML = rangeBar(e.orders_low, e.orders_high, e.orders_expected);
  $("#est-confidence").textContent = { high: "high confidence", medium: "fair confidence", low: "rough guess" }[e.confidence] || `${e.confidence} confidence`;
  $("#est-source").textContent = String(e.source).startsWith("account_history") ? "from your Meta history" : "from benchmarks";
  $("#est-usd").innerHTML = `${fmt.usd(e.budget_usd)}<small>${fmt.pkr(e.budget_pkr)}</small>`;
  $("#est-daily").innerHTML = `${fmt.usd(e.daily_usd)}<small>per day</small>`;
  $("#est-cpa").innerHTML = `${fmt.usd(e.cpa_usd)}<small>${fmt.pkr(e.cpa_pkr)}</small>`;
  $("#est-cpm").innerHTML = `${fmt.usd(e.cpm_usd)}<small>CPM</small>`;
  $("#est-ctr").innerHTML = `${fmt.pct(e.ctr)}<small>CTR</small>`;
  $("#est-cvr").innerHTML = `${fmt.pct(e.cvr)}<small>conversion</small>`;
  const hasProfit = e.profit_usd !== null && e.profit_usd !== undefined;
  $("#est-profit").hidden = !hasProfit;
  if (hasProfit) {
    $("#est-margin").innerHTML = `${fmt.usd(e.margin_usd)}<small>after cost and shipping</small>`;
    $("#est-breakeven").innerHTML = `${fmt.usd(e.break_even_cpa_usd)}<small>${e.cpa_usd > e.break_even_cpa_usd ? "estimate is above it" : "estimate is below it"}</small>`;
    const p = e.profit_usd;
    $("#est-profit-v").innerHTML = `<span class="${p < 0 ? "neg" : "pos"}">${p < 0 ? "−" : ""}${fmt.usd(Math.abs(p))}</span><small>${fmt.usd(e.profit_low_usd)} to ${fmt.usd(e.profit_high_usd)}</small>`;
  }
  const se = e.season || {};
  $("#est-season").innerHTML = se.label
    ? `<span class="tag ${se.label === "peak season" ? "ahead" : se.label === "off season" ? "behind" : "on_track"}">${esc(se.label)}</span> ${esc(se.start)} to ${esc(se.end)} · Meta prices ×${se.cpm} · demand ×${se.cvr}`
    : "";
  $("#est-warnings").innerHTML = e.warnings.map((w) => `<li>${icon("alert")}<span>${esc(w)}</span></li>`).join("");
}

// ---------- generate ----------

$("#plan-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = $("#btn-generate");
  btn.disabled = true; btn.innerHTML = `<span class="spin"></span>Writing and checking the plan, about a minute`;
  try {
    const plan = await api("/api/plans", { method: "POST", body: formInputs() });
    renderEstimate(plan.estimate);
    renderPlan(plan);
    $("#btn-new-plan").hidden = false;
    $("#plan-card").scrollIntoView({ behavior: "smooth", block: "start" });
    toast("Plan ready. Read it, change anything you like, then approve.");
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; btn.textContent = "Create the plan"; }
});

// ---------- plan render ----------

function latest(plan) { return plan.versions[plan.versions.length - 1].content; }

function setPlanTitle(plan) {
  $("#page-title").textContent = `Plan #${plan.id}: ${plan.inputs.product}`;
  const store = BRANDS.length > 1 ? (BRANDS.find((b) => b.id === (plan.inputs.brand_id || 0))?.name || "a removed store") + " · " : "";
  $("#page-sub").textContent = `${store}${plan.inputs.country} · ${fmt.pkr(plan.inputs.budget_pkr)} over ${plan.inputs.duration_days} days`;
}

const NEXT_STEP = {
  draft: "Read the plan below. Change anything with Edit, then approve it.",
  approved: "Fix anything marked with a cross in the checklist, then create the campaign on Meta. It is created paused, so nothing spends yet.",
  launched: "The campaign is on Meta, paused. Switch it on in Ads Manager when you are ready; the Campaigns page then tracks it.",
  running: "The campaign is running. The Campaigns page checks it every few hours and emails you about problems.",
};

function renderPlan(plan) {
  currentPlan = plan;
  const c = latest(plan);
  setPlanTitle(plan);
  $("#btn-new-plan").hidden = false;
  const lastLaunch = (plan.launches || [])[plan.launches?.length - 1];
  const stage = plan.status === "launched" && lastLaunch && (lastLaunch.activated_at || lastLaunch.status === "active") ? "running" : plan.status;
  const order = ["draft", "approved", "launched", "running"];
  document.querySelectorAll("#plan-steps li").forEach((li) => {
    const i = order.indexOf(li.dataset.step), cur = order.indexOf(stage);
    li.className = i < cur ? "done" : i === cur ? "current" : "";
  });
  $("#plan-next").textContent = NEXT_STEP[stage] || "";
  $("#plan-card").hidden = false;
  $("#plan-id").textContent = `#${plan.id}`;
  const st = $("#plan-status");
  st.textContent = fmt.label(plan.status); st.className = `tag ${plan.status}`;
  $("#btn-approve").hidden = plan.status !== "draft";
  $("#btn-edit").hidden = plan.status === "launched" || plan.status === "launching";
  $("#btn-launch").hidden = plan.status !== "approved";
  $("#btn-delete-plan").hidden = ["launched", "launching"].includes(plan.status) || (plan.launches || []).length > 0;
  $("#plan-editor").hidden = true;
  setFormLocked(false);
  $("#plan-view").hidden = false;
  renderLaunch(plan);
  renderPolicy(plan);
  loadReadiness(plan);

  const li = (arr) => `<ul>${(arr || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`;
  const t = c.monitoring_targets || {};
  const totalDays = c.phases.reduce((n, p) => n + (+p.days || 0), 0) || 1;
  $("#plan-view").innerHTML = `
    <div class="plan-section">
      <p class="summary">${esc(c.summary)}</p>
      <p class="objective">Objective: ${esc(c.objective)}</p>
    </div>
    <div class="plan-section">
      <div class="section-head"><h3>Phases</h3><span class="muted small">${totalDays} days</span></div>
      <div class="phase-bar">${c.phases.map((p) => `<span style="flex:${+p.days || 1}" title="${esc(p.name)}"></span>`).join("")}</div>
      <div class="phases">${c.phases.map((p) => `
        <div class="phase"><div class="head"><span>${esc(p.name)}</span><span class="mono">${esc(p.days)} d · ${esc(p.budget_pct)}%</span></div>
        <p>${esc(p.goal)}</p></div>`).join("")}</div>
    </div>
    <div class="plan-section">
      <div class="section-head"><h3>Ad sets</h3><span class="muted small">share of daily budget</span></div>
      <table class="adsets"><tbody>${c.ad_sets.map((a) => `
        <tr><td>${esc(a.name)}</td>
        <td><div class="aud">${esc(a.audience)}</div><div class="meta">${esc(a.placements)} · optimise for ${esc(a.optimization)}</div></td>
        <td><div class="share"><i><b style="width:${Math.min(100, +a.daily_budget_pct || 0)}%"></b></i>${esc(a.daily_budget_pct)}%</div></td></tr>`).join("")}</tbody></table>
    </div>
    <div class="plan-section">
      <div class="section-head"><h3>Creatives</h3></div>
      <div class="creatives">${c.creatives.map((cr, i) => `
        <div class="creative"><div class="fmt">${i + 1} · ${esc(cr.format)}${cr.angle ? ` <span class="angle">${esc(cr.angle)}</span>` : ""}</div>
        <div class="hook">${esc(cr.hook)}</div>
        <div class="copy">${esc(cr.primary_text)}</div>
        <div class="foot"><span>${esc(cr.headline)}</span><span class="cta">${esc(cr.cta)}</span></div></div>`).join("")}</div>
    </div>
    <div class="plan-section">
      <div class="section-head"><h3>Targets the app checks</h3><span class="muted small">you get an alert when a campaign misses one</span></div>
      <dl class="targets">
        <div class="target"><dt>Ad cost per order, at most</dt><dd>${fmt.usd(t.max_cpa_usd)}</dd><small>CPA</small></div>
        <div class="target"><dt>Click rate, at least</dt><dd>${fmt.pct(t.min_ctr)}</dd><small>CTR</small></div>
        <div class="target"><dt>Times one person sees the ad, at most</dt><dd>${esc(t.max_frequency)}</dd><small>frequency</small></div>
        <div class="target"><dt>Sales per $1 of ads, at least</dt><dd>$${esc(t.min_roas)}</dd><small>ROAS</small></div>
      </dl>
    </div>
    <div class="plan-section">
      <div class="rules-cols">
        <div><h4>Stop when</h4>${li(c.kill_rules)}</div>
        <div><h4>Scale when</h4>${li(c.scale_rules)}</div>
        <div><h4>Risks</h4>${li(c.risks)}</div>
      </div>
    </div>`;

  loadCreatives(plan).catch((e) => toast(e.message, true));

  $("#version-list").innerHTML = plan.versions.map((v) =>
    `<li>v${v.version} · ${v.source === "ai" ? "generated" : "edited"} · ${utc(v.created_at).toLocaleString()}${v.note ? ` · ${esc(v.note)}` : ""}</li>`
  ).join("");
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
}

// ---------- edit ----------

const EDITABLE = ["summary", "objective", "phases", "ad_sets", "creatives", "kill_rules", "scale_rules", "monitoring_targets", "risks"];
const SECTION_LABELS = {
  summary: "Summary", objective: "Objective", phases: "Phases", ad_sets: "Ad sets (audiences)", creatives: "Ads (creatives)",
  kill_rules: "Stop an ad set when", scale_rules: "Give more budget when", monitoring_targets: "Targets", risks: "Risks",
};
const FIELD_LABELS = {
  name: "Name", days: "Days", budget_pct: "Share of budget %", goal: "Goal", audience: "Who sees it", placements: "Where it shows",
  daily_budget_pct: "Share of daily budget %", optimization: "Optimise for", format: "Format", hook: "Hook (first line)",
  primary_text: "Ad text", headline: "Headline", cta: "Button", visual_prompt: "Image brief", max_cpa_usd: "Ad cost per order, at most (USD)",
  min_ctr: "Click rate, at least (0.01 = 1%)", max_frequency: "Times one person sees the ad, at most", min_roas: "Sales per $1 of ads, at least",
};
const LONG_FIELDS = new Set(["primary_text", "visual_prompt", "audience", "goal"]);
const label = (k) => FIELD_LABELS[k] || fmt.label(k).replace(/^./, (c) => c.toUpperCase());

// Fields of each list item, so an empty list can still get a correctly shaped new item.
const ITEM_FIELDS = {
  phases: { name: "", days: 0, budget_pct: 0, goal: "" },
  ad_sets: { name: "", audience: "", placements: "Advantage+ placements", daily_budget_pct: 0, optimization: "Purchase" },
  creatives: { format: "Static image 1:1", angle: "", hook: "", primary_text: "", headline: "", cta: "SHOP_NOW", visual_prompt: "" },
};
const LIST_SECTIONS = new Set(Object.keys(ITEM_FIELDS));
const LINE_SECTIONS = new Set(["kill_rules", "scale_rules", "risks"]);
const isScalar = (x) => x === null || ["string", "number"].includes(typeof x);

function fieldInput(path, key, val) {
  const name = esc(path);
  if (typeof val === "number") return `<label class="field"><span>${esc(label(key))}</span><input type="number" step="any" data-path="${name}" data-type="number" value="${esc(val)}" /></label>`;
  if (LONG_FIELDS.has(key) || String(val ?? "").length > 70) return `<label class="field wide"><span>${esc(label(key))}</span><textarea rows="2" data-path="${name}">${esc(val ?? "")}</textarea></label>`;
  return `<label class="field"><span>${esc(label(key))}</span><input type="text" data-path="${name}" value="${esc(val ?? "")}" /></label>`;
}

function sectionEditor(k, v) {
  if (LIST_SECTIONS.has(k)) {
    const items = Array.isArray(v) ? v.filter((x) => x && typeof x === "object") : [];
    return `<div class="items" data-list="${k}">${items.map((item, i) => itemEditor(k, i, item)).join("")}</div>
      <button type="button" class="btn small" data-add="${k}">${icon("plus")}Add another</button>`;
  }
  if (LINE_SECTIONS.has(k)) {
    const lines = (Array.isArray(v) ? v : []).filter((x) => typeof x === "string");
    return `<textarea rows="${Math.max(3, lines.length + 1)}" data-path="${k}" data-type="lines">${esc(lines.join("\n"))}</textarea><small class="hint">One per line</small>`;
  }
  if (v && typeof v === "object") return `<div class="item-grid">${Object.entries(v).filter(([, x]) => isScalar(x)).map(([f, x]) => fieldInput(`${k}.${f}`, f, x)).join("")}</div>`;
  return `<textarea rows="${k === "summary" ? 3 : 2}" data-path="${k}">${esc(v ?? "")}</textarea>`;
}

function itemEditor(k, i, item, isNew = false) {
  const fields = { ...ITEM_FIELDS[k], ...item };
  return `<div class="item" data-index="${esc(i)}">
    <div class="item-head"><b>${esc(item.name || item.format || (isNew ? `New ${SECTION_LABELS[k].split(" ")[0].toLowerCase().replace(/s$/, "")}` : `${SECTION_LABELS[k]} ${+i + 1}`))}</b>
      <button type="button" class="btn small ghost" data-remove>${icon("x")}Remove</button></div>
    <div class="item-grid">${Object.entries(fields).filter(([, x]) => isScalar(x)).map(([f, x]) => fieldInput(`${k}.${i}.${f}`, f, x)).join("")}</div></div>`;
}

$("#btn-edit").addEventListener("click", () => {
  const c = latest(currentPlan);
  $("#editor-sections").innerHTML = EDITABLE.map((k) => `
    <section class="edit-section" data-section="${k}"><h3>${esc(SECTION_LABELS[k])}</h3>${sectionEditor(k, c[k])}</section>`).join("");
  $("#edit-note").value = "";
  $("#plan-view").hidden = true;
  $("#creatives-panel").hidden = true;
  $("#plan-editor").hidden = false;
  setFormLocked(true);
  $("#plan-editor").scrollIntoView({ behavior: "smooth", block: "start" });
});

$("#editor-sections").addEventListener("click", (ev) => {
  const rm = ev.target.closest("[data-remove]");
  if (rm) { rm.closest(".item").remove(); return; }
  const add = ev.target.closest("[data-add]");
  if (add) {
    const k = add.dataset.add;
    const list = document.querySelector(`[data-list="${k}"]`);
    list.insertAdjacentHTML("beforeend", itemEditor(k, `n${list.children.length}`, { ...ITEM_FIELDS[k] }, true));
    list.lastElementChild.querySelector("input, textarea")?.focus();
  }
});

// Read the form back into plan content. Throws when a number is left empty.
function numberOf(el) {
  if (el.value.trim() === "" || !Number.isFinite(+el.value)) {
    el.focus();
    throw new Error(`"${el.closest(".field")?.querySelector("span")?.textContent || "A number"}" needs a number`);
  }
  return +el.value;
}

function collectEdits() {
  const c = JSON.parse(JSON.stringify(latest(currentPlan)));
  for (const k of EDITABLE) {
    const sec = document.querySelector(`[data-section="${k}"]`);
    const list = sec.querySelector(`[data-list="${k}"]`);
    if (list) {
      c[k] = [...list.querySelectorAll(".item")].map((item) => {
        const o = {};
        item.querySelectorAll("[data-path]").forEach((el) => {
          const f = el.dataset.path.split(".").pop();
          o[f] = el.dataset.type === "number" ? numberOf(el) : el.value.trim();
        });
        return o;
      });
      continue;
    }
    sec.querySelectorAll("[data-path]").forEach((el) => {
      const parts = el.dataset.path.split(".");
      const val = el.dataset.type === "number" ? numberOf(el)
        : el.dataset.type === "lines" ? el.value.split("\n").map((x) => x.trim()).filter(Boolean) : el.value.trim();
      if (parts.length === 1) c[k] = val; else c[k][parts[1]] = val;
    });
  }
  return c;
}

$("#btn-cancel-edit").addEventListener("click", () => renderPlan(currentPlan));

$("#btn-save-edit").addEventListener("click", async () => {
  let content;
  try { content = collectEdits(); } catch (e) { return toast(e.message, true); }
  if (!content.ad_sets?.length) return toast("Keep at least one ad set.", true);
  const share = (content.ad_sets || []).reduce((n, a) => n + (+a.daily_budget_pct || 0), 0);
  if (content.ad_sets?.length && Math.abs(share - 100) > 1 &&
      !(await ask("Budget shares do not add up", `The ad sets' shares of the daily budget add up to ${share}%, not 100%. The app will scale them to 100% when it creates the campaign. Save anyway?`, "Save anyway"))) return;
  const btn = $("#btn-save-edit");
  btn.disabled = true;
  try {
    const plan = await api(`/api/plans/${currentPlan.id}/edit`, {
      method: "POST", body: { content, note: $("#edit-note").value },
    });
    renderPlan(plan);
    const n = plan.learned_rules?.length || 0;
    toast(n ? `Saved. The app learned ${n} rule${n > 1 ? "s" : ""} from this for next time.` : "Saved as a new version.");
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; }
});

$("#btn-approve").addEventListener("click", async () => {
  const btn = $("#btn-approve");
  if (btn.disabled) return;
  btn.disabled = true;
  try {
    renderPlan(await api(`/api/plans/${currentPlan.id}/approve`, { method: "POST" }));
    toast("Approved. Next: check the list and create the campaign on Meta.");
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; }
});
// ---------- creatives (AI images) ----------

async function loadCreatives(plan) {
  const panel = $("#creatives-panel");
  panel.hidden = false;
  if (panel.dataset.plan !== String(plan.id)) {
    $("#creative-slots").innerHTML = `<p class="muted">Loading images</p>`;
    $("#product-photo-preview").innerHTML = icon("image");
    $("#brand-style").value = "";
    panel.dataset.plan = String(plan.id);
  }
  const [st, rows] = await Promise.all([api(`/api/creatives/status?brand_id=${plan.inputs.brand_id || 0}`), api(`/api/plans/${plan.id}/creatives`)]);
  if (currentPlan?.id !== plan.id) return;  // another plan was opened meanwhile
  $("#creatives-status").textContent = st.configured
    ? `Generated with ${st.provider}` : `Generation is off: ${st.hint || "set IMAGE_API_KEY in .env"}. Uploads still work`;
  $("#brand-style").value = st.brand_style || "";
  $("#product-photo-preview").innerHTML = st.product_photo
    ? `<img src="${esc(st.product_photo)}" alt="Reference product photo" />`
    : icon("image");

  const c = latest(plan);
  const byIdx = {};
  rows.forEach((r) => (byIdx[r.creative_index] ||= []).push(r));
  $("#creative-slots").innerHTML = c.creatives.map((cr, i) => `
    <div class="slot" data-idx="${i}">
      <div class="head">
        <div><b>Creative ${i + 1}</b><span class="fmt">${esc(cr.format)}</span></div>
        <div class="toolbar">
          <select class="aspect" aria-label="Aspect ratio">
            <option value="">Auto shape</option><option value="1:1">1:1</option><option value="4:5">4:5</option><option value="9:16">9:16</option>
          </select>
          <input type="file" class="upload" accept="image/*" hidden />
          <button class="btn small upload-btn" type="button">${icon("upload")}Upload</button>
          <button class="btn small primary gen" type="button" ${st.configured ? "" : "disabled"}>${/carousel/i.test(cr.format) ? "Add a card" : "Generate"}</button>
        </div>
      </div>
      <div class="prompt">${esc(cr.visual_prompt || "This plan version has no image brief. Regenerate the plan to get one")}</div>
      ${/carousel/i.test(cr.format) ? (() => { const n = (byIdx[i] || []).filter((r) => r.selected).length;
        return `<p class="carousel-hint ${n >= 2 ? "ok" : ""}">Carousel: every image marked <b>Use</b> becomes a card, in this order (2 to 10). ${n} card${n === 1 ? "" : "s"} now${n < 2 ? ", add at least " + (2 - n) + " more" : ""}.</p>`; })() : ""}
      <div class="thumbs">${(byIdx[i] || []).map(thumb).join("") || `<span class="muted small">No images yet</span>`}</div>
    </div>`).join("");

  panel.querySelectorAll(".slot").forEach((slot) => {
    const idx = +slot.dataset.idx;
    slot.querySelector(".gen").addEventListener("click", async (ev) => {
      const b = ev.currentTarget; b.disabled = true; b.textContent = "Generating, about a minute";
      try {
        await api(`/api/plans/${plan.id}/creatives/generate`, { method: "POST",
          body: { creative_index: idx, aspect: slot.querySelector(".aspect").value || null } });
        toast("Image generated."); await loadCreatives(plan);
      } catch (e) { toast(e.message, true); b.disabled = false; b.textContent = /carousel/i.test(c.creatives[idx]?.format) ? "Add a card" : "Generate"; }
    });
    const fileInput = slot.querySelector(".upload");
    slot.querySelector(".upload-btn").addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", async () => {
      if (!fileInput.files[0]) return;
      try {
        await upload(`/api/plans/${plan.id}/creatives/upload?creative_index=${idx}`, fileInput.files[0]);
        toast("Uploaded."); await loadCreatives(plan);
      } catch (e) { toast(e.message, true); }
    });
  });
  panel.querySelectorAll("[data-toggle]").forEach((b) => b.addEventListener("click", async () => {
    try { await api(`/api/creatives/${b.dataset.toggle}/toggle`, { method: "POST" }); await loadCreatives(plan); }
    catch (e) { toast(e.message, true); }
  }));
  panel.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!(await ask("Delete this image?", "It is removed from this plan. Ads already on Meta keep it.", "Delete", true))) return;
    try { await api(`/api/creatives/${b.dataset.del}`, { method: "DELETE" }); await loadCreatives(plan); }
    catch (e) { toast(e.message, true); }
  }));
}

function thumb(r) {
  return `<div class="thumb ${r.selected ? "selected" : ""}" title="${esc(r.prompt || "")}">
    <img src="${esc(r.url)}" alt="Ad image ${esc(r.format)}" loading="lazy" />
    <div class="bar"><span>${esc(r.format)}${r.meta_image_hash ? " · on Meta" : ""}</span>
      <span><button data-toggle="${r.id}">${r.selected ? "In use" : "Use"}</button><button class="del" data-del="${r.id}" aria-label="Delete image">${icon("x")}</button></span></div>
  </div>`;
}

$("#product-photo").addEventListener("change", async () => {
  const f = $("#product-photo").files[0]; if (!f) return;
  try {
    await upload(`/api/creatives/product-photo?brand_id=${currentPlan?.inputs.brand_id || 0}`, f);
    toast("Product photo saved."); if (currentPlan) loadCreatives(currentPlan).catch((e) => toast(e.message, true));
  } catch (e) { toast(e.message, true); }
});

$("#btn-brand-save").addEventListener("click", async () => {
  try { await api(`/api/creatives/brand-style?brand_id=${currentPlan?.inputs.brand_id || 0}`, { method: "POST", body: { brand_style: $("#brand-style").value } }); toast("Brand style saved for this store."); }
  catch (e) { toast(e.message, true); }
});

// ---------- readiness ----------

async function loadReadiness(plan) {
  const box = $("#readiness");
  if (plan.status !== "approved") { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = `<span class="muted">Checking launch readiness</span>`;
  try {
    const r = await api(`/api/plans/${plan.id}/readiness`);
    if (currentPlan?.id !== plan.id) return;
    box.innerHTML = `<div class="panel-title">${r.ready ? "Ready to launch" : "Not ready to launch"}</div>` + r.checks.map((c) => `
      <div class="check ${c.ok ? "ok" : c.hard ? "fail" : "warn"}">${icon(c.ok ? "check" : c.hard ? "x" : "alert")}<span>${esc(c.text)}</span></div>`).join("");
    $("#btn-launch").disabled = !r.ready;
    $("#btn-launch").title = r.ready ? "" : "Fix the items marked with a cross first";
  } catch (e) {
    if (currentPlan?.id !== plan.id) return;
    box.innerHTML = `<span style="color:var(--neg)">${esc(e.message)}</span>`;
    $("#btn-launch").disabled = false;  // the server checks again on launch
  }
}

// ---------- launch ----------

function renderLaunch(plan) {
  const box = $("#launch-info");
  const l = plan.launches && plan.launches[plan.launches.length - 1];
  if (!l) { box.hidden = true; return; }
  box.hidden = false;
  const adsets = l.adsets || [], ads = l.ads || [], notes = l.notes || [];
  const live = l.activated_at || l.status === "active";
  box.innerHTML = `
    <div class="panel-title">${live ? "Running on Meta" : `On Meta, ${esc(fmt.label(l.status || "paused"))}`}
      ${l.ads_manager_url ? `<a class="btn small ghost" href="${esc(l.ads_manager_url)}" target="_blank" rel="noopener">Open in Ads Manager ${icon("external")}</a>` : ""}</div>
    <div>Campaign <code>${esc(l.campaign_id)}</code> · ${adsets.length} ad sets · ${ads.length} ads</div>
    ${adsets.length ? `<div class="muted">Daily budgets: ${adsets.map((a) => `${esc(a.name)} ${esc(a.daily_budget ?? "")} ${esc(a.currency || "")}`).join(" · ")}</div>` : ""}
    ${notes.length ? `<ul>${notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}`;
}

$("#btn-launch").addEventListener("click", async () => {
  if (!(await ask("Create the campaign on Meta?", "The campaign, its ad sets and ads are created PAUSED. Nothing spends until you switch it on in Ads Manager.", "Create, paused"))) return;
  const btn = $("#btn-launch");
  const plan = currentPlan;
  btn.disabled = true; btn.innerHTML = `<span class="spin"></span>Creating on Meta`;
  try {
    const out = await api(`/api/plans/${plan.id}/launch`, { method: "POST" });
    if (currentPlan?.id === plan.id) renderPlan(out);
    toast("Created on Meta in paused state.");
  } catch (e) { toast(e.message, true); }
  finally {
    btn.textContent = "Create on Meta, paused";
    if (currentPlan?.id === plan.id && currentPlan.status === "approved") loadReadiness(currentPlan).catch(() => {});
  }
});

// ---------- monitor tab ----------

async function loadMonitor() {
  const [ov, alerts] = await Promise.all([
    api("/api/monitor/overview"),
    api(`/api/alerts?status=${$("#alert-filter").value}`),
  ]);
  const st = ov.state;
  $("#monitor-state").textContent = st.last_run
    ? `Last check ${new Date(st.last_run + "Z").toLocaleString()}${st.last_result?.errors?.length ? " · errors: " + st.last_result.errors.join("; ") : ""}${st.last_result?.note ? " · " + st.last_result.note : ""}`
    : "No check has run yet. Checks run every few hours once a plan is launched";
  renderAlerts(alerts);
  renderLaunches(ov.launches);
  refreshAlertBadge();
  loadAutomation().catch((e) => toast(e.message, true));
  loadActionLog().catch((e) => toast(e.message, true));
}

function renderAlerts(alerts) {
  const box = $("#alert-list");
  if (!alerts.length) { box.innerHTML = `<p class="alert-empty">${icon("check")}All good. Nothing needs attention</p>`; return; }
  box.innerHTML = alerts.map((a) => {
    const diag = a.suggestions.find((s) => s.diagnosis);
    const opts = a.suggestions.filter((s) => s.action);
    const m = a.metrics || {};
    const stats = ["spend_usd", "impressions", "clicks", "purchases", "ctr", "cpa_usd", "frequency", "roas"]
      .filter((k) => m[k] !== undefined)
      .map((k) => `${k.replace("_usd", "")} ${k === "ctr" ? fmt.pct(m[k]) : k.endsWith("_usd") ? fmt.usd(m[k]) : typeof m[k] === "number" ? fmt.int(m[k]) : m[k]}`)
      .join(" · ");
    const btn = (label, action) => `<button class="btn small ghost" data-id="${a.id}" data-action="${action}">${label}</button>`;
    return `<div class="alert ${a.severity}">
      <span class="sev" title="${a.severity}"></span>
      <div>
        <div class="head">
          <div>
            <div class="msg">${esc(a.message)}</div>
            <div class="meta">${esc(a.product)} · ${a.object_level} ${esc(a.object_name)} · ${fmt.label(a.kind)} · seen ${a.times_seen}× · ${utc(a.updated_at).toLocaleString()}</div>
            <div class="meta figures">${esc(stats)}</div>
          </div>
          <div class="toolbar">
            ${a.status === "open" ? btn("Seen it", "acknowledge") : ""}
            ${a.status !== "resolved" ? btn("Mark fixed", "resolve") : btn("Reopen", "reopen")}
          </div>
        </div>
        ${diag ? `<div class="diag">${esc(diag.diagnosis)}</div>` : ""}
        ${opts.length ? `<ol>${opts.map((o, i) => `<li><div>${esc(o.action)}<span class="eff">${esc(o.expected_effect)} <span class="risk">· risk: ${esc(o.risk)}</span></span></div>${o.spec && a.status !== "resolved" ? `<button class="btn small owner-only" data-apply="${a.id}" data-idx="${i}" title="${esc(specLabel(o.spec))}">Do this</button>` : "<span></span>"}</li>`).join("")}</ol>` : ""}
      </div>
    </div>`;
  }).join("");
  box.querySelectorAll("button[data-action]").forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      try { await api(`/api/alerts/${b.dataset.id}/${b.dataset.action}`, { method: "POST" }); loadMonitor(); refreshAlertBadge(); }
      catch (e) { toast(e.message, true); b.disabled = false; }
    })
  );
  box.querySelectorAll("button[data-apply]").forEach((b) =>
    b.addEventListener("click", async () => {
      if (!(await ask("Make this change on Meta now?", `${b.title}. You can undo it from the action log.`, "Apply"))) return;
      b.disabled = true;
      try {
        const r = await api(`/api/alerts/${b.dataset.apply}/apply`, { method: "POST", body: { option_index: +b.dataset.idx } });
        toast(`Done: ${r.description}`);
        loadMonitor().catch((e) => toast(e.message, true));
      } catch (e) { toast(e.message, true); b.disabled = false; }
    })
  );
}

function specLabel(s) {
  if (s.type === "pause") return `Pause ${s.level} ${s.object_id}`;
  if (s.type === "resume") return `Resume ${s.level} ${s.object_id}`;
  if (s.type === "budget_pct") return `Change ${s.level} ${s.object_id} daily budget by ${s.pct > 0 ? "+" : ""}${s.pct}%`;
  if (s.type === "move_budget") return `Move ${s.pct}% of ad set ${s.object_id} daily budget to ad set ${s.to}; the total stays the same`;
  if (s.type === "clone_winner") return `Clone winning ad ${s.object_id} into the other ad sets (paused)`;
  if (s.type === "refresh_creative") return `Generate a fresh AI image for ad ${s.object_id} and add it as a paused ad (costs one image)`;
  return "";
}

// ---------- automation + action log ----------

const KIND_LABELS = {
  rebalance: "Weak and strong ad sets → move 20% of the weak one's budget to the strong one",
  no_purchases: "Spend with 0 purchases → pause the ad set / ad",
  high_cpa: "CPA above target → cut budget 30% (ad set) / pause (ad)",
  low_ctr: "Low CTR → pause the ad",
  high_frequency: "High frequency → cut ad set budget 20%",
  low_roas: "Low ROAS → cut ad set budget 20%",
  scale_candidate: "Scale candidate → raise ad set budget 20%",
  creative_loser: "Creative test loser → pause the ad",
  creative_winner: "Creative test winner → clone it into the other ad sets (paused)",
};

async function loadAutomation() {
  const a = await api("/api/automation");
  $("#auto-kinds").innerHTML = a.kinds.map((k) => `
    <label class="toggle"><input type="checkbox" data-kind="${k}" ${a.enabled[k] ? "checked" : ""} /><span>${esc(KIND_LABELS[k] || k)}</span></label>`).join("");
  $("#auto-max-pct").value = a.max_budget_pct;
}

$("#btn-auto-save").addEventListener("click", async () => {
  const enabled = {};
  document.querySelectorAll("#auto-kinds input[data-kind]").forEach((c) => (enabled[c.dataset.kind] = c.checked));
  const on = Object.values(enabled).filter(Boolean).length;
  if (on && !(await ask("Turn on automatic changes?", `For ${on} alert type${on > 1 ? "s" : ""}, the app will change your Meta campaigns itself, without asking first. Every change can be undone.`, "Turn on"))) return;
  try {
    await api("/api/automation", { method: "POST", body: { enabled, max_budget_pct: +$("#auto-max-pct").value } });
    toast("Automation saved.");
  } catch (e) { toast(e.message, true); }
});

async function loadActionLog() {
  const rows = await api("/api/actions");
  $("#action-log").innerHTML = rows.length ? rows.map((l) => `
    <li class="${l.ok ? "" : "bad"}"><span>${esc(l.description)}${l.error ? ` · ${esc(l.error)}` : ""}
      <span class="meta">${l.mode} · ${utc(l.created_at).toLocaleString()}${l.undone ? " · undone" : ""}</span></span>
      <span class="toolbar">${l.outcome ? `<span class="tag ${l.outcome.verdict}" title="${esc(l.outcome.note)}">${fmt.label(l.outcome.verdict)}</span>` : ""}
      ${l.ok && !l.undone && l.mode !== "undo" ? `<button class="btn small ghost owner-only" data-undo="${l.id}">${icon("undo")}Undo</button>` : ""}</span></li>`).join("")
    : `<li class="empty">No changes made yet</li>`;
  document.querySelectorAll("#action-log button[data-undo]").forEach((b) =>
    b.addEventListener("click", async () => {
      if (!(await ask("Undo this change?", "The ad set or ad goes back to how it was on Meta.", "Undo"))) return;
      try { await api(`/api/actions/${b.dataset.undo}/undo`, { method: "POST" }); toast("Reverted."); loadActionLog(); }
      catch (e) { toast(e.message, true); }
    })
  );
}

function renderLaunches(launches) {
  const box = $("#launch-list");
  if (!launches.length) { box.innerHTML = `<div class="card"><div class="card-title">Campaigns</div><p class="muted" style="margin:0">No launched campaigns yet. Approve a plan and launch it from its page</p></div>`; return; }
  box.innerHTML = launches.map((l) => {
    const t = l.targets || {};
    const cls = (m) => ({
      cpa: m.purchases && t.max_cpa_usd ? (m.cpa_usd > t.max_cpa_usd * 1.25 ? "bad" : m.cpa_usd <= t.max_cpa_usd * 0.8 ? "good" : "") : "",
      ctr: m.impressions >= 2000 && t.min_ctr ? (m.ctr < t.min_ctr ? "bad" : "") : "",
      freq: m.impressions >= 1000 && t.max_frequency ? (m.frequency > t.max_frequency ? "bad" : "") : "",
      roas: m.purchases >= 3 && t.min_roas ? (m.roas < t.min_roas ? "bad" : "good") : "",
    });
    const rows = (l.metrics || []).map((m) => { const c = cls(m); return `
      <tr class="${m.level}"><td>${esc(m.name)} <span class="sub">${esc(fmt.label(m.status).toLowerCase())}</span></td>
      <td class="num">${fmt.usd(m.spend_usd)}</td><td class="num">${fmt.int(m.impressions)}</td><td class="num">${fmt.int(m.clicks)}</td><td class="num">${m.purchases}</td>
      <td class="num ${c.ctr}">${fmt.pct(m.ctr)}</td><td class="num ${c.cpa}">${m.purchases ? fmt.usd(m.cpa_usd) : "–"}</td>
      <td class="num ${c.freq}">${m.frequency}</td><td class="num ${c.roas}">${m.purchases ? m.roas + "x" : "–"}</td></tr>`; }).join("");
    return `<div class="card flush launch-card">
      <div class="card-head padded">
        <div>
          <div class="card-title">${esc(l.product)} <span class="muted">to ${esc(l.country)}</span></div>
          <div class="toolbar"><span class="tag ${l.status}">${fmt.label(l.status)}</span>${l.open_alerts ? `<span class="tag warning">${l.open_alerts} open alerts</span>` : ""}</div>
        </div>
        <a class="btn small" href="${esc(l.ads_manager_url || "#")}" target="_blank" rel="noopener">Ads Manager ${icon("external")}</a>
      </div>
      <div style="padding:0 24px">
        ${l.outcome ? outcomeLine(l.outcome, l.launch_id) : ""}
        <div class="targets-line">Plan #${l.plan_id} · targets CPA ≤ ${fmt.usd(t.max_cpa_usd)} · CTR ≥ ${fmt.pct(t.min_ctr)} · frequency ≤ ${t.max_frequency || "–"} · ROAS ≥ ${t.min_roas || "–"}x${l.taken_at ? ` · last 7 days as of ${utc(l.taken_at).toLocaleString()}` : " · no data yet"}</div>
      </div>
      ${rows ? `<table class="table"><thead><tr><th>Campaign, ad set, ad</th><th class="num">Spend</th><th class="num">Impressions</th><th class="num">Clicks</th><th class="num">Orders</th><th class="num">CTR</th><th class="num">CPA</th><th class="num">Freq.</th><th class="num">ROAS</th></tr></thead><tbody>${rows}</tbody></table>` : ""}
    </div>`;
  }).join("");
}

function outcomeLine(o, launchId) {
  const pct = Math.min(100, Math.round((o.days_active / Math.max(o.duration_days, 1)) * 100));
  return `<div class="outcome">
    <div class="outcome-row">
      <div><div class="fig">${o.purchases}<small>orders, ${o.predicted_so_far} expected by now</small></div>
        <div class="toolbar" style="margin-top:6px"><span class="tag ${o.verdict}">${fmt.label(o.verdict)}</span><span class="muted small">${fmt.usd(o.spend_usd)} spent${o.revenue_usd ? ` · ${fmt.usd(o.revenue_usd)} revenue` : ""}</span></div></div>
      <div>${rangeBar(o.predicted_low, o.predicted_high, o.predicted_expected, o.purchases)}
        <div class="progress" title="Day ${o.days_active} of ${o.duration_days}"><div style="width:${pct}%"></div></div>
        <div class="hint">Day ${o.days_active} of ${o.duration_days} · plan ${o.predicted_low}–${o.predicted_high} orders</div></div>
      <div class="side">${o.completed ? `Completed${o.rules_added?.length ? `<br>${o.rules_added.length} rules learned` : ""}`
        : `<button class="btn small owner-only" data-complete="${launchId}">Close and learn</button>`}</div>
    </div>
    ${o.summary ? `<p class="outcome-summary">${esc(o.summary)}</p>` : ""}
  </div>`;
}

document.addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-complete]");
  if (!b) return;
  if (b.disabled) return;
  if (!(await ask("Close this campaign's tracking?", "Its results update the estimates and the learned rules, and the app stops checking it. The campaign itself is not changed on Meta.", "Close and learn"))) return;
  b.disabled = true;
  try { await api(`/api/launches/${b.dataset.complete}/complete`, { method: "POST" }); toast("Closed. Rules and calibration updated."); loadMonitor(); }
  catch (e) { toast(e.message, true); b.disabled = false; }
});

$("#alert-filter").addEventListener("change", loadMonitor);
$("#btn-monitor-run").addEventListener("click", async () => {
  const btn = $("#btn-monitor-run");
  btn.disabled = true;
  try {
    const r = await api("/api/monitor/run", { method: "POST" });
    toast(r.skipped ? "A check is already running; results appear in a minute." :
          r.note || (r.errors?.length ? r.errors[0] : `Checked ${r.checked?.length || 0} campaign(s).`), !!r.errors?.length);
    loadMonitor().catch((e) => toast(e.message, true));
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; }
});

// ---------- meta tab ----------

async function loadMeta() {
  const ms = await api("/api/meta/settings");
  const f = $("#meta-form");
  for (const [k, v] of Object.entries(ms.settings)) if (f.elements[k]) f.elements[k].value = v;
  $("#meta-dot").classList.toggle("on", ms.connected);
  loadStats().catch((e) => toast(e.message, true));
  loadNotify().catch((e) => toast(e.message, true));
  if (ME.is_superuser) loadBrandList().catch((e) => toast(e.message, true));
}

async function loadNotify() {
  const n = await api("/api/notify/status");
  $("#notify-status").innerHTML = n.configured
    ? `<div class="conn-ok">${icon("check")}<span><b>Working.</b> Alerts are sent${n.gmail_sender ? ` from <b>${esc(n.gmail_sender)}</b>` : ""} to ${esc(n.to)}</span></div>
       <p class="hint">New warnings and critical alerts go out after each check, plus a weekly summary. Nothing to set up</p>`
    : `<div class="conn-bad">${icon("alert")}<span><b>Not set up.</b> Set <code>ALERT_EMAIL</code> in the server's <code>.env</code>, then connect a Gmail below</span></div>`;
  $("#gmail-box").open = !n.configured;
  $("#btn-notify-test").disabled = !n.configured;
  $("#btn-digest").disabled = !n.configured;
  $("#gmail-state").innerHTML = n.gmail_can_connect ? ""
    : "Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in the server's .env first (the Social Agent's Google client works).";
  $('[data-connect="gmail"]').disabled = !n.gmail_can_connect;
  $('[data-disconnect="gmail"]').hidden = !n.gmail_in_app;
  const hf = await api("/api/higgsfield/status");
  $("#hf-state").innerHTML = (hf.connected ? `Connected${hf.email ? ` as ${esc(hf.email)}` : ""}. ` : "Not connected. ")
    + `Model ${esc(hf.settings)}` + (hf.in_use ? "" : ". Not in use: IMAGE_PROVIDER in .env is not higgsfield");
  $('[data-disconnect="higgsfield"]').hidden = !hf.connected;
}

document.addEventListener("click", async (ev) => {
  const c = ev.target.closest("[data-connect],[data-finish],[data-disconnect]");
  if (!c) return;
  const svc = c.dataset.connect || c.dataset.finish || c.dataset.disconnect;
  const box = document.querySelector(`.connect[data-svc="${svc}"]`);
  try {
    if (c.dataset.connect) {
      const { url } = await api(`/api/${svc}/connect`, { method: "POST" });
      const paste = box.querySelector(".paste");
      paste.hidden = false;
      paste.querySelector(".open-link")?.remove();
      paste.insertAdjacentHTML("afterbegin", `<p class="hint open-link">If no tab opened, <a href="${esc(url)}" target="_blank" rel="noopener">open the sign-in page</a>.</p>`);
      window.open(url, "_blank", "noopener");
    } else if (c.dataset.finish) {
      const r = await api(`/api/${svc}/complete`, { method: "POST", body: { url: box.querySelector(".paste input").value } });
      box.querySelector(".paste").hidden = true;
      box.querySelector(".paste input").value = "";
      toast(`${svc === "gmail" ? "Gmail" : "Higgsfield"} connected${r.email ? ": " + r.email : ""}.`);
      await loadNotify();
    } else {
      if (!(await ask(`Disconnect ${svc === "gmail" ? "this Gmail" : "Higgsfield"}?`, svc === "gmail" ? "Alerts go back to the Gmail set on the server." : "Image generation stops until you connect again.", "Disconnect", true))) return;
      await api(`/api/${svc}/disconnect`, { method: "POST" });
      await loadNotify();
    }
  } catch (e) { toast(e.message, true); }
});

$("#btn-digest").addEventListener("click", async () => {
  const btn = $("#btn-digest");
  btn.disabled = true;
  try { const r = await api("/api/notify/digest", { method: "POST" }); toast(`Digest sent: ${r.launches} campaign(s), ${r.actions} action(s).`); }
  catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; }
});

$("#btn-notify-test").addEventListener("click", async () => {
  const btn = $("#btn-notify-test");
  btn.disabled = true; btn.textContent = "Sending";
  try {
    const r = await api("/api/notify/test", { method: "POST" });
    toast(`Test email sent to ${r.to} via ${r.via}.`);
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; btn.innerHTML = `${icon("send")}Test email`; }
});


$("#meta-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const values = Object.fromEntries(new FormData($("#meta-form")).entries());
  try {
    await api("/api/meta/settings", { method: "POST", body: { values } });
    toast("Saved.");
    loadMeta().catch((e) => toast(e.message, true));
  } catch (e) { toast(e.message, true); }
});

$("#btn-meta-check").addEventListener("click", async () => {
  const out = $("#meta-status");
  out.textContent = "Checking";
  try {
    if (ME?.is_superuser) {
      const values = Object.fromEntries(new FormData($("#meta-form")).entries());
      await api("/api/meta/settings", { method: "POST", body: { values } });
    }
    const st = await api("/api/meta/status");
    const list = (arr, extra) => arr.length
      ? `<ul>${arr.map((x) => `<li><code>${esc(x.id)}</code> ${esc(x.name || "")}${extra ? extra(x) : ""}</li>`).join("")}</ul>`
      : `<p class="muted">None</p>`;
    out.innerHTML = `
      <p style="color:var(--text)">Token of ${esc(st.user.name)} <code>${esc(st.user.id)}</code></p>
      <h4>Ad accounts</h4>${list(st.accounts, (a) => ` · ${esc(a.currency)} · status ${esc(a.account_status)}`)}
      <h4>Pages</h4>${list(st.pages)}
      <h4>Pixels on this account</h4>${list(st.pixels)}`;
    $("#meta-dot").classList.add("on");
  } catch (e) { out.innerHTML = `<span style="color:var(--danger)">${esc(e.message)}</span>`; }
});

$("#btn-sync").addEventListener("click", async () => {
  const btn = $("#btn-sync");
  btn.disabled = true;
  try {
    const r = await api("/api/meta/sync", { method: "POST" });
    $("#sync-info").textContent = `Synced ${r.rows} monthly rows for ${r.countries} countries, ${r.since} to ${r.until}, in ${r.currency}`;
    toast(`History synced. Estimates now use your account data${r.winners ? `; ${r.winners} best-selling ads will guide new plans` : ""}.`);
    await loadStats();
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; }
});

async function loadStats() {
  const rows = await api("/api/meta/stats");
  $("#stats-table tbody").innerHTML = rows.length ? rows.map((r) => `
    <tr><td>${esc(r.name)} <span class="sub">${esc(r.country)}</span></td><td class="num">${fmt.usd(r.spend_usd)}</td><td class="num">${fmt.int(r.impressions)}</td>
    <td class="num">${fmt.int(r.clicks)}</td><td class="num">${fmt.int(r.purchases)}</td><td class="num">${fmt.usd(r.cpm_usd)}</td><td class="num">${fmt.pct(r.ctr)}</td>
    <td class="num">${fmt.pct(r.cvr)}</td><td><span class="tag ${r.confidence}">${r.confidence}</span></td></tr>`).join("")
    : `<tr class="empty"><td colspan="9">No history yet. Save the Meta settings, then sync</td></tr>`;
  if (rows.length) $("#sync-info").textContent = `Synced ${utc(rows[0].synced_at).toLocaleString()} · ${rows[0].since} to ${rows[0].until} · these replace the benchmarks for each country listed`;
}

// ---------- history ----------

async function loadHistory() {
  const rows = await api("/api/plans");
  $("#history-table tbody").innerHTML = rows.length ? rows.map((p) => `
    <tr data-id="${p.id}"><td class="num">${p.id}</td><td>${esc(p.product)}</td><td>${esc(p.country)}</td>
    <td class="num">${fmt.pkr(p.budget_pkr)}</td><td class="num">${p.orders}</td>
    <td>${p.outcome ? `<div class="toolbar"><span class="mono">${p.outcome.purchases}</span><span class="tag ${p.outcome.verdict}">${fmt.label(p.outcome.verdict)}</span></div>` : `<span class="sub">not launched</span>`}</td>
    <td><span class="tag ${p.status}">${fmt.label(p.status)}</span></td>
    <td class="num">${utc(p.created_at).toLocaleDateString()}</td><td class="open"><button type="button" class="btn small ghost" aria-label="Open plan ${p.id}">Open ${icon("arrow")}</button></td></tr>`).join("")
    : `<tr class="empty"><td colspan="9">No plans yet. Start one from New plan</td></tr>`;
  document.querySelectorAll("#history-table tbody tr[data-id]").forEach((tr) =>
    tr.addEventListener("click", () => openPlan(tr.dataset.id).catch((e) => toast(e.message, true)))
  );
}

// ---------- rules ----------

async function loadOutcomes() {
  const o = await api("/api/outcomes");
  $("#calibration").innerHTML = o.calibration.length
    ? o.calibration.map((c) => `<div class="cal"><div class="name">${esc(c.name)}</div><div class="factor ${c.factor > 1.02 ? "up" : c.factor < 0.98 ? "down" : ""}">×${(+c.factor).toFixed(2)}</div><div class="hint" style="margin:0">${c.samples} launch${c.samples === 1 ? "" : "es"}</div></div>`).join("")
    : `<p class="hint" style="margin:0">No completed launches yet, so estimates run without a correction</p>`;
  $("#outcome-table tbody").innerHTML = o.launches.length ? o.launches.map((l) => `
    <tr><td>${esc(l.product)} <span class="sub">#${l.plan_id}</span></td><td>${esc(l.country)}</td><td class="num">${l.days_active}/${l.duration_days}</td>
    <td>${rangeBar(l.predicted_low, l.predicted_high, l.predicted_expected, l.purchases, { compact: true })}</td>
    <td class="num">${l.purchases}</td><td class="num">${fmt.usd(l.spend_usd)}</td>
    <td><span class="tag ${l.verdict}">${fmt.label(l.verdict)}${l.completed ? ", final" : ""}</span></td>
    <td class="sub">${(l.rules_added || []).length ? `${l.rules_added.length} rules` : (l.summary ? esc(l.summary) : "–")}</td></tr>`).join("")
    : `<tr class="empty"><td colspan="8">No launches tracked yet</td></tr>`;
  const st = Object.entries(o.action_stats || {});
  $("#action-stats").innerHTML = st.length
    ? "Changes, judged 3 days later: " + st.map(([k, v]) => `${esc(fmt.label(k))} improved ${v.improved}, neutral ${v.neutral}, worse ${v.worse}`).join(" · ")
    : "";
}

async function loadRules() {
  loadOutcomes().catch((e) => toast(e.message, true));
  const rules = await api("/api/rules");
  $("#rule-list").innerHTML = rules.length ? rules.map((r) => `
    <li class="${r.active ? "" : "inactive"}"><span>${esc(r.rule)}<span class="meta">weight ${(+r.weight).toFixed(1)}${r.source_plan_id ? ` · from plan #${r.source_plan_id}` : " · added by hand"}</span></span>
    ${r.active ? `<button class="btn small ghost" data-id="${r.id}">Deactivate</button>` : `<span class="sub">inactive</span>`}</li>`).join("")
    : `<li class="empty">Nothing learned yet. Edit a plan and say why in the note</li>`;
  document.querySelectorAll("#rule-list button").forEach((b) =>
    b.addEventListener("click", async () => {
      try { await api(`/api/rules/${b.dataset.id}`, { method: "DELETE" }); loadRules(); } catch (e) { toast(e.message, true); }
    })
  );
}

$("#rule-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const rule = $("#rule-input").value.trim();
  if (!rule) return;
  try {
    await api("/api/rules", { method: "POST", body: { rule } });
    $("#rule-input").value = "";
    loadRules().catch((e) => toast(e.message, true));
  } catch (e) { toast(e.message, true); }
});


// ---------- activity ----------

const AREA_LABELS = { auth: "sign-in", plan: "plan", launch: "launch", monitor: "monitor", action: "change on Meta",
  email: "email", image: "images", settings: "settings", learning: "learning" };
let activityOldest = 0;

let activitySeq = 0, activityBusy = false;
async function loadActivity(append = false) {
  if (append && activityBusy) return;
  const f = $("#activity-filters");
  const params = new URLSearchParams({ kind: f.elements.kind.value, level: f.elements.level.value, q: f.elements.q.value.trim(), limit: 100 });
  if (append && activityOldest) params.set("before_id", activityOldest);
  if (!append) { activityOldest = 0; $("#btn-activity-more").hidden = true; }
  const seq = ++activitySeq;
  activityBusy = true;
  let data;
  try { data = await api(`/api/activity?${params}`); }
  finally { if (seq === activitySeq) activityBusy = false; }
  if (seq !== activitySeq) return;  // a newer search or filter replaced this one
  if (f.elements.kind.options.length === 1) {
    f.elements.kind.insertAdjacentHTML("beforeend", data.kinds.map((k) => `<option value="${k}">${esc(AREA_LABELS[k] || k)}</option>`).join(""));
  }
  const box = $("#activity-list");
  if (!append) { box.innerHTML = ""; box.dataset.lastDay = ""; }
  if (!data.items.length && !append) box.innerHTML = `<p class="activity-empty">Nothing recorded yet</p>`;
  let html = "";
  for (const a of data.items) {
    const d = utc(a.created_at);
    const day = d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
    if (day !== box.dataset.lastDay) { html += `<div class="day">${esc(day)}</div>`; box.dataset.lastDay = day; }
    html += `<div class="event ${a.level}">
      <time>${d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}</time>
      <span class="lvl" title="${a.level}"></span>
      <div class="what"><span class="t">${esc(a.title)}</span><span class="area">${esc(AREA_LABELS[a.kind] || a.kind)}</span>${a.detail ? `<div class="d">${esc(a.detail)}</div>` : ""}</div>
      <span class="who">${esc(a.actor)}</span></div>`;
  }
  box.insertAdjacentHTML("beforeend", html);
  if (data.items.length) activityOldest = data.items[data.items.length - 1].id;
  $("#btn-activity-more").hidden = !data.more;
}

let activityTimer;
$("#activity-filters").addEventListener("input", () => {
  clearTimeout(activityTimer);
  $("#btn-activity-more").hidden = true;  // the old list's paging no longer applies
  activityTimer = setTimeout(() => loadActivity().catch((e) => toast(e.message, true)), 250);
});
$("#activity-filters").addEventListener("submit", (ev) => ev.preventDefault());
$("#btn-activity-more").addEventListener("click", () => loadActivity(true).catch((e) => toast(e.message, true)));

// ---------- payments ----------

const FEATURE_ORDER = ["Plans", "Ad images", "Alert advice", "Learning", "Result rules"];

async function loadBilling() {
  const b = await api("/api/billing");
  const pct = b.budget_usd ? Math.min(100, b.spent_usd / b.budget_usd * 100) : 0;
  const pkr = (usd) => fmt.pkr(usd * b.pkr_per_usd);
  $("#billing-kpis").innerHTML = `
    <div class="kpi"><div class="label">AI cost, ${esc(b.month)}</div><div class="value accent">${fmt.usd(b.spent_usd)}</div>
      <div class="sub">${b.budget_usd ? `of ${fmt.usd(b.budget_usd)} budget · ${b.budget_used_pct}% used` : "no monthly limit"}</div>
      ${b.budget_usd ? `<div class="meter${pct >= 100 ? " over" : ""}"><div style="width:${pct}%"></div></div>` : ""}</div>
    <div class="kpi"><div class="label">Meta ad spend, all campaigns</div><div class="value">${fmt.usd(b.ad_spend_usd)}</div>
      <div class="sub">${pkr(b.ad_spend_usd)}${b.ad_revenue_usd ? ` · ${fmt.usd(b.ad_revenue_usd)} revenue` : ""}</div></div>
    <div class="kpi"><div class="label">Budget in running campaigns</div><div class="value">${fmt.usd(b.ad_budget_usd)}</div>
      <div class="sub">${pkr(b.ad_budget_usd)}</div></div>
    <div class="kpi"><div class="label">AI calls this month</div><div class="value">${fmt.int(b.calls)}</div>
      <div class="sub">${fmt.int(b.images)} images${b.failed ? ` · ${b.failed} failed` : ""}</div></div>`;

  // one bar per day of this month; today carries the accent
  $("#billing-month").textContent = b.month;
  const now = new Date();
  const days = new Date(now.getUTCFullYear(), now.getUTCMonth() + 1, 0).getDate();
  const byDay = Object.fromEntries(b.by_day.map((d) => [Number(d.day.slice(8, 10)), d.cost_usd]));
  const max = Math.max(0, ...Object.values(byDay));
  $("#billing-chart").innerHTML = max > 0
    ? Array.from({ length: days }, (_, i) => {
        const v = byDay[i + 1] || 0;
        return `<div class="bar${i + 1 === now.getUTCDate() ? " today" : ""}" style="height:${v ? Math.max(v / max * 100, 2) : 0.5}%" title="Day ${i + 1}: ${fmt.usd(v)}"></div>`;
      }).join("")
    : `<span class="bars-empty">No AI cost yet this month</span>`;
  document.querySelector(".bars-axis")?.remove();
  if (max > 0) $("#billing-chart").insertAdjacentHTML("afterend", `<div class="bars-axis"><span>1</span><span>highest day ${fmt.usd(max)}</span><span>${days}</span></div>`);

  const feats = [...b.by_feature].sort((x, y) => FEATURE_ORDER.indexOf(x.feature) - FEATURE_ORDER.indexOf(y.feature));
  $("#billing-features tbody").innerHTML = feats.length ? feats.map((f) => `
    <tr><td>${esc(f.feature)}</td><td class="num">${fmt.int(f.calls)}</td><td class="num">${f.images ? fmt.int(f.images) : "–"}</td><td class="num">${fmt.usd(f.cost_usd)}</td></tr>`).join("")
    : `<tr class="empty"><td colspan="4">No AI calls yet this month</td></tr>`;

  $("#billing-ads tbody").innerHTML = b.ads.length ? b.ads.map((a) => {
    const used = a.budget_usd ? Math.min(100, a.spend_usd / a.budget_usd * 100) : 0;
    return `<tr><td>${esc(a.product)} <span class="sub">to ${esc(a.country)} · plan #${a.plan_id}</span></td>
      <td><span class="tag ${a.status}">${fmt.label(a.status)}</span></td>
      <td class="num">${fmt.usd(a.budget_usd)}</td>
      <td><div class="spent"><i><b style="width:${used}%"></b></i><span>${Math.round(used)}%</span></div></td>
      <td class="num">${fmt.usd(a.spend_usd)}</td><td class="num">${a.purchases}</td><td class="num">${a.revenue_usd ? fmt.usd(a.revenue_usd) : "–"}</td></tr>`;
  }).join("") : `<tr class="empty"><td colspan="7">No campaigns launched yet</td></tr>`;

  $("#billing-form").elements.budget_usd.value = b.budget_usd;
  $("#price-rows").innerHTML = `<div class="price-row head"><span>Provider and model</span><span>In / 1M</span><span>Out / 1M</span><span>Image</span></div>` +
    Object.entries(b.prices).map(([k, v]) => `<div class="price-row" data-key="${esc(k)}"><code>${esc(k)}</code>
      <input type="number" step="0.001" min="0" data-f="in" value="${v.in ?? ""}" placeholder="–" aria-label="${esc(k)} input price" />
      <input type="number" step="0.001" min="0" data-f="out" value="${v.out ?? ""}" placeholder="–" aria-label="${esc(k)} output price" />
      <input type="number" step="0.001" min="0" data-f="image" value="${v.image ?? ""}" placeholder="–" aria-label="${esc(k)} image price" /></div>`).join("");

  $("#billing-recent tbody").innerHTML = b.recent.length ? b.recent.slice(0, 15).map((c) => `
    <tr><td class="sub">${utc(c.created_at).toLocaleString()}</td><td>${esc(c.feature)}${c.ok ? "" : ` <span class="tag behind" title="${esc(c.error)}">failed</span>`}</td>
    <td class="num">${c.images ? `${c.images} img` : fmt.int(c.input_tokens + c.output_tokens)}</td><td class="num">${fmt.usd(c.cost_usd)}</td></tr>`).join("")
    : `<tr class="empty"><td colspan="4">No AI calls yet</td></tr>`;
}

$("#billing-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const prices = {};
  document.querySelectorAll("#price-rows .price-row[data-key]").forEach((row) => {
    const rates = {};
    row.querySelectorAll("input").forEach((i) => { if (i.value !== "") rates[i.dataset.f] = +i.value; });
    prices[row.dataset.key] = rates;
  });
  try {
    await api("/api/billing", { method: "POST", body: { budget_usd: +$("#billing-form").elements.budget_usd.value || 0, prices } });
    toast("Budget and prices saved");
    loadBilling().catch((e) => toast(e.message, true));
  } catch (e) { toast(e.message, true); }
});


// ---------- today ----------

async function loadToday() {
  const t = await api("/api/today");
  const st = t.stats;
  $("#today-date").textContent = new Date(t.date + "T00:00").toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
  $("#today-stats").innerHTML = `
    <div class="kpi"><div class="label">Running campaigns</div><div class="value">${st.campaigns}</div><div class="sub">${st.open_alerts} open alerts</div></div>
    <div class="kpi"><div class="label">Orders from running campaigns</div><div class="value accent">${fmt.int(st.orders)}</div><div class="sub">since each launch</div></div>
    <div class="kpi"><div class="label">Spent on them</div><div class="value">${fmt.usd(st.spend_usd)}</div><div class="sub">${fmt.pkr(st.spend_pkr)}</div></div>
    <div class="kpi"><div class="label">Cost per order</div><div class="value">${st.cpa_usd != null ? fmt.usd(st.cpa_usd) : "–"}</div><div class="sub">across running campaigns</div></div>`;
  $("#today-list").innerHTML = t.items.length ? t.items.map((i, n) => `
    <div class="todo-item ${i.priority}">
      <span class="pri" title="${i.priority} priority"></span>
      <div><div class="t">${esc(i.title)}</div><div class="d">${esc(i.text)}</div></div>
      <button class="btn small" data-go="${i.view}" ${i.plan_id ? `data-plan="${i.plan_id}"` : ""}>${esc(i.label)}${icon("arrow")}</button>
    </div>`).join("")
    : `<p class="activity-empty">${icon("check")}Nothing needs you right now</p>`;
}

document.addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-go]");
  if (!b) return;
  if (b.dataset.plan) { openPlan(b.dataset.plan).catch((e) => toast(e.message, true)); return; }
  if (b.dataset.go === "plan") newPlan();
  go(b.dataset.go);
});


// ---------- plan: copy and delete ----------

$("#btn-copy-plan").addEventListener("click", () => {
  const inputs = { ...currentPlan.inputs };
  newPlan();
  fillForm(inputs);
  updateConversions();
  liveEstimate();
  window.scrollTo({ top: 0, behavior: "smooth" });
  showView("plan");
  toast("Inputs copied. Change what you need, then create the plan.");
});

$("#btn-delete-plan").addEventListener("click", async () => {
  if (!(await ask(`Delete plan #${currentPlan.id}?`, "The plan, its versions and its ad images are removed. This cannot be undone.", "Delete", true))) return;
  try {
    await api(`/api/plans/${currentPlan.id}`, { method: "DELETE" });
    newPlan();
    go("history");
    toast("Plan deleted.");
  } catch (e) { toast(e.message, true); }
});

// ---------- account ----------

async function loadAccount() {
  if (!ME.is_superuser) return;
  const [users, backups] = await Promise.all([api("/api/users"), api("/api/backups")]);
  $("#team-list").innerHTML = users.map((u) => `
    <li><span>${esc(u.email)}${u.is_superuser ? `<span class="tag approved who-tag">owner</span>` : ""}${u.is_active ? "" : `<span class="tag behind who-tag">locked out</span>`}
      <span class="meta">added ${utc(u.created_at).toLocaleDateString()}</span></span>
      ${u.id === ME.id || u.is_superuser ? "" : `<button class="btn small ghost" data-user="${u.id}" data-active="${u.is_active ? 0 : 1}">${u.is_active ? "Lock out" : "Let back in"}</button>`}</li>`).join("");
  $("#backup-table tbody").innerHTML = backups.length ? backups.map((b) => `
    <tr><td>${utc(b.created_at).toLocaleString()}</td><td class="num">${(b.bytes / 1024 / 1024).toFixed(2)} MB</td>
    <td class="num"><button class="btn small" data-backup="${esc(b.name)}">${icon("download")}Download</button></td></tr>`).join("")
    : `<tr class="empty"><td colspan="3">No backup yet. The first one is made a minute after the server starts</td></tr>`;
}

$("#team-list").addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-user]");
  if (!b) return;
  const lock = b.dataset.active === "0";
  if (lock && !(await ask("Lock this person out?", "They can no longer sign in. You can let them back in later.", "Lock out", true))) return;
  try { await api(`/api/users/${b.dataset.user}/active`, { method: "POST", body: { active: !lock } }); loadAccount(); }
  catch (e) { toast(e.message, true); }
});

$("#team-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.currentTarget;
  try {
    await api("/api/users/register", { method: "POST", body: { email: f.email.value.trim(), password: f.password.value } });
    f.reset();
    toast("Added. Give them the email and password; they can change the password on this page.");
    loadAccount().catch((e) => toast(e.message, true));
  } catch (e) { toast(e.message, true); }
});

$("#password-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.currentTarget;
  if (f.new_password.value !== f.repeat.value) return toast("The two new passwords are not the same.", true);
  try {
    const t = await api("/api/users/me/password", { method: "POST", body: { current_password: f.current_password.value, new_password: f.new_password.value } });
    tokens.set(t.access_token, t.refresh_token);  // this device stays signed in; every other one is signed out
    f.reset();
    toast("Password changed. Other devices were signed out.");
  } catch (e) { toast(e.message, true); }
});

$("#btn-backup").addEventListener("click", async () => {
  const btn = $("#btn-backup");
  btn.disabled = true;
  try { await api("/api/backups", { method: "POST" }); toast("Backup made."); loadAccount(); }
  catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; }
});

// Downloads need the auth header, so fetch the file and hand it to the browser.
$("#backup-table").addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-backup]");
  if (!b) return;
  const get = () => fetch(`/api/backups/${encodeURIComponent(b.dataset.backup)}`, { headers: { Authorization: `Bearer ${tokens.access}` } });
  let res;
  try {
    res = await get();
    if (res.status === 401 && (await tryRefresh())) res = await get();
  } catch { return toast("Download failed: the server could not be reached.", true); }
  if (res.status === 401) return showLogin();
  if (!res.ok) return toast("Download failed.", true);
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url; a.download = b.dataset.backup; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
});


// ---------- light / dark ----------

function applyTheme(t) {
  document.documentElement.setAttribute("data-theme", t);
  const b = $("#btn-theme");
  b.setAttribute("aria-pressed", String(t === "dark"));
  b.querySelector("span").textContent = t === "dark" ? "Light mode" : "Dark mode";
  b.querySelector("use").setAttribute("href", t === "dark" ? "#i-sun" : "#i-moon");
  document.querySelector('meta[name="theme-color"]').setAttribute("content", t === "dark" ? "#18191a" : "#f0f2f5");
}
applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light");
$("#btn-theme").addEventListener("click", () => {
  const t = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  applyTheme(t);
  try { localStorage.setItem("theme", t); } catch { /* private mode: this visit only */ }
});


// ---------- sign-in: show / hide password ----------
$("#pw-toggle").addEventListener("click", () => {
  const input = $("#login-form").elements.password;
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  $("#pw-toggle").textContent = show ? "Hide" : "Show";
  $("#pw-toggle").setAttribute("aria-pressed", String(show));
  $("#pw-toggle").setAttribute("aria-label", show ? "Hide password" : "Show password");
  input.focus();
});


// ---------- stores (brands): one Meta connection each ----------

let BRANDS = [];

// The Store picker on the plan form; hidden while there is only the main store.
async function loadBrandOptions() {
  BRANDS = await api("/api/brands");
  const sel = $("#brand-select");
  const keep = sel.value;
  sel.innerHTML = BRANDS.map((b) => `<option value="${b.id}">${esc(b.name)}${b.connected ? "" : " (not connected)"}</option>`).join("");
  if (keep && BRANDS.some((b) => String(b.id) === keep)) sel.value = keep;
  $("#brand-field").hidden = BRANDS.length < 2;
}

async function loadBrandList() {
  await loadBrandOptions();
  $("#brand-list").innerHTML = BRANDS.map((b) => `
    <li><span>${esc(b.name)}${b.main ? ` <span class="tag">main</span>` : ""}
      <span class="meta">${b.account ? esc(b.account) : "no ad account"} · ${b.connected ? "connected" : "not connected"}</span></span>
      ${b.main ? `<span class="sub">edit it above</span>` : `<button class="btn small ghost" data-brand-edit="${b.id}">Edit</button>`}</li>`).join("");
}

function openBrandForm(b) {
  const f = $("#brand-form");
  f.reset();
  f.elements.id.value = b ? b.id : "";
  for (const k of ["name", "meta_access_token", "meta_ad_account_id", "meta_page_id", "meta_pixel_id", "store_url", "creative_image_urls"])
    f.elements[k].value = b ? (b[k] || "") : "";
  $("#brand-form-title").textContent = b ? `Edit ${b.name}` : "New store";
  $("#btn-brand-delete").hidden = !b;
  $("#brand-check").innerHTML = "";
  f.hidden = false;
  f.elements.name.focus();
}

$("#btn-brand-new").addEventListener("click", () => openBrandForm(null));
$("#btn-brand-cancel").addEventListener("click", () => { $("#brand-form").hidden = true; });
$("#brand-list").addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-brand-edit]");
  if (!b) return;
  try { openBrandForm(await api(`/api/brands/${b.dataset.brandEdit}`)); } catch (e) { toast(e.message, true); }
});

function brandValues() {
  const f = $("#brand-form");
  const v = {};
  for (const k of ["name", "meta_access_token", "meta_ad_account_id", "meta_page_id", "meta_pixel_id", "store_url", "creative_image_urls"])
    v[k] = f.elements[k].value.trim();
  return v;
}

// Save, then return the id (a new store gets one on its first save).
async function saveBrand() {
  const f = $("#brand-form");
  if (!f.elements.name.value.trim()) { f.elements.name.focus(); throw new Error("Give the store a name"); }
  const id = f.elements.id.value;
  const out = await api(id ? `/api/brands/${id}` : "/api/brands", { method: "POST", body: { values: brandValues() } });
  f.elements.id.value = out.id;
  f.elements.meta_access_token.value = out.meta_access_token;  // masked from now on
  $("#btn-brand-delete").hidden = false;
  $("#brand-form-title").textContent = `Edit ${out.name}`;
  await loadBrandList();
  return out.id;
}

$("#brand-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try { await saveBrand(); toast("Store saved."); $("#brand-form").hidden = true; } catch (e) { toast(e.message, true); }
});

$("#btn-brand-check").addEventListener("click", async () => {
  const out = $("#brand-check");
  out.textContent = "Checking";
  try {
    const id = await saveBrand();
    const st = await api(`/api/brands/${id}/check`, { method: "POST" });
    const list = (arr) => arr.length ? arr.map((x) => `<code>${esc(x.id)}</code> ${esc(x.name || "")}`).join(" · ") : "none";
    out.innerHTML = `<div>Token of <b>${esc(st.user.name || "")}</b></div>
      <div>Ad accounts: ${list(st.accounts)}</div><div>Pages: ${list(st.pages)}</div><div>Pixels: ${list(st.pixels)}</div>`;
  } catch (e) { out.innerHTML = `<span style="color:var(--neg)">${esc(e.message)}</span>`; }
});

$("#btn-brand-delete").addEventListener("click", async () => {
  const f = $("#brand-form");
  if (!f.elements.id.value) return;
  if (!(await ask("Delete this store?", "Its connection is removed from the app. Nothing changes on Meta. Stores that plans use cannot be deleted.", "Delete", true))) return;
  try { await api(`/api/brands/${f.elements.id.value}`, { method: "DELETE" }); f.hidden = true; toast("Store deleted."); loadBrandList(); }
  catch (e) { toast(e.message, true); }
});


// ---------- sales brief: prefill with this store's last answers ----------

const BRIEF_KEYS = ["offer", "usp", "delivery_days", "returns", "social_proof"];
let briefSeq = 0;
async function prefillBrief() {
  const f = $("#plan-form");
  if (currentPlan || BRIEF_KEYS.some((k) => String(f.elements[k].value || "").trim())) return;  // never overwrite
  const seq = ++briefSeq;
  try {
    const b = await api(`/api/brief?brand_id=${+f.elements.brand_id.value || 0}`);
    if (seq !== briefSeq || currentPlan) return;
    for (const k of BRIEF_KEYS) if (b[k] && !String(f.elements[k].value || "").trim()) f.elements[k].value = b[k];
    if (b.cod && f.elements.country.value === "PK") f.elements.cod.checked = true;
    $("#sell-box").open = !BRIEF_KEYS.some((k) => b[k]);  // open it when nothing is filled yet
  } catch { /* the form still works without it */ }
}
$("#plan-form").elements.brand_id.addEventListener("change", () => {
  const f = $("#plan-form");
  for (const k of BRIEF_KEYS) f.elements[k].value = "";  // another store, other facts
  f.elements.cod.checked = false;
  prefillBrief();
});


$("#btn-brand-winners").addEventListener("click", async () => {
  const btn = $("#btn-brand-winners"), out = $("#brand-check");
  btn.disabled = true; out.textContent = "Reading this store's ad history (up to a minute)";
  try {
    const id = await saveBrand();
    const r = await api(`/api/brands/${id}/winners`, { method: "POST" });
    out.textContent = r.winners ? `${r.winners} best-selling ads found (${r.since} to ${r.until}). New plans for this store start from them.`
      : "No ad with 3 or more purchases in the last 2 years yet.";
  } catch (e) { out.innerHTML = `<span style="color:var(--neg)">${esc(e.message)}</span>`; }
  finally { btn.disabled = false; }
});


// ---------- Meta policy findings in the ad copy ----------
function renderPolicy(plan) {
  const box = $("#policy");
  const items = plan.policy || [];
  box.hidden = !items.length;
  if (!items.length) return;
  const blocks = items.filter((f) => f.level === "block").length;
  box.innerHTML = `<div class="panel-title">${icon("alert")}${blocks ? `Meta would likely reject ${blocks} part${blocks > 1 ? "s" : ""} of this copy: fix before launching` : "Check this copy against Meta's rules"}</div>`
    + items.map((f) => `<div class="check ${f.level === "block" ? "fail" : "warn"}">${icon(f.level === "block" ? "x" : "alert")}<span><b>${esc(f.where)}:</b> ${esc(f.text)}</span></div>`).join("")
    + `<p class="hint" style="margin-top:8px">Rejected ads hurt the ad account's standing. Use Edit to change the words.</p>`;
}
