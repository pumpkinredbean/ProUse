"use strict";
const $ = (s) => document.querySelector(s);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const pretty = (v) => JSON.stringify(v, null, 2);
const time = (v) => (v ? new Date(v).toLocaleString("ko-KR") : "—");
const decodeBase64 = (value) => {
  if (!value) return "";
  try { return new TextDecoder().decode(Uint8Array.from(atob(value), (c) => c.charCodeAt(0))); }
  catch { return "[base64 output could not be decoded as UTF-8]"; }
};
const terminal = [
  "succeeded",
  "failed",
  "scope_violation",
  "timed_out",
  "cancelled",
  "closed",
  "interrupted",
  "conflict",
];
const names = {
  overview: "운영 현황",
  workspaces: "워크스페이스",
  profiles: "위임 워커 프로필",
  policies: "읽기 정책",
  tasks: "위임 작업",
  executions: "직접 실행",
  connection: "연결 · 진단",
  history: "변경 이력",
};
let csrf = "",
  data = null,
  tab = "overview",
  busy = false,
  taskFilter = "",
  taskStatus = "",
  history = null;
function toast(s) {
  $("#toast").textContent = s;
  $("#toast").style.display = "block";
  setTimeout(() => ($("#toast").style.display = "none"), 5000);
}
async function api(path, body) {
  const r = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const v = await r.json();
  if (!r.ok) {
    throw Error(v.error || `HTTP ${r.status}`);
  }
  return v;
}
function status(s) {
  let label =
    {
      succeeded: "성공",
      running: "실행 중",
      queued: "대기",
      failed: "실패",
      scope_violation: "범위 위반",
      timed_out: "시간 초과",
      cancelled: "취소",
      closed: "닫힘",
      interrupted: "중단됨",
      conflict: "충돌",
      open: "열림",
      opening: "여는 중",
      cancelling: "취소 중",
      corrupt: "확인 필요",
    }[s] || s;
  return `<span class="badge ${["succeeded", "closed"].includes(s) ? "good" : ["running", "queued", "open", "opening", "cancelling"].includes(s) ? "warn" : "bad"}">${esc(label)}</span>`;
}
function health(ok, label) {
  return `<span class="badge ${ok ? "good" : "bad"}">${ok ? "●" : "○"} ${esc(label)}</span>`;
}
function button(label, action, cls = "", attrs = "") {
  return `<button type="button" class="${cls}" data-action="${action}" ${attrs}>${label}</button>`;
}
function connectionError(error) {
  data = null;
  $("#app").innerHTML =
    `<main class="main"><section class="card"><h1>Admin 연결 확인</h1><p class="error">${esc(error.message)}</p>${button("다시 연결", "reconnect")}</section></main>`;
}
async function connect() {
  try {
    csrf = (await api("/api/session")).csrf;
    await refresh(true);
  } catch (error) {
    connectionError(error);
  }
}
async function refresh() {
  data = await api("/api/state");
  if (tab === "history") history = await api("/api/history");
  render();
}
async function applyBundle(bundle, message) {
  await api("/api/config/apply", {
    bundle,
    expected_revision: data.revision,
  });
  if ($("#dialog").open) $("#dialog").close();
  await refresh();
  toast(message);
}
function render() {
  if (!data) return;
  const active = [...data.tasks, ...(data.direct_executions || [])].filter((t) => !terminal.includes(t.status));
  $("#app").innerHTML =
    `<div class="shell"><aside class="sidebar"><div class="brand">ProUse<small>Use Pro models across your local workspaces.</small></div><nav class="nav">${Object.entries(
      names,
    )
      .map(([k, v]) =>
        button(v, "nav", "" + (tab === k ? "active" : ""), `data-tab="${k}"`),
      )
      .join(
        "",
      )}</nav><div class="sidebar-footer">로컬 운영자 전용<br>Admin 권한은 MCP에 노출되지 않습니다.<br>저장한 설정은 즉시 적용됩니다.</div></aside><main class="main"><div class="topbar"><span class="eyebrow">CONTROL / ${esc(names[tab])}</span><div class="actions">${health(data.runtime.ready, "MCP 터널")}${button("새로고침", "refresh")}</div></div>${data.paused ? '<div class="notice">신규 위임과 직접 command가 중지되어 있습니다. 기존 직접 operation의 조회와 취소는 유지됩니다.</div>' : ""}<div class="title-row"><div><h1>${names[tab]}</h1><p class="muted">${{ overview: "위임 작업과 Pro가 직접 제어하는 실행을 구분해 실제 상태를 확인합니다.", workspaces: "실행할 프로젝트와 위임 워커 기본 프로필을 명시적으로 승인합니다.", profiles: "위임 워커의 모델과 추론 수준을 관리합니다.", policies: "상위 모델에 공개할 파일을 한정하고 저장 즉시 적용합니다.", tasks: "독립 워커의 모델·thread 출처와 결과를 확인합니다.", executions: "command·출력·diff 영수증을 확인하고 직접 operation을 취소합니다.", connection: "MCP와 직접 실행 runtime을 실제 로컬 진단으로 확인합니다.", history: "설정 적용과 운영 조작을 추적하고 이전 설정을 복구합니다." }[tab]}</p></div>${tab === "workspaces" ? button("+ 워크스페이스", "workspace-add", "primary") : tab === "profiles" ? button("+ 워커 프로필", "profile-add", "primary") : ""}</div>${{ overview: () => overview(active), workspaces, profiles, policies, tasks, executions, connection, history: historyView }[tab]()}</main></div>`;
}
function overview(active) {
  const ws = data.bundle.config.workspaces,
    fail = data.tasks.filter((t) =>
      ["failed", "scope_violation", "timed_out"].includes(t.status),
    );
  const direct = data.direct_executions || [];
  const activeDirect = direct.filter((t) => !terminal.includes(t.status));
  const failedDirect = direct.filter((t) => ["failed", "scope_violation", "timed_out", "corrupt"].includes(t.status));
  return `<div class="grid"><div class="stat"><span>활성 워크스페이스</span><strong>${ws.filter((w) => w.enabled).length}<small> / ${ws.length}</small></strong><span>명시적으로 승인된 실행 대상</span></div><div class="stat"><span>위임 작업</span><strong>${data.tasks.filter((t) => !terminal.includes(t.status)).length}</strong><span>독립 모델 추론</span></div><div class="stat"><span>직접 실행</span><strong>${activeDirect.length}</strong><span>추가 모델 추론 없음</span></div><div class="stat"><span>실패 · 확인 필요</span><strong>${fail.length + failedDirect.length}</strong><span>누적 영수증 기준</span></div></div><div class="split"><section class="card"><h2>최근 직접 실행</h2>${executionTable(direct.slice(0, 5))}</section><section class="card"><h2>실행 제어</h2><p class="muted">유지보수 중에는 새 위임과 직접 명령·패치를 중지하세요. 설정 저장과 전용 터널 재시작은 실행 중인 위임 작업·직접 operation이 없을 때 가능합니다.</p>${button(data.paused ? "신규 작업 접수 재개" : "신규 작업 접수 중지", "pause", data.paused ? "primary" : "")}<div class="list-item"><div><strong>기본 워크스페이스</strong><small>${esc(data.bundle.config.default_workspace_id || "유일한 활성 대상 자동 선택")}</small></div></div><div class="list-item"><div><strong>현재 설정 버전</strong><small class="mono">${data.revision.slice(0, 16)}</small></div><span class="badge">저장됨</span></div></section></div>`;
}
function workspaces() {
  const bundle = data.bundle;
  return `<section class="card"><div class="table-wrap"><table><thead><tr><th>워크스페이스</th><th>상태</th><th>위임 기본</th><th>컨텍스트</th><th></th></tr></thead><tbody>${bundle.config.workspaces.map((w) => `<tr><td><strong>${esc(w.label)}</strong><span class="secondary mono">${esc(w.id)} ${w.id === bundle.config.default_workspace_id ? "· 기본" : ""}</span><span class="secondary mono">${esc(w.root)}</span></td><td>${health(w.enabled, w.enabled ? "활성" : "비활성")}</td><td>${esc(w.default_worker_profile || bundle.config.default_worker_profile)}</td><td>${bundle.policies[w.id] ? "정책 지정됨" : "미공개"}</td><td>${button("수정", "workspace-edit", "", `data-id="${esc(w.id)}"`)}</td></tr>`).join("") || '<tr><td colspan="5" class="empty">등록된 워크스페이스가 없습니다.</td></tr>'}</tbody></table></div></section><p class="muted">같은 실제 경로나 상하위 경로도 별도 workspace ID로 등록할 수 있습니다. 겹치는 실행은 하나의 writer lock으로 직렬화되며 저장한 설정은 즉시 적용됩니다.</p>`;
}
function profiles() {
  const config = data.bundle.config;
  return `<section class="card"><table><thead><tr><th>프로필</th><th>모델</th><th>추론 수준</th><th>사용 중인 대상</th><th></th></tr></thead><tbody>${config.worker_profiles.map((p) => `<tr><td><strong>${esc(p.label || p.id)}</strong><span class="secondary mono">${esc(p.id)} ${p.id === config.default_worker_profile ? "· 전역 기본" : ""}</span></td><td class="mono">${esc(p.model)}</td><td><span class="badge">${esc(p.reasoning_effort)}</span></td><td>${config.workspaces.filter((w) => (w.default_worker_profile || config.default_worker_profile) === p.id).length}</td><td>${button("수정", "profile-edit", "", `data-id="${esc(p.id)}"`)}</td></tr>`).join("")}</tbody></table></section><div class="notice">프로필 선택 순서: 요청에서 지정한 프로필 → 워크스페이스 기본값 → 전역 기본값. 실제 실행 모델과 추론 수준은 작업 영수증에서 별도로 검증됩니다.</div>`;
}
function policies() {
  return (
    data.bundle.config.workspaces
      .map((w) => {
        const p = data.bundle.policies[w.id];
        return `<section class="card"><div class="title-row"><div><h2>${esc(w.label)}</h2><small class="mono">${esc(w.id)}</small></div><div class="actions">${button("정책 편집", "policy-edit", "", `data-id="${esc(w.id)}"`)}${button("접근 범위 미리보기", "policy-preview", "", `data-id="${esc(w.id)}" ${p ? "" : "disabled"}`)}</div></div>${p ? `<div class="split"><div><h3>개별 파일 · ${(p.files || []).length}</h3><pre class="detail">${esc((p.files || []).join("\n") || "없음")}</pre></div><div><h3>디렉터리 · ${(p.directories || []).length}</h3>${(p.directories || []).map((d) => `<div class="list-item"><div><strong class="mono">${esc(d.path)}</strong><small>${esc(d.extensions.join(" · "))}</small></div></div>`).join("") || '<p class="muted">없음</p>'}</div></div>` : '<p class="muted">컨텍스트를 공개하지 않습니다. 정책을 추가하면 지정된 파일만 읽을 수 있습니다.</p>'}</section>`;
      })
      .join("") ||
    '<div class="card empty">워크스페이스를 먼저 등록하세요.</div>'
  );
}
function taskTable(list) {
  return `<div class="table-wrap"><table><thead><tr><th>작업 / 워크스페이스</th><th>상태</th><th>프로필</th><th></th></tr></thead><tbody>${list.map((t) => `<tr><td class="task-id"><strong>${esc(t.orchestrator_task_id)}</strong><span class="secondary">${esc(t.workspace_id)} · ${time(t.created_at)} ${t.legacy_receipt ? "· 과거 영수증" : ""}</span></td><td>${status(t.status)}${t.cancel_requested ? '<span class="secondary">취소 요청됨</span>' : ""}</td><td>${esc(t.worker_profile_id || "—")}</td><td>${button("상세", "task-detail", "", `data-id="${esc(t.orchestrator_task_id)}" data-ws="${esc(t.workspace_id)}"`)}</td></tr>`).join("") || '<tr><td colspan="4" class="empty">해당 작업이 없습니다.</td></tr>'}</tbody></table></div>`;
}
function executionTable(list) {
  return `<div class="table-wrap"><table><thead><tr><th>operation / 워크스페이스</th><th>상태</th><th>시작</th><th></th></tr></thead><tbody>${list.map((x) => `<tr><td class="task-id"><strong>${esc(x.operation_id)}</strong><span class="secondary">${esc(x.workspace_id)}</span></td><td>${status(x.status)}</td><td>${time(x.updated_at || x.created_at)}</td><td>${button("상세", "execution-detail", "", `data-id="${esc(x.operation_id)}" data-ws="${esc(x.workspace_id)}"`)}</td></tr>`).join("") || '<tr><td colspan="4" class="empty">직접 operation 기록이 없습니다.</td></tr>'}</tbody></table></div>`;
}
function tasks() {
  return `<div class="filters"><input id="task-search" aria-label="작업 검색" placeholder="작업 ID · 워크스페이스 검색" value="${esc(taskFilter)}"><select id="task-status" aria-label="작업 상태"><option value="">모든 상태</option>${["running", "queued", ...terminal].map((s) => `<option ${taskStatus === s ? "selected" : ""}>${s}</option>`).join("")}</select></div><div class="card" id="task-table">${filteredTasks()}</div><p class="muted">실패한 작업은 자동 재실행하지 않습니다. 같은 ID는 기존 영수증을 보존하며, 새 실행은 상위 오케스트레이터가 새로운 작업 ID로 요청해야 합니다.</p>`;
}
function executions() {
  return `<section class="card">${executionTable(data.direct_executions || [])}</section><p class="muted">직접 실행은 Pro가 명시한 command만 실행하는 operation입니다. 별도 Codex 모델 thread나 worker usage는 생성하지 않습니다. 완료되지 않은 operation은 여기서 취소할 수 있습니다.</p>`;
}
function filteredTasks() {
  return taskTable(
    data.tasks.filter(
      (t) =>
        (!taskStatus || t.status === taskStatus) &&
        `${t.orchestrator_task_id} ${t.workspace_id}`
          .toLowerCase()
          .includes(taskFilter.toLowerCase()),
    ),
  );
}
function connection() {
  const r = data.runtime;
  const direct = data.direct_diagnostics || {};
  return `<section class="card"><h2>직접 실행 backend</h2><div class="list-item"><strong>상태</strong>${health(direct.status === "ready", direct.status || "미확인")}</div><div class="list-item"><div><strong>adapter / CLI / schema</strong><small class="mono">${esc(direct.adapter || "—")} / ${esc(direct.cli_version || "—")} / ${esc(direct.schema_version || "—")}</small></div><span>${esc(direct.active_executions ?? 0)} active</span></div><p class="muted">진단 정보는 local backend 상태입니다. 직접 실행은 대화에서 해당 도구를 호출할 때만 시작되며, 이 화면에서 runtime·권한·한도·모드를 설정하지 않습니다.</p></section><div class="split"><section class="card"><h2>전용 터널</h2><div class="list-item"><strong>프로세스 health</strong>${health(r.health, r.health ? "정상" : "연결 불가")}</div><div class="list-item"><strong>MCP ready</strong>${health(r.ready, r.ready ? "준비됨" : "준비되지 않음")}</div><p class="muted">진단은 현재 소스와 레지스트리로 실제 MCP stdio 세션을 열어 도구 목록과 상태를 확인합니다. 재시작은 연결된 전용 터널 하나에만 적용됩니다.</p><div class="actions">${button("MCP 연결 진단", "probe", "primary")}${button("독립 워커 검증", "worker-probe")}${button("전용 터널 재시작", "restart", "danger", r.configured ? "" : "disabled")}${r.operator_url ? `<a href="${esc(r.operator_url)}" target="_blank" rel="noopener noreferrer">터널 상세 상태 ↗</a>` : ""}</div><div id="probe-output" class="preview"></div></section><section class="card"><h2>설정 반영 상태</h2><p>각 MCP 호출은 검증된 최신 레지스트리를 읽습니다.</p><div class="list-item"><div><strong>현재 레지스트리</strong><small class="mono">${esc(r.desired_registry_sha256?.slice(0, 20))}</small></div></div><div class="list-item"><div><strong>마지막 MCP 호출에서 관측</strong><small>${time(r.last_mcp_call?.observed_at)}</small><small class="mono">${esc(r.last_mcp_call?.registry_sha256?.slice(0, 20) || "아직 호출되지 않음")}</small></div>${health(r.last_mcp_call?.registry_sha256 === r.desired_registry_sha256, "설정 일치")}</div><p class="muted">로컬 discovery 성공과 ChatGPT 대화의 도구 목록은 별도입니다. 목록이 이전 상태면 connector tool catalogue를 새로고침하세요.</p></section></div><div class="notice">상위 Pro가 연구 판단과 다음 작업을 선택합니다. 직접 실행은 Pro가 명시한 command를 무모델 backend에 전달하고, 위임 worker는 별도 모델 추론과 thread provenance를 유지합니다.</div>`;
}
function historyView() {
  if (!history) return '<div class="card">이력을 불러오는 중…</div>';
  return `<div class="split"><section class="card"><h2>설정 버전</h2>${history.revisions.map((r) => `<div class="list-item"><div><strong>${time(r.saved_at)}</strong><small class="mono">${r.revision.slice(0, 20)}</small></div>${r.revision === data.revision ? '<span class="badge good">현재</span>' : button("복구", "rollback", "", `data-id="${r.revision}"`)}</div>`).join("")}</section><section class="card"><h2>운영 변경 이력</h2><div class="timeline">${history.events.map((e) => `<p><strong>${esc({ configuration_applied: "설정 적용", configuration_restored: "이전 설정 복구", task_cancel_requested: "작업 취소 요청", admission_changed: e.paused ? "신규 접수 중지" : "신규 접수 재개", mcp_probe: "MCP 연결 진단", worker_probe_submitted: "독립 워커 검증 시작", orphaned_task_reconciled: "중단 작업 회수", dedicated_runtime_restarted: "전용 터널 재시작" }[e.action] || e.action)}</strong><br>${time(e.time)}<br><span class="mono">${esc(e.orchestrator_task_id || e.after?.slice(0, 20) || "")}</span></p>`).join("") || '<p class="muted">아직 운영 변경이 없습니다.</p>'}</div></section></div>`;
}
function modal(title, html, onSubmit) {
  $("#dialog").innerHTML =
    `<button class="close" data-action="close" aria-label="닫기">×</button><h2>${title}</h2>${html}<div id="form-error"></div>`;
  $("#dialog").showModal();
  const f = $("#dialog form");
  if (f)
    f.onsubmit = async (e) => {
      e.preventDefault();
      try {
        await onSubmit(new FormData(f));
      } catch (err) {
        $("#form-error").className = "error";
        $("#form-error").textContent = err.message;
      }
    };
}
function footer(save = "저장하고 즉시 적용") {
  return `<footer>${button("취소", "close")}<button class="primary" type="submit">${save}</button></footer>`;
}
async function workspaceEdit(id) {
  const w = data.bundle.config.workspaces.find((w) => w.id === id) || {
    id: "",
    label: "",
    root: "",
    enabled: true,
  };
  let discovery = { status: "unavailable", message: "최근 Codex 작업 폴더를 불러오지 못했습니다.", candidates: [], browse_roots: [] };
  try {
    discovery = await api("/api/workspace-candidates");
  } catch (error) {
    discovery.message = error.message;
  }
  const options = discovery.candidates
    .map((candidate) => {
      const source = candidate.source === "registered" ? `등록됨 · ${candidate.registered_workspace_id}` : "최근 Codex 작업";
      return `<option value="${esc(candidate.path)}" data-label="${esc(candidate.label)}" ${candidate.path === w.root ? "selected" : ""}>${esc(candidate.label)} · ${esc(source)} — ${esc(candidate.path)}</option>`;
    })
    .join("");
  modal(
    id ? "워크스페이스 수정" : "워크스페이스 등록",
    `<form><div class="row"><label>안정적인 ID<input name="id" value="${esc(w.id)}" pattern="[a-z][a-z0-9_-]{0,63}" required ${id ? "readonly" : ""}><span>영문 소문자로 시작, 숫자·밑줄·하이픈 사용</span></label><label>이름<input name="label" value="${esc(w.label)}" required maxlength="120"></label></div><div class="workspace-picker"><label>최근 Codex 워크스페이스<select id="workspace-candidate"><option value="">선택하세요</option>${options}</select></label><div class="actions">${button("선택한 경로 사용", "workspace-candidate-use")}${button("폴더에서 찾기", "workspace-browse", "", `data-id="${esc(id || "")}"`)}</div>${discovery.message ? `<p class="muted">${esc(discovery.message)}</p>` : '<p class="muted">Codex에서 최근 사용한 프로젝트를 불러왔습니다. 경로를 직접 입력할 필요가 없습니다.</p>'}<div id="workspace-browser"></div></div><label>선택된 프로젝트 루트<input name="root" value="${esc(w.root)}" placeholder="위 목록이나 폴더 탐색에서 선택" required><span>직접 붙여넣기도 가능합니다. 같은 실제 경로와 상하위 경로도 별도 ID로 등록할 수 있습니다.</span></label><label>기본 워커 프로필<select name="profile"><option value="">전역 기본값 사용</option>${data.bundle.config.worker_profiles.map((p) => `<option value="${esc(p.id)}" ${p.id === w.default_worker_profile ? "selected" : ""}>${esc(p.label || p.id)} · ${esc(p.id)}</option>`).join("")}</select></label><label><input type="checkbox" name="enabled" ${w.enabled ? "checked" : ""}>실행과 컨텍스트 조회 활성화</label><label><input type="checkbox" name="default" ${data.bundle.config.default_workspace_id === w.id ? "checked" : ""}>기본 워크스페이스로 사용</label>${id ? button("삭제", "workspace-delete", "danger", `data-id="${esc(id)}" type="button"`) : '<p class="muted">등록 후 읽기 정책에서 공개할 파일을 지정하세요.</p>'}${footer()}</form>`,
    async (f) => {
      const next = structuredClone(data.bundle);
      let nw = {
        id: f.get("id"),
        label: f.get("label"),
        root: f.get("root"),
        enabled: f.has("enabled"),
      };
      if (!id && next.config.workspaces.some((w) => w.id === nw.id))
        throw Error("이미 사용 중인 ID입니다.");
      if (f.get("profile")) nw.default_worker_profile = f.get("profile");
      if (w.context_policy) nw.context_policy = w.context_policy;
      if (f.has("default")) {
        if (!nw.enabled)
          throw Error("기본 워크스페이스는 활성 상태여야 합니다.");
        next.config.default_workspace_id = nw.id;
      } else if (next.config.default_workspace_id === nw.id)
        delete next.config.default_workspace_id;
      const i = next.config.workspaces.findIndex((v) => v.id === id);
      if (i < 0) next.config.workspaces.push(nw);
      else next.config.workspaces[i] = nw;
      await applyBundle(next, "워크스페이스 설정을 저장하고 즉시 적용했습니다.");
    },
  );
}
function useWorkspacePath(path, label) {
  const form = $("#dialog form");
  if (!form || !path) return;
  form.elements.root.value = path;
  const base = label || path.split("/").filter(Boolean).pop() || "workspace";
  if (!form.elements.label.value) form.elements.label.value = base;
  if (!form.elements.id.value) {
    const id = base.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "");
    if (/^[a-z]/.test(id)) form.elements.id.value = id.slice(0, 64);
  }
}
async function workspaceBrowse(path, workspaceId) {
  const result = await api("/api/workspace/browse", {
    path: path || null,
    exclude_workspace_id: workspaceId || null,
  });
  const target = $("#workspace-browser");
  if (!target) return;
  const current = result.path
    ? `<div class="workspace-browser-head"><strong class="mono">${esc(result.path)}</strong><div class="actions">${result.parent ? button("상위 폴더", "workspace-browse-dir", "", `data-path="${esc(result.parent)}" data-id="${esc(workspaceId || "")}"`) : ""}${button("이 폴더 선택", "workspace-browse-select", "primary", `data-path="${esc(result.path)}" ${result.can_select ? "" : "disabled"}`)}</div></div>`
    : '<p class="muted">탐색을 시작할 위치를 선택하세요.</p>';
  target.innerHTML = `<div class="workspace-browser">${current}<div class="workspace-directory-list">${result.directories.map((item) => button(`📁 ${esc(item.name)}`, "workspace-browse-dir", "", `data-path="${esc(item.path)}" data-id="${esc(workspaceId || "")}"`)).join("") || '<span class="muted">하위 폴더가 없습니다.</span>'}</div></div>`;
}
function profileEdit(id) {
  const p = data.bundle.config.worker_profiles.find((p) => p.id === id) || {
    id: "",
    label: "",
    model: Object.keys(data.capabilities)[0],
    reasoning_effort: "high",
  };
  modal(
    id ? "워커 프로필 수정" : "워커 프로필 등록",
    `<form><div class="row"><label>프로필 ID<input name="id" pattern="[a-z][a-z0-9_-]{0,63}" required value="${esc(p.id)}" ${id ? "readonly" : ""}></label><label>이름<input name="label" value="${esc(p.label || "")}" maxlength="120"></label></div><label>설치된 모델<select name="model" id="profile-model">${Object.keys(
      data.capabilities,
    )
      .map(
        (m) => `<option ${p.model === m ? "selected" : ""}>${esc(m)}</option>`,
      )
      .join(
        "",
      )}</select></label><label>추론 수준<select name="effort" id="profile-effort">${data.capabilities[p.model].map((e) => `<option ${p.reasoning_effort === e ? "selected" : ""}>${esc(e)}</option>`).join("")}</select></label><label><input type="checkbox" name="default" ${data.bundle.config.default_worker_profile === p.id ? "checked" : ""}>전역 기본 프로필</label>${id ? button("삭제", "profile-delete", "danger", `type="button" data-id="${esc(id)}"`) : ""}${footer()}</form>`,
    async (f) => {
      const next = structuredClone(data.bundle);
      const np = {
        id: f.get("id"),
        model: f.get("model"),
        reasoning_effort: f.get("effort"),
      };
      if (f.get("label")) np.label = f.get("label");
      if (!id && next.config.worker_profiles.some((p) => p.id === np.id))
        throw Error("이미 사용 중인 ID입니다.");
      next.config.model_capabilities[np.model] = data.capabilities[np.model];
      const i = next.config.worker_profiles.findIndex((v) => v.id === id);
      if (i < 0) next.config.worker_profiles.push(np);
      else next.config.worker_profiles[i] = np;
      if (f.has("default")) next.config.default_worker_profile = np.id;
      await applyBundle(next, "워커 프로필을 저장하고 즉시 적용했습니다.");
    },
  );
  $("#profile-model").onchange = (e) => {
    $("#profile-effort").innerHTML = data.capabilities[e.target.value]
      .map((x) => `<option>${esc(x)}</option>`)
      .join("");
  };
}
function policyEdit(id) {
  const p = data.bundle.policies[id] || {
    version: 1,
    name: id + "-context",
    files: [],
    directories: [],
  };
  modal(
    "읽기 정책 편집",
    `<form><label>정책 이름<input name="name" value="${esc(p.name || id)}" required maxlength="120"></label><label>개별 파일<textarea name="files" class="mono" placeholder="README.md">${esc((p.files || []).join("\n"))}</textarea><span>워크스페이스 상대 경로를 한 줄에 하나씩 입력하세요.</span></label><label>디렉터리와 확장자<textarea name="dirs" class="mono" placeholder="src | .py, .md">${esc((p.directories || []).map((d) => d.path + " | " + d.extensions.join(", ")).join("\n"))}</textarea><span>한 줄에 디렉터리 | 확장자 목록. 예: src | .py, .md (Dockerfile·Makefile 같은 확장자 없는 파일도 포함되며, 루트 전체는 . 로 지정합니다)</span></label><label><input type="checkbox" name="remove">이 워크스페이스의 컨텍스트 공개 중지</label><p class="muted">비밀 파일, 런타임 디렉터리, 심볼릭 링크와 경로 이탈 차단은 정책으로 해제할 수 없습니다.</p>${footer()}</form>`,
    async (f) => {
      const next = structuredClone(data.bundle);
      if (f.has("remove")) delete next.policies[id];
      else {
        const files = f
          .get("files")
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean);
        const directories = f
          .get("dirs")
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean)
          .map((s) => {
            const [path, ext] = s.split("|");
            if (!ext)
              throw Error("디렉터리는 경로 | .py, .md 형식으로 입력하세요.");
            return {
              path: path.trim(),
              extensions: ext
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean),
            };
          });
        next.policies[id] = {
          version: 1,
          name: f.get("name"),
          files,
          directories,
        };
      }
      await applyBundle(next, "읽기 정책을 저장하고 즉시 적용했습니다.");
    },
  );
}
function taskDetail(id, ws) {
  const t = data.tasks.find(
    (t) => t.orchestrator_task_id === id && t.workspace_id === ws,
  );
  modal(
    "작업 영수증",
    `<p class="mono">${esc(id)}</p>${status(t.status)}<div class="list-item"><strong>워커 프로필 / 실제 모델</strong><span class="mono">${esc(t.worker_profile_id)} / ${esc(t.actual_model || "미확인")} / ${esc(t.actual_reasoning_effort || "미확인")}</span></div><div class="list-item"><strong>독립 Codex thread</strong><span class="mono">${esc(t.codex_thread_id || "미확인")}</span></div><p class="muted">종료 코드 ${esc(t.exit_code ?? "—")} · 변경 ${(t.changed_paths || []).length} · 예상 밖 변경 ${(t.unexpected_changed_paths || []).length}</p><pre class="detail">${esc(pretty(t))}</pre><footer>${button("영수증 다운로드", "download", "", `data-id="${esc(id)}" data-ws="${esc(ws)}"`)}${!terminal.includes(t.status) ? button("중단 기록 확인 · 회수", "reconcile", "", `data-id="${esc(id)}" data-ws="${esc(ws)}"`) : ""}${!terminal.includes(t.status) ? button("작업 취소 요청", "cancel", "danger", `data-id="${esc(id)}" data-ws="${esc(ws)}" ${t.cancel_requested ? "disabled" : ""}`) : ""}${button("닫기", "close")}</footer>`,
  );
}
function executionDetail(operationId, workspaceId) {
  const x = (data.direct_executions || []).find((item) => item.operation_id === operationId && item.workspace_id === workspaceId);
  if (!x) throw Error("직접 operation 영수증을 찾을 수 없습니다.");
  const live = !terminal.includes(x.status);
  modal(
    "직접 operation 영수증",
    `<p class="mono">${esc(workspaceId)} / ${esc(operationId)}</p>${status(x.status)}<div class="list-item"><strong>독립 worker provenance</strong><span class="mono">worker_model: ${esc(x.worker_model ?? "null")} · codex_thread_id: ${esc(x.codex_thread_id ?? "null")}</span></div><p class="muted">직접 실행에는 별도 worker model·Codex thread가 없습니다. 출력과 diff는 이 operation의 실제 controller 결과만 표시됩니다.</p><div id="execution-inspect" class="preview"></div><pre class="detail">${esc(pretty(x))}</pre><footer>${button("출력 · 상태 갱신", "execution-get", "", `data-id="${esc(operationId)}" data-ws="${esc(workspaceId)}"`)}${button("workspace diff", "execution-diff", "", `data-id="${esc(operationId)}" data-ws="${esc(workspaceId)}"`)}${live ? button("operation 취소", "execution-cancel", "danger", `data-id="${esc(operationId)}" data-ws="${esc(workspaceId)}"`) : ""}${button("영수증 다운로드", "execution-download", "", `data-id="${esc(operationId)}" data-ws="${esc(workspaceId)}"`)}${button("닫기", "close")}</footer>`,
  );
}
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-action]");
  if (!b || busy) return;
  const a = b.dataset.action,
    id = b.dataset.id,
    ws = b.dataset.ws;
  try {
    if (a === "nav") {
      tab = b.dataset.tab;
      if (tab === "history") history = await api("/api/history");
      render();
    } else if (a === "close") $("#dialog").close();
    else if (a === "refresh") await refresh();
    else if (a === "reconnect") await connect();
    else if (a === "workspace-add") await workspaceEdit();
    else if (a === "workspace-edit") await workspaceEdit(id);
    else if (a === "workspace-candidate-use") {
      const selected = $("#workspace-candidate")?.selectedOptions[0];
      if (!selected?.value) throw Error("최근 워크스페이스를 선택하세요.");
      useWorkspacePath(selected.value, selected.dataset.label);
    } else if (a === "workspace-browse") await workspaceBrowse(null, id);
    else if (a === "workspace-browse-dir") await workspaceBrowse(b.dataset.path, id);
    else if (a === "workspace-browse-select") {
      useWorkspacePath(b.dataset.path);
      $("#workspace-browser").innerHTML = "";
    }
    else if (a === "profile-add") profileEdit();
    else if (a === "profile-edit") profileEdit(id);
    else if (a === "policy-edit") policyEdit(id);
    else if (a === "workspace-delete") {
      if (data.tasks.some((t) => t.workspace_id === id) || (data.direct_executions || []).some((x) => x.workspace_id === id))
        throw Error("위임 또는 직접 실행 이력이 있습니다. 삭제 대신 비활성화하세요.");
      const next = structuredClone(data.bundle);
      next.config.workspaces = next.config.workspaces.filter(
        (w) => w.id !== id,
      );
      delete next.policies[id];
      if (next.config.default_workspace_id === id)
        delete next.config.default_workspace_id;
      await applyBundle(next, "워크스페이스를 삭제하고 즉시 적용했습니다.");
    } else if (a === "profile-delete") {
      const next = structuredClone(data.bundle);
      if (
        next.config.default_worker_profile === id ||
        next.config.workspaces.some((w) => w.default_worker_profile === id)
      )
        throw Error("기본 프로필로 사용 중입니다. 참조를 먼저 변경하세요.");
      next.config.worker_profiles = next.config.worker_profiles.filter(
        (p) => p.id !== id,
      );
      await applyBundle(next, "워커 프로필을 삭제하고 즉시 적용했습니다.");
    } else if (a === "policy-preview") {
      busy = true;
      b.disabled = true;
      const v = await api("/api/policy/preview", {
        bundle: data.bundle,
        workspace_id: id,
      });
      modal(
        "실제 읽기 범위 미리보기",
        `<p>첫 50개 항목. 제한되거나 읽을 수 없는 파일도 결과에 표시됩니다.</p><pre class="detail">${esc(pretty(v))}</pre>`,
      );
    } else if (a === "task-detail") taskDetail(id, ws);
    else if (a === "execution-detail") executionDetail(id, ws);
    else if (a === "download") {
      const t = data.tasks.find(
        (t) => t.orchestrator_task_id === id && t.workspace_id === ws,
      );
      const url = URL.createObjectURL(
        new Blob([pretty(t)], { type: "application/json" }),
      );
      const link = document.createElement("a");
      link.href = url;
      link.download = id + ".json";
      link.click();
      URL.revokeObjectURL(url);
    } else if (a === "execution-get") {
      const v = await api("/api/direct/get", { workspace_id: ws, operation_id: id, output_offset: 0, output_limit: 16000 });
      $("#execution-inspect").innerHTML = `<h3>stdout</h3><pre class="detail">${esc(decodeBase64(v.stdout_base64))}</pre><h3>stderr</h3><pre class="detail">${esc(decodeBase64(v.stderr_base64))}</pre><details><summary>출력 cursor · 영수증 metadata</summary><pre class="detail">${esc(pretty({ ...v, stdout_base64: undefined, stderr_base64: undefined }))}</pre></details>`;
    } else if (a === "execution-diff") {
      const v = await api("/api/direct/diff", { workspace_id: ws, operation_id: id });
      $("#execution-inspect").innerHTML = `<pre class="detail diff">${esc(pretty(v))}</pre>`;
    } else if (a === "cancel") {
      await api("/api/tasks/cancel", { workspace_id: ws, task_id: id });
      $("#dialog").close();
      await refresh();
      toast("취소를 요청했습니다. 완료 후 변경 범위를 확인하세요.");
    } else if (a === "execution-cancel") {
      await api("/api/direct/cancel", { workspace_id: ws, operation_id: id });
      $("#dialog").close(); await refresh(); toast("직접 operation 취소를 요청했습니다. 영수증과 diff를 확인하세요.");
    } else if (a === "execution-download") {
      const x = (data.direct_executions || []).find((item) => item.operation_id === id && item.workspace_id === ws);
      const url = URL.createObjectURL(new Blob([pretty(x)], { type: "application/json" }));
      const link = document.createElement("a"); link.href = url; link.download = id + ".json"; link.click(); URL.revokeObjectURL(url);
    } else if (a === "pause") {
      await api("/api/admission", { paused: !data.paused });
      await refresh();
    } else if (a === "probe") {
      busy = true;
      b.disabled = true;
      b.textContent = "실제 MCP 연결 중…";
      const v = await api("/api/runtime/probe", {});
      $("#probe-output").innerHTML =
        `<div class="notice">진단 통과 · ${v.tools.length}개 도구 확인</div><pre class="detail">${esc(pretty(v))}</pre>`;
    } else if (a === "reconcile") {
      await api("/api/tasks/reconcile", { workspace_id: ws, task_id: id });
      $("#dialog").close();
      await refresh();
      toast("중단된 작업 기록을 회수했습니다. 변경 내용을 확인하세요.");
    } else if (a === "worker-probe") {
      modal(
        "독립 워커 실행 검증",
        `<form><p>선택한 프로필로 독립 Codex 워커를 실제 실행합니다. 파일 쓰기와 프로젝트 읽기를 요청하지 않는 고정된 진단 작업입니다.</p><label>워크스페이스<select name="workspace">${data.bundle.config.workspaces
          .filter((w) => w.enabled)
          .map((w) => `<option value="${esc(w.id)}">${esc(w.label)}</option>`)
          .join(
            "",
          )}</select></label><label>워커 프로필<select name="profile">${data.bundle.config.worker_profiles.map((p) => `<option value="${esc(p.id)}">${esc(p.label || p.id)} · ${esc(p.model)} / ${esc(p.reasoning_effort)}</option>`).join("")}</select></label><p class="muted">실제 모델 사용량이 발생합니다. 진행과 결과는 작업 화면에서 확인할 수 있습니다. 저장된 설정을 사용합니다.</p>${footer("워커 검증 실행")}</form>`,
        async (f) => {
          await api("/api/runtime/worker-probe", {
            workspace_id: f.get("workspace"),
            worker_profile_id: f.get("profile"),
          });
          $("#dialog").close();
          tab = "tasks";
          await refresh();
          toast("독립 워커 검증이 시작되었습니다.");
        },
      );
    } else if (a === "restart") {
      modal(
        "전용 터널 재시작",
        `<p>현재 MCP 연결이 잠시 끊깁니다. 실행 중인 작업이 있으면 재시작하지 않습니다.</p><footer>${button("닫기", "close")}${button("전용 터널 재시작 실행", "restart-confirm", "danger")}</footer>`,
      );
    } else if (a === "restart-confirm") {
      busy = true;
      b.disabled = true;
      b.textContent = "재시작 · 준비 상태 확인 중…";
      await api("/api/runtime/restart", {});
      $("#dialog").close();
      await refresh();
      toast("전용 터널 재시작 후 ready를 확인했습니다.");
    } else if (a === "rollback") {
      const version = await api("/api/revisions/" + id);
      modal(
        "이전 설정 복구",
        `<p class="mono">${esc(id)}</p><p>워크스페이스·프로필·읽기 정책을 아래 설정으로 복구합니다. 작업 이력과 워커 산출물은 보존됩니다.</p><pre class="detail">${esc(pretty(version.bundle))}</pre><footer>${button("닫기", "close")}${button("이 버전으로 복구", "rollback-confirm", "primary", `data-id="${id}"`)}</footer>`,
      );
    } else if (a === "rollback-confirm") {
      busy = true;
      await api("/api/config/rollback", {
        revision: id,
        expected_revision: data.revision,
      });
      $("#dialog").close();
      await refresh(true);
      toast("이전 설정을 복구했습니다.");
    }
  } catch (err) {
    if ($("#dialog").open) {
      $("#form-error").className = "error";
      $("#form-error").textContent = err.message;
    } else toast(err.message);
  } finally {
    busy = false;
    if (b.isConnected) {
      b.disabled = false;
      if (a === "probe") b.textContent = "MCP 연결 진단";
    }
  }
});
document.addEventListener("input", (e) => {
  if (e.target.id === "task-search") {
    taskFilter = e.target.value;
    $("#task-table").innerHTML = filteredTasks();
  }
});
document.addEventListener("change", (e) => {
  if (e.target.id === "task-status") {
    taskStatus = e.target.value;
    $("#task-table").innerHTML = filteredTasks();
  }
});
connect();
setInterval(async () => {
  if (
    data &&
    !busy &&
    !$("#dialog").open &&
    ["overview", "tasks", "executions", "connection"].includes(tab) &&
    !document.hidden &&
    !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)
  ) {
    try {
      await refresh();
    } catch {}
  }
}, 10000);
