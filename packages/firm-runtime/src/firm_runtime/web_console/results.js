import {bytes, time, filePreviewable, fileFolder, fileDeliveries, resultHref} from "./format.js";

function readLink(result, {element, rememberResult}, label = "Read") {
  const link = element("a", "button-link", label);
  link.href = resultHref(result);
  link.addEventListener("click", () => rememberResult(result));
  return link;
}

function downloadLink(file, element) {
  const link = element("a", "button-link", "Download");
  link.href = `/api/files/read?${new URLSearchParams({path: file.path, name: file.name})}`;
  link.setAttribute("download", file.name);
  return link;
}

export function attachmentChip(file, message, context) {
  const {element} = context;
  const chip = element("div", "attachment-chip");
  const result = {...message, ...file};
  chip.append(element("strong", "file-name", file.name), readLink(result, context), downloadLink(file, element));
  return chip;
}

export function resultCard(result, context) {
  const {element} = context;
  const card = element("article", "result-card");
  const title = element("div", "file-title");
  const stamp = element("span", "", time(result.created_at)); stamp.dataset.timestamp = result.created_at;
  const meta = element("p", "role-meta", `${result.author.identity || result.author.address} · `);
  meta.append(stamp);
  title.append(element("strong", "file-name", result.name), element("p", "", result.subject), meta);
  const actions = element("div", "file-actions");
  actions.append(readLink(result, context), downloadLink(result, element));
  card.append(title, actions);
  return card;
}

