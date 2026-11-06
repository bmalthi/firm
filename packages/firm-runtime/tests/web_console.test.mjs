import assert from "node:assert/strict";
import test from "node:test";
import {age, bytes, clockTime, time, incidentTitle, vmState, runtimeState, decisionStatus, incidentStatus, inboxGroups, inboxWindow, machineName, needsYou, pageTitle, readinessPhrase, seconds, statusHeadline, statusSentence} from "../src/firm_runtime/web_console/format.js";
import {buildRoleForest} from "../src/firm_runtime/web_console/role_tree.js";
import {filePreviewable, fileFolder, fileDeliveries, resultHref, firmMenuLabel, fileReplyMatches} from "../src/firm_runtime/web_console/format.js";

test("a follow-up draft belongs to its exact file and delivering message", () => {
  const file = {message_id: "m1", path: "shared", name: "report.md"};
  const attached = {path: "shared", name: "report.md"};
  assert.equal(fileReplyMatches(file, "m1", [attached]), true);
  assert.equal(fileReplyMatches(file, "m2", [attached]), false);
  assert.equal(fileReplyMatches({...file, name: "other.md"}, "m1", [attached]), false);
  assert.equal(fileReplyMatches({...file, path: "shared/other"}, "m1", [attached]), false);
  assert.equal(fileReplyMatches(file, "m1", []), false);
  assert.equal(fileReplyMatches(file, "m1", [attached, attached]), false);
});

test("result links preserve untrusted file names and the delivering message", () => {
  const file = {path: "shared/reports & notes", name: "<report>#1?.MD", message_id: "m3"};
  const href = resultHref(file);
  assert.ok(href.startsWith("#results?"));
  const params = new URLSearchParams(href.split("?")[1]);
  assert.equal(params.get("path"), file.path);
  assert.equal(params.get("name"), file.name);
  assert.equal(params.get("message"), "m3");
  assert.equal(new URLSearchParams(resultHref({path: "shared", name: "x"}).split("?")[1]).has("message"), false);
});

test("preview types and size match the server's bounded preview contract", () => {
  for (const name of ["file.html", "file.MD", "file.markdown", "file.txt"]) {
    assert.equal(filePreviewable(name), true);
    assert.equal(filePreviewable(name, 2 * 1024 * 1024), true);
    assert.equal(filePreviewable(name, 2 * 1024 * 1024 + 1), false);
  }
  for (const name of ["file.pdf", "file.md.exe", "file", "file.xhtml"]) assert.equal(filePreviewable(name), false);
});

test("file delivery history matches both folder and name and preserves newest-first order", () => {
  const latest = {path: "shared/reports", name: "report.md", message_id: "m3"};
  const earlier = {...latest, message_id: "m1"};
  const values = [latest, {...latest, name: "other.md"}, {...latest, path: "shared/other"}, earlier];
  assert.deepEqual(fileDeliveries(values, latest), [latest, earlier]);
  assert.equal(values.length, 4);
  assert.equal(fileFolder("shared/reports"), "Shared deliverables › reports");
  assert.equal(fileFolder("org/founder/work"), "org › founder › work");
});

test("Firm menu uses the same sentence pattern in every lifecycle state", () => {
  for (const state of ["running", "stopped", "draining", "stopping", "future", undefined]) {
    assert.equal(firmMenuLabel(state), `Firm: ${runtimeState(state).toLowerCase()}`);
  }
  assert.equal(firmMenuLabel("stopped"), "Firm: not accepting work");
});

test("unmeasured counters stay distinct from measured zero", () => {
  for (const format of [bytes, seconds]) {
    assert.equal(format(null), format(undefined));
    assert.notEqual(format(null), format(0));
  }
  assert.equal(bytes(2 ** 30), "1.0 GiB");
  assert.equal(bytes(2 ** 20), "1 MiB");
  assert.equal(bytes(1024), "1 KiB");
  assert.equal(bytes(120), "120 B");
  assert.equal(bytes(0), "0 B");
  assert.equal(seconds(125), "2m 5s");
});

test("machine labels distinguish the host from its guest VMs", () => {
  const labels = ["host", "linux", "mac"].map(machineName);
  assert.equal(new Set(labels).size, 3);
  assert.equal(machineName("new-machine"), "new-machine");
});

