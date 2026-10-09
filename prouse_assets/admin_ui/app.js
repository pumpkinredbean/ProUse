"use strict";
const $ = (selector) => document.querySelector(selector);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
const tabs = { workspaces: "Workspaces", connection: "Connection" };
const statuses = {
  ready: ["Active", "good"],
  disabled: ["Off", ""],
  unavailable: ["Unavailable", "bad"],
};
let csrf = "";
let data = null;
let tab = "workspaces";

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.style.display = "block";
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (element.style.display = "none"), 5000);
}

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const value = await response.json();
  if (!response.ok) throw Error(value.error || `HTTP ${response.status}`);
  return value;
}

function button(label, action, cls = "", attrs = "") {
  return `<button type="button" class="${cls}" data-action="${action}" ${attrs}>${label}</button>`;
}

function badge(label, cls = "") {
  return `<span class="badge ${cls}">${esc(label)}</span>`;
}

function header(title, description, action = "") {
  return `<div class="title-row"><div><h1>${title}</h1><p class="muted">${description}</p></div><div class="actions">${action}${button("Refresh", "refresh")}</div></div>`;
}

async function connect() {
  try {
    csrf = (await api("/api/session")).csrf;
    await refresh();
  } catch (error) {
    data = null;
    $("#app").innerHTML = `<main class="main"><section class="card"><h1>Cannot reach the dashboard</h1><p class="error">${esc(error.message)}</p><p class="muted">Run <code>prouse status</code> in a terminal to check it.</p>${button("Reconnect", "reconnect")}</section></main>`;
  }
}

async function refresh() {
  data = await api("/api/state");
  render();
}

function render() {
  if (!data) return;
  const nav = Object.entries(tabs)
    .map(([key, label]) => button(label, "nav", tab === key ? "active" : "", `data-tab="${key}"`))
    .join("");
  const view = tab === "workspaces" ? workspacesView() : connectionView();
  $("#app").innerHTML = `<div class="shell"><aside class="sidebar"><div class="brand">ProUse<small>Use Pro models across your local workspaces.</small></div><nav class="nav">${nav}</nav><div class="sidebar-footer">Local dashboard for this computer.<br>Changes apply from the next tool call.</div></aside><main class="main">${view}</main></div>`;
}

