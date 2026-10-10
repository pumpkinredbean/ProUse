"use strict";
const $ = (selector) => document.querySelector(selector);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
const shapes = {
  folder:
    '<path class="solid" d="M3 6.75C3 5.78 3.78 5 4.75 5h4.13c.46 0 .9.18 1.24.51L11.6 7h7.65c.97 0 1.75.78 1.75 1.75v8.5c0 .97-.78 1.75-1.75 1.75H4.75C3.78 19 3 18.22 3 17.25z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3L19.5 9"/><path d="M19.5 4.5V9H15"/>',
  chevron: '<path d="M9.5 6l6 6-6 6"/>',
  up: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  close: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
  copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="2.5"/><path d="M15.5 8.5V6.75c0-1.24-1-2.25-2.25-2.25h-6.5c-1.24 0-2.25 1-2.25 2.25v6.5c0 1.24 1 2.25 2.25 2.25H8.5"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  alert: '<path d="M12 9.5v4M12 16.75v.01"/><path d="M10.27 4.5 3.4 16.5a2 2 0 0 0 1.73 3h13.74a2 2 0 0 0 1.73-3L13.73 4.5a2 2 0 0 0-3.46 0z"/>',
  trash: '<path d="M4.5 7h15M10 11v5.5M14 11v5.5M6.5 7l.8 11.1a2 2 0 0 0 2 1.9h5.4a2 2 0 0 0 2-1.9L17.5 7M9.5 7V5.5A1.5 1.5 0 0 1 11 4h2a1.5 1.5 0 0 1 1.5 1.5V7"/>',
  terminal: '<rect x="3" y="4.5" width="18" height="15" rx="3"/><path d="M7.5 9.5l3 2.5-3 2.5M13 15h3.5"/>',
  prompt: '<path d="M6 8l5 4-5 4M13 17h5"/>',
};
const icon = (name) => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${shapes[name]}</svg>`;
const tabs = { workspaces: "Workspaces", connection: "Connection" };
let csrf = "";
let data = null;
let tab = "workspaces";
let checkResult = "";

function toast(message, kind = "ok") {
  const element = $("#toast");
  element.className = `show ${kind}`;
  element.innerHTML = `${icon(kind === "ok" ? "check" : "alert")}<span>${esc(message)}</span>`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove("show"), 4000);
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

function button(label, action, cls, attrs = "") {
  return `<button type="button" class="${cls}" data-action="${action}" ${attrs}>${label}</button>`;
}

function iconButton(name, action, label, attrs = "") {
  return `<button type="button" class="icon-button" data-action="${action}" aria-label="${label}" title="${label}" ${attrs}>${icon(name)}</button>`;
}

function tilde(path) {
  const home = data?.home;
  return home && (path === home || path.startsWith(home + "/")) ? "~" + path.slice(home.length) : path;
}

function capitalize(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function header(title, description, action = "") {
  return `<div class="page-head"><div><h1>${title}</h1><p>${description}</p></div>${action}</div>`;
}

async function connect() {
  try {
    csrf = (await api("/api/session")).csrf;
    await refresh();
  } catch (error) {
    data = null;
    $("#app").innerHTML = `<main class="center"><section class="group notice"><span class="tile bad">${icon("alert")}</span><h1>Can't reach the dashboard</h1><p class="notice-error">${esc(error.message)}</p><p>Run <code>prouse status</code> in a terminal to check on it.</p>${button("Try again", "reconnect", "primary")}</section></main>`;
  }
}

async function refresh() {
  data = await api("/api/state");
  render();
}