test("decisions include every indefinite incident in both scopes and pending requests, never mail", () => {
  const snapshot = {needs_you: {owner_mail: 200, requests: [
    {request_id: "q1", address: "founder/dev", resource: "pypi.org", purpose: "Install", kind: "network_host"},
    {request_id: "q2", kind: "future_request", status: "pending"},
    {request_id: "q3", status: "denied"},
  ]}, accounts: {incidents: [
    ...["account", "profile"].flatMap((scope) => ["auth", "config", "quota", "outage"].map((kind) =>
      ({incident_id: `${scope}-${kind}`, scope, kind, dependency: "codex-main", until_at: null, resolved_at: null}))),
    {incident_id: "timed", until_at: 100},
    {incident_id: "resolved", resolved_at: 0},
  ]}};
  const before = JSON.stringify(snapshot);
  const {count, items} = needsYou(snapshot);
  assert.equal(count, 10);
  assert.equal(items.filter((item) => item.type === "request").length, 2);
  assert.equal(items.filter((item) => item.type === "incident").length, 8);
  assert.equal(JSON.stringify(snapshot), before);
  assert.equal(pageTitle("demo · Firm console", count), "(10) demo · Firm console");
  assert.equal(pageTitle("demo · Firm console", 0), "demo · Firm console");
  assert.deepEqual(needsYou(undefined), {count: 0, items: []});
});

const row = (reason, extra = {}) => ({employee_id: "e1", identity: "Founder", address: "founder",
  activity: "blocked", primary_reason: reason, reasons: [reason], next_check_at: null,
  actionable_by: "runtime_or_time", blockers: [], ...extra});
const sample = (rows = [row("idle_no_wake")], extra = {}) => ({firm: {state: "running"},
  infrastructure_state: "running", readiness: {sampled_at: 1000, rows}, active: [],
  needs_you: {requests: []}, accounts: {incidents: []}, ...extra});
const blocked = (kind = "auth", scope = "account", until_at = null) => sample([
  row("pinned_dependency", {next_check_at: until_at, actionable_by: "owner_or_manager",
    blockers: [{incident_id: "i1", alternative_account_usable: false}]}),
], {accounts: {incidents: [{incident_id: "i1", dependency: "codex-main", kind, scope, until_at, resolved_at: null}]}});

test("status ladder: disconnection overrides every firm and role state", () => {
  const value = sample([row("running")], {firm: {state: "stopped"}});
  assert.equal(statusHeadline(value, false), "Can't reach the firm");
  assert.match(statusSentence(value, false), /last successful sample/);
});

test("status ladder: stopped, draining and force stop take precedence over work", () => {
  for (const [state, expected] of [["stopped", "Stopped"], ["draining", "Stopping after current work"], ["stopping", "Force stopping"]]) {
    const value = sample([row("running")], {firm: {state}, infrastructure_state: "stopping"});
    assert.equal(statusHeadline(value), expected);
    assert.match(statusSentence(value), /Linux VM: Stopping/);
  }
});

test("status ladder: an admitted or running period is Working, even without readiness", () => {
  for (const state of ["admitted", "running"]) {
    assert.equal(statusHeadline(sample([row(state)])), "Working");
    assert.equal(statusHeadline(sample([], {readiness: null, active: [{employee_id: "e1", state}]})), "Working");
  }
  assert.equal(statusHeadline(sample([], {active: [{employee_id: "e1", state: "completed"}]})), "Not working");
});

test("status ladder: ready, capacity and due reminder precede decisions", () => {
  for (const reason of ["ready", "firm_capacity", "reminder_due"]) {
    const value = blocked();
    value.readiness.rows.push(row(reason, {employee_id: "e2"}));
    assert.equal(statusHeadline(value), "Starting work");
  }
});

