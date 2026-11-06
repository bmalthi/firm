// Owner console application shell and polling loop.
import {age, time, firmMenuLabel, needsYou, pageTitle, resultHref, fileReplyMatches} from "./format.js";
import {createOverview} from "./overview.js";
import {createInbox} from "./inbox.js";
import {createTeam} from "./team.js";
import {createResults} from "./results.js";
import {createSystem} from "./system.js";

const $ = (selector) => document.querySelector(selector);
let snapshot = null;
let controller = null;
let secondaryController = null;
let connected = false;
let disconnectedAt = null;
let firmStorageScope = null;
let hostStorageScope = null;
let replyTarget = null;
let replyAttachments = [];
let sending = false;
let control = null;
let pendingControl = null;
let route = "overview";
const sources = new Map();
const sourceKey = (path) => path.split("?")[0];
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
const number = (value) => value == null ? "unavailable" : Number(value).toLocaleString();
const total = (tokens = {}) => tokens.input_tokens == null && tokens.output_tokens == null ? null : (tokens.input_tokens || 0) + (tokens.output_tokens || 0);

function notice(text = "", error = false) {
  $("#notice").hidden = !text;
  $("#notice").textContent = text;
  $("#notice").classList.toggle("error", error);
}

async function api(path, signal) {
  const key = sourceKey(path);
  const timeout = AbortSignal.timeout(8000);
  const bounded = signal ? AbortSignal.any([signal, timeout]) : timeout;
  try {
    const response = await fetch(path, {signal: bounded, headers: {Accept: "application/json"}});
    const value = await response.json();
    if (!response.ok) throw new Error(value?.error?.message || `HTTP ${response.status}`);
    sources.set(key, {sampledAt: value.sampled_at ?? value.readiness?.sampled_at ?? Date.now() / 1000, failed: false});
    return value;
  } catch (error) {
    if (!signal?.aborted) sources.set(key, {...sources.get(key), failed: true});
    throw error;
  }
}
function sourceText(path, always = false) {
  const source = sources.get(sourceKey(path));
  const maxAge = path.startsWith("/api/roles/") ? Infinity : path.includes("resources") ? 120 : 10;
  const stale = source?.sampledAt != null && Date.now() / 1000 - source.sampledAt > maxAge;
  if (!always && connected && !source?.failed && !stale) return "";
  return source?.sampledAt != null ? `${source.failed ? "Refresh unavailable · " : stale ? "Stale · " : ""}updated ${age(source.sampledAt)}` : source?.failed ? "Unavailable · no sample received" : "Not sampled";
}
function operationKey() {
  return `op-${crypto.randomUUID()}`;
}
function scopedStorageKey(key) {
  if (key.startsWith(`firm-console:${firmStorageScope}:`)) return key;
  const scope = key.startsWith("firm-control:") ? hostStorageScope : firmStorageScope;
  if (!scope) throw new Error("Firm identity is not ready yet.");
  return `firm-console:${scope}:${key}`;
}
async function post(path, body, storageKey) {
  if (!connected) throw new Error("Can’t reach the firm. Actions are off until the connection returns.");
  storageKey = scopedStorageKey(storageKey);
  let saved;
  try { saved = JSON.parse(sessionStorage.getItem(storageKey) || "null"); } catch { saved = null; }
  const payload = saved?.payload || body;
  const operation_id = saved?.operation_id || operationKey();
  if (!saved) sessionStorage.setItem(storageKey, JSON.stringify({operation_id, payload}));
  const response = await fetch(path, {
    method: "POST",
    headers: {
      "Accept": "application/json",
      "Content-Type": "application/json",
      "X-Firm-Console": "same-origin",
    },
    body: JSON.stringify({...payload, operation_id}),
  });
  const value = await response.json();
  if (!response.ok) {
    if (response.status < 500) sessionStorage.removeItem(storageKey);
    throw new Error(value?.error?.message || `HTTP ${response.status}`);
  }
  sessionStorage.removeItem(storageKey);
  return value;
}