function render() {
  if (!data) return;
  const nav = Object.entries(tabs)
    .map(([key, label]) => button(label, "nav", tab === key ? "active" : "", `data-tab="${key}" aria-pressed="${tab === key}"`))
    .join("");
  const view = tab === "workspaces" ? workspacesView() : connectionView();
  $("#app").innerHTML = `<header class="topbar"><div class="bar"><span class="brand"><span class="logo">${icon("prompt")}</span>ProUse</span><nav class="segmented" aria-label="Sections">${nav}</nav>${iconButton("refresh", "refresh", "Refresh")}</div></header><main class="main">${view}<p class="page-footer">Runs only on this computer. Changes apply from the next tool call.</p></main>`;
}

function workspaceRow(w) {
  const state = w.status === "unavailable" ? capitalize(w.reason || "unavailable") : w.status === "disabled" ? "Off" : "";
  const chip = state ? `<span class="chip ${w.status}">${w.status === "unavailable" ? icon("alert") : ""}<span>${esc(state)}</span></span>` : "";
  const badge = w.default ? '<span class="pill">Default</span>' : "";
  return `<li><button type="button" class="row${w.status === "ready" ? "" : " dim"}" data-action="workspace-edit" data-id="${esc(w.id)}"><span class="tile">${icon("folder")}</span><span class="row-text"><span class="row-title"><span class="name">${esc(w.label)}</span>${badge}${chip}</span><span class="row-path" title="${esc(w.root)}">${esc(tilde(w.root))}</span></span>${icon("chevron")}</button></li>`;
}

function workspacesView() {
  const add = button(`${icon("plus")}Add workspace`, "workspace-add", "primary");
  const body = data.workspaces.length
    ? `<ul class="group list">${data.workspaces.map(workspaceRow).join("")}</ul><p class="hint">Relative paths in tool calls resolve against the default workspace. Removing a workspace keeps its folder and files.</p>`
    : `<section class="group empty"><span class="tile">${icon("folder")}</span><strong>No workspaces yet</strong><p>Add a project folder so AI chats can read and edit it.</p>${add}</section>`;
  return header("Workspaces", "Project folders that AI chats can read and edit through ProUse.", data.workspaces.length ? add : "") + body;
}

function highlight(json) {
  return esc(json).replace(/(&quot;(?:[^&]|&(?!quot;))*&quot;)(\s*:)?/g, (match, text, colon) =>
    colon ? `<span class="key">${text}</span>${colon}` : `<span class="str">${text}</span>`,
  );
}

function connectionView() {
  const config = JSON.stringify(data.mcp_config, null, 2);
  const check = `<section class="group"><div class="card-row"><span class="tile">${icon("terminal")}</span><div class="card-text"><strong>MCP server</strong><span>Starts ProUse's MCP server on this computer, lists its tools and reads the default workspace.</span></div>${button(checkResult ? "Run again" : "Run check", "mcp-check", "secondary")}</div><div id="check-result">${checkResult}</div></section>`;
  const steps = [
    ["Open the Claude Desktop config file", 'On macOS it is <code>~/Library/Application Support/Claude/claude_desktop_config.json</code>.'],
    [
      "Add the ProUse server",
      "If the file is empty, paste this block as is. If it already has <code>mcpServers</code>, add or replace only the <code>prouse</code> entry.",
      `<div class="code"><pre id="mcp-config">${highlight(config)}</pre>${button(`${icon("copy")}<span>Copy</span>`, "copy-config", "secondary small")}</div>`,
    ],
    ["Restart Claude Desktop", "Quit it completely, then open it again."],
  ]
    .map(([title, text, extra = ""], index) => `<li><span class="step">${index + 1}</span><div class="step-body"><strong>${title}</strong><div class="step-text">${text}</div>${extra}</div></li>`)
    .join("");
  return (
    header("Connection", "Connect Claude Desktop or another MCP client to ProUse.") +
    check +
    `<h2 class="section-title">Set up Claude Desktop</h2><ol class="group steps">${steps}</ol>` +
    `<p class="meta">ProUse ${esc(data.version)} · Settings in <span title="${esc(data.registry)}">${esc(tilde(data.registry))}</span></p>`
  );
}