test("status ladder: only a decision which blocks a role makes Waiting on you", () => {
  for (const kind of ["auth", "config", "quota", "outage"]) {
    for (const scope of ["account", "profile"]) assert.equal(statusHeadline(blocked(kind, scope)), "Waiting on you");
  }
  const request = sample([row("owner_request_pending", {blockers: [{request_ids: ["q1"]}]})],
    {needs_you: {requests: [{request_id: "q1", kind: "network_host"}]}});
  assert.equal(statusHeadline(request), "Waiting on you");
  const alternative = blocked();
  alternative.readiness.rows = [row("idle_no_wake", {blockers: [{incident_id: "i1", alternative_account_usable: true}]})];
  assert.equal(needsYou(alternative).count, 1);
  assert.equal(statusHeadline(alternative), "Idle, nothing scheduled");
  assert.equal(statusHeadline(sample([row("idle_no_wake")], {needs_you: request.needs_you})), "Idle, nothing scheduled");
});

test("status ladder: indefinite nondecision owner/manager holds precede timed waits", () => {
  for (const [reason, phrase] of Object.entries({refusal: "Held · work refused", attempts_exhausted: "Held · retry attempts exhausted", temporary_exhausted: "Held · temporary work periods exhausted", owner_paused: "Paused by you", level_pending: "Waiting for role routing", profile_config_hold: "Paused · profile configuration needs repair"})) {
    const value = sample([row(reason, {actionable_by: "owner_or_manager"}), row("scheduled_wake", {employee_id: "e2", next_check_at: 5000})]);
    assert.equal(statusHeadline(value), "Held");
    assert.ok(statusSentence(value).includes(`Founder: ${phrase}.`));
    assert.ok(!statusSentence(value).includes(reason));
  }
  assert.equal(statusHeadline(sample([row("refusal", {next_check_at: 5000, reasons: ["refusal", "scheduled_wake"]})])), "Held");
  assert.equal(statusHeadline(sample([row("level_pending", {next_check_at: 5000, reasons: ["level_pending", "scheduled_wake"]})])), "Held");
  assert.equal(statusHeadline(sample([row("level_pending", {next_check_at: 5000, blockers: [{retry_at: 5000}]})])), `Waiting until ${clockTime(5000)}`);
  // Level retry timestamps are retained in blockers but are not currently included
  // in analysis.py's next_check_at. Do not invent a deadline or call them indefinite.
  assert.equal(statusHeadline(sample([row("level_pending", {blockers: [{retry_at: 5000}]})])), "Not working");
});

test("status ladder: timed outage or quota is a wait, never a decision or Held", () => {
  for (const scope of ["account", "profile"]) {
    for (const kind of ["outage", "quota"]) {
      const value = blocked(kind, scope, 5000);
      value.readiness.rows.push(row("retry_wait", {employee_id: "e2", next_check_at: 4000}));
      assert.equal(statusHeadline(value), `Waiting until ${clockTime(4000)}`);
      assert.equal(needsYou(value).count, 0);
    }
  }
});

test("a timed incident does not hide a separately reported account or profile configuration hold", () => {
  for (const scope of ["account", "profile"]) {
    const value = blocked("outage", scope, 5000);
    value.readiness.rows[0].reasons.push(`${scope}_config_hold`);
    assert.equal(statusHeadline(value), "Held");
    assert.equal(statusSentence(value), `0 of 1 roles are working now. Founder: Paused · ${scope} configuration needs repair.`);
    value.accounts.incidents[0].kind = "config";
    assert.equal(statusHeadline(value), `Waiting until ${clockTime(5000)}`);
  }
});

test("Held names the indefinite role blocker before a timed incident, without raw codes", () => {
  const value = blocked("outage", "account", 5000);
  value.readiness.rows[0].reasons.push("refusal", "account_hold");
  assert.equal(statusHeadline(value), "Held");
  assert.equal(statusSentence(value), "0 of 1 roles are working now. Founder: Held · work refused.");
  assert.equal(readinessPhrase(value.readiness.rows[0], value), "Held · work refused");
  assert.equal(statusHeadline(sample([row("future_reason", {actionable_by: "owner_or_manager"})])), "Not working");
});

test("status ladder: post-restart running firm with all idle roles is idle, not Working", () => {
  const value = sample([row("idle_no_wake"), row("pending_mail_no_wake", {employee_id: "e2"}), row("retired", {employee_id: "e3", activity: "retired"})]);
  assert.equal(statusHeadline(value), "Idle, nothing scheduled");
  assert.match(statusSentence(value), /does not mean the mandate is finished/);
  value.readiness.rows[0].reasons.push("future_reason");
  assert.equal(statusHeadline(value), "Not working");
  assert.match(statusSentence(value), /Founder: idle_no_wake, future_reason/);
});

