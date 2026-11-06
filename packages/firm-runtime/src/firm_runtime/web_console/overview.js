import {age, time, incidentTitle, vmState, runtimeState, needsYou, readinessPhrase, statusHeadline, statusSentence} from "./format.js";
import {resultCard} from "./results.js";

export const founderTemplate = "Continue the research programme from your saved notes.";

export function createOverview({$, api, post, element, button, mutationButton, refresh, reply, instruct, sourceText, connected, notice, syncChildren, technicalDetails, rememberResult}) {
  let snapshot = null;
  let requestPage = {pending: [], recent: []};
  let incidents = {open: [], recent: []};
  let mail = [];
  let mailLimit = 100;
  let mailCursor = null;
  let network = null;
  let resultsSignature = null;
  let mandateKey = null;
  const cards = new Map();
  const mailRows = new Map();
  const mailGroups = new Map();
  let mailLoaded = false;
  const founder = () => snapshot?.roles.find((role) => role.address === "founder");
  const name = (id, fallback) => snapshot?.readiness?.rows.find((row) => row.employee_id === id)?.identity || fallback || id;

  function requestCard(request) {
    const who = name(request.employee_id, request.address);
    const resource = request.resource || request.repository || request.station || request.kind;
    const kind = {network_host: "website access", github_repository: "GitHub repository", mac_station: "Mac station"}[request.kind] || "request";
    const card = element("article", "request-card decision-card");
    card.dataset.requestId = request.request_id;
    card.append(element("p", "eyebrow", `Decision · ${kind}`), element("h3", "", `${who} asks for ${resource}`));
    const asked = element("p", "role-meta", "Asked ");
    const stamp = element("span", "", age(request.created_at)); stamp.dataset.observedAt = request.created_at;
    asked.append(stamp); card.append(asked);
    card.append(element("p", "", `Their reason, in their words: “${request.purpose || "No purpose given."}”`));
    const causes = {
      network_host_not_allowed: "the site is not on this firm's approved list",
      github_owner_revoked: "you previously revoked this repository access",
      github_not_this_firm: "the repository is outside this firm's existing access",
      github_auto_create_off: "automatic repository creation is off",
      github_limit_reached: "the firm's automatic repository limit was reached",
      mac_needs_recovery: "the Mac station needs recovery",
      mac_not_enrolled: "the Mac station is not enrolled",
      mac_session_not_ready: "the Mac session is not ready",
    };
    card.append(element("p", "role-meta", `Waiting because ${causes[request.cause] || request.cause || "this request needs your approval"}.`));
    const consequences = element("div", "consequences");
    const allow = request.kind === "network_host" ? `Every role in this firm can reach ${resource} until you revoke it. ${who} is told and can try again.`
      : request.kind === "github_repository" ? request.create
        ? `Firm creates ${resource} in your configured organization and gives ${who} write access.`
        : `${who} gets access to ${resource}.`
      : request.kind === "mac_station" ? "The Mac station is marked ready and current requests for it are answered."
        : `The requested service for ${resource} is authorized.`;
    const decline = request.kind === "network_host" ? `${who} is told your reason and carries on without the site.`
      : request.kind === "github_repository" ? `${who} is told your reason and continues without this repository access.`
        : request.kind === "mac_station" ? `${who} is told your reason; this request will not make the station available.`
          : `${who} is told your reason and this request is declined.`;
    for (const [title, text] of [["If you approve", allow], ["If you decline", decline]]) {
      const box = element("div", "consequence"); box.append(element("strong", "", title), element("p", "", text)); consequences.append(box);
    }
    card.append(consequences);
    if (request.kind === "network_host") card.append(element("p", "role-meta", "Allowing a site permits any traffic to it, uploads included. It does not authorize sign-ups, purchases, outreach or sharing private data; those still need your approval."));
    const controls = element("div", "owner-controls");
    const approve = request.kind === "network_host" ? `Allow ${resource}` : request.kind === "github_repository" && request.create ? "Approve and create" : "Approve";
    controls.append(mutationButton(approve, async () => {
      await post("/api/requests/decide", {request_id: request.request_id, action: "grant"}, `firm-request:${request.request_id}:grant`);
      notice(`Approved ${resource}.`); await refresh(true);
    }, "primary"));
    const form = element("form", "deny-form"); form.hidden = true;
    const label = element("label", "", `Your reason (${who} will see this)`);
    const reason = element("textarea"); reason.required = true; reason.rows = 3;
    label.append(reason);
    const submit = element("button", "", "Decline request"); submit.type = "submit"; submit.dataset.mutation = "";
    form.append(label, element("p", "role-meta", "A reason is required. Your decision and reason are kept in the decision history."), submit,
      button("Keep it pending", () => { form.hidden = true; }));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!reason.value.trim()) { reason.setCustomValidity("Give a reason for declining."); reason.reportValidity(); return; }
      submit.dataset.busy = "true"; submit.disabled = true;
      try {
        await post("/api/requests/decide", {request_id: request.request_id, action: "deny", reason: reason.value.trim()}, `firm-request:${request.request_id}:deny`);
        notice(`Declined ${resource}.`); await refresh(true);
      } catch (error) { notice(error.message, true); }
      finally { delete submit.dataset.busy; submit.disabled = !connected(); }
    });
    reason.addEventListener("input", () => reason.setCustomValidity(""));
    controls.append(mutationButton("Decline…", () => { form.hidden = false; reason.focus(); }));
    card.append(controls, form, technicalDetails(`Request: ${request.request_id} · Kind: ${request.kind}`));
    return card;
  }

  function incidentCard(incident) {
    const account = incident.scope === "account" && snapshot.accounts.accounts.find((item) => item.account_id === incident.dependency);
    const card = element("article", "incident-card decision-card");
    card.dataset.incidentId = incident.incident_id;
    card.append(element("p", "eyebrow", "Needs your decision"), element("h3", "", incidentTitle(incident)));
    const opened = element("p", "role-meta", "Opened ");
    const stamp = element("span", "", age(incident.created_at)); stamp.dataset.observedAt = incident.created_at;
    opened.append(stamp); card.append(opened);
    const blocked = (incidents.open.find((item) => item.incident_id === incident.incident_id)?.blocked_roles || []).map((role) => role.identity || role.address);
    card.append(element("p", "", blocked.length ? `Waiting on it: ${blocked.join(", ")}. Their conversations are kept.` : "Work that needs this dependency is paused."));
    if (incident.kind === "auth") {
      card.append(element("p", "", "Sign in again and save the new login, then resume work. Credentials stay on your computer and never pass through this page."));
      if (account?.default_auth_file) {
        const command = `firm save-credentials ${account.account_id} --provider ${account.provider}`;
        const line = element("div", "command-line"); line.append(element("code", "", command), button("Copy", async () => { await navigator.clipboard.writeText(command); notice("Command copied."); })); card.append(line);
      } else if (account?.auth_file) card.append(element("p", "message-body", `Replace the saved login in the configured auth_file: ${account.auth_file}`));
      else card.append(element("p", "", `Check the credentials configured for ${incident.scope} ${incident.dependency}.`));
    } else if (incident.kind === "config") {
      card.append(element("p", "", `Fix the entry for ${incident.scope} ${incident.dependency} in the firm's configuration, then resume work.`));
    } else if (["quota", "outage"].includes(incident.kind)) {
      card.append(element("p", "", "This hold has no end time. Resume work when you are ready to lift it."));
    } else card.append(element("p", "", `Resolve the ${incident.kind} hold on ${incident.dependency}, then resume work.`));
    card.append(mutationButton(incident.kind === "auth" ? "I've saved a new login · resume work" : "Resume work", async () => {
      await post("/api/incidents/clear", {incident_id: incident.incident_id}, `firm-incident:${incident.incident_id}:clear`);
      await refresh(true);
    }, "primary"));
    card.append(element("p", "role-meta", "This clears the hold. A new failure brings the card back."), technicalDetails(`Incident: ${incident.incident_id} · Kind: ${incident.kind}`));
    return card;
  }

  function renderDecisions() {
    const items = needsYou(snapshot).items;
    const nodes = items.map((item) => {
      const id = item.request_id || item.incident_id;
      const detail = item.type === "request" ? requestPage.pending.find((r) => r.request_id === id) || item : item;
      // Keep the denial form and its draft mounted across polls of the same request.
      const account = snapshot.accounts.accounts.find((a) => a.account_id === item.dependency);
      const blocked = incidents.open.find((i) => i.incident_id === id)?.blocked_roles;
      const signature = JSON.stringify({...detail, age_seconds: undefined,
        identity: name(item.employee_id, item.address),
        auth: account ? [account.auth_file, account.default_auth_file, account.provider] : null, blocked});
      if (cards.get(id)?.signature !== signature) cards.set(id, {signature, node: item.type === "request" ? requestCard(detail) : incidentCard(detail)});
      return cards.get(id).node;
    });
    for (const id of cards.keys()) if (!items.some((item) => (item.request_id || item.incident_id) === id)) cards.delete(id);
    if (!nodes.length) nodes.push(element("p", "empty-state", "Nothing needs your decision."));
    for (const incident of incidents.recent.filter((item) => item.resolved_by === "owner" && item.until_at == null).slice(0, 5)) {
      if (snapshot.accounts.incidents.some((item) => item.scope === incident.scope && item.dependency === incident.dependency)) continue;
      const card = element("article", "recovery-card");
      card.append(element("strong", "", `Hold cleared at ${time(incident.resolved_at)} · ${incident.dependency}`));
      const account = incident.scope === "account" && snapshot.accounts.accounts.find((item) => item.account_id === incident.dependency);
      if (account) card.append(element("p", "", account.last_started_at != null && account.last_started_at >= incident.resolved_at
        ? `Work started on ${incident.dependency} at ${time(account.last_started_at)}`
        : `Waiting for the next work period on ${incident.dependency}.`));
      nodes.push(card);
    }
    // Reorder existing cards without replacing focused controls.
    syncChildren($("#decision-list"), nodes);
  }

  function renderMail() {
    const groups = new Map();
    const nodes = [];
    for (const message of mail) {
      const runtime = message.author.employee_id === "runtime";
      let parent;
      if (runtime && message.regarding?.kind === "incident") {
        const key = message.regarding.id;
        parent = groups.get(key);
        if (!parent) {
          parent = mailGroups.get(key) || element("article", "mail-card runtime-notice");
          mailGroups.set(key, parent); groups.set(key, parent); nodes.push(parent);
          if (!parent.firstElementChild) parent.append(element("strong"), technicalDetails(`Incident: ${key}`));
          const incident = [...(snapshot?.accounts.incidents || []), ...incidents.recent].find((item) => item.incident_id === key);
          parent.firstElementChild.textContent = `${incident ? incidentTitle(incident) : "Execution hold"} · ${message.regarding.status}${message.regarding.resolved_at ? ` ${time(message.regarding.resolved_at)}` : ""}`;
        }
      } else {
        parent = mailGroups.get(message.message_id) || element("article", runtime ? "mail-card runtime-notice" : "mail-card");
        mailGroups.set(message.message_id, parent); nodes.push(parent);
      }
      if (mailRows.has(message.message_id)) continue;
      const row = element("div", "mail-item"); row.dataset.messageId = message.message_id;
      const meta = element("p", "role-meta", `${runtime ? "Runtime notice" : name(message.author.employee_id, message.author.address)} · `);
      const stamp = element("span", "", age(message.created_at)); stamp.dataset.observedAt = message.created_at;
      meta.append(stamp);
      row.append(meta, element("h3", "", message.subject || "Message"));
      const body = element("details"); body.append(element("summary", "", "Read message"), element("p", "message-body", message.body)); row.append(body);
      const controls = element("div", "owner-controls");
      if (!runtime) controls.append(mutationButton("Reply", () => reply(message)));
      controls.append(mutationButton("Mark done", async () => {
        await post("/api/messages/dismiss", {message_ids: [message.message_id]}, `firm-message:dismiss:${message.message_id}`); await refresh(true);
      }));
      row.append(controls); mailRows.set(message.message_id, row);
    }
    for (const parent of nodes) {
      const ids = mail.filter((message) => message.author.employee_id === "runtime" && message.regarding?.kind === "incident"
        ? mailGroups.get(message.regarding.id) === parent : mailGroups.get(message.message_id) === parent);
      const children = ids.map((message) => mailRows.get(message.message_id));
      if (parent.firstElementChild?.tagName === "STRONG") children.unshift(parent.firstElementChild, parent.children[1]);
      syncChildren(parent, children);
    }
    const present = new Set(mail.map((message) => message.message_id));
    for (const id of mailRows.keys()) if (!present.has(id)) mailRows.delete(id);
    for (const [id, group] of mailGroups) if (!nodes.includes(group)) mailGroups.delete(id);
    if (!nodes.length) nodes.push(element("p", "empty-state", mailLoaded ? "No new mail from the firm." : "Owner mail unavailable; waiting for a sample."));
    if (mailCursor && mailLimit < 200) nodes.push(button("Load more owner mail", async () => { mailLimit = 200; await loadMail(); renderMail(); }));
    syncChildren($("#owner-mail"), nodes);
  }

  function renderFacts() {
    const facts = [
      ["Firm runtime", runtimeState(snapshot.firm.state), "/api/snapshot"],
      ["Linux VM", vmState(snapshot.infrastructure_state), "/api/snapshot"],
    ];
    if (snapshot.mac?.configured) facts.push(["Mac VM", snapshot.mac.state ? vmState(snapshot.mac.state) : `${snapshot.mac.stations?.length || 0} stations configured`, "/api/snapshot"]);
    if (!snapshot.accounts.accounts.length) facts.push(["Accounts", "No named accounts configured", "/api/snapshot"]);
    for (const account of snapshot.accounts.accounts) {
      const hold = snapshot.accounts.incidents.find((i) => i.scope === "account" && i.dependency === account.account_id);
      facts.push([account.account_id, `${hold ? {auth: "Can't sign in", config: "Configuration needs repair", quota: "Allowance hold", outage: "Outage hold"}[hold.kind] || "Paused" : "Available"} · ${account.active} of ${account.max_active} slots`, "/api/snapshot"]);
    }
    facts.push(["Web access", !network ? "Unavailable" : !network.live ? "Live status unavailable" : network.enabled ? `${network.allowed_hosts.length} hosts listed` : "Disabled", "/api/analysis/network"]);
    $("#facts").replaceChildren(...facts.map(([title, text, source]) => {
      const stamp = element("dd", "source-age", sourceText(source)); stamp.dataset.source = source;
      const item = element("div"); item.append(element("dt", "", title), element("dd", "", text), stamp); return item;
    }));
  }
  function render() {
    $("#status-title").textContent = statusHeadline(snapshot, connected());
    $("#status-sentence").textContent = statusSentence(snapshot, connected());
    if (!snapshot) return;
    $("#instruct-founder").hidden = statusHeadline(snapshot, connected()) !== "Idle, nothing scheduled" || !founder();
    renderFacts(); renderDecisions();
    $("#team-now").replaceChildren(...(snapshot.readiness?.rows || []).filter((row) => row.activity !== "retired").map((row) => {
      const li = element("li"); li.append(element("strong", "", row.identity || row.address), element("span", "role-meta", readinessPhrase(row, snapshot))); return li;
    }));
    if (!snapshot.readiness?.rows) $("#team-now").append(element("li", "", "Readiness unavailable"));
    freshness();
  }

  function freshness() {
    $("#status-age").textContent = sourceText("/api/snapshot");
    $("#team-age").textContent = sourceText("/api/snapshot");
    const details = [["Decisions", "/api/snapshot"], ["Request details", "/api/requests"], ["Incidents", "/api/incidents"]]
      .map(([label, path]) => sourceText(path) ? `${label}: ${sourceText(path)}` : "").filter(Boolean).join(" · ");
    $("#decisions-age").textContent = details;
    $("#owner-mail-age").textContent = sourceText("/api/inbox");
    if (founder()) $("#mandate-age").textContent = sourceText(`/api/roles/${founder().employee_id}`);
  }

  async function loadMandate(signal) {
    const role = founder();
    if (!role) { $("#mandate-text").textContent = "Founder’s mandate unavailable."; return; }
    const key = `${snapshot.firm.console_scope}:${role.employee_id}:${role.mandate_revision}`;
    if (mandateKey === key) return;
    let cursor = null; let text = ""; let doc;
    do {
      const page = await api(`/api/roles/${encodeURIComponent(role.employee_id)}?section=mandate${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`, signal);
      doc = page.documents.mandate; text += doc.text; cursor = page.continuation;
    } while (cursor);
    if (signal?.aborted) return;
    mandateKey = key;
    $("#mandate-title").textContent = `Mandate${doc.revision != null ? ` · revision ${doc.revision}` : ""}${doc.author_id === "owner" ? " · set by you" : ""}`;
    $("#mandate-text").textContent = text ? text.length > 360 ? `${text.slice(0, 360)}…` : text : "No mandate recorded.";
    $("#full-mandate").hidden = text.length <= 360;
    $("#full-mandate p").textContent = text;
  }
  async function loadMail(signal) {
    const incoming = []; let cursor = null;
    do {
      const page = await api(`/api/inbox?view=new&limit=100${cursor ? `&before=${encodeURIComponent(cursor)}` : ""}`, signal);
      incoming.push(...page.messages); cursor = page.next_cursor;
    } while (cursor && incoming.length < mailLimit);
    mail = incoming.slice(0, mailLimit); mailCursor = cursor; mailLoaded = true;
  }
  $("#instruct-founder").addEventListener("click", () => instruct(founder(), founderTemplate));
  return {
    update(data) { snapshot = data; render(); },
    render,
    freshness,
    async refresh(signal) {
      const jobs = [
        api("/api/results?limit=3", signal).then((page) => {
          if (signal?.aborted) return;
          const signature = JSON.stringify(page.results);
          if (resultsSignature === signature) return;
          resultsSignature = signature;
          $("#latest-results").replaceChildren(...(page.results.length
            ? page.results.map((result) => resultCard(result, {element, rememberResult}))
            : [element("p", "empty-state", "No files have been sent to you yet.")]));
        }).catch((error) => {
          if (!signal?.aborted && resultsSignature === null) $("#latest-results").textContent = `Results unavailable: ${error.message}`;
          throw error;
        }),
        api("/api/requests", signal).then((value) => { requestPage = value; }),
        api("/api/incidents?recent=50", signal).then((value) => { incidents = value; }),
        api("/api/analysis/network?limit=200", signal).then((value) => { network = value; }),
      ];
      if (snapshot) jobs.push(loadMail(signal), loadMandate(signal).catch((error) => {
        if (!signal?.aborted && !mandateKey) $("#mandate-text").textContent = `Mandate unavailable: ${error.message}`;
        throw error;
      }));
      await Promise.allSettled(jobs);
      if (signal?.aborted) return;
      render(); renderMail();
    },
  };
}
