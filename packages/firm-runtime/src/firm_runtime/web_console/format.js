// Display formatting shared by the console panels and the analysis views.
export function machineName(name) {
  return {host: "This Mac (host)", linux: "Linux VM", mac: "Mac VM"}[name] || name;
}
export const seconds = (value) => value == null ? "Not reported" : value < 60 ? `${Math.round(value)}s`
  : value < 3600 ? `${Math.floor(value / 60)}m ${Math.round(value % 60)}s` : `${Math.floor(value / 3600)}h ${Math.floor(value % 3600 / 60)}m`;
export const bytes = (value) => value == null ? "Not reported" : value >= 1073741824
  ? `${(value / 1073741824).toFixed(1)} GiB` : value >= 1048576 ? `${Math.round(value / 1048576)} MiB`
    : value >= 1024 ? `${Math.round(value / 1024)} KiB` : `${value} B`;
const validTime = (value) => typeof value === "number" && Number.isFinite(value);
export function age(timestamp, now = Date.now() / 1000) {
  if (!validTime(timestamp) || !validTime(now)) return "Age unavailable";
  const elapsed = Math.max(0, Math.floor(now - timestamp));
  if (elapsed < 60) return `${elapsed} s ago`;
  if (elapsed < 3600) return `${Math.floor(elapsed / 60)} min ago`;
  if (elapsed < 86400) return `${Math.floor(elapsed / 3600)} h ago`;
  return `${Math.floor(elapsed / 86400)} d ago`;
}
export function clockTime(timestamp) {
  return validTime(timestamp) ? new Date(timestamp * 1000).toLocaleTimeString(undefined,
    {hour: "2-digit", minute: "2-digit", hourCycle: "h23"}) : "time unavailable";
}

export function time(timestamp, now = Date.now() / 1000) {
  if (!validTime(timestamp) || !validTime(now)) return "time unavailable";
  const date = new Date(timestamp * 1000);
  return date.toDateString() === new Date(now * 1000).toDateString()
    ? clockTime(timestamp) : `${date.toLocaleDateString()} ${clockTime(timestamp)}`;
}

export function incidentTitle(incident) {
  const subject = `${incident.scope === "profile" ? "Profile" : "Account"} ${incident.dependency}`;
  const cause = {auth: "can't sign in", config: "has a configuration problem", quota: "has an allowance hold", outage: "has an outage hold"}[incident.kind] || "has an execution hold";
  return `${subject} ${cause}`;
}

export function vmState(state) {
  return {running: "Running", stopped: "Stopped", starting: "Starting", stopping: "Stopping", failed: "Failed", unknown: "Unknown", not_configured: "Not configured"}[state] || state || "Unknown";
}

export function runtimeState(state) {
  return {running: "Accepting work", stopped: "Not accepting work", draining: "Stopping after current work", stopping: "Force stopping"}[state] || state || "Unknown";
}

export function incidentStatus(incident) {
  if (!incident || incident.status === null) return "Status unavailable";
  if (validTime(incident.resolved_at)) {
    const duration = validTime(incident.created_at)
      ? ` · paused ${Math.floor(Math.max(0, incident.resolved_at - incident.created_at) / 60)} min` : "";
    return `Resolved ${time(incident.resolved_at)}${incident.resolved_by === "owner" ? " · cleared by you" : ""}${duration}`;
  }
  return validTime(incident.until_at) ? `Paused until ${time(incident.until_at)}` : "Paused · needs attention";
}

export function decisionStatus(request) {
  const status = {granted: "Allowed", denied: "Declined", withdrawn: "Withdrawn"}[request.status] || request.status;
  const who = request.decided_by === "owner" ? " · by you" : request.decided_by === "runtime" ? " · by the runtime" : "";
  return `${status} ${time(request.decided_at)}${who}`;
}