export function createResults(context) {
  const {$, api, element, button, mutationButton, reply, technicalDetails, rememberResult, syncChildren, placeComposer} = context;
  let fileRoots = [];
  let currentPath = null;
  let controller;
  let rootsLoading = false;
  let deliveries = [];
  let truncated = false;
  let remembered = null;
  let readerKey = null;
  const cards = new Map();

  function resetReaderFrame() {
    const previous = $("#reader-frame");
    const frame = previous.cloneNode(false);
    frame.removeAttribute("src");
    previous.replaceWith(frame);
    return frame;
  }

  function renderReader() {
    const [route, query] = location.hash.slice(1).split("?");
    const params = new URLSearchParams(query);
    const path = params.get("path"), name = params.get("name"), message = params.get("message");
    const visible = route === "results" && Boolean(path && name);
    $("#result-reader").hidden = !visible;
    if (!visible) {
      if (readerKey !== null) resetReaderFrame();
      readerKey = null; placeComposer(); return;
    }
    const file = {path, name};
    const history = fileDeliveries(deliveries, file);
    const selected = history.find((item) => item.message_id === message)
      || (remembered?.path === path && remembered?.name === name && remembered?.message_id === message ? remembered : null)
      || (!message ? history[0] : null);
    if (selected && !history.some((item) => item.message_id === selected.message_id)) history.push(selected);
    const canReply = selected && selected.author?.employee_id !== "runtime"
      && (selected.owner_delivery || deliveries.some((item) => item.message_id === selected.message_id));
    const key = JSON.stringify([path, name, selected?.message_id, Boolean(canReply)]);
    if (readerKey !== key) {
      readerKey = key;
      $("#reader-title").textContent = name;
      // A fresh frame avoids adding a second navigation to the page's hash history.
      const frame = resetReaderFrame();
      const previewable = filePreviewable(name);
      frame.hidden = !previewable;
      frame.setAttribute("sandbox", /\.html$/i.test(name) ? "allow-scripts" : "");
      frame.title = `Preview of ${name}`;
      const query = new URLSearchParams(file);
      frame.src = previewable ? `/api/files/preview?${query}` : "about:blank";
      $("#reader-unavailable").hidden = previewable;
      $("#reader-unavailable").textContent = "Preview is not available for this file type. Download the file to open it.";
      $("#reader-isolation").textContent = /\.html$/i.test(name)
        ? "Written by the firm; shown in an isolated frame with network access off."
        : "Written by the firm; shown in an isolated frame with scripts and network access off.";
      $("#reader-actions").replaceChildren(downloadLink(file, element), button("Open folder", async () => {
        await browseFiles(path); $("#deliverables").scrollIntoView({block: "start"});
      }));
      $("#reader-follow-up").replaceChildren(canReply
        ? mutationButton("Write a follow-up", () => reply(selected, file))
        : element("p", "role-meta", "Open a file sent to you to reply in its delivering conversation."));
    }
    const about = $("#reader-about");
    const sent = element("ul");
    for (const item of history) {
      const li = element("li");
      const link = element("a", "", item.subject);
      link.href = `#inbox?message=${encodeURIComponent(item.message_id)}`;
      const meta = element("p", "role-meta", `${item.author.identity || item.author.address} · `);
      const stamp = element("span", "", time(item.created_at)); stamp.dataset.timestamp = item.created_at;
      meta.append(stamp);
      li.append(link, meta);
      sent.append(li);
    }
    // Keep disclosures and keyboard focus while polling unchanged delivery metadata.
    const signature = JSON.stringify([file, history, truncated]);
    if (about.dataset.signature !== signature || about.dataset.path !== path) {
      about.dataset.signature = signature; about.dataset.path = path;
      about.replaceChildren(element("strong", "", "Folder"), element("p", "", fileFolder(path)),
        element("strong", "", "Sent with"), history.length ? sent : element("p", "role-meta", "No delivery in the loaded results."));
      if (truncated) about.append(element("p", "role-meta", "Delivery history covers the latest 200 attachments sent to you."));
      const details = technicalDetails(`Path: ${path}/${name}`);
      for (const item of history) {
        details.append(element("p", "role-meta", `Message: ${item.message_id} · Conversation: ${item.conversation_id}`));
      }
      about.append(details);
    }
    placeComposer();
  }

  async function refresh(signal) {
    const page = await api("/api/results?limit=200", signal);
    if (signal?.aborted) return;
    deliveries = page.results; truncated = page.truncated;
    const occurrences = new Map();
    const nodes = deliveries.map((item) => {
      const identity = JSON.stringify([item.message_id, item.path, item.name]);
      const occurrence = occurrences.get(identity) || 0;
      occurrences.set(identity, occurrence + 1);
      const key = `${identity}:${occurrence}`;
      if (!cards.has(key)) cards.set(key, resultCard(item, {element, rememberResult}));
      return cards.get(key);
    });
    for (const [key, node] of cards) if (!nodes.includes(node)) cards.delete(key);
    syncChildren($("#result-list"), nodes.length ? nodes : [element("p", "empty-state", "No files have been sent to you yet.")]);
    $("#results-limit").textContent = truncated ? "Showing the latest 200 attachments sent to you." : "";
    renderReader();
  }

  async function refreshFileRoots() {
    if (rootsLoading) return;
    rootsLoading = true;
    const status = $("#file-status");
    const select = $("#file-root");
    const selected = select.value;
    try {
      const result = await api("/api/files/roots");
      fileRoots = result.roots || [];
      select.replaceChildren(...fileRoots.map((root) => {
        const option = element("option", "", root.label);
        option.value = root.path;
        return option;
      }));
      if (fileRoots.some((root) => root.path === selected)) select.value = selected;
      if (!fileRoots.some((root) => currentPath === root.path || currentPath?.startsWith(`${root.path}/`))) currentPath = null;
      select.disabled = !result.available;
      status.textContent = result.available ? "Choose a folder to browse." : (result.reason || "Linux VM is stopped; browsing will not start it.");
      if (result.available && fileRoots.length) await browseFiles(currentPath || select.value);
      else { $("#file-list").replaceChildren(); $("#file-breadcrumbs").replaceChildren(); }
    } catch (error) { status.textContent = `Files unavailable: ${error.message}`; }
    finally { rootsLoading = false; }
  }

  function breadcrumbs(path, root) {
    const parts = path.slice(root.path.length).split("/").filter(Boolean);
    const segments = [{name: root.label, path: root.path}];
    for (const name of parts) segments.push({name, path: `${segments.at(-1).path}/${name}`});
    $("#file-breadcrumbs").replaceChildren(...segments.flatMap((part, index) => {
      const node = index === segments.length - 1 ? element("span", "", part.name)
        : button(part.name, () => browseFiles(part.path), "breadcrumb-link");
      if (index === segments.length - 1) node.setAttribute("aria-current", "location");
      if (!index) return [node];
      const separator = element("span", "", "›");
      separator.setAttribute("aria-hidden", "true");
      return [separator, node];
    }));
  }

  async function browseFiles(path = $("#file-root").value) {
    if (!path) return;
    const root = fileRoots.find((item) => path === item.path || path.startsWith(`${item.path}/`));
    if (!root) return;
    controller?.abort();
    const current = new AbortController(); controller = current;
    const status = $("#file-status");
    status.textContent = "Loading folder…";
    try {
      const result = await api(`/api/files/list?path=${encodeURIComponent(path)}`, current.signal);
      if (current.signal.aborted) return;
      currentPath = path;
      $("#file-root").value = root.path;
      breadcrumbs(path, root);
      status.textContent = result.truncated ? "First 200 entries shown" : `${result.entries.length} ${result.entries.length === 1 ? "entry" : "entries"}`;
      const rows = [];
      if (path !== root.path) rows.push(button("↑ Parent folder", () => browseFiles(path.split("/").slice(0, -1).join("/")), "parent-folder"));
      for (const entry of result.entries) {
        const row = element("div", "file-entry");
        const title = element("div", "file-title");
        title.append(element("strong", "file-name", entry.name), element("span", "role-meta", entry.type === "file" ? bytes(entry.size) : entry.type));
        const actions = element("div", "file-actions");
        if (entry.type === "directory") {
          actions.append(button("Open", () => browseFiles(`${path}/${entry.name}`)));
        } else if (entry.type === "file") {
          const params = new URLSearchParams({path, name: entry.name});
          const download = element("a", "button-link", "Download");
          download.href = `/api/files/read?${params}`;
          download.setAttribute("download", entry.name);
          if (filePreviewable(entry.name, entry.size)) {
            actions.append(button("Preview", () => previewArtifact(path, entry.name)));
          }
          actions.append(download);
        }
        row.append(title, actions);
        rows.push(row);
      }
      if (!result.entries.length) rows.push(element("p", "muted", "This folder is empty."));
      $("#file-list").replaceChildren(...rows);
    } catch (error) {
      if (!current.signal.aborted) status.textContent = `Folder unavailable: ${error.message}`;
    }
  }

  function previewArtifact(path, name) {
    const params = new URLSearchParams({path, name});
    $("#artifact-title").textContent = name;
    const frame = $("#artifact-frame");
    frame.setAttribute("sandbox", /\.html$/i.test(name) ? "allow-scripts" : "");
    frame.src = `/api/files/preview?${params}`;
    $("#artifact-dialog").showModal();
  }

  $("#browse-files").addEventListener("click", () => void refreshFileRoots());
  $("#file-root").addEventListener("change", () => void browseFiles());
  $("#artifact-close").addEventListener("click", () => $("#artifact-dialog").close());
  $("#artifact-dialog").addEventListener("close", () => { $("#artifact-frame").src = "about:blank"; });
  return {
    open() { renderReader(); return refreshFileRoots(); },
    close() { if (readerKey !== null) resetReaderFrame(); readerKey = null; },
    refresh,
    remember(result) { remembered = result; },
    selection() {
      const params = new URLSearchParams(location.hash.split("?")[1]);
      return {message_id: params.get("message"), path: params.get("path"), name: params.get("name")};
    },
  };
}
