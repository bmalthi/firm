// Observations only. No runtime commands or provider-backed summaries.
import {bytes, machineName, seconds} from "./format.js";

const $ = (selector) => document.querySelector(selector);
const root = $("#analysis");
const rows = new Map();
const names = new Map();
const expanded = new Set();
const conversationContents = new Map();
let section = "timeline", cursor = null, filters = {}, generation = 0;
let pageMeta = null, overviewBusy = false;
let usageBreakdown = "role";
let viewError = "";
const number = (value) => value == null ? "Unknown" : Number(value).toLocaleString();
const time = (value) => value == null ? "Unknown" : new Date(value * 1000).toLocaleString();
const label = (id) => names.get(id) || id || "Unknown";
const ratio = (value) => value == null ? "Not reported" : `${Math.round(value * 100)}%`;
const millis = (value) => value == null ? "Not reported" : value < 1000 ? `${Math.round(value)}ms` : seconds(value / 1000);
// The window's last sample, not `disk` current state: kept apart so historical
// analysis of a past window is never mistaken for what a machine looks like now.
const diskLast = (m) => m.disk_free_last_bytes == null ? "Not reported"
  : `${bytes(m.disk_used_last_bytes)} of ${bytes(m.disk_total_bytes)} · ${bytes(m.disk_free_last_bytes)} free${m.low_disk_last ? " · low" : ""}`;
// A lease's own reading, machine-wide, not what the lease used; unknown for a lease
// recorded before this was tracked.
const leaseDisk = (d) => d.free_bytes == null && d.total_bytes == null ? "Unknown"
  : `${bytes(d.free_bytes)} free of ${bytes(d.total_bytes)}`;