test("status ladder: unknown reasons stay raw and unavailable readiness is never healthy", () => {
  assert.equal(statusHeadline(sample([row("new_reason")])), "Not working");
  assert.equal(statusSentence(sample([row("new_reason")])), "Founder: new_reason.");
  for (const readiness of [null, undefined, {}]) {
    const value = sample([], {readiness});
    assert.equal(statusHeadline(value), "Status unknown");
    assert.match(statusSentence(value), /Readiness is unavailable/);
  }
  assert.equal(statusHeadline(sample([])), "Not working");
});

test("status sentence counts roles once and lists at most two exceptions with identities", () => {
  const value = sample([
    row("running"), row("running", {employee_id: "e2", identity: "Investigator"}),
    row("owner_request_pending", {employee_id: "e3", identity: "Reviewer", blockers: [{request_ids: ["q1"]}]}),
    row("scheduled_wake", {employee_id: "e4", identity: "Researcher", next_check_at: 5000}),
    row("idle_no_wake", {employee_id: "e5", identity: "Archivist"}),
  ], {active: [{employee_id: "e1", state: "running"}], needs_you: {requests: [{request_id: "q1", kind: "network_host"}]}});
  assert.equal(statusSentence(value), `2 of 5 roles are working now. Reviewer is waiting on your decision. Researcher: Idle · next check ${clockTime(5000)}.`);
});

test("readiness phrases cover running age, scheduled wakes, retries and owner requests", () => {
  const value = sample([row("running")], {roles: [{employee_id: "e1", activation: {started_at: 100}}]});
  assert.equal(readinessPhrase(value.readiness.rows[0], value, 1180), "Working · 18 min");
  assert.equal(readinessPhrase(row("admitted"), sample()), "Working");
  assert.equal(readinessPhrase(row("scheduled_wake", {next_check_at: 4000})), `Idle · next check ${clockTime(4000)}`);
  assert.equal(readinessPhrase(row("retry_wait", {next_check_at: 3000, blockers: [{retry_at: 4000}]})), `Retrying at ${clockTime(4000)}`);
  for (const [kind, phrase] of [["network_host", "website access"], ["github_repository", "repository request"], ["mac_station", "Mac station"], ["new_kind", "new_kind"]]) {
    const current = row("owner_request_pending", {blockers: [{request_ids: ["q1"]}]});
    assert.equal(readinessPhrase(current, sample([current], {needs_you: {requests: [{request_id: "q1", kind}]}})), `Waiting on you · ${phrase}`);
  }
  assert.equal(readinessPhrase(row("owner_request_pending")), "Waiting on you · request pending");
  assert.equal(readinessPhrase(null), "Readiness unavailable");
  assert.equal(readinessPhrase(row("new_reason")), "new_reason");
});

test("readiness dependency phrases name the incident and respect timing and alternatives", () => {
  for (const [kind, suffix] of [["auth", "can't sign in"], ["config", "configuration needs repair"], ["quota", "allowance exhausted"], ["outage", "outage hold"]]) {
    const value = blocked(kind);
    assert.equal(readinessPhrase(value.readiness.rows[0], value), `Paused · codex-main ${suffix}`);
  }
  const value = blocked("outage", "profile", 5000);
  assert.equal(readinessPhrase(value.readiness.rows[0], value), `Paused · codex-main until ${clockTime(5000)}`);
  value.readiness.rows[0].reasons = ["idle_no_wake", "profile_hold"];
  assert.equal(readinessPhrase(value.readiness.rows[0], value), `Paused · codex-main until ${clockTime(5000)}`);
  value.readiness.rows[0] = row("idle_no_wake", {blockers: [{incident_id: "i1", alternative_account_usable: true}]});
  assert.equal(readinessPhrase(value.readiness.rows[0], value), "Idle · nothing scheduled");
});

