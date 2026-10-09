"use strict";
const $ = (selector) => document.querySelector(selector);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
const tabs = { workspaces: "워크스페이스", connection: "연결" };
const statuses = {
  ready: ["사용 중", "good"],
  disabled: ["꺼짐", ""],
  unavailable: ["사용 불가", "bad"],
};
const reasons = {
  "folder not found": "폴더를 찾을 수 없습니다.",
  "home or filesystem root is not allowed": "홈 폴더나 최상위 폴더는 쓸 수 없습니다.",
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
  return `<div class="title-row"><div><h1>${title}</h1><p class="muted">${description}</p></div><div class="actions">${action}${button("새로고침", "refresh")}</div></div>`;
}

async function connect() {
  try {
    csrf = (await api("/api/session")).csrf;
    await refresh();
  } catch (error) {
    data = null;
    $("#app").innerHTML = `<main class="main"><section class="card"><h1>대시보드에 연결할 수 없습니다</h1><p class="error">${esc(error.message)}</p><p class="muted">터미널에서 <code>prouse status</code>로 상태를 확인하세요.</p>${button("다시 연결", "reconnect")}</section></main>`;
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
  $("#app").innerHTML = `<div class="shell"><aside class="sidebar"><div class="brand">ProUse<small>Use Pro models across your local workspaces.</small></div><nav class="nav">${nav}</nav><div class="sidebar-footer">이 컴퓨터에서 쓰는 관리 화면입니다.<br>바꾼 설정은 다음 도구 호출부터 바로 적용됩니다.</div></aside><main class="main">${view}</main></div>`;
}

function workspacesView() {
  const rows = data.workspaces
    .map((w) => {
      const [label, cls] = statuses[w.status] || [w.status, ""];
      const reason = w.reason ? `<span class="secondary">${esc(reasons[w.reason] || w.reason)}</span>` : "";
      const actions = [
        w.status === "ready" && !w.default ? button("기본으로", "workspace-default", "", `data-id="${esc(w.id)}"`) : "",
        button("수정", "workspace-edit", "", `data-id="${esc(w.id)}"`),
        button("삭제", "workspace-remove", "danger", `data-id="${esc(w.id)}"`),
      ].join("");
      return `<tr><td><strong>${esc(w.label)}</strong> ${w.default ? badge("기본", "good") : ""}<span class="secondary mono">${esc(w.id)} · ${esc(w.root)}</span></td><td>${badge(label, cls)}${reason}</td><td><div class="actions end">${actions}</div></td></tr>`;
    })
    .join("");
  const table = rows
    ? `<div class="table-wrap"><table><thead><tr><th>워크스페이스</th><th>상태</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
    : '<p class="empty">등록된 워크스페이스가 없습니다. AI 채팅에서 다룰 프로젝트 폴더를 추가하세요.</p>';
  return (
    header(
      "워크스페이스",
      "AI 채팅이 ProUse 도구로 읽고 고칠 수 있는 프로젝트 폴더입니다.",
      button("+ 워크스페이스 추가", "workspace-add", "primary"),
    ) +
    `<section class="card">${table}</section><p class="muted">상대 경로는 기본 워크스페이스를 기준으로 풀립니다. 삭제는 등록만 해제하며 폴더와 파일은 그대로 둡니다.</p>`
  );
}

function connectionView() {
  return (
    header("연결", "Claude Desktop 같은 MCP 클라이언트가 ProUse 도구를 쓰도록 연결합니다.") +
    `<section class="card"><h2>Claude Desktop에 연결</h2><ol class="steps"><li>Claude Desktop 설정 파일을 엽니다. macOS에서는 <code>~/Library/Application Support/Claude/claude_desktop_config.json</code>입니다.</li><li>파일이 비어 있으면 아래 내용을 그대로 넣고, 이미 <code>mcpServers</code>가 있으면 그 안에 <code>prouse</code> 항목만 추가하거나 바꿉니다.</li><li>Claude Desktop을 완전히 종료했다가 다시 엽니다.</li></ol><pre class="detail" id="mcp-config">${esc(JSON.stringify(data.mcp_config, null, 2))}</pre><div class="actions">${button("복사", "copy-config")}</div></section>` +
    `<section class="card"><h2>연결 확인</h2><p class="muted">이 컴퓨터에서 ProUse MCP 서버를 직접 띄워 초기화, 도구 목록, 기본 워크스페이스 읽기를 확인합니다. Claude Desktop 쪽 연결 상태는 Claude Desktop에서 확인하세요.</p><div class="actions">${button("MCP 연결 확인", "mcp-check", "primary")}</div><div id="check-result" class="preview"></div></section>` +
    `<p class="muted">ProUse ${esc(data.version)} · 설정 파일 <span class="mono">${esc(data.registry)}</span></p>`
  );
}

function modal(title, html, onSubmit) {
  const dialog = $("#dialog");
  dialog.innerHTML = `<button type="button" class="close" data-action="close" aria-label="닫기">×</button><h2>${title}</h2>${html}<div id="form-error"></div>`;
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
  return `<footer>${button("취소", "close")}<button class="primary" type="submit">${label}</button></footer>`;
}

function find(id) {
  return data.workspaces.find((w) => w.id === id);
}

function workspaceAdd() {
  modal(
    "워크스페이스 추가",
    `<form><label>프로젝트 폴더<input name="path" required autocomplete="off" placeholder="~/projects/app"><span>경로를 붙여넣거나 아래에서 폴더를 고르세요.</span></label><div class="workspace-picker">${button("홈 폴더에서 찾기", "browse")}<div id="browser"></div></div><label>이름<input name="label" maxlength="120" placeholder="비워 두면 폴더 이름을 씁니다"></label><label><input type="checkbox" name="default">기본 워크스페이스로 사용</label>${footer("추가")}</form>`,
    async (form) => {
      const result = await api("/api/workspaces/add", {
        path: form.get("path"),
        label: form.get("label") || null,
        default: form.has("default"),
      });
      closeDialog();
      await refresh();
      toast(`${result.workspace_id} 워크스페이스를 추가했습니다.`);
    },
  );
}

async function browse(path) {
  const result = await api("/api/browse", { path: path || null });
  const target = $("#browser");
  if (!target) return;
  const actions = [
    result.parent ? button("상위 폴더", "browse", "", `data-path="${esc(result.parent)}"`) : "",
    button("이 폴더 선택", "browse-select", "primary", `data-path="${esc(result.path)}" ${result.can_select ? "" : "disabled"}`),
  ].join("");
  const list =
    result.directories.map((d) => button(`📁 ${esc(d.name)}`, "browse", "", `data-path="${esc(d.path)}"`)).join("") ||
    '<span class="muted">하위 폴더가 없습니다.</span>';
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
  const defaultNote = w.default ? "<span>다른 워크스페이스를 기본으로 지정하면 바뀝니다.</span>" : "";
  modal(
    "워크스페이스 수정",
    `<form><p class="mono">${esc(w.root)}</p><label>이름<input name="label" required maxlength="120" value="${esc(w.label)}"></label><label><input type="checkbox" name="enabled" ${w.enabled ? "checked" : ""}>사용<span>끄면 AI 채팅이 이 폴더를 읽거나 고칠 수 없습니다.</span></label><label><input type="checkbox" name="default" ${w.default ? "checked disabled" : ""}>기본 워크스페이스로 사용${defaultNote}</label>${footer("저장")}</form>`,
    async (form) => {
      await api("/api/workspaces/update", {
        id,
        label: form.get("label"),
        enabled: form.has("enabled"),
        default: form.has("default"),
      });
      closeDialog();
      await refresh();
      toast("워크스페이스 설정을 저장했습니다.");
    },
  );
}

function workspaceRemove(id) {
  const w = find(id);
  if (!w) return;
  modal(
    "워크스페이스 삭제",
    `<p><strong>${esc(w.label)}</strong> 등록을 해제합니다. 폴더와 파일은 그대로 남습니다.</p><p class="mono">${esc(w.root)}</p><footer>${button("취소", "close")}${button("삭제", "workspace-remove-confirm", "danger", `data-id="${esc(id)}"`)}</footer>`,
  );
}

async function copyConfig() {
  const element = $("#mcp-config");
  try {
    await navigator.clipboard.writeText(element.textContent);
    toast("설정을 복사했습니다.");
  } catch {
    const range = document.createRange();
    range.selectNodeContents(element);
    getSelection().removeAllRanges();
    getSelection().addRange(range);
    toast("자동 복사가 막혀 있어 내용을 선택해 두었습니다. 직접 복사하세요.");
  }
}

async function mcpCheck(control) {
  control.disabled = true;
  control.textContent = "확인 중…";
  try {
    const result = await api("/api/mcp/check", {});
    $("#check-result").innerHTML =
      result.status === "ok"
        ? `<div class="notice">연결 정상 · 도구 ${esc(result.tools)}개 · 기본 워크스페이스 읽기 확인</div>`
        : `<p class="error">${esc(result.error)}</p>`;
  } finally {
    if (control.isConnected) {
      control.disabled = false;
      control.textContent = "MCP 연결 확인";
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
      toast("등록을 해제했습니다. 폴더와 파일은 그대로입니다.");
    } else if (action === "workspace-default") {
      await api("/api/workspaces/update", { id, default: true });
      await refresh();
      toast("기본 워크스페이스를 바꿨습니다.");
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