function modal({ title, subtitle = "", lead = "", body, onSubmit }) {
  const dialog = $("#dialog");
  dialog.innerHTML = `<div class="sheet"><header>${lead}<div class="sheet-title"><h2>${title}</h2>${subtitle ? `<p>${subtitle}</p>` : ""}</div>${iconButton("close", "close", "Close")}</header>${body}</div>`;
  if (!dialog.open) dialog.showModal();
  dialog.querySelector("[autofocus]")?.focus();
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
  if (!element) return toast(message, "bad");
  element.hidden = false;
  element.textContent = message;
}

function footer(submit, extra = "") {
  return `<p id="form-error" class="form-error" role="alert" hidden></p><footer>${extra}<span class="spacer"></span>${button("Cancel", "close", "secondary")}<button type="submit" class="primary">${submit}</button></footer>`;
}

function setting(name, title, text, attrs = "") {
  return `<label class="setting"><span><strong>${title}</strong><small>${text}</small></span><input type="checkbox" class="switch" name="${name}" ${attrs}></label>`;
}

function find(id) {
  return data.workspaces.find((w) => w.id === id);
}

function workspaceAdd() {
  modal({
    title: "Add workspace",
    subtitle: "Choose a project folder for AI chats to work in.",
    body: `<form><div class="field"><label for="path">Folder</label><div class="input-row"><input id="path" name="path" required autocomplete="off" spellcheck="false" placeholder="~/projects/app" autofocus>${button("Browse…", "browse", "secondary")}</div><div id="browser"></div></div><div class="field"><label for="label">Name <span class="optional">Optional</span></label><input id="label" name="label" maxlength="120" autocomplete="off" placeholder="Uses the folder name"></div><div class="settings">${setting("default", "Make it the default", "Relative paths resolve against the default workspace.")}</div>${footer("Add workspace")}</form>`,
    onSubmit: async (form) => {
      const result = await api("/api/workspaces/add", {
        path: form.get("path"),
        label: form.get("label") || null,
        default: form.has("default"),
      });
      closeDialog();
      await refresh();
      toast(`Added ${find(result.workspace_id)?.label || result.workspace_id}.`);
    },
  });
}

async function browse(path) {
  const result = await api("/api/browse", { path: path || null });
  const target = $("#browser");
  if (!target) return;
  const up = result.parent ? iconButton("up", "browse", "Parent folder", `data-path="${esc(result.parent)}"`) : "";
  const choose = button("Choose", "browse-select", "primary small", `data-path="${esc(result.path)}" ${result.can_select ? "" : "disabled"}`);
  const list =
    result.directories.map((d) => button(`${icon("folder")}<span>${esc(d.name)}</span>`, "browse", "folder", `data-path="${esc(d.path)}"`)).join("") ||
    '<p class="browser-empty">No subfolders</p>';
  target.innerHTML = `<div class="browser"><div class="browser-head">${up}<span class="browser-path" title="${esc(result.path)}"><bdi dir="ltr">${esc(tilde(result.path))}</bdi></span>${choose}</div><div class="browser-list">${list}</div></div>`;
}

function selectFolder(path) {
  const form = $("#dialog form");
  if (!form) return;
  form.elements.path.value = tilde(path);
  form.elements.label.placeholder = path.split("/").filter(Boolean).pop() || "Uses the folder name";
  $("#browser").innerHTML = "";
  form.elements.label.focus();
}