test("every other readiness reason has a phrase; unrecognized reasons remain visible", () => {
  const expected = {
    ready: "Starting soon", firm_capacity: "Queued for a slot", account_capacity: "Queued for an account slot",
    reminder_due: "Reminder due · starting soon", owner_paused: "Paused by you", idle_no_wake: "Idle · nothing scheduled",
    pending_mail_no_wake: "Mail waiting, nothing scheduled", refusal: "Held · work refused",
    attempts_exhausted: "Held · retry attempts exhausted", temporary_exhausted: "Held · temporary work periods exhausted",
    level_pending: "Waiting for role routing", exit_pending: "Finishing current work", retired: "Retired",
    firm_stopped: "Firm stopped", firm_draining: "Stopping after current work", firm_stopping: "Force stopping",
    infrastructure_not_ready: "Waiting for the VM", account_config_hold: "Paused · account configuration needs repair",
    profile_config_hold: "Paused · profile configuration needs repair", account_quota_hold: "Paused · account allowance exhausted",
    account_hold: "Paused · account hold", profile_hold: "Paused · profile hold", pinned_dependency: "Paused · execution dependency held", unknown: "unknown",
  };
  for (const [reason, phrase] of Object.entries(expected)) assert.equal(readinessPhrase(row(reason)), phrase);
  assert.equal(readinessPhrase(row("idle_no_wake", {reasons: ["idle_no_wake", "profile_config_hold"]})), "Paused · profile configuration needs repair");
});

test("ages distinguish missing, future, zero and elapsed samples without rounded overclaims", () => {
  assert.equal(age(null, 100), "Age unavailable");
  assert.equal(age(undefined, 100), "Age unavailable");
  assert.equal(age(NaN, 100), "Age unavailable");
  assert.equal(age(200, 100), "0 s ago");
  assert.equal(age(0, 59), "59 s ago");
  assert.equal(age(0, 60), "1 min ago");
  assert.equal(age(0, 3599), "59 min ago");
  assert.equal(age(0, 3600), "1 h ago");
  assert.equal(age(0, 86400), "1 d ago");
  assert.equal(clockTime(null), "time unavailable");
  assert.match(clockTime(0), /^\d{2}:\d{2}$/);
});

test("role hierarchy is ordered independently of arrival order without changing input", () => {
  const roles = ["founder/z", "founder/a/child", "founder", "founder/a"]
    .map((address) => Object.freeze({address}));
  Object.freeze(roles);
  const forest = buildRoleForest(roles);
  assert.equal(forest.length, 1);
  assert.equal(forest[0].role.address, "founder");
  assert.deepEqual(forest[0].children.map((node) => node.role.address), ["founder/a", "founder/z"]);
  assert.equal(forest[0].children[0].children[0].role.address, "founder/a/child");
  assert.deepEqual(buildRoleForest([...roles].reverse()), forest);
});

test("partially loaded branches stay visible when their parent is absent", () => {
  const role = {address: "founder/team/worker"};
  assert.deepEqual(buildRoleForest([role]), [{role, children: []}]);
  assert.deepEqual(buildRoleForest([]), []);
});

test("incident chips use recorded resolution, duration and explicit owner attribution", () => {
  const incident = {created_at: 0, resolved_at: 2340, resolved_by: "owner"};
  assert.equal(incidentStatus(incident), `Resolved ${time(2340)} · cleared by you · paused 39 min`);
  for (const resolved_by of [null, "runtime", "expired", "superseded"]) {
    assert.equal(incidentStatus({...incident, resolved_by}), `Resolved ${time(2340)} · paused 39 min`);
  }
  assert.equal(incidentStatus({resolved_at: 0}), `Resolved ${time(0)}`);
  assert.equal(incidentStatus({until_at: 3000}), `Paused until ${time(3000)}`);
  assert.equal(incidentStatus({}), "Paused · needs attention");
  assert.equal(incidentStatus({status: null}), "Status unavailable");
  assert.equal(incidentStatus(null), "Status unavailable");
});

