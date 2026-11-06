// System owns these observations; analysis.js keeps its persistent diagnostic panel.
import {age, bytes, machineName, incidentStatus, incidentTitle, decisionStatus, vmState, runtimeState} from "./format.js";

export function createSystem({$, api, element, number, time, sourceText, technicalDetails, syncChildren}) {
  let snapshot = null;
  let resources = null;
  let resourcesAt = 0;
  let decisions = null;
  let incidents = null;
  let renderedDay = new Date().toDateString();
  const signatures = new Map();
  const historyRows = new Map();

  function keyed(node, key) { node.dataset.systemKey = key; return node; }
  function nodeKey(node) {
    return node.dataset.systemKey || (node.tagName === "SUMMARY" ? "summary"
      : node.matches("details.technical-details") ? "technical" : null);
  }
  // Retain cards, disclosures and their summaries. Refreshing their contents must
  // not close an owner's disclosure or replace the control holding keyboard focus.
  function syncRecords(root, nodes) {
    const existing = new Map([...root.children].filter(nodeKey).map((node) => [nodeKey(node), node]));
    const retained = nodes.map((node) => {
      const previous = existing.get(nodeKey(node));
      if (!previous || previous.tagName !== node.tagName) return node;
      previous.className = node.className;
      if (node.children.length) syncRecords(previous, [...node.children]);
      else previous.textContent = node.textContent;
      return previous;
    });
    syncChildren(root, retained);
  }

  function name(id, address) {
    return snapshot?.readiness?.rows.find((role) => role.employee_id === id || role.address === address)?.identity
      || snapshot?.roles.find((role) => role.employee_id === id || role.address === address)?.identity
      || address || "Role";
  }
  function observation(label, value, measured) {
    const row = element("tr");
    const title = element("th", "", label); title.scope = "row";
    row.append(title, element("td", "", value), element("td", "role-meta", measured));
    return row;
  }
  function renderHealth() {
    if (!snapshot) return;
    const measured = sourceText("/api/snapshot", true);
    const rows = [
      observation("Firm runtime", runtimeState(snapshot.firm.state), measured),
      observation("Linux VM", vmState(snapshot.infrastructure_state), measured),
    ];
    if (snapshot.mac?.configured) {
      rows.push(observation("Mac VM", snapshot.mac.state ? vmState(snapshot.mac.state) : `${snapshot.mac.stations.length} stations configured`, measured));
      for (const station of snapshot.mac.stations || []) {
        const status = !station.enrolled ? "Not enrolled" : station.held
          ? `In use by ${name(station.holder, station.holder)} since ${time(station.since)}`
          : station.dirty ? "Needs owner recovery" : "Free";
        rows.push(observation(`Mac station · ${station.station}`, status, measured));
      }
    }
    const disks = resources?.disk || {};
    for (const [machine, disk] of Object.entries(disks)) {
      if (machine === "mac" && !snapshot.mac?.configured) continue;
      const stale = disk.stale || Boolean(sourceText("/api/analysis/resources"));
      const status = disk.unknown ? "Disk reading unknown"
        : `${bytes(disk.free_bytes)} free of ${bytes(disk.total_bytes)}${stale ? " · stale reading" : disk.low_disk ? " · low disk" : ""}`;
      rows.push(observation(`${machineName(machine)} disk`, status, age(disk.observed_at)));
    }
    if (!Object.keys(disks).length) rows.push(observation("Disk space", "No disk readings available", sourceText("/api/analysis/resources", true)));
    $("#health-rows").replaceChildren(...rows);
  }
  function renderAccounts() {
    const status = snapshot.accounts;
    $("#capacity-summary").textContent = `${snapshot.active.length} working · ${snapshot.waiting_roles} queued · ${snapshot.held.length} with a recorded hold or retry`;
    const signature = JSON.stringify(status);
    if (signatures.get("accounts") === signature) return;
    signatures.set("accounts", signature);
    const cards = status.accounts.map((account) => {
      const card = keyed(element("article", "account-card"), `account:${account.account_id}`);
      const holds = status.incidents.filter((incident) => incident.scope === "account" && incident.dependency === account.account_id);
      card.append(element("strong", "", account.account_id), element("p", "role-meta", `${account.provider} · ${holds.length ? "paused" : "available"} · ${account.active} of ${account.max_active} slots in use${account.isolated ? ` · ${account.isolated} isolated` : ""}`));
      for (const hold of holds) card.append(element("p", "role-meta", `${hold.kind} · ${incidentStatus(hold)}`));
      const quota = account.subscription_quota;
      if (quota) {
        const windows = element("ul", "quota-windows");
        for (const window of quota.windows || []) {
          windows.append(element("li", "", `${window.label}: ${window.used_percent}% used · ${window.expired ? "reset passed" : "resets"} ${time(window.resets_at)}${quota.state === "stale" || window.expired ? " · stale · last recorded reading" : ""}`));
        }
        card.append(windows);
        let detail = "";
        if (quota.state === "awaiting_stream") detail = "Subscription allowance appears after a Claude role runs.";
        else if (!quota.observed_at) detail = quota.state === "loading" ? "Checking subscription allowance…" : "Subscription allowance unavailable; will retry.";
        else if (quota.state === "stale" || quota.error) detail = `Stale quota reading · observed ${time(quota.observed_at)}${quota.checked_at ? ` · checked ${time(quota.checked_at)}` : ""}${quota.error ? " · latest refresh unavailable" : ""}`;
        if (detail) card.append(element("p", "quota-detail", detail));
        card.append(technicalDetails(`Quota observed ${time(quota.observed_at)}${quota.polled ? ` · checked ${time(quota.checked_at)} · refreshes every 5 minutes` : " · updates while Claude roles run"}`));
      } else card.append(element("p", "quota-detail", account.headroom === "known"
        ? `${Math.round(account.remaining_fraction * 100)}% allowance remaining (last supplied reading)`
        : "Remaining allowance unavailable · no fresh quota reading. Work remains eligible unless blocked."));
      return card;
    });
    for (const incident of status.incidents.filter((item) => item.scope !== "account")) {
      const card = keyed(element("article", "account-card"), `incident:${incident.incident_id}`);
      card.append(element("strong", "", incidentTitle(incident)), element("p", "role-meta", incidentStatus(incident)), technicalDetails(`Incident ${incident.incident_id} · ${incident.scope}`));
      cards.push(card);
    }
    for (const warning of status.warnings || []) cards.push(element("p", "notice", warning));
    if (!cards.length) cards.push(element("p", "muted", "No named accounts configured."));
    if (!status.incidents.length) cards.push(element("p", "role-meta", "No open incidents."));
    syncRecords($("#accounts"), cards);
  }
  function disclosure(key, title, nodes) {
    const details = keyed(element("details", "system-disclosure"), key);
    details.append(element("summary", "", title), ...nodes);
    return details;
  }
  function renderNetwork(page) {
    const signature = JSON.stringify({...page, sampled_at: null, window: null});
    if (signatures.get("network") === signature) return;
    signatures.set("network", signature);
    const nodes = [element("p", "", page.semantics)];
    nodes.push(element("p", "role-meta", !page.live ? "Live web access status unavailable." : page.enabled ? `${page.allowed_hosts.length} hosts listed.` : "Web access is disabled."));
    const hosts = (page.allowed_hosts || []).map((host) => element("li", "", `${host.host} · ${host.source || "configured"}`));
    const list = element("ul"); list.append(...hosts);
    nodes.push(disclosure("hosts", `Listed hosts (${hosts.length})`, [hosts.length ? list : element("p", "muted", "No hosts listed.")]));
    const traffic = page.rows.map((role) => {
      const row = keyed(element("article", "history-row"), role.id);
      row.append(element("strong", "", page.role_labels?.[role.employee_id]?.identity || name(role.employee_id)), element("p", "role-meta", `${number(role.requests)} requests · ${number(role.denied)} denied · ${bytes(role.bytes_up)} sent · ${bytes(role.bytes_down)} received`));
      const hosts = element("ul");
      for (const host of role.hosts) hosts.append(element("li", "", `${host.host} · ${number(host.requests)} requests · ${number(host.denied)} denied`));
      row.append(hosts, technicalDetails(`Role ${role.employee_id || "unknown"} · sandbox role ${role.sandbox_role || "unknown"}`));
      return row;
    });
    if (!traffic.length) traffic.push(element("p", "muted", page.egress?.live ? "No traffic recorded." : "Live traffic unavailable."));
    if (page.next_cursor) traffic.push(element("p", "role-meta", "First 200 roles shown."));
    nodes.push(disclosure("traffic", "Traffic by role", traffic));
    const pending = page.pending_requests || [];
    nodes.push(disclosure("pending", `Pending requests (${pending.length})`, pending.length ? pending.map((request) => element("p", "", `${name(request.employee_id, request.address)} · ${request.resource || request.kind} · ${request.purpose || "No purpose given"}`)) : [element("p", "muted", "No pending requests.")]));
    const coverage = (page.coverage?.limits || []).map((limit) => element("p", "role-meta", limit));
    if (page.coverage?.recording_failure_pending || page.coverage?.recorded_gaps) coverage.push(element("p", "role-meta", "Some history is missing."));
    if (coverage.length) nodes.push(disclosure("coverage", "Coverage and limitations", coverage));
    syncRecords($("#network-details"), nodes);
  }
  function history(id, entries, empty) {
    const retained = historyRows.get(id) || new Map();
    const next = new Map();
    const nodes = entries.map(({key, title, text, technical}) => {
      const signature = JSON.stringify([title, text, technical]);
      let row = retained.get(key);
      if (row?.signature !== signature) {
        const node = element("article", "history-row");
        node.dataset.recordId = key;
        node.tabIndex = -1;
        node.append(element("strong", "", title), element("p", "role-meta", text));
        if (technical) node.append(technicalDetails(technical));
        row = {node, signature};
      }
      next.set(key, row);
      return row.node;
    });
    historyRows.set(id, next);
    syncChildren($(`#${id}`), nodes.length ? nodes : [element("p", "muted", empty)]);
  }
  function renderDecisions(page) {
    history("decision-history", page.recent.map((request) => ({
      key: request.request_id,
      title: `${name(request.employee_id, request.address)} · ${request.resource || request.repository || request.station || request.kind}`,
      text: `${decisionStatus(request)}${request.decision_note ? ` · ${request.decision_note}` : ""}${request.purpose ? ` · ${request.purpose}` : ""}`,
      technical: `Request ${request.request_id} · ${request.kind} · ${request.address} · decided ${time(request.decided_at)}`,
    })), "No decisions recorded.");
  }
  function renderIncidents(page) {
    history("incident-history", page.recent.map((incident) => ({
      key: incident.incident_id, title: incidentTitle(incident),
      text: incidentStatus(incident),
      technical: `Incident ${incident.incident_id} · ${incident.scope} · began ${time(incident.created_at)} · resolved ${time(incident.resolved_at)}`,
    })), "No resolved incidents recorded.");
  }
  function renderHistory() {
    history("drain-history", snapshot.drain_outcomes.map((item) => ({
      key: item.activation_id, title: name(item.employee_id, item.address),
      text: `Requested ${time(item.requested_at)} · ${item.confirmed_exit
        ? `Exited ${time(item.finished_at)} · ${item.handoff_saved ? "handoff saved" : "no handoff notes"}`
        : item.request_presented ? "Drain request presented; exit not yet confirmed" : "Drain request pending"}`,
      technical: `Work period ${item.activation_id} · ${item.address}`,
    })), "No drain requests recorded.");
    history("failure-history", snapshot.failure_history.map((item) => ({
      key: item.activation_id, title: name(item.employee_id, item.address),
      text: `${time(item.finished_at)} · ${item.failure_category || item.state}`,
      technical: `Work period ${item.activation_id} · ${item.address}${item.failure ? ` · ${item.failure}` : ""}`,
    })), "No failed or interrupted work periods recorded.");
    const github = snapshot.github;
    history("github-history", [
      ...github.repositories.map((item) => ({key: `repo:${item.repository}`, title: item.repository, text: `Created by ${name(null, item.address)} ${time(item.created_at)} · ${item.purpose}`})),
      ...github.grants.map((item) => ({key: `grant:${item.repository}:${item.address}`, title: item.repository, text: `${name(null, item.address)} · ${item.access} · ${item.source} ${time(item.created_at)}`})),
      ...github.revocations.map((item) => ({key: `revoke:${item.repository}:${item.address}`, title: item.repository, text: `Revoked by you for ${item.address ? name(null, item.address) : "every role"} ${time(item.revoked_at)}`})),
    ], "No repositories created or access requested.");
    history("session-history", snapshot.usage.sessions.map((item) => ({
      key: item.session_id, title: name(item.employee_id, item.address),
      text: `Updated ${time(item.observed_at)} · last reported context ${number(item.current_context_tokens)} · peak context ${number(item.high_water_context_tokens)} · provider context limit ${number(item.provider_context_window)} · cumulative input ${number(item.cumulative_input_tokens)} · cumulative output ${number(item.cumulative_output_tokens)} · recorded compactions ${number(item.native_compactions)}`,
      technical: `Session ${item.session_id} · ${item.address || "address unavailable"}`,
    })), "No model sessions recorded.");
    const caveat = $("#session-history").querySelector(".session-caveat");
    if (caveat) caveat.remove();
    $("#session-history").append(element("p", "role-meta session-caveat", `Each session is a role's retained model conversation and may span multiple work periods. Cumulative usage includes repeated context; it is not the current context size. During a Claude turn, reported context can omit the latest response's output until the turn completes. Unavailable means the measurement was not reported.${snapshot.usage.sessions_omitted ? ` ${snapshot.usage.sessions_omitted} older sessions omitted.` : ""}`));
  }
  const actions = [
    ["Stop after current work", "Stops new work. Roles finish their current work period and save notes, then the VMs shut down. Nothing is deleted."],
    ["Force stop now", "Interrupts active work and powers off the VMs at once. For when a normal stop is stuck. Asks you to confirm."],
    ["Resume Firm", "Starts the VMs and accepts work again. Idle roles stay idle until they have work, so Overview will say so."],
    ["Close Firm", "Closes this console and the local host. Reopen with firm start."],
  ];
  for (const [title, text] of actions) {
    const row = element("div"); row.append(element("strong", "", title), element("p", "role-meta", text));
    $("#firm-actions").append(row);
  }
  return {
    revealDecision(id) {
      const record = [...$("#decision-history").children].find((node) => node.dataset.recordId === id);
      if (!record) return false;
      for (let parent = record.parentElement; parent; parent = parent.parentElement) {
        if (parent.tagName === "DETAILS") parent.open = true;
      }
      record.focus({preventScroll: true});
      record.scrollIntoView({block: "center"});
      return true;
    },
    async update(data, signal) {
      snapshot = data;
      const macTab = $("#analysis-tabs [data-section=mac]");
      macTab.hidden = !data.mac?.configured;
      if (macTab.hidden && macTab.getAttribute("aria-pressed") === "true") $("#analysis-tabs [data-section=timeline]").click();
      renderHealth(); renderAccounts(); renderHistory();
      const jobs = [
        api("/api/analysis/network?limit=200", signal).then((page) => { if (!signal.aborted) renderNetwork(page); }),
        api("/api/requests", signal).then((page) => { if (!signal.aborted) { decisions = page; renderDecisions(page); } }),
        api("/api/incidents?recent=50", signal).then((page) => { if (!signal.aborted) { incidents = page; renderIncidents(page); } }),
      ];
      if (Date.now() - resourcesAt >= 60000) jobs.push(api("/api/analysis/resources?row_type=role&from=0&limit=200", signal).then((page) => {
        if (signal.aborted) return;
        resources = page; resourcesAt = Date.now(); renderHealth();
      }));
      const outcomes = await Promise.allSettled(jobs);
      if (signal.aborted) return;
      const failed = outcomes.find((outcome) => outcome.status === "rejected");
      if (failed) throw failed.reason;
    },
    freshness() {
      if (snapshot && renderedDay !== new Date().toDateString()) {
        renderedDay = new Date().toDateString();
        signatures.delete("accounts");
        renderAccounts(); renderHistory();
        if (decisions) renderDecisions(decisions);
        if (incidents) renderIncidents(incidents);
      }
      $("#system-age").textContent = sourceText("/api/snapshot");
      $("#network-age").textContent = sourceText("/api/analysis/network");
      renderHealth();
    },
  };
}