// Reclaimed is the station's own figure, not machine-wide; null when reclaim did not run.
const reclaimed = (bytesValue) => bytesValue == null ? "Unknown" : bytes(bytesValue);
function rememberNames(data) {
  for (const [id, role] of Object.entries(data.role_labels || {})) {
    names.set(id, role.identity && role.identity !== role.address ? `${role.identity} · ${role.address}` : role.address);
  }
}
const reason = (code) => code === "ready" ? "No observed blocker" : code.replaceAll("_", " ");
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function button(text, action) {
  const node = el("button", text);
  node.type = "button";
  node.addEventListener("click", async () => {
    node.disabled = true;
    try { await action(); } catch (error) { status(error.message, true); }
    finally { node.disabled = false; }
  });
  return node;
}
function status(text, error = false) {
  $("#analysis-status").textContent = viewError || text;
  $("#analysis-status").classList.toggle("error", error || Boolean(viewError));
}
async function api(path, params = {}) {
  const response = await fetch(`/api/analysis/${path}?${new URLSearchParams(params)}`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || `Analysis unavailable (${response.status})`);
  return data;
}
function captureFilters() {
  const result = {};
  const mode = $("#analysis-window").value;
  if (mode === "all") result.from = 0;
  if (mode === "custom") {
    const start = new Date($("#analysis-from").value).getTime() / 1000;
    const end = new Date($("#analysis-until").value).getTime() / 1000;
    if (!Number.isFinite(start) || !Number.isFinite(end) || start >= end) throw new Error("Choose a valid start and end time.");
    result.from = start; result.until = end;
  }
  const role = $("#analysis-role").value.trim();
  if (role) result.employee_id = role;
  return result;
}
function table(headers, values) {
  const table = el("table", undefined, "analysis-table");
  const head = el("thead"), heading = el("tr"), body = el("tbody");
  headers.forEach((text) => { const cell = el("th", text); cell.scope = "col"; heading.append(cell); });
  head.append(heading);
  for (const valuesRow of values) {
    const row = el("tr");
    for (const value of valuesRow) {
      const cell = el("td");
      if (value instanceof Node) cell.append(value); else cell.textContent = value ?? "Unknown";
      row.append(cell);
    }
    body.append(row);
  }
  table.append(head, body);
  return table;
}
function detail(id, title, content) {
  const node = el("details", undefined, "analysis-detail");
  node.append(el("summary", title), content);
  node.open = expanded.has(id);
  node.addEventListener("toggle", () => {
    if (!node.isConnected) return;
    if (node.open) expanded.add(id); else expanded.delete(id);
  });
  return node;
}
function token(metric) {
  if (!metric) return "Unknown";
  return metric.value == null ? "Not reported" : `${number(metric.value)}${metric.complete ? "" : " · partial"}`;
}
async function overview() {
  if (!root.open || overviewBusy) return;
  overviewBusy = true;
  const current = generation;
  try {
    const data = await api("overview", filters);
    if (generation !== current) return;
    rememberNames(data);
    const selected = $("#analysis-role").value;
    const all = el("option", "All roles"); all.value = "";
    $("#analysis-role").replaceChildren(all, ...[...names].map(([id, name]) => {
      const option = el("option", name); option.value = id; return option;
    }));
    $("#analysis-role").value = selected;
    const coverage = data.coverage;
    status(`Recorded firm state: ${data.firm_state}. Host liveness is unverified.` +
      (coverage.recording_failure_pending || coverage.recorded_gaps ? " Some history is missing." : "") +
      (coverage.journal_recording_from == null ? " Historical recording unavailable." : ""));
  } catch (error) { if (generation === current) status(error.message, true); }
  finally { overviewBusy = false; }
}
function timeline(items) {
  const outcome = (r) => {
    if (r.open) return r.started_at == null ? "Starting" : "Working · last recorded";
    if (r.failure_category === "host_restart") return "Interrupted · end uncertain";
    if (r.state === "completed") return r.next_intent === "continue" ? "Handed off · continuing" : "Work period ended";
    return reason(r.state);
  };
  const woken = (r) => [...new Set((r.wake_reasons || []).map((w) => reason(w.reason || "unknown")))].join(", ") || "Not recorded";
  // Names and counts only; arguments and results are never recorded.
  const called = (r) => {
    const tools = Object.entries(r.tool_calls || {}).sort((a, b) => b[1] - a[1]);
    const operations = Object.entries(r.operations || {}).sort((a, b) => b[1] - a[1]);
    const node = el("div");
    node.append(el("div", tools.length
      ? tools.slice(0, 4).map(([name, count]) => `${name} ×${count}`).join(" · ") + (tools.length > 4 ? ` · +${tools.length - 4} more` : "")
      : "Tool calls not measured"));
    if (operations.length) node.append(el("div", operations.map(([name, count]) => `${name.replaceAll("_", " ")} ×${count}`).join(" · "), "role-meta"));
    return node;
  };
  const periods = [...items].sort((a, b) => b.created_at - a.created_at || a.id.localeCompare(b.id));
  return [table(["Role", "Work period", "Duration", "Woken by", "What it called", "Outcome"], periods.map((r) => {
    const period = el("div");
    period.append(el("div", r.started_at == null ? `Requested ${time(r.created_at)}` : time(r.started_at)));
    if (!r.open) period.append(el("div", `to ${time(r.finished_at)}`, "role-meta"));
    return [label(r.employee_id), period,
      r.duration_seconds == null ? "—" : `${seconds(r.duration_seconds)}${r.time_quality === "observed" ? " · observed" : ""}`,
      woken(r), called(r), outcome(r)];
  }))];
}
function usage(items) {
  if (usageBreakdown === "tool") {
    const tools = [...items].sort((a, b) => b.calls - a.calls);
    const meta = pageMeta.tool_calls || {};
    return [el("p", `${number(meta.calls)} tool calls from ${number(meta.tallied_executions)} of ${number(meta.eligible_executions)} work periods. Periods with no tally were not measured, not idle. Names only: arguments and results are never recorded.`, "role-meta"),
      table(["Tool", "Where", "Calls", "Work periods"], tools.map((r) => [r.label, r.group === "guest" ? "In the guest" : `Runtime · ${r.group}`, number(r.calls), number(r.executions)]))];
  }
  const titleFor = (r) => r.type === "session" ? `${label(r.employee_id)} · ${r.session_id.slice(0, 8)}`
    : r.type === "role" ? label(r.label) : Array.isArray(r.label) ? r.label.map((v) => v || "Unknown").join(" / ") : r.label || "Unknown";
  const sorted = [...items].sort((a, b) => (b.usage.input_tokens?.value ?? -1) - (a.usage.input_tokens?.value ?? -1));
  const heading = {role: "Role", model: "Configured model", session: "Conversation", workload: "Work type"}[usageBreakdown];
  const models = (r) => (r.resolved_models || [null]).map((m) => m ?? "Unknown").join(", ");
  const nodes = [el("p", `Input ${token(pageMeta.totals.input_tokens)} · output ${token(pageMeta.totals.output_tokens)} · ${pageMeta.sessions_used} model conversations.`, "role-meta"),
    table([heading, "Reported models", "Runs", "Input tokens", "Cached input", "Output tokens"], sorted.map((r) => [titleFor(r), models(r), number(r.executions), ...["input_tokens", "cached_input_tokens", "output_tokens"].map((key) => token(r.usage[key]))]))];
  const temporary = items.filter((r) => r.type === "role" && r.appointment?.kind === "temporary");
  if (temporary.length) nodes.push(el("h3", "Temporary roles"), table(["Role", "Work periods used", "Work periods remaining"], temporary.map((r) => [label(r.label), number(r.appointment.used_periods), number(r.appointment.remaining_periods)])));
  return nodes;
}
function conversationDetail(id, cacheKey = id) {
  if (conversationContents.has(cacheKey)) return conversationContents.get(cacheKey);
  const content = el("div");
  conversationContents.set(cacheKey, content);
  let next = null;
  const more = button("Load more messages", () => load(next));
  async function load(cursorValue) {
    const data = await api(`conversations/${encodeURIComponent(id)}`, {from: 0, ...(filters.employee_id ? {employee_id: filters.employee_id} : {}), ...(cursorValue ? {cursor: cursorValue} : {})});
    for (const message of data.rows) {
      const article = el("article", undefined, "message-card");
      article.append(el("strong", `${label(message.author_id)} · ${time(message.created_at)}`), el("p", message.subject), el("p", message.body, "message-body"), el("p", message.deliveries.map((d) => `${label(d.employee_id)}: ${d.disposition === "claimed" ? "picked up" : d.disposition}`).join(" · "), "role-meta"));
      content.append(article);
    }
    next = data.next_cursor;
    more.hidden = !next; content.append(more);
    if (!data.rows.length && !cursorValue) content.prepend(el("p", "No stored messages in this conversation."));
  }
  content.append(button("Read stored messages", async () => { content.replaceChildren(); await load(null); }));
  return content;
}
function messaging(items) {
  const conversations = items.filter((r) => r.type === "conversation");
  const traffic = items.filter((r) => r.type === "traffic");
  const backlog = items.filter((r) => r.type === "backlog");
  const nodes = [el("p", `${pageMeta.message_count} messages · typical time to pick up mail: ${seconds(pageMeta.claim_delay.median_seconds)}.`, "role-meta")];
  if (conversations.length) nodes.push(el("h3", "Conversations"),
    table(["Between", "First subject in window", "Messages", "Waiting to be picked up"], conversations.sort((a, b) => b.messages - a.messages).map((r) => [detail(r.id, r.participants.map(label).join(", "), conversationDetail(r.conversation_id)), r.subject || "", r.messages, r.dispositions.pending || 0])));
  if (traffic.length) nodes.push(el("h3", "Who is talking to whom"), table(["From", "To", "Messages delivered"], traffic.map((r) => [label(r.author_id), label(r.employee_id), r.deliveries])));
  if (backlog.length) nodes.push(el("h3", "Older mail still waiting"), table(["Recipient", "Waiting for", "Conversation"], backlog.map((r) => [label(r.employee_id), seconds(r.pending_age_seconds), detail(r.id, "Read conversation", conversationDetail(r.conversation_id, r.id))])));
  const undeliverable = Object.entries(pageMeta.undeliverable_reasons || {}).sort((a, b) => b[1] - a[1]);
  if (undeliverable.length) nodes.push(el("h3", "Mail that could not be delivered"),
    table(["Reason", "Deliveries"], undeliverable.map(([why, count]) => [why, number(count)])));
  return nodes;
}
function readiness(items) {
  const activities = {running: "Working", admitted: "Starting", ready: "Queued", idle: "Idle", blocked: "Blocked", retired: "Retired"};
  const explanations = {
    idle_no_wake: "No work requested", scheduled_wake: "Scheduled to run later", retry_wait: "Waiting to retry",
    owner_paused: "Paused by owner", firm_capacity: "All execution slots occupied", account_capacity: "Account slots occupied",
    temporary_exhausted: "Temporary work allowance used up", pending_mail_no_wake: "Mail waiting without a work request",
    exit_pending: "Stopping", ready: "Waiting for an execution slot", pinned_dependency: "Waiting for an account or profile issue",
  };
  return [table(["Role", "Current activity", "Waiting on", "Next check"], items.map((r) => {
    const active = ["running", "admitted"].includes(r.activity);
    const reasons = r.reasons.filter((code) => !["running", "admitted", "ready"].includes(code)
      && !(active && ["idle_no_wake", "firm_capacity", "account_capacity"].includes(code)));
    return [label(r.employee_id), activities[r.activity] || reason(r.activity),
      reasons.map((code) => explanations[code] || reason(code)).join("; ") || "—", r.next_check_at == null ? "—" : time(r.next_check_at)];
  }))];
}
function mac(items) {
  const leases = items.filter((r) => r.type === "lease");
  const busy = items.filter((r) => r.type === "busy");
  const diskFull = items.filter((r) => r.type === "disk_full");
  const nodes = [];
  if (leases.length) nodes.push(el("h3", "Leases"), table(["Role", "Station", "Acquired", "Released", "Held for", "Outcome", "Free at acquire", "Free at release", "Reclaimed at release"], leases.map((r) => [
    label(r.employee_id), r.station == null ? "—" : String(r.station), time(r.acquired_at), r.open ? "still held" : time(r.released_at), seconds(r.duration_seconds), r.outcome || "held",
    leaseDisk(r.disk.acquired), leaseDisk(r.disk.released), reclaimed(r.disk.released.reclaimed_bytes)])));
  if (busy.length) nodes.push(el("h3", "Turned away: no station free, some in use"), table(["Role", "When"], busy.map((r) => [label(r.employee_id), time(r.at)])));
  if (diskFull.length) nodes.push(el("h3", "Refused: the Mac was too full"), table(["Role", "Station", "When", "Free", "Reclaimed first"], diskFull.map((r) => [
    label(r.employee_id), r.station == null ? "—" : String(r.station), time(r.at), leaseDisk(r), reclaimed(r.reclaimed_bytes)])));
  // Every call is a row, which is far too many to show; the summary is what it costs.
  const operations = Object.entries(pageMeta.by_operation || {}).sort((a, b) => b[1].count - a[1].count);
  if (operations.length) nodes.push(el("h3", "Desktop operations"), table(["Operation", "Calls", "Median", "Longest", "Refused"], operations.map(([op, o]) => [
    op.replaceAll("_", " "), number(o.count), millis(o.median_duration_ms), millis(o.max_duration_ms), number(o.failures)])));
  return nodes;
}
function resources(items) {
  const machines = Object.entries(pageMeta.machines || {});
  const runs = items.filter((r) => r.type === "run");
  const nodes = [];
  if (machines.length) nodes.push(el("h3", "Machines"),
    table(["Machine", "Samples", "Memory", "Lowest free", "CPU busy (mean / p95)", "Stalled (memory / CPU)", "OOM kills", "Swapouts", "Disk (window peak)", "Disk (last sample)"], machines.map(([name, m]) => [
      machineName(name), `${number(m.samples)}${m.boots > 1 ? ` · ${m.boots} boots` : ""}`,
      bytes(m.memory_total_bytes), bytes(m.memory_available_low_bytes),
      `${ratio(m.cpu_utilization_mean)} / ${ratio(m.cpu_utilization_p95)}`,
      `${seconds(m.memory_stall_seconds)} / ${seconds(m.cpu_stall_seconds)}`,
      number(m.oom_kills), number(m.swapouts),
      m.disk_total_bytes == null ? "Not reported" : `${bytes(m.disk_used_max_bytes)} of ${bytes(m.disk_total_bytes)}`,
      diskLast(m),
    ])));
  if (runs.length) nodes.push(el("h3", "Work periods"),
    table(["Role", "Started", "Duration", "Peak memory", "CPU time", "Mean vCPUs", "OOM kills"], runs.sort((a, b) => (b.memory_peak_bytes ?? -1) - (a.memory_peak_bytes ?? -1)).map((r) => [
      label(r.employee_id), time(r.started_at), seconds(r.duration_seconds), bytes(r.memory_peak_bytes),
      r.cpu_seconds == null ? "Not reported" : seconds(r.cpu_seconds),
      r.mean_vcpus == null ? "Not reported" : r.mean_vcpus.toFixed(2), number(r.oom_kills)])));
  nodes.push(el("p", `Peaks include page cache, so they bound demand from above. If every measured work period had peaked at once: ${bytes(pageMeta.run_memory_peak_concurrent_bound_bytes)} across at most ${number(pageMeta.max_active)} at a time.`, "role-meta"));
  return nodes;
}
function render() {
  const target = $("#analysis-content");
  const items = [...rows.values()];
  const views = {timeline, usage, conversations: messaging, readiness, mac, resources};
  const empty = section === "mac" && $("#analysis-tabs [data-section=mac]").hidden
    ? "This firm has no Mac." : "No recorded observations in this selection.";
  const present = items.length
    || (section === "resources" && Object.keys(pageMeta?.machines || {}).length);
  target.replaceChildren(...(present ? views[section](items) : [el("p", empty, "muted")]));
  $("#analysis-more").hidden = !cursor;
}
async function load(more = false) {
  const current = generation;
  $("#analysis-more").disabled = true;
  try {
    const rowType = section === "timeline" ? "activation" : section === "usage" ? usageBreakdown
      : section === "resources" ? "run" : null;
    const data = await api(section, {...filters, ...(rowType ? {row_type: rowType} : {}), ...(more && cursor ? {cursor} : {})});
    if (current !== generation) return;
    if (!more) rows.clear();
    for (const row of data.rows) rows.set(row.id, row);
    cursor = data.next_cursor; pageMeta = data; viewError = "";
    rememberNames(data);
    const descriptions = {
      timeline: "Each row is one work period: why it was woken, and what it called while it ran. An ended period does not mean the role is done; new mail can start another.",
      usage: "Usage for runs admitted in this window. Cached input is part of input; conversations can span several work periods.",
      conversations: "Messages sent in this window, by the people talking, with their current pickup status. Picking up mail does not mean the task is complete.",
      readiness: "Current recorded activity and blockers, regardless of the selected history window. Host liveness is unverified.",
      mac: "Mac leases overlapping this window, the roles turned away while no station was free or the Mac was too full, and what each brokered desktop call cost.",
      resources: "What the machines and each work period used. Counters are sampled once a minute while a VM runs, so short dips are not seen.",
    };
    $("#analysis-description").textContent = `${descriptions[section]} Updated ${time(data.sampled_at)}.${data.has_more ? ` Showing ${rows.size} of ${data.total_rows}; load more for the rest.` : ""}`;
    $("#analysis-view-options").hidden = section !== "usage";
    render();
  } catch (error) { if (current === generation) { viewError = `Analysis failed: ${error.message}. Last loaded observations remain visible.`; status(viewError, true); } }
  finally { $("#analysis-more").disabled = false; }
}
async function refresh() {
  try { filters = captureFilters(); } catch (error) { status(error.message, true); return; }
  generation++; cursor = null; conversationContents.clear();
  await Promise.all([overview(), load()]);
}
$("#analysis-filters").addEventListener("submit", (event) => { event.preventDefault(); refresh(); });
$("#analysis-window").addEventListener("change", () => document.querySelectorAll(".analysis-custom").forEach((node) => { node.hidden = $("#analysis-window").value !== "custom"; }));
$("#analysis-tabs").addEventListener("click", (event) => {
  const next = event.target.dataset.section;
  if (!next) return;
  section = next; generation++; cursor = null;
  document.querySelectorAll("#analysis-tabs button").forEach((node) => node.setAttribute("aria-pressed", String(node.dataset.section === section)));
  load();
});
$("#analysis-more").addEventListener("click", () => load(true));
root.addEventListener("toggle", () => { if (root.open && !pageMeta) refresh(); });
$("#analysis-breakdown").addEventListener("change", (event) => {
  usageBreakdown = event.target.value;
  generation++; cursor = null;
  load();
});
setInterval(overview, 3000);