function workspaceEdit(id) {
  const w = find(id);
  if (!w) return;
  const problem =
    w.status === "unavailable"
      ? `<p class="callout">${icon("alert")}<span>${esc(capitalize(w.reason || "unavailable"))}. AI chats can't use this workspace until the folder is back.</span></p>`
      : "";
  const defaultText = w.default ? "Make another workspace the default to change this." : "Relative paths resolve against this folder.";
  modal({
    title: esc(w.label),
    subtitle: `<span title="${esc(w.root)}">${esc(tilde(w.root))}</span>`,
    lead: `<span class="tile${w.status === "ready" ? "" : " dim"}">${icon("folder")}</span>`,
    body: `<form>${problem}<div class="field"><label for="label">Name</label><input id="label" name="label" required maxlength="120" autocomplete="off" value="${esc(w.label)}" autofocus></div><div class="settings">${setting("enabled", "Enabled", "When off, AI chats can't read or edit this folder.", w.enabled ? "checked" : "")}${setting("default", "Default workspace", defaultText, w.default ? "checked disabled" : "")}</div>${footer("Save", button(`${icon("trash")}Remove`, "workspace-remove", "ghost danger", `data-id="${esc(id)}"`))}</form>`,
    onSubmit: async (form) => {
      await api("/api/workspaces/update", {
        id,
        label: form.get("label"),
        enabled: form.has("enabled"),
        default: form.has("default"),
      });
      closeDialog();
      await refresh();
      toast("Saved.");
    },
  });
}

function workspaceRemove(id) {
  const w = find(id);
  if (!w) return;
  modal({
    title: `Remove ${esc(w.label)}?`,
    lead: `<span class="tile bad">${icon("trash")}</span>`,
    body: `<p class="sheet-text">ProUse stops offering this folder to AI chats. The folder and its files stay where they are.</p><p class="path-box">${esc(tilde(w.root))}</p><p id="form-error" class="form-error" role="alert" hidden></p><footer><span class="spacer"></span>${button("Cancel", "close", "secondary", "autofocus")}${button("Remove", "workspace-remove-confirm", "danger-solid", `data-id="${esc(id)}"`)}</footer>`,
  });
}

async function copyConfig(control) {
  const element = $("#mcp-config");
  try {
    await navigator.clipboard.writeText(element.textContent);
    control.innerHTML = `${icon("check")}<span>Copied</span>`;
    clearTimeout(copyConfig.timer);
    copyConfig.timer = setTimeout(() => {
      if (control.isConnected) control.innerHTML = `${icon("copy")}<span>Copy</span>`;
    }, 2000);
  } catch {
    const range = document.createRange();
    range.selectNodeContents(element);
    getSelection().removeAllRanges();
    getSelection().addRange(range);
    toast("Copying is blocked, so the text is selected. Copy it manually.", "bad");
  }
}

async function mcpCheck(control) {
  control.disabled = true;
  control.classList.add("busy");
  control.textContent = "Checking…";
  try {
    const result = await api("/api/mcp/check", {});
    checkResult =
      result.status === "ok"
        ? `<div class="result ok">${icon("check")}<span>Connected · ${esc(result.tools)} tools · default workspace readable</span></div>`
        : `<div class="result bad">${icon("alert")}<span>${esc(result.error)}</span></div>`;
    $("#check-result").innerHTML = checkResult;
  } finally {
    if (control.isConnected) {
      control.disabled = false;
      control.classList.remove("busy");
      control.textContent = checkResult ? "Run again" : "Run check";
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
      const label = find(id)?.label || id;
      await api("/api/workspaces/remove", { id });
      closeDialog();
      await refresh();
      toast(`Removed ${label}. Its folder and files are unchanged.`);
    } else if (action === "browse") {
      await browse(path);
    } else if (action === "browse-select") {
      selectFolder(path);
    } else if (action === "copy-config") {
      await copyConfig(target);
    } else if (action === "mcp-check") {
      await mcpCheck(target);
    }
  } catch (error) {
    if ($("#dialog").open) showError(error.message);
    else toast(error.message, "bad");
  }
});

const dialog = $("#dialog");
dialog.addEventListener("mousedown", (event) => (dialog.pressedOutside = event.target === dialog));
dialog.addEventListener("click", (event) => {
  if (event.target === dialog && dialog.pressedOutside) closeDialog();
});

window.addEventListener("focus", () => {
  if (data && !$("#dialog").open) refresh().catch(() => {});
});

connect();