function workspacesView() {
  const rows = data.workspaces
    .map((w) => {
      const [label, cls] = statuses[w.status] || [w.status, ""];
      const reason = w.reason ? `<span class="secondary">${esc(w.reason)}</span>` : "";
      const actions = [
        w.status === "ready" && !w.default ? button("Make default", "workspace-default", "", `data-id="${esc(w.id)}"`) : "",
        button("Edit", "workspace-edit", "", `data-id="${esc(w.id)}"`),
        button("Remove", "workspace-remove", "danger", `data-id="${esc(w.id)}"`),
      ].join("");
      return `<tr><td><strong>${esc(w.label)}</strong> ${w.default ? badge("Default", "good") : ""}<span class="secondary mono">${esc(w.id)} · ${esc(w.root)}</span></td><td>${badge(label, cls)}${reason}</td><td><div class="actions end">${actions}</div></td></tr>`;
    })
    .join("");
  const table = rows
    ? `<div class="table-wrap"><table><thead><tr><th>Workspace</th><th>Status</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
    : '<p class="empty">No workspaces yet. Add a project folder you want AI chats to work in.</p>';
  return (
    header(
      "Workspaces",
      "Project folders that AI chats can read and edit through ProUse tools.",
      button("+ Add workspace", "workspace-add", "primary"),
    ) +
    `<section class="card">${table}</section><p class="muted">Relative paths resolve against the default workspace. Removing a workspace only unregisters it; the folder and its files stay.</p>`
  );
}

function connectionView() {
  return (
    header("Connection", "Connect an MCP client such as Claude Desktop to the ProUse tools.") +
    `<section class="card"><h2>Connect Claude Desktop</h2><ol class="steps"><li>Open the Claude Desktop config file. On macOS it is <code>~/Library/Application Support/Claude/claude_desktop_config.json</code>.</li><li>If the file is empty, paste the block below as is. If it already has <code>mcpServers</code>, add or replace only the <code>prouse</code> entry inside it.</li><li>Quit Claude Desktop completely and open it again.</li></ol><pre class="detail" id="mcp-config">${esc(JSON.stringify(data.mcp_config, null, 2))}</pre><div class="actions">${button("Copy", "copy-config")}</div></section>` +
    `<section class="card"><h2>Check the connection</h2><p class="muted">Starts the ProUse MCP server on this computer and checks initialization, the tool list and a read from the default workspace. Check the Claude Desktop side in Claude Desktop itself.</p><div class="actions">${button("Check MCP connection", "mcp-check", "primary")}</div><div id="check-result" class="preview"></div></section>` +
    `<p class="muted">ProUse ${esc(data.version)} · config file <span class="mono">${esc(data.registry)}</span></p>`
  );
}

function modal(title, html, onSubmit) {
  const dialog = $("#dialog");
  dialog.innerHTML = `<button type="button" class="close" data-action="close" aria-label="Close">×</button><h2>${title}</h2>${html}<div id="form-error"></div>`;
  dialog.showModal();
  const form = dialog.querySelector("form");
  if (form && onSubmit) {
    form.onsubmit = async (event) => {
      event.preventDefault();
      const submit = form.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        await onSubmit(new FormData(form));
      } catch (error) {
        showError(error.message);
      } finally {
        if (submit.isConnected) submit.disabled = false;
      }
    };
  }
}

function closeDialog() {
  if ($("#dialog").open) $("#dialog").close();
}

function showError(message) {
  const element = $("#form-error");
  element.className = "error";
  element.textContent = message;
}

function footer(label) {
  return `<footer>${button("Cancel", "close")}<button class="primary" type="submit">${label}</button></footer>`;
}

function find(id) {
  return data.workspaces.find((w) => w.id === id);
}

function workspaceAdd() {
  modal(
    "Add workspace",
    `<form><label>Project folder<input name="path" required autocomplete="off" placeholder="~/projects/app"><span>Paste a path or pick a folder below.</span></label><div class="workspace-picker">${button("Browse from home folder", "browse")}<div id="browser"></div></div><label>Name<input name="label" maxlength="120" placeholder="Leave empty to use the folder name"></label><label><input type="checkbox" name="default">Use as default workspace</label>${footer("Add")}</form>`,
    async (form) => {
      const result = await api("/api/workspaces/add", {
        path: form.get("path"),
        label: form.get("label") || null,
        default: form.has("default"),
      });
      closeDialog();
      await refresh();
      toast(`Added workspace ${result.workspace_id}.`);
    },
  );
}

async function browse(path) {
  const result = await api("/api/browse", { path: path || null });
  const target = $("#browser");
  if (!target) return;
  const actions = [
    result.parent ? button("Parent folder", "browse", "", `data-path="${esc(result.parent)}"`) : "",
    button("Select this folder", "browse-select", "primary", `data-path="${esc(result.path)}" ${result.can_select ? "" : "disabled"}`),
  ].join("");
  const list =
    result.directories.map((d) => button(`📁 ${esc(d.name)}`, "browse", "", `data-path="${esc(d.path)}"`)).join("") ||
    '<span class="muted">No subfolders.</span>';
  target.innerHTML = `<div class="workspace-browser"><div class="workspace-browser-head"><strong class="mono">${esc(result.path)}</strong><div class="actions">${actions}</div></div><div class="workspace-directory-list">${list}</div></div>`;
}

function selectFolder(path) {
  const form = $("#dialog form");
  if (!form) return;
  form.elements.path.value = path;
  $("#browser").innerHTML = "";
  form.elements.label.focus();
}

function workspaceEdit(id) {
  const w = find(id);
  if (!w) return;
  const defaultNote = w.default ? "<span>Changes when you make another workspace the default.</span>" : "";
  modal(
    "Edit workspace",
    `<form><p class="mono">${esc(w.root)}</p><label>Name<input name="label" required maxlength="120" value="${esc(w.label)}"></label><label><input type="checkbox" name="enabled" ${w.enabled ? "checked" : ""}>Enabled<span>When off, AI chats cannot read or edit this folder.</span></label><label><input type="checkbox" name="default" ${w.default ? "checked disabled" : ""}>Use as default workspace${defaultNote}</label>${footer("Save")}</form>`,
    async (form) => {
      await api("/api/workspaces/update", {
        id,
        label: form.get("label"),
        enabled: form.has("enabled"),
        default: form.has("default"),
      });
      closeDialog();
      await refresh();
      toast("Saved workspace settings.");
    },
  );
}

function workspaceRemove(id) {
  const w = find(id);
  if (!w) return;
  modal(
    "Remove workspace",
    `<p>Unregisters <strong>${esc(w.label)}</strong>. The folder and its files stay.</p><p class="mono">${esc(w.root)}</p><footer>${button("Cancel", "close")}${button("Remove", "workspace-remove-confirm", "danger", `data-id="${esc(id)}"`)}</footer>`,
  );
}

async function copyConfig() {
  const element = $("#mcp-config");
  try {
    await navigator.clipboard.writeText(element.textContent);
    toast("Copied the config.");
  } catch {
    const range = document.createRange();
    range.selectNodeContents(element);
    getSelection().removeAllRanges();
    getSelection().addRange(range);
    toast("Automatic copy is blocked, so the text is selected. Copy it manually.");
  }
}

async function mcpCheck(control) {
  control.disabled = true;
  control.textContent = "Checking…";
  try {
    const result = await api("/api/mcp/check", {});
    $("#check-result").innerHTML =
      result.status === "ok"
        ? `<div class="notice">Connected · ${esc(result.tools)} tools · default workspace readable</div>`
        : `<p class="error">${esc(result.error)}</p>`;
  } finally {
    if (control.isConnected) {
      control.disabled = false;
      control.textContent = "Check MCP connection";
    }
  }
}

document.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-action]");
  if (!target || target.disabled) return;
  const { action, id, path } = target.dataset;
  try {
    if (action === "nav") {
      tab = target.dataset.tab;
      render();
    } else if (action === "refresh") {
      await refresh();
    } else if (action === "reconnect") {
      await connect();
    } else if (action === "close") {
      closeDialog();
    } else if (action === "workspace-add") {
      workspaceAdd();
    } else if (action === "workspace-edit") {
      workspaceEdit(id);
    } else if (action === "workspace-remove") {
      workspaceRemove(id);
    } else if (action === "workspace-remove-confirm") {
      await api("/api/workspaces/remove", { id });
      closeDialog();
      await refresh();
      toast("Unregistered. The folder and its files are unchanged.");
    } else if (action === "workspace-default") {
      await api("/api/workspaces/update", { id, default: true });
      await refresh();
      toast("Changed the default workspace.");
    } else if (action === "browse") {
      await browse(path);
    } else if (action === "browse-select") {
      selectFolder(path);
    } else if (action === "copy-config") {
      await copyConfig();
    } else if (action === "mcp-check") {
      await mcpCheck(target);
    }
  } catch (error) {
    if ($("#dialog").open) showError(error.message);
    else toast(error.message);
  }
});

window.addEventListener("focus", () => {
  if (data && !$("#dialog").open) refresh().catch(() => {});
});

connect();
