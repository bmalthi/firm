import {buildRoleForest} from "./role_tree.js";
import {readinessPhrase} from "./format.js";

export function createTeam({$, api, element, button, number, total, time, syncChildren, refresh, sourceText}) {
  const records = new Map();
  const tree = element("ul", "role-tree");
  tree.setAttribute("aria-label", "Role reporting hierarchy");
  let view = {query: "", root: null, pages: 1};
  let snapshot;
  let scope;

  function facts(role, readiness) {
    const execution = role.execution || {};
    const context = role.context || {};
    return [
      ["Settings", execution.source === "retained_session" ? "Retained session" : execution.source === "configured_profile" ? "Configured profile" : "Unavailable"],
      ["Profile", execution.profile || role.execution_profile || "Unavailable"],
      ["Provider", execution.provider || "Unavailable"],
      ["Model", execution.resolved_model || execution.model || "Unavailable"],
      ...(execution.resolved_model && execution.resolved_model !== execution.model
        ? [["Configured model", execution.model || "Unavailable"]] : []),
      ["Effort", execution.effort || "Unavailable"],
      ["Level", role.level ? `${role.level.model_class || "unclassified"} class · ${role.level.effort || "unknown"} effort · ${role.level.state}` : "Unavailable"],
      ["Level reason", role.level?.reason || "None recorded"],
      ["Tokens", `${number(total(role.tokens))} total · input ${number(role.tokens?.input_tokens)} · cached input ${number(role.tokens?.cached_input_tokens)} (included in input) · output ${number(role.tokens?.output_tokens)}`],
      ["Context", context.current_context_tokens == null ? "Unavailable" : `${number(context.current_context_tokens)} tokens · reported ${time(context.observed_at)}`],
      ["Soft window", `${number(execution.context_window)} tokens`],
      ["Advisory", execution.context_advisory_fraction == null ? "Unavailable" : `${Math.round(execution.context_advisory_fraction * 100)}%`],
      ["Handoff reserve", `${number(execution.handoff_reserve)} tokens`],
      ["Hold", role.hold_reason || "No role hold recorded"],
      ["Retry", role.retry_at ? time(role.retry_at) : "None scheduled"],
      ["Readiness reasons", readiness?.reasons?.join(", ") || readiness?.primary_reason || "Unavailable"],
      ["Blockers", readiness?.blockers?.length ? readiness.blockers.map((blocker) => [
        blocker.incident_id && `incident ${blocker.incident_id}`,
        blocker.request_ids?.length && `requests ${blocker.request_ids.join(", ")}`,
        blocker.hold_reason,
        blocker.retry_at && `retry ${time(blocker.retry_at)}`,
        blocker.alternative_account_usable != null && `alternative account ${blocker.alternative_account_usable ? "usable" : "unavailable"}`,
      ].filter(Boolean).join(" · ")).join("; ") : "None recorded"],
      ["Mail waiting", number(role.pending_messages)],
      ["Joined", time(role.created_at)],
      ["Role ID", role.employee_id],
    ];
  }

  async function loadMandate(record) {
    const role = record.role;
    const key = `${scope}:${role.mandate_revision}`;
    if (record.mandateKey === key || record.loadingKey === key) return;
    record.loadingKey = key;
    record.mandate.textContent = "Loading mandate…";
    try {
      let cursor = null;
      let text = "";
      do {
        const page = await api(`/api/roles/${encodeURIComponent(role.employee_id)}?section=mandate${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
        text += page.documents.mandate.text;
        cursor = page.continuation;
      } while (cursor);
      if (record.loadingKey !== key) return;
      record.mandateKey = key;
      record.mandate.textContent = text || "No mandate recorded.";
    } catch (error) {
      if (record.loadingKey === key) record.mandate.textContent = `Mandate unavailable: ${error.message}. Close and reopen to retry.`;
    } finally {
      if (record.loadingKey === key) record.loadingKey = null;
    }
  }

  function createRecord(role) {
    const item = element("li", "role-branch");
    const card = element("article", "role-card");
    const children = element("ul", "role-children");
    children.id = `reports-${role.employee_id}`;
    const record = {role, item, card, children};
    const heading = element("div", "role-heading");
    const title = element("div", "role-title");
    record.identity = element("strong");
    record.address = element("span", "role-subtitle");
    title.append(record.identity, record.address);
    record.toggle = button("▾", () => {
      children.hidden = !children.hidden;
      updateToggle(record);
    }, "role-toggle");
    record.toggle.setAttribute("aria-controls", children.id);
    heading.append(record.toggle, title);
    record.readiness = element("p", "role-readiness");
    record.reports = element("p", "role-meta");
    record.browse = button("Browse reports", () => changeView({query: "", root: record.role.employee_id, pages: 1}));
    record.details = element("details", "role-details");
    record.summary = element("summary", "", "Mandate and technical details");
    record.mandate = element("p", "role-mandate message-body");
    record.facts = element("dl", "role-facts");
    const freshness = element("p", "source-age");
    freshness.dataset.source = `/api/roles/${encodeURIComponent(role.employee_id)}`;
    record.details.append(record.summary, element("h3", "", "Mandate"), record.mandate, freshness,
      element("h3", "", "Execution details"), record.facts,
      element("p", "role-meta", "Token usage is reported usage, not a quota reading. During a Claude turn, context can omit the latest response’s output until the turn completes."));
    record.details.addEventListener("toggle", () => { if (record.details.open) void loadMandate(record); });
    card.append(heading, record.readiness, record.reports, record.browse, record.details);
    item.append(card, children);
    return record;
  }

  function updateToggle(record) {
    record.toggle.setAttribute("aria-expanded", String(!record.children.hidden));
    record.toggle.setAttribute("aria-label", `${record.children.hidden ? "Expand" : "Collapse"} reports for ${record.role.identity}`);
    record.toggle.textContent = record.children.hidden ? "▸" : "▾";
  }

  function renderRoles(roles, continuation) {
    const readiness = new Map((snapshot.readiness?.rows || []).map((row) => [row.employee_id, row]));
    const roleSnapshot = {...snapshot, roles};
    function branch(node) {
      const role = node.role;
      let record = records.get(role.employee_id);
      if (!record) { record = createRecord(role); records.set(role.employee_id, record); }
      record.role = role;
      record.identity.textContent = role.identity || role.address;
      record.address.textContent = role.address;
      record.readiness.textContent = readinessPhrase(readiness.get(role.employee_id), roleSnapshot);
      record.toggle.hidden = !node.children.length;
      updateToggle(record);
      record.reports.hidden = !role.direct_report_count;
      record.reports.textContent = `${role.direct_report_count} direct report${role.direct_report_count === 1 ? "" : "s"}`;
      record.browse.hidden = !(role.direct_report_count > node.children.length);
      record.facts.replaceChildren(...facts(role, readiness.get(role.employee_id)).flatMap(([label, value]) =>
        [element("dt", "", label), element("dd", "", value)]));
      if (record.details.open) void loadMandate(record);
      syncChildren(record.children, node.children.map(branch));
      return record.item;
    }
    syncChildren(tree, buildRoleForest(roles).map(branch));
    const content = roles.length ? [tree] : [element("p", "muted", "No matching roles.")];
    if (continuation) content.push(button("Load more organization roles", () => changeView({...view, pages: view.pages + 1})));
    syncChildren($("#roles"), content);
    $("#role-count").textContent = roles.length;
    $("#all-roles").hidden = !view.query && !view.root;
  }

  async function rolePage(data, signal) {
    let cursor = null;
    const roles = new Map();
    const currentView = view;
    for (let pageIndex = 0; pageIndex < currentView.pages; pageIndex++) {
      const base = currentView.query
        ? `/api/roles/search?query=${encodeURIComponent(currentView.query)}&limit=50`
        : `/api/roles/tree?depth=2&limit=50${currentView.root ? `&root=${encodeURIComponent(currentView.root)}` : ""}`;
      const page = pageIndex === 0 && !currentView.query && !currentView.root
        ? {roles: data.roles, continuation: data.role_tree.continuation}
        : await api(`${base}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`, signal);
      for (const role of page.roles || [page.root, ...page.nodes]) roles.set(role.employee_id, role);
      cursor = page.continuation;
      if (!cursor) break;
    }
    return {roles: [...roles.values()], cursor};
  }

  function changeView(next) { view = next; return refresh(true); }
  $("#role-search").addEventListener("submit", (event) => { event.preventDefault(); void changeView({query: $("#role-query").value.trim(), root: null, pages: 1}); });
  $("#all-roles").addEventListener("click", () => { $("#role-query").value = ""; void changeView({query: "", root: null, pages: 1}); });
  return {freshness() {
    const paths = ["/api/snapshot"];
    if (view.query) paths.push("/api/roles/search");
    else if (view.root || view.pages > 1) paths.push("/api/roles/tree");
    $("#team-view-age").textContent = [...new Set(paths.map((path) => sourceText(path)).filter(Boolean))].join(" · ");
  }, async update(data, signal) {
    const page = await rolePage(data, signal);
    if (signal?.aborted) return;
    snapshot = data;
    if (scope !== data.firm.console_scope) { records.clear(); scope = data.firm.console_scope; }
    renderRoles(page.roles, page.cursor);
  }};
}