// Only recorded conversation/regarding links associate mail. Never match prose or subjects.
export function inboxGroups(messages, decisions = []) {
  const groups = new Map();
  const requestGroups = new Map();
  const ensure = (key, type) => {
    if (!groups.has(key)) groups.set(key, {key, type, messages: [], decisions: [], activity: 0});
    return groups.get(key);
  };
  for (const message of messages) {
    const incident = message.author.employee_id === "runtime" && message.regarding?.kind === "incident";
    const key = incident ? `incident:${message.regarding.id}` : `conversation:${message.conversation_id || message.message_id}`;
    const group = ensure(key, incident ? "incident" : "conversation");
    group.messages.push(message);
    group.activity = Math.max(group.activity, message.created_at);
    if (message.regarding?.kind === "request") requestGroups.set(message.regarding.id, key);
  }
  for (const decision of decisions) {
    const key = requestGroups.get(decision.request_id) || `decision:${decision.request_id}`;
    const group = ensure(key, "decision");
    group.decisions.push(decision);
    group.activity = Math.max(group.activity, decision.decided_at ?? 0);
  }
  for (const group of groups.values()) group.messages.sort((a, b) => b.created_at - a.created_at || b.sequence - a.sequence);
  return [...groups.values()].sort((a, b) => b.activity - a.activity || a.key.localeCompare(b.key));
}

export function inboxWindow(ownerMessages, roleMessages = [], limit = 200) {
  const merged = new Map(ownerMessages.map((message) => [message.message_id, message]));
  for (const message of roleMessages) {
    if (!message.recipients.some((recipient) => recipient.employee_id === "owner") && !merged.has(message.message_id)) {
      merged.set(message.message_id, message);
    }
  }
  return [...merged.values()].sort((a, b) => b.sequence - a.sequence).slice(0, limit);
}

// Only decisions enter the Overview badge and tab title. Owner mail has its own badge.
export function needsYou(snapshot) {
  const requests = (snapshot?.needs_you?.requests || [])
    .filter((item) => item.status == null || item.status === "pending")
    .map((item) => ({...item, type: "request"}));
  const incidents = (snapshot?.accounts?.incidents || [])
    .filter((item) => item.resolved_at == null && item.until_at == null)
    .map((item) => ({...item, type: "incident"}));
  const items = [...requests, ...incidents];
  return {count: items.length, items};
}
export const pageTitle = (base, count) => count > 0 ? `(${count}) ${base}` : base;

const reasonsOf = (row) => row.reasons?.length ? row.reasons : [row.primary_reason || "unknown"];
const roleName = (row) => row.identity || row.address || row.employee_id;
const working = (row) => ["admitted", "running"].includes(row.activity)
  || reasonsOf(row).some((reason) => ["admitted", "running"].includes(reason));
const activeRows = (snapshot) => (snapshot?.readiness?.rows || [])
  .filter((row) => row.activity !== "retired" && !reasonsOf(row).includes("retired"));
const workIds = (snapshot) => new Set([
  ...(snapshot?.active || []).filter((item) => ["admitted", "running"].includes(item.state)).map((item) => item.employee_id),
  ...activeRows(snapshot).filter(working).map((row) => row.employee_id),
]);
function blockingDecisions(row, snapshot) {
  return needsYou(snapshot).items.filter((decision) => (row.blockers || []).some((blocker) =>
    decision.type === "request" ? blocker.request_ids?.includes(decision.request_id)
      : blocker.incident_id === decision.incident_id && !blocker.alternative_account_usable));
}
const ownerHolds = new Set(["refusal", "attempts_exhausted", "temporary_exhausted", "owner_paused"]);
function indefiniteHold(row, snapshot) {
  const reasons = reasonsOf(row);
  // A reminder does not lift an indefinite role hold. next_check_at is not its end.
  const ownerHold = reasons.find((reason) => ownerHolds.has(reason));
  if (ownerHold) return ownerHold;
  if (reasons.includes("level_pending") && !(row.blockers || []).some((blocker) => validTime(blocker.retry_at))) return "level_pending";
  const incidents = (snapshot?.accounts?.incidents || []).filter((item) =>
    (row.blockers || []).some((blocker) => blocker.incident_id === item.incident_id && !blocker.alternative_account_usable));
  // A timed outage cannot explain away a separately reported configuration hold.
  return ["account_config_hold", "profile_config_hold"].find((reason) => reasons.includes(reason)
    && !incidents.some((incident) => reason === `${incident.scope}_config_hold` && incident.kind === "config"));
}