test("decision rows name only known decision makers", () => {
  for (const [status, label] of [["granted", "Allowed"], ["denied", "Declined"], ["withdrawn", "Withdrawn"], ["new_status", "new_status"]]) {
    const request = {status, decided_at: 100};
    assert.equal(decisionStatus(request), `${label} ${time(100)}`);
    assert.equal(decisionStatus({...request, decided_by: "owner"}), `${label} ${time(100)} · by you`);
    assert.equal(decisionStatus({...request, decided_by: "runtime"}), `${label} ${time(100)} · by the runtime`);
  }
});

test("timestamps use local calendar days and change from time to date at midnight", () => {
  const date = new Date(2026, 9, 29, 23, 59);
  const at = date.getTime() / 1000;
  assert.equal(time(at, at), "23:59");
  assert.equal(time(at, new Date(2026, 9, 30, 0, 0).getTime() / 1000), `${date.toLocaleDateString()} 23:59`);
  assert.equal(time(at, new Date(2025, 9, 29, 23, 59).getTime() / 1000), `${date.toLocaleDateString()} 23:59`);
  for (const invalid of [null, undefined, NaN, Infinity]) assert.equal(time(invalid, at), "time unavailable");
  assert.equal(time(at, null), "time unavailable");
});

test("incident titles describe both scopes and each cause in plain words", () => {
  for (const [scope, subject] of [["account", "Account"], ["profile", "Profile"]]) {
    for (const [kind, cause] of [["auth", "can't sign in"], ["config", "has a configuration problem"], ["quota", "has an allowance hold"], ["outage", "has an outage hold"], ["future", "has an execution hold"]]) {
      assert.equal(incidentTitle({scope, dependency: "main", kind}), `${subject} main ${cause}`);
    }
  }
});

test("VM and runtime labels share state wording without hiding unknown states", () => {
  for (const [state, label] of Object.entries({running: "Running", stopped: "Stopped", starting: "Starting", stopping: "Stopping", failed: "Failed", unknown: "Unknown", not_configured: "Not configured"})) assert.equal(vmState(state), label);
  for (const [state, label] of Object.entries({running: "Accepting work", stopped: "Not accepting work", draining: "Stopping after current work", stopping: "Force stopping"})) assert.equal(runtimeState(state), label);
  for (const format of [vmState, runtimeState]) {
    assert.equal(format(undefined), "Unknown");
    assert.equal(format("future_state"), "future_state");
  }
});

const mail = (id, at, extra = {}) => ({message_id: id, sequence: at, created_at: at,
  conversation_id: id, author: {employee_id: "role"}, recipients: [{employee_id: "owner"}], ...extra});
test("Inbox groups only by conversation or incident references and orders decisions by decision time", () => {
  const messages = [mail("m1", 10, {conversation_id: "c1"}), mail("m2", 40, {conversation_id: "c1"}),
    mail("m3", 30, {author: {employee_id: "runtime"}, regarding: {kind: "incident", id: "i1"}}),
    mail("m4", 20, {author: {employee_id: "runtime"}, regarding: {kind: "incident", id: "i1"}}),
    mail("m5", 25, {conversation_id: "c1", regarding: {kind: "request", id: "q1"}}),
  ];
  const decisions = [{request_id: "q2", created_at: 1, decided_at: 50}, {request_id: "q1", decided_at: 60}];
  const before = JSON.stringify(messages);
  const groups = inboxGroups(messages, decisions);
  assert.deepEqual(groups.map((group) => group.key), ["conversation:c1", "decision:q2", "incident:i1"]);
  assert.deepEqual(groups[0].messages.map((item) => item.message_id), ["m2", "m5", "m1"]);
  assert.equal(groups[0].decisions[0].request_id, "q1");
  assert.deepEqual(groups[2].messages.map((item) => item.message_id), ["m3", "m4"]);
  assert.equal(JSON.stringify(messages), before);
  assert.deepEqual(inboxGroups([]), []);
});

test("including role mail preserves the selected owner view and its fresh disposition", () => {
  const pending = mail("new", 4, {owner_delivery: {disposition: "pending"}});
  const done = mail("done", 6);
  const between = mail("roles", 5, {recipients: [{employee_id: "another-role"}]});
  assert.deepEqual(inboxWindow([pending], [done, between, mail("new", 4)]), [between, pending]);
  assert.deepEqual(inboxWindow([pending], [between], 1), [between]);
  assert.deepEqual(inboxWindow([]), []);
});
