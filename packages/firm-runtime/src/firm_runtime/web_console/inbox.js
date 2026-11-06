import {decisionStatus, incidentStatus, incidentTitle, inboxGroups, inboxWindow} from "./format.js";
import {attachmentChip} from "./results.js";

export function createInbox({$, api, post, element, button, mutationButton, time, reply, refresh,
  syncChildren, technicalDetails, placeComposer, sourceText, rememberResult}) {
  let view = "new";
  let loaded = 50;
  let messages = [];
  let decisions = [];
  let incidents = new Map();
  let hasMore = false;
  let generation = 0;
  let names = new Map();
  let renderedDay = new Date().toDateString();
  const rows = new Map();
  const groups = new Map();
  const decisionRows = new Map();
  const who = (person) => person.employee_id === "owner" ? "You" : person.employee_id === "runtime" ? "Runtime notice"
    : names.get(person.employee_id) || person.identity || person.address || "Role";

  function messageRow(message) {
    let row = rows.get(message.message_id);
    if (!row) {
      const runtime = message.author.employee_id === "runtime";
      const card = element("article", `message-card${runtime ? " runtime-notice" : ""}`);
      card.dataset.messageId = message.message_id;
      const header = element("div", "message-header");
      const state = element("span", "badge");
      const requestStatus = element("span", "request-status role-meta");
      const author = element("strong");
      const recipients = element("span", "role-meta", `to ${message.recipients.map((r) => r.employee_id === "owner" ? "you" : who(r)).join(", ")}`);
      const stamp = element("time", "message-meta", time(message.created_at));
      stamp.dataset.timestamp = message.created_at;
      header.append(state, requestStatus, author, recipients, stamp);
      const body = element("p", "message-body");
      const full = message.body || "";
      const long = full.length > 600 || full.split("\n").length > 8;
      const excerpt = full.slice(0, 600).split("\n").slice(0, 8).join("\n");
      body.textContent = long ? `${excerpt}…` : full;
      card.append(header, element("h3", "", message.subject || "Message"), body);
      if (long) {
        const toggle = button("Show full message", () => {
          const expanded = toggle.getAttribute("aria-expanded") !== "true";
          body.textContent = expanded ? full : `${excerpt}…`;
          toggle.textContent = expanded ? "Show less" : "Show full message";
          toggle.setAttribute("aria-expanded", String(expanded));
        }, "text-button");
        toggle.setAttribute("aria-expanded", "false");
        card.append(toggle);
      }
      for (const file of message.attachments || []) card.append(attachmentChip(file, message, {element, rememberResult}));
      const actions = element("div", "owner-controls");
      if (!runtime) actions.append(mutationButton("Reply", () => reply(row.message), "reply-button"));
      const done = mutationButton("Mark done", async () => {
        await post("/api/messages/dismiss", {message_ids: [message.message_id]}, `firm-dismiss:${message.message_id}`);
        await refresh(true);
      });
      actions.append(done);
      card.append(actions, technicalDetails(`Message: ${message.message_id} · Conversation: ${message.conversation_id || "unavailable"}${message.regarding ? ` · ${message.regarding.kind}: ${message.regarding.id}` : ""}`));
      row = {card, state, requestStatus, author, done, message};
      rows.set(message.message_id, row);
    }
    row.message = message;
    row.author.textContent = who(message.author);
    const pending = message.owner_delivery?.disposition === "pending";
    row.state.textContent = message.owner_delivery ? pending ? "New" : "Done" : message.author.employee_id === "owner" ? "Sent" : "Between roles";
    row.state.classList.toggle("new-mail", pending);
    const regarding = message.regarding;
    row.requestStatus.hidden = regarding?.kind !== "request";
    // Current envelope state is independent of the bounded recent-decisions page.
    row.requestStatus.textContent = regarding?.kind === "request"
      ? `Request now: ${{granted: "Granted", denied: "Denied", withdrawn: "Withdrawn", pending: "Pending"}[regarding.status] || regarding.status}` : "";
    row.done.hidden = !pending;
    return row.card;
  }

  function decisionRow(item) {
    let row = decisionRows.get(item.request_id);
    if (!row) {
      const card = element("article", "decision-row");
      const text = element("span");
      const link = element("a", "", "Decision record"); link.href = `#system?decision=${encodeURIComponent(item.request_id)}`;
      card.append(text, " ", link);
      row = {card, text}; decisionRows.set(item.request_id, row);
    }
    row.text.textContent = `${decisionStatus(item)} · ${item.resource || item.kind} for ${who(item)}${item.decision_note ? ` · ${item.decision_note}` : ""}`;
    return row.card;
  }

  function render() {
    const composer = $("#send-form");
    // Capture before any reparenting: moving a focused descendant blurs it.
    const focused = composer.contains(document.activeElement) ? document.activeElement : null;
    const position = focused?.getBoundingClientRect().top;
    const selection = focused && "selectionStart" in focused
      ? [focused.selectionStart, focused.selectionEnd, focused.selectionDirection] : null;
    const replyCard = composer.closest(".message-card");
    if (replyCard && !messages.some((message) => message.message_id === replyCard.dataset.messageId)) $("#inbox-composer").append(composer);
    const current = inboxGroups(messages, view === "new" ? [] : decisions);
    const cards = current.map((group) => {
      if (group.type === "decision") return decisionRow(group.decisions[0]);
      let row = groups.get(group.key);
      if (!row) {
        const card = element("section", `conversation-card${group.type === "incident" ? " incident-group" : ""}`);
        const header = element("div", "conversation-heading");
        const title = element("h2");
        const status = element("span", "badge incident-status");
        const caption = element("p", "role-meta");
        const content = element("div");
        header.append(title, status);
        card.append(header, caption, content);
        row = {card, title, status, caption, content}; groups.set(group.key, row);
      }
      const first = group.messages[0];
      row.status.hidden = group.type !== "incident";
      if (group.type === "incident") {
        const incident = incidents.get(first.regarding.id);
        row.title.textContent = incident ? incidentTitle(incident) : "Execution incident";
        // The envelope stays current even when the incident is older than recent history.
        row.status.textContent = incidentStatus({...incident, ...first.regarding});
        row.status.classList.toggle("live", first.regarding.status === "resolved");
        row.caption.textContent = `${group.messages.length} runtime ${group.messages.length === 1 ? "notice" : "notices"} about this incident. Their text is shown unchanged; the status above is the incident's status now.`;
      } else {
        row.title.textContent = first?.subject || "Conversation";
        row.caption.textContent = `${group.messages.length} ${group.messages.length === 1 ? "message" : "messages"}${group.decisions.length ? ` · ${group.decisions.length} decisions` : ""}`;
      }
      const entries = [
        ...group.messages.map((message) => ({at: message.created_at, node: messageRow(message)})),
        ...group.decisions.map((item) => ({at: item.decided_at, node: decisionRow(item)})),
      ].sort((a, b) => b.at - a.at);
      syncChildren(row.content, entries.map((entry) => entry.node));
      return row.card;
    });
    const form = $("#send-form");
    if (form.closest("#messages") && !cards.some((card) => card.contains(form))) $("#inbox-composer").append(form);
    if (!cards.length) cards.push(element("p", "empty-state", view === "new" ? "No new messages. Everything to you has been marked done." : "No messages in this view yet."));
    syncChildren($("#messages"), cards);
    for (const [key] of groups) if (!current.some((group) => group.key === key)) groups.delete(key);
    for (const [key] of rows) if (!messages.some((message) => message.message_id === key)) rows.delete(key);
    $("#inbox-more").hidden = !hasMore || loaded >= 200;
    $("#inbox-count").textContent = `Showing ${messages.length} messages${loaded >= 200 && hasMore ? " · latest 200 messages" : ""}.`;
    placeComposer();
    if (focused?.isConnected) {
      focused.focus({preventScroll: true});
      if (selection) focused.setSelectionRange(...selection);
      window.scrollBy(0, focused.getBoundingClientRect().top - position);
    }
    renderedDay = new Date().toDateString();
  }

  async function windowPage(base, signal) {
    const incoming = [];
    let cursor = null;
    do {
      const page = await api(`${base}&limit=${Math.min(100, loaded - incoming.length)}${cursor ? `&before=${encodeURIComponent(cursor)}` : ""}`, signal);
      incoming.push(...page.messages);
      cursor = page.next_cursor;
    } while (cursor && incoming.length < loaded);
    return {messages: incoming, cursor};
  }
  function changeView(next) {
    view = next; loaded = 50; generation++;
    for (const node of $("#inbox-views").querySelectorAll("button")) node.setAttribute("aria-pressed", String(node.dataset.view === view));
    return refresh(true);
  }
  for (const node of $("#inbox-views").querySelectorAll("button")) node.addEventListener("click", () => void changeView(node.dataset.view));
  $("#include-role-mail").addEventListener("change", () => void changeView(view));
  $("#inbox-more").addEventListener("click", () => { loaded = Math.min(200, loaded + 50); void refresh(true); });
  return {
    openMessage() {
      view = "all"; loaded = 200; generation++;
      for (const node of $("#inbox-views").querySelectorAll("button")) node.setAttribute("aria-pressed", String(node.dataset.view === view));
    },
    revealMessage(id) {
      const card = rows.get(id)?.card;
      if (!card?.isConnected) return false;
      card.tabIndex = -1; card.focus({preventScroll: true}); card.scrollIntoView({block: "center"});
      return true;
    },
    prepareReply(message) {
      if (!rows.get(message.message_id)?.card.isConnected) {
        view = "all"; generation++;
        for (const node of $("#inbox-views").querySelectorAll("button")) node.setAttribute("aria-pressed", String(node.dataset.view === view));
        messages = [message, ...messages.filter((item) => item.message_id !== message.message_id)];
        render();
      }
    },
    async refresh(signal, snapshot) {
      const version = generation;
      const include = $("#include-role-mail").checked;
      const results = await Promise.allSettled([
        windowPage(`/api/inbox?view=${view}`, signal),
        include ? windowPage("/api/messages?scope=between_roles", signal) : Promise.resolve({messages: []}),
        api("/api/requests", signal), api("/api/incidents?recent=100", signal),
      ]);
      if (signal?.aborted || version !== generation) return;
      if (results[0].status === "rejected" || results[1].status === "rejected") throw results.find((result) => result.status === "rejected").reason;
      const [owner, roles] = results.map((result) => result.value);
      messages = inboxWindow(owner.messages, roles.messages, loaded);
      hasMore = Boolean(owner.cursor || roles.cursor || owner.messages.length + roles.messages.length > loaded);
      if (results[2].status === "fulfilled") decisions = results[2].value.recent;
      if (results[3].status === "fulfilled") incidents = new Map([...results[3].value.open, ...results[3].value.recent].map((item) => [item.incident_id, item]));
      names = new Map((snapshot?.readiness?.rows || snapshot?.roles || []).map((role) => [role.employee_id, role.identity || role.address]));
      render();
    },
    freshness() {
      if (renderedDay !== new Date().toDateString()) render();
      const sources = [["Owner mail", "/api/inbox"], ["Decisions", "/api/requests"], ["Incidents", "/api/incidents"]];
      if ($("#include-role-mail").checked) sources.push(["Mail between roles", "/api/messages"]);
      $("#inbox-age").textContent = sources.map(([label, path]) => sourceText(path) ? `${label}: ${sourceText(path)}` : "").filter(Boolean).join(" · ");
    },
  };
}