// This is the plan's ordered ladder. Firm state never stands in for role activity.
export function statusHeadline(snapshot, connected = true) {
  if (!connected) return "Can't reach the firm";
  const state = {stopped: "Stopped", draining: "Stopping after current work", stopping: "Force stopping"}[snapshot?.firm?.state];
  if (state) return state;
  if (workIds(snapshot).size) return "Working";
  if (!Array.isArray(snapshot?.readiness?.rows)) return "Status unknown";
  const rows = activeRows(snapshot);
  if (rows.some((row) => reasonsOf(row).some((reason) => ["ready", "firm_capacity", "reminder_due"].includes(reason)))) return "Starting work";
  if (rows.some((row) => blockingDecisions(row, snapshot).length)) return "Waiting on you";
  if (rows.some((row) => indefiniteHold(row, snapshot))) return "Held";
  const deadlines = rows.map((row) => row.next_check_at).filter(validTime);
  if (deadlines.length) return `Waiting until ${clockTime(Math.min(...deadlines))}`;
  if (rows.length && rows.every((row) => reasonsOf(row).every((reason) => ["idle_no_wake", "pending_mail_no_wake"].includes(reason)))) return "Idle, nothing scheduled";
  return "Not working";
}

export function filePreviewable(name, size = 0) {
  return /\.(html|md|markdown|txt)$/i.test(name) && size <= 2 * 1024 * 1024;
}

export function resultHref(result) {
  const params = new URLSearchParams({path: result.path, name: result.name});
  if (result.message_id) params.set("message", result.message_id);
  return `#results?${params}`;
}

export function fileFolder(path) {
  return path.split("/").map((part, index) => index === 0 && part === "shared" ? "Shared deliverables" : part).join(" › ");
}

export function fileDeliveries(results, file) {
  return results.filter((item) => item.path === file.path && item.name === file.name);
}

export function fileReplyMatches(file, messageId, attachments) {
  return file.message_id === messageId && attachments.length === 1
    && file.path === attachments[0].path && file.name === attachments[0].name;
}

export function firmMenuLabel(state) {
  return `Firm: ${runtimeState(state).toLowerCase()}`;
}