function button(label, action, className = "") {
  const node = element("button", className, label);
  node.type = "button";
  node.addEventListener("click", async () => {
    node.dataset.busy = "true"; node.disabled = true;
    try { await action(); } catch (error) { notice(error.message, true); }
    finally { delete node.dataset.busy; node.disabled = node.hasAttribute("data-mutation") && !connected; updateActions(); }
  });
  return node;
}
function mutationButton(label, action, className = "") {
  const node = button(label, action, className); node.dataset.mutation = ""; node.disabled = !connected; return node;
}
function syncChildren(root, nodes) {
  const focused = root.contains(document.activeElement) ? document.activeElement : null;
  nodes.forEach((node, index) => { if (root.children[index] !== node) root.insertBefore(node, root.children[index] || null); });
  while (root.children.length > nodes.length) root.lastElementChild.remove();
  if (focused?.isConnected && document.activeElement !== focused) focused.focus({preventScroll: true});
}
function technicalDetails(text) {
  const details = element("details", "technical-details");
  details.append(element("summary", "", "Technical details"), element("p", "role-meta", text));
  return details;
}

function updateActions() {
  for (const node of document.querySelectorAll("[data-mutation]")) {
    const controlAction = node.dataset.control;
    const allowed = !controlAction || controlAllowed(controlAction);
    node.disabled = !connected || !allowed || node.dataset.busy === "true" || (node.closest("#send-form") && sending);
    node.title = !connected ? "Can't reach the firm. Actions are off until the connection returns." : !allowed ? "Unavailable in the current firm state." : "";
    if (!connected) node.setAttribute("aria-describedby", "banner"); else node.removeAttribute("aria-describedby");
  }
}
function controlAllowed(action) {
  if (!control || sources.get("/api/control/status")?.failed) return false;
  return action === "stop" ? control.firm_state === "running"
    : action === "force-stop" ? ["running", "draining"].includes(control.firm_state)
      : action === "resume" ? control.can_resume : control.can_close;
}
function renderFreshness() {
  $("#freshness").textContent = `${connected ? "Live" : "Disconnected"} · ${sourceText("/api/snapshot", true)}`;
  $("#banner").hidden = connected;
  $("#banner").textContent = `Can't reach the firm${disconnectedAt ? ` since ${time(disconnectedAt)}` : ""}. Showing what was true then; it may have changed. Retrying every 3 seconds. Buttons that change the firm are off until the connection returns. ${sourceText("/api/snapshot")}.`;
  $("#sampled-at").textContent = sourceText("/api/snapshot");
  for (const node of document.querySelectorAll("[data-source]")) node.textContent = sourceText(node.dataset.source);
  for (const node of document.querySelectorAll("[data-observed-at]")) node.textContent = age(Number(node.dataset.observedAt));
  for (const node of document.querySelectorAll("[data-timestamp]")) node.textContent = time(Number(node.dataset.timestamp));
  overview.freshness(); inbox.freshness(); team.freshness(); system.freshness();
  for (const node of document.querySelectorAll(".source-age")) node.hidden = !node.textContent;
}
async function refreshControl(signal) {
  try {
    control = await api("/api/control/status", signal);
    $("#lifecycle-state").textContent = firmMenuLabel(control.firm_state);
    $("#startup-error").hidden = !control.startup_error;
    $("#startup-error").textContent = control.startup_error || "";
  } catch (error) {
    if (!signal?.aborted) $("#lifecycle-state").textContent = "Firm: status unavailable";
  }
  updateActions();
}
const controls = {
  stop: ["Stop after current work", "The firm stops accepting new work. Current work can finish, then the VMs shut down."],
  "force-stop": ["Force stop now", "Active work may be interrupted and the VMs will be powered off."],
  resume: ["Resume Firm", "Start the VMs and let the firm accept work again. Idle roles still need a message or scheduled wake."],
  close: ["Close Firm", "Close this firm's local host. You can reopen it later with firm start."],
};
function confirmControl(action) {
  if (!connected || !controlAllowed(action)) return;
  pendingControl = action;
  $("#control-title").textContent = controls[action][0];
  $("#control-description").textContent = controls[action][1];
  $("#confirm-control").textContent = controls[action][0];
  $("#confirm-control").dataset.control = action;
  $("#firm-menu").open = false;
  $("#control-dialog").showModal();
  updateActions();
}
async function runControl(action) {
  if (!connected || !controlAllowed(action)) return;
  const result = await post(`/api/control/${action}`, {action, ...(action === "force-stop" ? {confirmed: true} : {})}, `firm-control:${action}`);
  notice(action === "close" ? "Host closing." : `${controls[action][0]} requested.`);
  $("#control-dialog").close();
  await refresh(true);
  return result;
}
function selectRecipient(value) {
  const select = $("#send-recipients");
  const role = snapshot?.readiness?.rows.find((r) => r.employee_id === value || r.address === value)
    || snapshot?.roles.find((r) => r.employee_id === value || r.address === value);
  const id = role?.employee_id || value;
  if (![...select.options].some((option) => option.value === id)) {
    const option = element("option", "", role?.identity || value); option.value = id; select.append(option);
  }
  select.value = id;
  recipientLabel();
}
function recipientLabel() {
  const selected = $("#send-recipients").selectedOptions[0];
  $("#send-address").textContent = selected?.dataset.address || "";
}
function populateRecipients(data) {
  const select = $("#send-recipients");
  const previous = select.value;
  const roles = data.readiness?.rows?.filter((r) => r.activity !== "retired") || data.roles.filter((r) => r.accepts_mail);
  const signature = JSON.stringify(roles.map((r) => [r.employee_id, r.identity, r.address]));
  if (select.dataset.roles !== signature) {
    select.dataset.roles = signature;
    select.replaceChildren(...roles.map((r) => { const option = element("option", "", r.identity || r.address); option.value = r.employee_id; option.dataset.address = r.address; return option; }));
    if (previous) selectRecipient(previous);
  }
  recipientLabel();
}
async function reply(message, attachment = null) {
  if ($("#send-form").dataset.pendingKey) { notice("Resolve the saved send before starting a reply."); return; }
  replyTarget = message.message_id;
  replyAttachments = attachment ? [attachment] : [];
  $("#send-form").classList.add("reply-mode");
  $("#send-recipients").disabled = true;
  $("#send-subject").disabled = true;
  $("#send-subject").value = message.subject || "";
  const role = snapshot?.readiness?.rows.find((item) => item.employee_id === message.author.employee_id);
  $("#ask-title").textContent = `Reply to ${role?.identity || message.author.address || "this conversation"}`;
  $("#send-status").textContent = "Sending marks this message done.";
  if (!attachment) inbox.prepareReply(message);
  // pushState avoids a second, queued hash navigation scrolling away the editor.
  const destination = attachment ? resultHref({...message, ...attachment}) : "#inbox";
  if (location.hash !== destination) history.pushState(null, "", destination);
  await navigate();
  if (route !== (attachment ? "results" : "inbox") || replyTarget !== message.message_id) return;
  $("#send-body").focus({preventScroll: true});
  $("#send-body").scrollIntoView({block: "center"});
}
function placeComposer() {
  const form = $("#send-form");
  const replyCard = route === "inbox" && replyTarget
    ? [...document.querySelectorAll("#messages .message-card")].find((card) => card.dataset.messageId === replyTarget) : null;
  const inReader = route === "results" && fileReplyMatches(results.selection(), replyTarget, replyAttachments);
  const host = inReader ? $("#results-composer") : route === "inbox" ? replyCard || $("#inbox-composer") : $("#overview-composer");
  if (form.parentElement !== host) host.append(form);
  $("#inbox-composer").hidden = form.parentElement !== $("#inbox-composer");
  $("#cancel-reply").hidden = !replyTarget;
  $("#cancel-reply").disabled = Boolean(form.dataset.pendingKey) || sending;
  form.querySelector("button[type=submit]").textContent = replyTarget ? "Send reply" : "Send message";
  if (!replyTarget) $("#ask-title").textContent = "Ask or instruct";
  $("#reader-follow-up").hidden = Boolean(inReader);
  $("#send-attachment").hidden = !replyAttachments.length;
  $("#send-attachment").textContent = replyAttachments.length ? `Attached: ${replyAttachments.map((item) => item.name).join(", ")}` : "";
}
function cancelReply() {
  if ($("#send-form").dataset.pendingKey || sending) return;
  replyTarget = null;
  replyAttachments = [];
  $("#send-form").classList.remove("reply-mode");
  unlockComposer();
  $("#send-subject").value = ""; $("#send-body").value = ""; $("#send-status").textContent = "";
  placeComposer();
}
function instruct(role, text) {
  if ($("#send-form").dataset.pendingKey) { notice("Resolve the saved send before starting a new instruction."); return; }
  replyTarget = null; replyAttachments = []; unlockComposer();
  $("#send-form").classList.remove("reply-mode");
  selectRecipient(role.employee_id);
  $("#send-subject").value = "Continue from your saved notes";
  $("#send-body").value = text;
  $("#send-status").textContent = "Suggested wording; edit it before you send. It goes to the founder as a normal message from you.";
  $("#send-body").focus();
}
function restoreComposer() {
  try {
    const sendKey = scopedStorageKey("firm-message:send");
    let key = sendKey;
    let saved = JSON.parse(sessionStorage.getItem(key) || "null");
    if (!saved) {
      const replyPrefix = `firm-console:${firmStorageScope}:firm-message:reply:`;
      key = Object.keys(sessionStorage).find((candidate) => candidate.startsWith(replyPrefix));
    }
    if (!saved && key && key !== sendKey) {
      saved = JSON.parse(sessionStorage.getItem(key) || "null");
    }
    if (!saved) return;
    const payload = saved.payload;
    replyAttachments = payload.attachments || [];
    const isReply = saved.operation === "reply" || Boolean(payload.message_id);
    if (isReply) {
      replyTarget = payload.message_id;
      $("#send-form").classList.add("reply-mode");
      $("#send-recipients").disabled = true;
      $("#send-subject").disabled = true;
      $("#ask-title").textContent = "Reply in this conversation";
    } else {
      selectRecipient(payload.recipients[0]);
      $("#send-subject").value = payload.subject || "";
    }
    $("#send-body").value = payload.body;
    $("#send-form").dataset.pendingKey = key;
    $("#send-recipients").disabled = true;
    $("#send-subject").disabled = true;
    $("#send-body").disabled = true;
    $("#send-status").textContent = "Checking the saved send against this firm's recorded operations…";
    void reconcileComposer(key, saved, isReply ? "reply" : "send");
  } catch { /* A corrupt tab-local retry record can be discarded by the owner. */ }
}
async function reconcileComposer(key, saved, operation) {
  try {
    const result = await api(`/api/messages/operation?operation=${operation}&operation_id=${encodeURIComponent(saved.operation_id)}`);
    if (result.status === "recorded") {
      sessionStorage.removeItem(key);
      if (operation === "reply") {
        replyTarget = null;
        replyAttachments = [];
        $("#send-form").classList.remove("reply-mode");
      }
      unlockComposer();
      $("#send-status").textContent = "The saved message was already recorded as sent. The original draft is retained for reference.";
      await refresh(true);
      return;
    }
    $("#send-status").textContent = `No receipt exists for the saved ${operation}. Retry sends this exact original text. A new draft may duplicate a message if it was sent outside the recorded operation.`;
    if (operation === "send") $("#new-message-draft").hidden = false;
  } catch (error) {
    $("#send-status").textContent = `Could not check the saved send: ${error.message}. Retry after reconnecting.`;
  }
}
function unlockComposer() {
  $("#send-form").dataset.pendingKey = "";
  $("#send-recipients").disabled = false;
  $("#send-subject").disabled = false;
  $("#send-body").disabled = false;
  $("#new-message-draft").hidden = true;
}
function beginNewDraft() {
  const key = $("#send-form").dataset.pendingKey;
  if (key) sessionStorage.removeItem(key);
  unlockComposer();
  $("#send-body").value = "";
  $("#send-status").textContent = "The prior operation may already have sent. A new send can create a duplicate.";
}
async function sendComposer() {
  if (!connected || sending) return;
  sending = true;
  updateActions();
  const sendKey = "firm-message:send";
  const pendingKey = $("#send-form").dataset.pendingKey || null;
  const replyKey = replyTarget ? `firm-message:reply:${replyTarget}` : null;
  const key = pendingKey || replyKey || sendKey;
  const saved = pendingKey ? JSON.parse(sessionStorage.getItem(pendingKey) || "null") : null;
  const payload = saved?.payload || (replyTarget
    ? {message_id: replyTarget, body: $("#send-body").value, attachments: replyAttachments}
    : {
      recipients: [$("#send-recipients").value],
      subject: $("#send-subject").value.trim() || null,
      body: $("#send-body").value,
    });
  const status = $("#send-status");
  try {
    const isReply = saved ? Boolean(saved.payload.message_id) : Boolean(replyTarget);
    await post(isReply ? "/api/messages/reply" : "/api/messages/send", payload, key);
    status.textContent = isReply ? "Sent. This message is marked done." : "Sent.";
    if (replyAttachments.length && route === "results") notice("Follow-up sent with the file attached. The delivering message is marked done.");
    $("#send-body").value = "";
    if (!replyTarget) {
      $("#send-recipients").value = "";
      $("#send-subject").value = "";
    } else {
      replyTarget = null;
      replyAttachments = [];
      $("#send-form").classList.remove("reply-mode");
      $("#send-recipients").disabled = false;
      $("#send-subject").disabled = false;
      $("#send-subject").value = "";
    }
    unlockComposer();
    placeComposer();
    await refresh(true);
  } catch (error) {
    status.textContent = `${error.message} Your message is retained; submit again to retry.`;
    notice(status.textContent, true);
    restoreComposer();
    await refresh(true);
  } finally { sending = false; updateActions(); }
}

const context = {$, api, post, element, button, mutationButton, number, total, time, syncChildren, technicalDetails, refresh, reply, instruct, placeComposer, sourceText, connected: () => connected, notice, rememberResult: (result) => results.remember(result)};
const overview = createOverview(context);
const inbox = createInbox(context);
const team = createTeam(context);
const results = createResults(context);
const system = createSystem(context);
async function navigate() {
  const hash = location.hash;
  const [requested, query] = hash.slice(1).split("?");
  const message = new URLSearchParams(query).get("message");
  route = ["overview", "inbox", "results", "team", "system"].includes(requested) ? requested : "overview";
  if (route !== "results" && $("#artifact-dialog").open) $("#artifact-dialog").close();
  if (route !== "results") results.close();
  for (const panel of document.querySelectorAll(".route")) panel.hidden = panel.id !== `route-${route}`;
  for (const a of document.querySelectorAll("#destinations a")) {
    if (a.hash === `#${route}`) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  placeComposer();
  if (route === "results") results.open();
  if (route === "inbox" && message) inbox.openMessage();
  window.scrollTo(0, 0);
  await refresh(true);
  const decision = new URLSearchParams(query).get("decision");
  if (location.hash === hash && route === "inbox" && message && !inbox.revealMessage(message)) {
    notice("This message is outside the latest 200 messages in this Inbox view.");
  }
  if (location.hash === hash && route === "system" && decision && !system.revealDecision(decision)) {
    notice("This decision is no longer in the recent history.");
  }
}
async function refresh(force = false) {
  if (controller && !force) return;
  controller?.abort();
  const current = new AbortController(); controller = current;
  if (force) secondaryController?.abort();
  try {
    try {
      const data = await api("/api/snapshot", current.signal);
      if (current.signal.aborted) return;
      snapshot = data; connected = true; disconnectedAt = null;
      populateRecipients(data);
      const changed = firmStorageScope !== data.firm.console_scope;
      if (changed && firmStorageScope !== null) {
        unlockComposer(); replyTarget = null; replyAttachments = []; $("#send-form").classList.remove("reply-mode");
        $("#send-subject").value = ""; $("#send-body").value = "";
      }
      firmStorageScope = data.firm.console_scope;
      hostStorageScope = `${data.firm.console_scope}:${data.host_epoch}`;
      if (changed) restoreComposer();
      $("#firm-name").textContent = data.firm.firm_id;
      const count = needsYou(data).count;
      $("#decision-count").textContent = count; $("#decision-count").hidden = !count;
      $("#decision-count").setAttribute("aria-label", `${count} decisions waiting`);
      const unread = data.needs_you.owner_mail;
      $("#mail-count").textContent = `${unread} new`; $("#mail-count").hidden = !unread;
      document.title = pageTitle(`${data.firm.firm_id} · Firm console`, count);
      overview.update(data);
    } catch (error) {
      if (current.signal.aborted) return;
      connected = false; disconnectedAt ??= Date.now() / 1000; overview.render();
    }
    renderFreshness(); updateActions();
  } finally { if (controller === current) controller = null; }
  await refreshSecondary(force);
}
async function refreshSecondary(force) {
  if (secondaryController && !force) return;
  secondaryController?.abort();
  const current = new AbortController(); secondaryController = current;
  try {
    const jobs = [refreshControl(current.signal)];
    if (route === "overview") jobs.push(overview.refresh(current.signal));
    if (route === "inbox") jobs.push(inbox.refresh(current.signal, snapshot));
    if (route === "results") jobs.push(results.refresh(current.signal));
    if (snapshot && route === "team") jobs.push(team.update(snapshot, current.signal));
    if (snapshot && route === "system") jobs.push(system.update(snapshot, current.signal));
    const outcomes = await Promise.allSettled(jobs);
    if (current.signal.aborted) return;
    for (const outcome of outcomes) if (outcome.status === "rejected") notice(outcome.reason.message, true);
    renderFreshness(); updateActions();
  } finally { if (secondaryController === current) secondaryController = null; }
}
$("#refresh").addEventListener("click", () => void refresh(true));
$("#send-form").addEventListener("submit", (event) => { event.preventDefault(); void sendComposer(); });
$("#send-recipients").addEventListener("change", recipientLabel);
$("#new-message-draft").addEventListener("click", beginNewDraft);
$("#cancel-reply").addEventListener("click", cancelReply);
for (const action of Object.keys(controls)) {
  const id = {stop: "stop-firm", "force-stop": "force-stop-firm", resume: "resume-firm", close: "close-firm"}[action];
  $(`#${id}`).dataset.control = action;
  $(`#${id}`).title = controls[action][1];
  $(`#${id}`).append(element("span", "control-explanation", controls[action][1]));
  $(`#${id}`).addEventListener("click", () => confirmControl(action));
}
$("#control-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const node = $("#confirm-control"); node.dataset.busy = "true"; updateActions();
  try { await runControl(pendingControl); } catch (error) { notice(error.message, true); }
  finally { delete node.dataset.busy; updateActions(); }
});
$("#cancel-control").addEventListener("click", () => $("#control-dialog").close());
$("#control-dialog").addEventListener("close", () => $("#firm-menu summary").focus());
window.addEventListener("hashchange", () => navigate());
window.setInterval(() => { void refresh(); }, 3000);
window.setInterval(renderFreshness, 1000);
await navigate();