const phrases = {
  ready: "Starting soon",
  firm_capacity: "Queued for a slot",
  account_capacity: "Queued for an account slot",
  reminder_due: "Reminder due · starting soon",
  owner_paused: "Paused by you",
  idle_no_wake: "Idle · nothing scheduled",
  pending_mail_no_wake: "Mail waiting, nothing scheduled",
  refusal: "Held · work refused",
  attempts_exhausted: "Held · retry attempts exhausted",
  temporary_exhausted: "Held · temporary work periods exhausted",
  level_pending: "Waiting for role routing",
  exit_pending: "Finishing current work",
  retired: "Retired",
  firm_stopped: "Firm stopped",
  firm_draining: "Stopping after current work",
  firm_stopping: "Force stopping",
  infrastructure_not_ready: "Waiting for the VM",
  account_config_hold: "Paused · account configuration needs repair",
  profile_config_hold: "Paused · profile configuration needs repair",
  account_quota_hold: "Paused · account allowance exhausted",
  account_hold: "Paused · account hold",
  profile_hold: "Paused · profile hold",
  pinned_dependency: "Paused · execution dependency held",
};
const requestKinds = {network_host: "website access", github_repository: "repository request", mac_station: "Mac station"};
export function readinessPhrase(row, snapshot, now = Date.now() / 1000) {
  if (!row) return "Readiness unavailable";
  if (working(row)) {
    const activation = (snapshot?.roles || []).find((role) => role.employee_id === row.employee_id)?.activation;
    const elapsed = validTime(activation?.started_at) ? ` · ${age(activation.started_at, now).replace(/ ago$/, "")}` : "";
    return `Working${elapsed}`;
  }
  const reasons = reasonsOf(row);
  const decisions = blockingDecisions(row, snapshot);
  const request = decisions.find((item) => item.type === "request");
  if (request && reasons.includes("owner_request_pending")) return `Waiting on you · ${requestKinds[request.kind] || request.kind}`;
  const hold = indefiniteHold(row, snapshot);
  if (hold) return phrases[hold];
  const incident = (snapshot?.accounts?.incidents || []).find((item) =>
    (row.blockers || []).some((blocker) => blocker.incident_id === item.incident_id && !blocker.alternative_account_usable));
  if (incident && reasons.some((reason) => reason.includes("hold") || reason === "pinned_dependency")) {
    const cause = validTime(incident.until_at) ? `until ${clockTime(incident.until_at)}`
      : {auth: "can't sign in", config: "configuration needs repair", quota: "allowance exhausted", outage: "outage hold"}[incident.kind] || incident.kind;
    return `Paused · ${incident.dependency} ${cause}`;
  }
  // Readiness can list idle mail before the account hold that prevents its next work.
  const reason = reasons.find((code) => !["idle_no_wake", "pending_mail_no_wake", "scheduled_wake"].includes(code)) || reasons[0];
  if (reason === "scheduled_wake") return `Idle · next check ${clockTime(row.next_check_at)}`;
  if (reason === "retry_wait") return `Retrying at ${clockTime((row.blockers || []).find((blocker) => validTime(blocker.retry_at))?.retry_at ?? row.next_check_at)}`;
  if (reason === "owner_request_pending") return "Waiting on you · request pending";
  return phrases[reason] || reason;
}

export function statusSentence(snapshot, connected = true) {
  const headline = statusHeadline(snapshot, connected);
  if (!connected) return "Showing what was true at the last successful sample; it may have changed. Buttons that change the firm are off until the connection returns.";
  if (headline === "Status unknown") return "Readiness is unavailable. The firm's current work status is unknown.";
  const rows = activeRows(snapshot);
  const ids = workIds(snapshot);
  const count = `${ids.size} of ${Math.max(rows.length, ids.size)} roles are working now.`;
  const vm = `Linux VM: ${vmState(snapshot?.infrastructure_state)}.`;
  if (headline === "Stopped") return `The firm is not accepting work. ${vm}`;
  if (headline === "Stopping after current work") return `${count} The VMs shut down when current work exits. ${vm}`;
  if (headline === "Force stopping") return `${count} Active work is being interrupted. ${vm}`;
  if (headline === "Idle, nothing scheduled") return "No role is working and none has a wake scheduled. Nothing happens until someone sends work. This does not mean the mandate is finished.";
  if (headline === "Not working") return rows.length
    ? rows.map((row) => `${roleName(row)}: ${reasonsOf(row).join(", ")}`).join(". ") + "."
    : "No active roles have readiness observations.";
  const exceptions = rows.filter((row) => !ids.has(row.employee_id));
  // Put the hold or decision responsible for the headline before incidental idle roles.
  exceptions.sort((a, b) => Number(Boolean(blockingDecisions(b, snapshot).length || indefiniteHold(b, snapshot)))
    - Number(Boolean(blockingDecisions(a, snapshot).length || indefiniteHold(a, snapshot))));
  return [count, ...exceptions.slice(0, 2).map((row) => {
    if (blockingDecisions(row, snapshot).length) return `${roleName(row)} is waiting on your decision.`;
    return `${roleName(row)}: ${readinessPhrase(row, snapshot)}.`;
  })].join(" ");
}
