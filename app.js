let hosts = [];

let runs = [];

let collectionProfiles = [];

let credentials = [];

let grafanaDashboards = [];

let grafanaIntegration = { state: "loading", message: "Checking Grafana", url: null, version: null };

let managers = [];

let internalAuditSettings = { enabled: false, loaded: false };

const statusName = { healthy: "Healthy", review: "Needs review", unreachable: "Unreachable" };
const lifecycleName = { active: "Active", disabled: "Disabled", decommissioned: "Decommissioned" };
const managerStateName = { ready: "Ready", connected: "Connected", testing: "Testing", syncing: "Syncing" };
const collectionProfileStateName = { enabled: "Enabled", paused: "Paused" };
const credentialStateName = { active: "Active", disabled: "Disabled" };
const defaultManualHostEnvironments = ["Production", "Staging", "Operations"];
let activeFilter = "all";
let activeSourceFilter = "all";
let activeCollectionTab = "runs";
let toastTimer;
let managerTestTimer;
let manualHostTestTimer;
let playbookSyntaxTimer;
let activePlaybookEditorTab = "edit";
let credentialDialogMode = "service";

const playbookRepository = "sentinel/sentinel-playbooks";
const playbookGitModels = {};
const summaryState = { status: "loading", data: null, message: "Loading operational summary" };

function sourceMetadata(profile) {
  const source = profile?.source;
  if (source && typeof source === "object") return source;
  return { repository: "", path: profile?.playbook || "", commitSha: "", state: "source-unavailable" };
}

function sourceState(source) {
  return String(source?.state || source?.status || "source-unavailable").toLowerCase();
}

function sourceBranch(source) {
  return source?.branch || source?.migration?.branch || source?.pullRequest?.branch || "Unresolved";
}

function sourcePullRequest(source) {
  const pullRequest = source?.pullRequest || source?.pr || source?.draft?.pullRequest || source?.draft?.pullRequestNumber || source?.migration?.pullRequest || source?.migration?.pr || source?.migration?.pullRequestNumber;
  if (typeof pullRequest === "string" || typeof pullRequest === "number") return `PR #${String(pullRequest).replace(/^PR\s*#/i, "")}`;
  if (pullRequest && typeof pullRequest === "object") return pullRequest.label || pullRequest.number || pullRequest.id ? `PR #${pullRequest.number || pullRequest.id || pullRequest.label}` : "";
  return "";
}

function sourceReview(source) {
  const review = source?.review || source?.reviewStatus || source?.draft?.state || source?.migration?.review || source?.migration?.reviewStatus || source?.migration?.state;
  if (typeof review === "string") return review;
  if (review && typeof review === "object") return review.state || review.status || review.label || "Review required";
  const state = sourceState(source);
  if (state === "pinned" || state === "merged") return "Review status not provided";
  if (state.includes("pending") || state === "draft") return "Review required";
  return "Source state unavailable";
}

function sourceIsRunnable(source) {
  const state = sourceState(source);
  const commitSha = String(source?.commitSha || "");
  return source?.runnable === true || (source?.runnable !== false && state === "pinned" && /^[a-f0-9]{40}$/i.test(commitSha));
}

function sourceStateLabel(source) {
  const state = sourceState(source);
  if (state === "migration-pending-review") return "Migration review required";
  if (state.includes("pending") || state === "draft") return "Review required";
  if (state === "pinned") return "Pinned";
  if (state === "merged") return "Merged";
  if (state === "unresolved" || state === "source-unavailable") return "Source unavailable";
  return state.replace(/[-_]/g, " ");
}

function sourceStateClass(source) {
  const state = sourceState(source);
  if (state.includes("pending") || state === "draft") return "draft";
  if (state === "pinned") return "pinned";
  if (state === "merged") return "merged";
  return "unavailable";
}

function normalizePlaybookVersion(raw, fallbackSource = {}) {
  const source = raw?.source && typeof raw.source === "object" ? raw.source : { ...fallbackSource, ...(raw || {}) };
  const sha = source.commitSha || raw?.commitSha || raw?.sha || raw?.commit || "";
  const state = sourceState(source);
  return {
    sha: String(sha || ""),
    branch: raw?.branch || raw?.ref || (raw?.commitSha ? "main" : sourceBranch(source)),
    kind: state,
    review: sourceReview(source),
    pullRequest: sourcePullRequest(source),
    author: raw?.author || raw?.committer || "Not reported",
    timestamp: raw?.timestamp || raw?.updatedAt || raw?.createdAt || "Not reported",
    message: raw?.message || raw?.title || raw?.description || "Revision metadata",
    runnable: sourceIsRunnable(source),
    content: typeof raw?.content === "string" ? raw.content : typeof raw?.sourceText === "string" ? raw.sourceText : "",
    diff: typeof raw?.diff === "string" ? raw.diff : "",
    state
  };
}

function buildPlaybookGitModel(profile) {
  const source = sourceMetadata(profile);
  const current = normalizePlaybookVersion({ source, message: "Selected profile source" }, source);
  return {
    repository: source.repository || "Repository unavailable",
    path: source.path || profile.playbook || "Path unavailable",
    selectedSha: current.sha,
    detailMode: "summary",
    versions: [current],
    api: { playbook: false, history: false, draft: false, review: false, syntax: false },
    loading: false,
    historyMessage: "Version history has not been loaded."
  };
}

function getPlaybookGitModel(profile) {
  if (!playbookGitModels[profile.id]) playbookGitModels[profile.id] = buildPlaybookGitModel(profile);
  return playbookGitModels[profile.id];
}

function selectedPlaybookVersion(model) {
  return model.versions.find((version) => version.sha && version.sha === model.selectedSha) || model.versions[0];
}

function shortSha(version) {
  return version?.sha ? version.sha.slice(0, 7) : "Unavailable";
}

function revisionLabel(version) {
  return `${version?.branch || "Unresolved"} · ${shortSha(version)}`;
}

function versionStateLabel(version) {
  return version?.pullRequest || sourceStateLabel(version);
}

function versionStateClass(version) {
  return sourceStateClass(version);
}

function isProfileRunnable(profile) {
  return sourceIsRunnable(sourceMetadata(profile));
}

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.detail || "The request could not be completed.");
    error.status = response.status;
    error.body = body;
    throw error;
  }
  return body;
}

async function optionalApi(path, options = {}) {
  try {
    return { available: true, body: await api(path, options) };
  } catch (error) {
    if ([404, 405, 501].includes(error.status)) return { available: false, error };
    return { available: false, error, unavailable: true };
  }
}

function applyBackendState(data) {
  hosts = data.hosts || [];
  runs = data.runs || [];
  credentials = data.credentials || [];
  managers = data.managers || [];
  collectionProfiles = data.collectionProfiles || [];
  grafanaDashboards = data.grafanaDashboards || [];
  grafanaIntegration = data.grafana || { state: "unavailable", message: "Grafana is unavailable.", url: null, version: null };
  Object.keys(playbookGitModels).forEach((key) => delete playbookGitModels[key]);
}

function renderAll() {
  renderSummary();
  renderAttention();
  renderHosts();
  renderRuns();
  renderCredentials();
  renderCollectionProfiles();
  renderPlaybookRepository();
  renderManagers();
  renderGrafanaDashboards();
  renderGrafanaAdministratorSettings();
  renderInternalAuditSettings();
}

async function loadBackendState() {
  const [backendState, auditSettings] = await Promise.all([
    api("/bootstrap"),
    api("/settings/internal-audit")
  ]);
  applyBackendState(backendState);
  internalAuditSettings = { enabled: auditSettings.enabled === true, loaded: true };
  renderAll();
}

function renderInternalAuditSettings() {
  const toggle = document.querySelector("#internal-audit-toggle");
  const exportButton = document.querySelector("#export-internal-audit-button");
  const status = document.querySelector("#internal-audit-status");
  if (!toggle || !exportButton || !status) return;
  toggle.checked = internalAuditSettings.enabled;
  toggle.disabled = !internalAuditSettings.loaded;
  exportButton.disabled = !internalAuditSettings.loaded;
  status.textContent = internalAuditSettings.loaded
    ? (internalAuditSettings.enabled
      ? "Capturing redacted API and scheduler errors for local NDJSON export."
      : "Disabled. No diagnostic error events are being retained.")
    : "Loading diagnostic setting";
}

function exportInternalAuditEvents() {
  if (!internalAuditSettings.loaded) return;
  const link = document.createElement("a");
  link.href = "/api/settings/internal-audit/export";
  link.download = "";
  document.body.appendChild(link);
  link.click();
  link.remove();
  showToast("Diagnostic export started.");
}

async function updateInternalAuditSettings(event) {
  const previous = internalAuditSettings.enabled;
  const enabled = event.target.checked;
  event.target.disabled = true;
  try {
    const settings = await api("/settings/internal-audit", {
      method: "PUT",
      body: JSON.stringify({ enabled })
    });
    internalAuditSettings = { enabled: settings.enabled === true, loaded: true };
    renderInternalAuditSettings();
    showToast(internalAuditSettings.enabled ? "Internal audit capture enabled." : "Internal audit capture disabled.");
  } catch (error) {
    internalAuditSettings = { enabled: previous, loaded: true };
    renderInternalAuditSettings();
    showRequestError(error);
  }
}

function renderGrafanaAdministratorSettings() {
  const button = document.querySelector("#reset-grafana-password-button");
  const username = document.querySelector("#grafana-admin-username");
  const status = document.querySelector("#grafana-admin-status");
  if (!button || !username || !status) return;
  const ready = grafanaIntegration.state === "ready";
  username.textContent = "Grafana administrator";
  status.textContent = ready
    ? `Connected to Grafana ${grafanaIntegration.version || ""}`.trim()
    : "Grafana must be connected before its administrator password can be reset.";
  button.disabled = !ready;
}

function setGrafanaPasswordResult(type = "", message = "") {
  const result = document.querySelector("#grafana-password-result");
  result.className = "connection-result";
  if (!message) {
    result.textContent = "";
    return;
  }
  result.classList.add(type);
  const stateIcon = type === "success" ? "check-circle" : type === "error" ? "alert" : "activity";
  result.innerHTML = `${icon(stateIcon)}<span>${escapeHtml(message)}</span>`;
}

function resetGrafanaPasswordForm() {
  document.querySelector("#grafana-password-form").reset();
  setGrafanaPasswordResult();
}

function openGrafanaPasswordDialog() {
  if (grafanaIntegration.state !== "ready") return;
  resetGrafanaPasswordForm();
  document.querySelector("#grafana-password-dialog").showModal();
  document.querySelector("#grafana-password-input").focus();
}

function closeGrafanaPasswordDialog() {
  document.querySelector("#grafana-password-dialog").close();
  resetGrafanaPasswordForm();
}

async function resetGrafanaPassword(event) {
  event.preventDefault();
  const form = document.querySelector("#grafana-password-form");
  if (!form.reportValidity()) return;
  const password = document.querySelector("#grafana-password-input").value;
  const confirmation = document.querySelector("#grafana-password-confirmation-input").value;
  if (password !== confirmation) {
    setGrafanaPasswordResult("error", "Passwords do not match.");
    return;
  }
  const submit = form.querySelector('button[type="submit"]');
  submit.disabled = true;
  submit.innerHTML = `${icon("activity")}<span>Resetting</span>`;
  setGrafanaPasswordResult("testing", "Updating Grafana administrator password...");
  try {
    const result = await api("/settings/grafana/admin-password/reset", {
      method: "POST",
      body: JSON.stringify({ newPassword: password, confirmation })
    });
    closeGrafanaPasswordDialog();
    showToast(`Grafana password updated for ${result.username}.`);
  } catch (error) {
    setGrafanaPasswordResult("error", error.message);
  } finally {
    submit.disabled = false;
    submit.innerHTML = `${icon("key")}<span>Reset password</span>`;
  }
}

function summaryUnavailableMessage() {
  return summaryState.status === "loading" ? "Loading operational summary" : summaryState.message || "Operational summary is unavailable";
}

function summaryValue(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function summaryInventory(summary) {
  const inventory = summary?.inventory || {};
  const counts = inventory.counts || inventory;
  const health = inventory.health || counts.health || {};
  return {
    total: summaryValue(counts.active ?? counts.total ?? counts.hosts ?? counts.managedHosts ?? summary?.hostCount),
    healthy: summaryValue(health.healthy ?? counts.healthy),
    review: summaryValue(health.review ?? health.needsReview ?? counts.review ?? counts.needsReview),
    unreachable: summaryValue(health.unreachable ?? health.offline ?? counts.unreachable ?? counts.offline)
  };
}

function summaryCollection(summary) {
  const collection = summary?.collection || summary?.collections || {};
  const outcomes = collection.outcomes || collection;
  const latest = collection.latest || {};
  const freshness = summary?.freshness || {};
  return {
    success: summaryValue(outcomes.success ?? outcomes.successRate ?? collection.success ?? collection.successRate ?? collection.healthyPercent),
    unreachable: summaryValue(outcomes.unreachable ?? collection.unreachable),
    lastCollection: latest.completedAt || collection.lastCollection || summary?.lastCollection || freshness.latestCollectedAt || "",
    freshness: freshness.state || collection.freshness || summary?.mode?.kind || summary?.mode || ""
  };
}

function summaryAlerts(summary) {
  const alerts = summary?.alerts;
  if (Array.isArray(alerts)) return { count: alerts.length, items: alerts };
  const items = Array.isArray(alerts?.items) ? alerts.items : Array.isArray(summary?.alertItems) ? summary.alertItems : [];
  return { count: summaryValue(alerts?.openCount ?? alerts?.open ?? alerts?.count ?? summary?.alertCount), items };
}

function summaryCapacity(summary) {
  const capacity = summary?.capacity || {};
  const disk = capacity.disk || {};
  return { percent: summaryValue(disk.utilizationPercent ?? capacity.percent ?? capacity.utilization ?? capacity.diskUtilization), label: capacity.label || capacity.description || (capacity.hostsWithFacts !== undefined ? `${capacity.hostsWithFacts} hosts with capacity facts` : "") };
}

function summaryActivity(summary) {
  return Array.isArray(summary?.recentActivity) ? summary.recentActivity : Array.isArray(summary?.activity) ? summary.activity : [];
}

function textOrUnavailable(value, fallback = "--") {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

function setText(selector, value) {
  const element = document.querySelector(selector);
  if (element) element.textContent = value;
}

function renderSummary() {
  const unavailable = summaryState.status !== "ready";
  const message = summaryUnavailableMessage();
  const summary = summaryState.data;
  const inventory = summaryInventory(summary);
  const collection = summaryCollection(summary);
  const alerts = summaryAlerts(summary);
  const capacity = summaryCapacity(summary);
  const hasSummary = !unavailable && summary && typeof summary === "object";
  const hasInventory = hasSummary && inventory.total !== null && inventory.total > 0;
  const hasCompletedCollection = hasSummary && Boolean(collection.lastCollection);
  const mode = typeof summary?.mode === "object" ? summary.mode.kind : summary?.mode;
  const freshnessLabel = collection.freshness ? String(collection.freshness) : "";
  const summaryQualifier = [freshnessLabel === "unavailable" ? "no collection data" : freshnessLabel, mode].filter(Boolean).join(" · ");

  setText("#metric-hosts", hasSummary ? textOrUnavailable(inventory.total) : "--");
  setText("#metric-hosts-note", hasSummary && inventory.total !== null ? hasInventory ? "Reported managed hosts" : "No hosts configured" : message);
  setText("#metric-collection-health", hasSummary && collection.success !== null ? `${collection.success}%` : "--");
  setText("#metric-collection-note", hasSummary && collection.success !== null ? `${textOrUnavailable(collection.unreachable, 0)} unreachable in latest summary${freshnessLabel ? ` · ${freshnessLabel}` : ""}` : hasSummary ? "No completed collection recorded" : message);
  setText("#metric-alerts", hasSummary && alerts.count !== null ? String(alerts.count) : "--");
  setText("#metric-alert-note", hasSummary && alerts.count !== null ? "Reported open alerts" : message);
  setText("#metric-capacity", hasSummary && capacity.percent !== null ? `${capacity.percent}%` : "--");
  setText("#metric-capacity-note", hasSummary && capacity.percent !== null ? capacity.label || "Reported capacity utilization" : hasSummary ? "No capacity facts reported" : message);

  const total = inventory.total;
  const healthy = inventory.healthy;
  setText("#health-ring-value", hasSummary && healthy !== null && total !== null ? total > 0 ? `${healthy}/${total}` : "0" : "--");
  setText("#health-ring-label", hasSummary && healthy !== null ? total > 0 ? "healthy" : "no hosts" : "summary unavailable");
  setText("#health-healthy", hasSummary ? textOrUnavailable(healthy) : "--");
  setText("#health-review", hasSummary ? textOrUnavailable(inventory.review) : "--");
  setText("#health-unreachable", hasSummary ? textOrUnavailable(inventory.unreachable) : "--");
  const ring = document.querySelector("#health-ring");
  if (ring) ring.setAttribute("aria-label", hasSummary && total !== null ? total > 0 ? `${textOrUnavailable(healthy, 0)} of ${total} hosts healthy` : "No hosts configured" : message);
  const footer = document.querySelector("#health-footer");
  const collectionStatus = hasCompletedCollection ? `${freshnessLabel === "stale" ? "Stale summary · " : ""}Last collection ${collection.lastCollection}` : hasSummary ? "No completed collection recorded" : message;
  if (footer) footer.innerHTML = `${icon("clock")} <strong>${escapeHtml(collectionStatus)}</strong>`;
  setText("#last-sync-time", hasCompletedCollection ? collection.lastCollection : hasSummary ? "No completed collection" : message);
  document.querySelector("#summary-activity-list").innerHTML = renderSummaryActivity(summaryActivity(summary), message, hasSummary);
  document.querySelector("#overview-alert-list").innerHTML = renderAlertRows(alerts.items, message, hasSummary, 3);
  document.querySelector("#alerts-full-list").innerHTML = renderAlertRows(alerts.items, message, hasSummary);
  setText("#alert-count", hasSummary && alerts.count !== null ? String(alerts.count) : "--");
  const alertCount = document.querySelector("#alert-count");
  if (alertCount) alertCount.classList.toggle("warning", Boolean(hasSummary && alerts.count));
  setText("#activity-description", hasSummary ? `Recent activity${summaryQualifier ? ` · ${summaryQualifier}` : ""}` : message);
}

function alertSeverity(alert) {
  const value = String(alert?.severity || alert?.level || alert?.status || "info").toLowerCase();
  if (value.includes("critical") || value.includes("unreachable") || value.includes("error")) return "critical";
  if (value.includes("warning") || value.includes("review")) return "warning-symbol";
  return "info";
}

function alertIcon(alert) {
  const severity = alertSeverity(alert);
  return severity === "critical" ? "alert" : severity === "warning-symbol" ? "hard-drive" : "package";
}

function alertTitle(alert) {
  if (alert?.title || alert?.name || alert?.message) return alert.title || alert.name || alert.message;
  if (alert?.hostName && alert?.category) return `${alert.hostName} · ${alert.category}`;
  return alert?.hostName || alert?.category || alert?.summary || "Unnamed alert";
}

function alertDetail(alert) {
  return alert?.detail || alert?.description || alert?.summary || "No additional detail reported";
}

function alertTime(alert) {
  return alert?.time || alert?.timestamp || alert?.observedAt || alert?.createdAt || alert?.updatedAt || "";
}

function renderAlertRows(items, unavailableMessage, hasSummary, limit) {
  const visible = (limit ? items.slice(0, limit) : items);
  if (!hasSummary) return `<div class="empty-state summary-empty-state">${escapeHtml(unavailableMessage)}</div>`;
  if (!visible.length) return '<div class="empty-state summary-empty-state">No open alerts are reported by the current summary.</div>';
  return visible.map((alert) => `<div class="alert-row"><span class="alert-symbol ${alertSeverity(alert)}">${icon(alertIcon(alert))}</span><div><strong>${escapeHtml(alertTitle(alert))}</strong><p>${escapeHtml(alertDetail(alert))}</p></div><time>${escapeHtml(alertTime(alert))}</time></div>`).join("");
}

function renderSummaryActivity(items, unavailableMessage, hasSummary) {
  if (!hasSummary) return `<div class="empty-state summary-empty-state">${escapeHtml(unavailableMessage)}</div>`;
  if (!items.length) return '<div class="empty-state summary-empty-state">No recent collection activity is reported by the current summary.</div>';
  return items.slice(0, 5).map((item) => {
    const title = item?.title || item?.name || item?.message || "Collection activity";
    const outcomes = item?.outcomes;
    const detail = item?.detail || item?.summary || (outcomes ? `${outcomes.successful || 0} successful · ${outcomes.unreachable || 0} unreachable · ${outcomes.failed || 0} failed` : item?.state || "No detail reported");
    const time = item?.time || item?.timestamp || item?.completedAt || item?.requestedAt || item?.createdAt || "";
    return `<div class="summary-activity-row"><span class="run-state">${icon("activity")}</span><div><strong>${escapeHtml(title)}</strong><p>${escapeHtml(detail)}</p></div><time>${escapeHtml(time)}</time></div>`;
  }).join("");
}

async function loadSummary() {
  const result = await optionalApi("/summary");
  if (!result.available) {
    summaryState.status = "unavailable";
    summaryState.data = null;
    summaryState.message = result.error?.status === 404 ? "Operational summary API is unavailable" : "Operational summary could not be loaded";
  } else if (!result.body || result.body.mode === "unavailable") {
    summaryState.status = "unavailable";
    summaryState.data = null;
    summaryState.message = result.body?.message || "No operational summary is available";
  } else {
    summaryState.status = "ready";
    summaryState.data = result.body;
    summaryState.message = "";
  }
  renderSummary();
  renderAttention();
}

function showRequestError(error) {
  showToast(error.message || "The request could not be completed.");
}

function icon(name) { return `<span class="icon" data-icon="${name}"></span>`; }
function escapeHtml(value) { return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]); }
function hostSourceType(host) { return host.sourceType || "olvm"; }
function hostSourceName(host) {
  if (hostSourceType(host) === "manual") return "Manual";
  const manager = managers.find((item) => item.id === host.sourceManagerId);
  if (manager) return manager.name;
  if (host.provenanceState === "legacy-unresolved") return "Provenance pending";
  if (host.sourceManagerId) return `Unavailable manager (${host.sourceManagerId})`;
  return "Manager unavailable";
}
function hostIps(host) { return host.ips?.length ? host.ips : [host.ip]; }
function renderIpCell(host) {
  const addresses = hostIps(host);
  const visibleAddresses = addresses.slice(0, 3);
  const additionalCount = addresses.length - visibleAddresses.length;
  const title = addresses.length > 1 ? `All addresses: ${addresses.join(", ")}` : "Primary management address";
  return `<div class="ip-cell" title="${escapeHtml(title)}"><span class="ip-cell-list">${visibleAddresses.map((address) => `<code>${escapeHtml(address)}</code>`).join("")}</span>${additionalCount ? `<span class="ip-count">+${additionalCount}</span>` : ""}</div>`;
}
function renderIpList(host) {
  return `<span class="ip-list">${hostIps(host).map((address, index) => `<span class="ip-entry"><code>${escapeHtml(address)}</code>${index === 0 ? '<span class="ip-primary">Primary</span>' : ""}</span>`).join("")}</span>`;
}

function renderAttention() {
  const list = document.querySelector("#attention-list");
  if (summaryState.status !== "ready") {
    list.innerHTML = `<div class="empty-state summary-empty-state">${escapeHtml(summaryUnavailableMessage())}</div>`;
    return;
  }
  const summary = summaryState.data || {};
  const items = Array.isArray(summary.attention) ? summary.attention : Array.isArray(summary.hostsNeedingReview) ? summary.hostsNeedingReview : [];
  if (!items.length) {
    list.innerHTML = '<div class="empty-state summary-empty-state">No hosts needing review are reported by the current summary.</div>';
    return;
  }
  list.innerHTML = items.slice(0, 4).map((item) => {
    const name = item.name || item.host || "Unnamed host";
    const status = String(item.status || item.state || "review").toLowerCase();
    const detail = item.detail || item.summary || [item.address || item.ip, item.role].filter(Boolean).join(" · ") || "No detail reported";
    return `<button class="host-mini-row" data-host="${escapeHtml(name)}" aria-label="View ${escapeHtml(name)} details">
      <span class="host-avatar">${icon("server")}</span>
      <span class="host-mini-copy"><strong>${escapeHtml(name)}</strong><span>${escapeHtml(detail)}</span></span>
      <span class="host-mini-status"><span class="legend-dot ${status === "unreachable" ? "offline" : "warning-dot"}"></span>${status === "unreachable" ? "Offline" : "Review"}</span>
    </button>`;
  }).join("");
}

function renderHosts() {
  const query = document.querySelector("#host-search").value.trim().toLowerCase();
  const visible = hosts.filter((host) => {
    const matchesFilter = activeFilter === "all" || host.status === activeFilter;
    const matchesSource = activeSourceFilter === "all" || hostSourceType(host) === activeSourceFilter;
    const value = `${host.name} ${hostIps(host).join(" ")} ${host.role || ""} ${host.environment || ""} ${(host.tags || []).join(" ")} ${hostSourceType(host)} ${hostSourceName(host)}`.toLowerCase();
    return matchesFilter && matchesSource && value.includes(query);
  });
  document.querySelector("#host-count").textContent = hosts.length;
  document.querySelector("#table-description").textContent = `${visible.length} ${visible.length === 1 ? "system" : "systems"} in view`;
  const emptyMessage = hosts.length
    ? "No hosts match the current search and filter."
    : "No hosts are configured. Add a manual host or synchronize a configured OLVM manager.";
  document.querySelector("#host-table-body").innerHTML = visible.map((host) => `
    <tr>
      <td><div class="host-cell"><span class="host-avatar">${icon("server")}</span><span><strong>${host.name}</strong><small>${host.role}</small></span></div></td>
      <td><div class="source-cell"><span class="source-badge ${hostSourceType(host)}">${hostSourceType(host) === "manual" ? "Manual" : "OLVM"}</span><small>${hostSourceType(host) === "manual" ? lifecycleName[host.lifecycle || "active"] : hostSourceName(host)}</small></div></td>
      <td><span class="status-badge ${host.status}">${statusName[host.status]}</span></td>
      <td><span class="tag">${host.environment}</span></td>
      <td>${host.os}</td>
      <td>${renderIpCell(host)}</td>
      <td>${host.collected}</td>
      <td><button class="row-button" data-host="${host.name}" aria-label="View ${host.name} details">${icon("arrow-right")}</button></td>
    </tr>`).join("") || `<tr><td colspan="8"><div class="empty-state">${emptyMessage}</div></td></tr>`;
  document.querySelector(".table-scroll").scrollLeft = 0;
}

function renderRuns() {
  document.querySelector("#run-list").innerHTML = runs.map((run) => `
    <div class="run-row">
      <span class="run-state ${run.type === "warn" ? "warn" : ""}">${icon(run.type === "warn" ? "alert" : "check")}</span>
      <div class="run-name"><strong>${escapeHtml(run.name || "Collection run")}</strong><span>${escapeHtml(run.date || "Time not reported")}</span></div>
      <span class="run-summary">${escapeHtml(run.summary || "No result summary reported")}</span>
      <span class="run-time">${escapeHtml(run.duration || "Duration not reported")}</span>
      <span class="status-badge healthy run-status">${escapeHtml(run.status || "Recorded")}</span>
    </div>`).join("") || '<div class="empty-state">No collection runs have been recorded.</div>';
}

function renderCollectionProfiles() {
  document.querySelector("#collection-profile-list").innerHTML = collectionProfiles.map((profile) => {
    const state = collectionProfileStateName[profile.state] || "Enabled";
    const source = sourceMetadata(profile);
    const version = selectedPlaybookVersion(getPlaybookGitModel(profile));
    const runnable = isProfileRunnable(profile);
    return `
      <article class="collection-profile-row">
        <span class="collection-profile-icon">${icon(profile.icon || "package")}</span>
        <div class="collection-profile-main">
          <div class="collection-profile-title"><strong>${escapeHtml(profile.name)}</strong><span class="profile-state ${profile.state}">${state}</span></div>
          <p>${escapeHtml(profile.description)}</p>
          <dl class="collection-profile-meta">
            <div><dt>Playbook</dt><dd><code>${escapeHtml(source.path || profile.playbook)}</code><span>${escapeHtml(revisionLabel(version))}</span></dd></div>
            <div><dt>Revision</dt><dd><span class="profile-git-state ${versionStateClass(version)}">${escapeHtml(sourceStateLabel(source))}</span><span>${escapeHtml(sourceReview(source))}</span></dd></div>
            <div><dt>Scope</dt><dd>${escapeHtml(profile.scope)}</dd></div>
            <div><dt>Schedule</dt><dd><code>${escapeHtml(profile.schedule || "Manual")}</code><span>${escapeHtml(profile.scheduleDescription || (profile.schedule ? "Schedule description unavailable." : "Manual only."))}</span></dd></div>
            <div><dt>Credential</dt><dd><code>${escapeHtml(profile.credential)}</code></dd></div>
            <div class="collection-profile-last-run"><dt>Last run</dt><dd>${escapeHtml(profile.lastRun)}<span>${escapeHtml(profile.coverage)}</span></dd></div>
          </dl>
        </div>
        <div class="collection-profile-run"><span>Last run</span><strong>${escapeHtml(profile.lastRun)}</strong><small>${escapeHtml(profile.coverage)}</small></div>
        <div class="collection-profile-actions">
          <button class="primary-button compact-button" data-collection-profile-run="${profile.id}" aria-label="Run ${escapeHtml(profile.name)}" title="${runnable ? "Run the selected immutable revision" : "A runnable reviewed source is required before collection"}" ${profile.state === "paused" || !runnable ? "disabled" : ""}>${icon("refresh")}<span class="button-label">Run</span></button>
          <button class="outline-button compact-button collection-profile-playbook-button" data-collection-profile-playbook="${profile.id}" aria-label="Open ${escapeHtml(profile.name)} playbook version control" title="Playbook versions">${icon("history")}<span class="button-label">Playbook</span></button>
          <button class="icon-button subtle collection-profile-edit-button" data-collection-profile-edit="${profile.id}" aria-label="Edit ${escapeHtml(profile.name)} profile settings" title="Profile settings">${icon("settings")}</button>
        </div>
      </article>`;
  }).join("") || '<div class="empty-state">No collection profiles are configured. Add a credential reference, then create a profile pinned to a reviewed Git revision.</div>';
}

function renderPlaybookRepository() {
  const list = document.querySelector("#playbook-repository-list");
  if (!list) return;
  const sources = collectionProfiles.map(sourceMetadata);
  const repository = sources.find((source) => source.repository)?.repository || "No profile sources";
  const pendingCount = sources.filter((source) => !sourceIsRunnable(source)).length;
  document.querySelector("#playbook-repository-name").textContent = repository;
  document.querySelector("#playbook-repository-status").textContent = !sources.length ? "Create a collection profile to attach a reviewed playbook source." : pendingCount ? `${pendingCount} profile source${pendingCount === 1 ? "" : "s"} requires review or resolution` : "Profile source state is reported by Sentinel";
  document.querySelector("#playbook-repository-protection").innerHTML = `${icon("lock")}<span>${!sources.length ? "No source" : pendingCount ? "Review required" : "Source reported"}</span>`;
  document.querySelector("#playbook-repository-note").textContent = "Source, review, and runnable status come from Sentinel. Browser edits are not saved or reviewed unless the Git workflow API confirms them.";
  list.innerHTML = collectionProfiles.map((profile) => {
    const source = sourceMetadata(profile);
    const version = selectedPlaybookVersion(getPlaybookGitModel(profile));
    return `
      <article class="playbook-repository-row">
        <span class="playbook-repository-row-icon">${icon(profile.icon || "package")}</span>
        <div class="playbook-repository-row-main">
          <div class="playbook-repository-row-title"><strong>${escapeHtml(source.path || profile.playbook)}</strong><span class="profile-git-state ${versionStateClass(version)}">${escapeHtml(sourceStateLabel(source))}</span></div>
          <p>${escapeHtml(profile.name)}</p>
          <dl class="playbook-repository-row-meta">
            <div><dt>Immutable commit</dt><dd><code>${escapeHtml(shortSha(version))}</code><span>${escapeHtml(version.branch)}</span></dd></div>
            <div><dt>Review</dt><dd>${escapeHtml(sourceReview(source))}${sourcePullRequest(source) ? `<span>${escapeHtml(sourcePullRequest(source))}</span>` : ""}</dd></div>
            <div><dt>Repository</dt><dd>${escapeHtml(source.repository || "Not reported")}</dd></div>
          </dl>
        </div>
        <span class="revision-runnable ${sourceIsRunnable(source) ? "ready" : "pending"}">${icon(sourceIsRunnable(source) ? "check-circle" : "alert")}<span>${sourceIsRunnable(source) ? "Runnable" : "Review required"}</span></span>
        <button class="icon-button subtle" type="button" data-collection-profile-playbook="${profile.id}" aria-label="Open ${escapeHtml(profile.playbook)} version history" title="Open version history">${icon("arrow-right")}</button>
      </article>`;
  }).join("") || '<div class="empty-state">No Git-owned playbooks are attached to collection profiles.</div>';
}

function renderCredentialOptions(selectedReference = "") {
  document.querySelectorAll("#collection-profile-credential-input, #manager-credential-input, #manual-host-credential-input").forEach((select) => {
    const currentReference = selectedReference || select.value;
    const options = credentials.filter((credential) => credential.state === "active" || credential.reference === currentReference);
    select.innerHTML = options.map((credential) => `<option value="${escapeHtml(credential.reference)}">${escapeHtml(credential.name)} · ${escapeHtml(credential.reference)}${credential.state === "disabled" ? " (disabled)" : ""}</option>`).join("") || '<option value="" selected disabled>No credential references configured</option>';
    if (currentReference && [...select.options].some((option) => option.value === currentReference)) select.value = currentReference;
  });
}

function isSshKeyCredential(credential) {
  return String(credential?.type || "").toLowerCase() === "ssh key";
}

function credentialUsageCount(credential) {
  const profilesUsingCredential = collectionProfiles.filter((profile) => profile.credential === credential.reference).length;
  const managersUsingCredential = managers.filter((manager) => manager.credential === credential.reference).length;
  const manualHostsUsingCredential = hosts.filter((host) => host.credential === credential.reference).length;
  return profilesUsingCredential + managersUsingCredential + manualHostsUsingCredential;
}

function credentialRow(credential) {
  const state = credentialStateName[credential.state] || "Active";
  const usageCount = credentialUsageCount(credential);
  return `
    <article class="credential-row">
      <span class="credential-icon">${icon("key")}</span>
      <div class="credential-main">
        <div class="credential-title"><strong>${escapeHtml(credential.name)}</strong><span class="credential-state ${credential.state}">${state}</span></div>
        <code>${escapeHtml(credential.reference)}</code>
        <dl class="credential-meta">
          <div><dt>Type</dt><dd>${escapeHtml(credential.type)}</dd></div>
          <div><dt>Principal</dt><dd>${escapeHtml(credential.principal)}</dd></div>
          <div><dt>Scope</dt><dd>${escapeHtml(credential.scope)}</dd></div>
          <div><dt>Used by</dt><dd>${usageCount} ${usageCount === 1 ? "reference" : "references"}</dd></div>
        </dl>
      </div>
      <div class="credential-activity"><span>Last used</span><strong>${escapeHtml(credential.lastUsed)}</strong></div>
      <button class="icon-button subtle credential-edit-button" data-credential-edit="${credential.id}" aria-label="Edit ${escapeHtml(credential.name)}" title="Edit reference">${icon("edit")}</button>
    </article>`;
}

function isManagedSshKey(credential) {
  return isSshKeyCredential(credential) && credential?.managedBy === "sentinel";
}

function sshKeyRow(credential) {
  const state = credentialStateName[credential.state] || "Active";
  const usageCount = credentialUsageCount(credential);
  const managed = isManagedSshKey(credential);
  const publicKey = String(credential.publicKey || "");
  const descriptor = managed ? "Sentinel managed" : "External reference";
  const publicKeyDisplay = publicKey
    ? `<div class="ssh-public-key"><span>Public key</span><code>${escapeHtml(publicKey)}</code><button class="icon-button subtle ssh-key-copy-button" type="button" data-ssh-public-key="${escapeHtml(publicKey)}" aria-label="Copy public key for ${escapeHtml(credential.name)}" title="Copy public key">${icon("copy")}</button></div>`
    : `<p class="ssh-public-key-unavailable">Public key is not available for this external secret reference.</p>`;
  return `
    <article class="credential-row ssh-key-row">
      <span class="credential-icon">${icon("key")}</span>
      <div class="credential-main">
        <div class="credential-title"><strong>${escapeHtml(credential.name)}</strong><span class="credential-state ${credential.state}">${state}</span></div>
        <span class="ssh-key-origin">${escapeHtml(descriptor)}</span>
        ${publicKeyDisplay}
        <dl class="credential-meta">
          <div><dt>Algorithm</dt><dd>${escapeHtml(credential.keyAlgorithm || "Not reported")}</dd></div>
          <div><dt>Fingerprint</dt><dd>${escapeHtml(credential.fingerprint || "Not reported")}</dd></div>
          <div><dt>Principal</dt><dd>${escapeHtml(credential.principal)}</dd></div>
          <div><dt>Scope</dt><dd>${escapeHtml(credential.scope)}</dd></div>
          <div><dt>Used by</dt><dd>${usageCount} ${usageCount === 1 ? "reference" : "references"}</dd></div>
        </dl>
      </div>
      <div class="credential-activity"><span>Last used</span><strong>${escapeHtml(credential.lastUsed)}</strong></div>
      <button class="icon-button subtle credential-edit-button" data-credential-edit="${credential.id}" aria-label="Edit ${escapeHtml(credential.name)}" title="Edit SSH key settings">${icon("edit")}</button>
    </article>`;
}

function renderCredentials() {
  const serviceCredentials = credentials.filter((credential) => !isSshKeyCredential(credential));
  const sshKeys = credentials.filter(isSshKeyCredential);
  document.querySelector("#credential-count").textContent = serviceCredentials.length;
  document.querySelector("#credential-list").innerHTML = serviceCredentials.map(credentialRow).join("") || '<div class="empty-state">No service credential references are configured.</div>';
  document.querySelector("#ssh-key-list").innerHTML = sshKeys.map(sshKeyRow).join("") || '<div class="empty-state">No SSH keys are configured. Generate a managed key to add one.</div>';
  renderCredentialOptions();
}

function updateActionAvailability() {
  const hasActiveHosts = hosts.some((host) => host.lifecycle === "active");
  const hasManagers = managers.length > 0;
  document.querySelectorAll("#collect-button, #collection-list-button").forEach((button) => {
    button.disabled = !hasActiveHosts;
    button.title = hasActiveHosts ? "Queue a live collection with an enabled reviewed profile" : "Add an active host before running a collection";
  });
  const syncAllButton = document.querySelector("#sync-all-button");
  syncAllButton.disabled = !hasManagers;
  syncAllButton.title = hasManagers ? "Synchronize all configured managers" : "Add an OLVM manager before synchronizing";
}

function renderGrafanaDashboards() {
  const openButton = document.querySelector("#open-grafana-button");
  const builderButton = document.querySelector("#open-grafana-builder-button");
  const copy = document.querySelector("#grafana-connection-copy");
  const dataSource = document.querySelector("#grafana-data-source");
  const access = document.querySelector("#grafana-access");
  const state = document.querySelector("#grafana-integration-state");
  const ready = grafanaIntegration.state === "ready" && Boolean(grafanaIntegration.url);
  if (openButton) {
    openButton.disabled = !ready;
    openButton.textContent = ready ? "Open Grafana" : "Grafana unavailable";
    openButton.insertAdjacentHTML("afterbegin", icon("arrow-right"));
    openButton.title = ready ? "Open the Grafana workspace" : grafanaIntegration.message;
  }
  if (builderButton) {
    builderButton.disabled = !ready;
    builderButton.textContent = ready ? "Manage in Grafana" : "Manage unavailable";
    builderButton.insertAdjacentHTML("afterbegin", icon("edit"));
    builderButton.title = ready ? "Open Grafana to create or edit dashboards" : grafanaIntegration.message;
  }
  if (copy) copy.querySelector("span:last-child").textContent = ready ? "Connected dashboard catalog" : grafanaIntegration.message;
  if (dataSource) dataSource.textContent = ready ? "PostgreSQL · sentinel_db.reporting" : "Unavailable";
  if (access) access.textContent = ready ? `Read-only reporting · Grafana ${grafanaIntegration.version || "connected"}` : "Not connected";
  if (state) {
    state.className = `integration-state ${ready ? "ready" : ""}`;
    state.innerHTML = `<span></span>${escapeHtml(ready ? "Connected" : "Unavailable")}`;
  }
  const grid = document.querySelector("#grafana-dashboard-grid");
  if (!grafanaDashboards.length) {
    grid.innerHTML = `<div class="empty-state summary-empty-state">${escapeHtml(ready ? "No dashboards have been provisioned in Grafana." : grafanaIntegration.message)}</div>`;
    return;
  }
  grid.innerHTML = grafanaDashboards.map((dashboard) => `
    <article class="grafana-dashboard-card">
      <header><span class="grafana-dashboard-icon">G</span><span class="grafana-dashboard-kind">Grafana</span><button class="icon-button subtle" data-open-grafana-dashboard="${escapeHtml(dashboard.url)}" aria-label="Open ${escapeHtml(dashboard.title)} in Grafana" title="Open dashboard in Grafana">${icon("arrow-right")}</button></header>
      <div class="grafana-dashboard-copy"><strong>${escapeHtml(dashboard.title)}</strong><p>Managed in Grafana and queried from Sentinel's reporting schema.</p></div>
      <dl class="grafana-dashboard-meta"><div><dt>Folder</dt><dd>${escapeHtml(dashboard.folder)}</dd></div><div><dt>UID</dt><dd>${escapeHtml(dashboard.uid)}</dd></div><div><dt>Tags</dt><dd>${escapeHtml((dashboard.tags || []).join(", ") || "None")}</dd></div><div><dt>Access</dt><dd>Read-only catalog</dd></div></dl>
      <footer><span>Open in Grafana</span><span class="grafana-dashboard-unavailable">${escapeHtml(grafanaIntegration.version ? `Grafana ${grafanaIntegration.version}` : "Unavailable")}</span></footer>
    </article>`).join("");
}

function openGrafana(url) {
  if (!url) {
    showToast(grafanaIntegration.message || "Grafana is unavailable.");
    return;
  }
  let destination;
  try {
    destination = new URL(url, window.location.origin);
  } catch {
    showToast("Grafana returned an invalid dashboard address.");
    return;
  }
  if (destination.origin !== window.location.origin || !destination.pathname.startsWith("/grafana/")) {
    showToast("Grafana returned an unsafe dashboard address.");
    return;
  }
  window.open(destination.href, "_blank", "noopener,noreferrer");
}

function selectCollectionTab(tab) {
  activeCollectionTab = tab;
  document.querySelectorAll("[data-collection-tab]").forEach((button) => {
    const active = button.dataset.collectionTab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  document.querySelector("#collection-runs-panel").hidden = tab !== "runs";
  document.querySelector("#collection-profiles-panel").hidden = tab !== "profiles";
  document.querySelector("#collection-playbooks-panel").hidden = tab !== "playbooks";
}

function setCollectionProfileFormResult(type, message) {
  const result = document.querySelector("#collection-profile-form-result");
  result.className = "connection-result";
  if (!message) { result.innerHTML = ""; return; }
  result.classList.add(type);
  result.innerHTML = `${icon(type === "error" ? "alert" : "check-circle")}<span>${escapeHtml(message)}</span>`;
}

function cronFieldValues(field, minimum, maximum, aliases = {}) {
  const normalized = Object.entries(aliases).reduce(
    (value, [alias, number]) => value.replace(new RegExp(`\\b${alias}\\b`, "gi"), String(number)),
    field,
  );
  if (!/^(\*|\*\/\d+|\d+(?:-\d+)?(?:\/\d+)?)(?:,(?:\*|\*\/\d+|\d+(?:-\d+)?(?:\/\d+)?))*$/.test(normalized)) return null;
  const values = new Set();
  for (const item of normalized.split(",")) {
    const [base, stepValue] = item.split("/");
    const step = stepValue ? Number(stepValue) : 1;
    if (!Number.isInteger(step) || step < 1 || step > maximum - minimum + 1) return null;
    let start = minimum;
    let end = maximum;
    if (base !== "*") {
      const range = base.split("-").map(Number);
      if (!range.every(Number.isInteger)) return null;
      [start, end] = range.length === 1 ? [range[0], range[0]] : range;
      if (start < minimum || end > maximum || start > end) return null;
    }
    for (let value = start; value <= end; value += step) values.add(value === 7 && minimum === 0 && maximum === 7 ? 0 : value);
  }
  return [...values].sort((left, right) => left - right);
}

function everyStep(values, minimum, maximum) {
  for (let step = 1; step <= maximum - minimum + 1; step += 1) {
    const expected = [];
    for (let value = minimum; value <= maximum; value += step) expected.push(value);
    if (expected.length === values.length && expected.every((value, index) => value === values[index])) return step;
  }
  return null;
}

function localCronDescription(expression) {
  const parts = expression.trim().split(/\s+/);
  if (!expression.trim()) return { valid: true, description: "Manual only." };
  if (parts.length !== 5) return { valid: false, description: "Use five cron fields: minute hour day-of-month month day-of-week." };
  const minute = cronFieldValues(parts[0], 0, 59);
  const hour = cronFieldValues(parts[1], 0, 23);
  const dayOfMonth = cronFieldValues(parts[2], 1, 31);
  const month = cronFieldValues(parts[3], 1, 12, { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 });
  const dayOfWeek = cronFieldValues(parts[4], 0, 7, { sun: 0, mon: 1, tue: 2, wed: 3, thu: 4, fri: 5, sat: 6 });
  if (![minute, hour, dayOfMonth, month, dayOfWeek].every(Boolean)) return { valid: false, description: "Enter a valid Linux cron expression." };
  const minuteStep = everyStep(minute, 0, 59);
  const hourStep = everyStep(hour, 0, 23);
  let description;
  if (minuteStep === 1 && hourStep === 1) description = "Every minute";
  else if (minute.length === 1 && hour.length === 1) description = `At ${String(hour[0]).padStart(2, "0")}:${String(minute[0]).padStart(2, "0")}`;
  else if (minute.length === 1 && hourStep === 1) description = `At minute ${String(minute[0]).padStart(2, "0")} past every hour`;
  else if (minute.length === 1 && hourStep && hourStep > 1) description = `At minute ${String(minute[0]).padStart(2, "0")} past every ${hourStep}th hour`;
  else if (minuteStep && minuteStep > 1 && hourStep === 1) description = `Every ${minuteStep} minutes`;
  else description = "Custom cron schedule";
  return { valid: true, description: `${description}.` };
}

function updateCronSchedulePreview(serverDescription = "") {
  const input = document.querySelector("#collection-profile-schedule-input");
  const preview = document.querySelector("#collection-profile-schedule-description");
  if (!input || !preview) return;
  const result = localCronDescription(input.value);
  input.setCustomValidity(result.valid ? "" : result.description);
  preview.textContent = serverDescription || result.description;
  preview.classList.toggle("invalid", !result.valid);
}

function resetCollectionProfileForm() {
  const form = document.querySelector("#collection-profile-form");
  form.reset();
  form.dataset.profileId = "";
  document.querySelector("#collection-profile-schedule-input").value = "0 * * * *";
  updateCronSchedulePreview();
  document.querySelector("#collection-profile-state-input").value = "enabled";
  renderCredentialOptions();
  setCollectionProfileFormResult();
}

function openCollectionProfileDialog(id) {
  const profile = id ? collectionProfiles.find((item) => item.id === id) : null;
  resetCollectionProfileForm();
  document.querySelector("#collection-profile-dialog-eyebrow").textContent = profile ? "Collection profile" : "New profile";
  document.querySelector("#collection-profile-dialog-title").textContent = profile ? `Edit ${profile.name}` : "New collection profile";
  if (profile) {
    document.querySelector("#collection-profile-form").dataset.profileId = profile.id;
    document.querySelector("#collection-profile-name-input").value = profile.name;
    document.querySelector("#collection-profile-playbook-input").value = profile.playbook;
    document.querySelector("#collection-profile-revision-input").value = profile.revision;
    document.querySelector("#collection-profile-schedule-input").value = profile.schedule || "";
    updateCronSchedulePreview(profile.scheduleDescription);
    document.querySelector("#collection-profile-scope-input").value = profile.scope;
    renderCredentialOptions(profile.credential);
    document.querySelector("#collection-profile-description-input").value = profile.description;
    document.querySelector("#collection-profile-state-input").value = profile.state;
  }
  document.querySelector("#edit-profile-playbook-button").hidden = !profile;
  document.querySelector("#edit-profile-playbook-button").dataset.profileId = profile?.id || "";
  document.querySelector("#collection-profile-dialog").showModal();
}

function closeCollectionProfileDialog() {
  document.querySelector("#collection-profile-dialog").close();
  resetCollectionProfileForm();
}

function collectionProfileFormValues() {
  const profileId = document.querySelector("#collection-profile-form").dataset.profileId;
  const existing = collectionProfiles.find((profile) => profile.id === profileId);
  return {
    id: profileId,
    name: document.querySelector("#collection-profile-name-input").value.trim(),
    playbook: document.querySelector("#collection-profile-playbook-input").value.trim(),
    revision: document.querySelector("#collection-profile-revision-input").value.trim(),
    schedule: document.querySelector("#collection-profile-schedule-input").value.trim() || null,
    scope: document.querySelector("#collection-profile-scope-input").value.trim(),
    credential: document.querySelector("#collection-profile-credential-input").value.trim(),
    description: document.querySelector("#collection-profile-description-input").value.trim(),
    state: document.querySelector("#collection-profile-state-input").value,
    ...(existing?.source ? { source: sourceMetadata(existing) } : {})
  };
}

async function saveCollectionProfile(event) {
  event.preventDefault();
  const form = document.querySelector("#collection-profile-form");
  if (!form.reportValidity()) return;
  const values = collectionProfileFormValues();
  try {
    await api(values.id ? `/profiles/${encodeURIComponent(values.id)}` : "/profiles", {
      method: values.id ? "PUT" : "POST",
      body: JSON.stringify(values)
    });
    await loadBackendState();
    closeCollectionProfileDialog();
    showToast(`${values.name} ${values.id ? "updated" : "added"}.`);
  } catch (error) {
    setCollectionProfileFormResult("error", error.message);
  }
}

async function runCollectionProfile(id) {
  const profile = collectionProfiles.find((item) => item.id === id);
  if (!profile || profile.state === "paused") return;
  if (!isProfileRunnable(profile)) {
    showToast(`${profile.name} requires a runnable reviewed source before collection can run.`);
    return;
  }
  try {
    const result = await api(`/profiles/${encodeURIComponent(id)}/run`, { method: "POST" });
    await loadBackendState();
    showToast(result.message);
  } catch (error) {
    showRequestError(error);
  }
}

function setPlaybookEditorResult(type, message) {
  const result = document.querySelector("#playbook-editor-result");
  result.className = "connection-result";
  if (!message) { result.innerHTML = ""; return; }
  result.classList.add(type);
  const stateIcon = type === "error" ? "alert" : type === "testing" ? "activity" : "check-circle";
  result.innerHTML = `${icon(stateIcon)}<span>${escapeHtml(message)}</span>`;
}

function activePlaybookEditorProfile() {
  const id = document.querySelector("#playbook-editor-dialog").dataset.profileId;
  return collectionProfiles.find((item) => item.id === id);
}

function renderPlaybookEditorStatus(version) {
  const status = document.querySelector("#playbook-editor-status");
  const runnable = Boolean(version?.runnable);
  status.innerHTML = `
    <span class="profile-git-state ${versionStateClass(version)}">${escapeHtml(versionStateLabel(version))}</span>
    <span>${escapeHtml(version?.review || "Review status unavailable")}</span>
    <span class="playbook-runnable-status ${runnable ? "ready" : "pending"}">${icon(runnable ? "check-circle" : "alert")}${runnable ? "Runnable source" : "Collection blocked"}</span>`;
}

function renderPlaybookVersionDetail(profile, model) {
  const version = selectedPlaybookVersion(model);
  const detail = document.querySelector("#playbook-version-detail");
  if (!version) {
    detail.innerHTML = '<div class="empty-state">No playbook revision metadata is available.</div>';
    return;
  }
  const actionLabel = versionStateLabel(version).toLowerCase().includes("draft") ? "Continue draft" : "Edit as draft";
  const reviewText = version.pullRequest
    ? `${version.pullRequest} is ${version.review}.`
    : version.runnable
      ? "Sentinel reports this immutable source as runnable."
      : "Sentinel requires migration review, source resolution, or review approval before collection can run.";
  detail.innerHTML = `
    <div class="playbook-version-detail-heading">
      <div><span class="profile-git-state ${versionStateClass(version)}">${escapeHtml(versionStateLabel(version))}</span><strong>${escapeHtml(version.message)}</strong><p>${escapeHtml(reviewText)}</p></div>
      <span class="revision-runnable ${version.runnable ? "ready" : "pending"}">${icon(version.runnable ? "check-circle" : "alert")}<span>${version.runnable ? "Runnable" : "Review required"}</span></span>
    </div>
    <dl class="playbook-version-detail-meta">
      <div><dt>Commit</dt><dd><code>${escapeHtml(shortSha(version))}</code></dd></div>
      <div><dt>Author</dt><dd>${escapeHtml(version.author)}</dd></div>
      <div><dt>Changed</dt><dd>${escapeHtml(version.timestamp)}</dd></div>
      <div><dt>Branch</dt><dd><code>${escapeHtml(version.branch)}</code></dd></div>
    </dl>
    <div class="playbook-version-detail-actions">
      <button class="outline-button compact-button" type="button" data-playbook-view-diff="${escapeHtml(version.sha)}" ${version.diff ? "" : "disabled"}>${icon("compare")}<span>View diff</span></button>
      <button class="outline-button compact-button" type="button" data-playbook-review-details="${escapeHtml(version.sha)}" ${version.pullRequest && model.api.review ? "" : "disabled"}>${icon("review")}<span>Review details</span></button>
      <button class="primary-button compact-button" type="button" data-playbook-create-draft="${escapeHtml(version.sha)}" ${model.api.draft && version.content ? "" : "disabled"}>${icon("branch")}<span>${actionLabel}</span></button>
    </div>
    ${model.detailMode === "diff" && version.diff ? `<div class="playbook-diff" aria-label="Revision comparison"><div><span>Revision diff</span><strong>${escapeHtml(shortSha(version))}</strong></div><pre><code>${escapeHtml(version.diff)}</code></pre></div>` : ""}
    ${model.detailMode === "review" && version.pullRequest ? `<div class="playbook-review-detail"><span>${icon("review")}</span><div><strong>${escapeHtml(version.pullRequest)} review</strong><p>${escapeHtml(version.review)}. This status was returned by Sentinel.</p></div></div>` : ""}`;
}

function renderPlaybookVersionList(profile, model) {
  const list = document.querySelector("#playbook-version-list");
  const selected = selectedPlaybookVersion(model);
  if (!model.api.history) {
    list.innerHTML = `<div class="empty-state">${escapeHtml(model.historyMessage || "Version history API is unavailable. Current source metadata is shown above.")}</div>`;
    renderPlaybookVersionDetail(profile, model);
    return;
  }
  list.innerHTML = model.versions.map((version) => `
    <article class="playbook-version-row ${version.sha === selected.sha ? "selected" : ""}">
      <button class="playbook-version-select" type="button" data-playbook-version-select="${escapeHtml(version.sha)}" aria-pressed="${String(version.sha === selected.sha)}">
        <span class="playbook-version-mark ${versionStateClass(version)}">${icon(sourceState(version) === "draft" ? "edit" : "check")}</span>
        <span class="playbook-version-copy"><strong>${escapeHtml(version.message)}</strong><span>${escapeHtml(shortSha(version))} · ${escapeHtml(version.branch)} · ${escapeHtml(version.author)} · ${escapeHtml(version.timestamp)}</span></span>
        <span class="profile-git-state ${versionStateClass(version)}">${escapeHtml(versionStateLabel(version))}</span>
      </button>
      <span class="playbook-version-review">${escapeHtml(version.pullRequest || version.review)}</span>
    </article>`).join("");
  renderPlaybookVersionDetail(profile, model);
}

function selectPlaybookEditorTab(tab) {
  activePlaybookEditorTab = tab;
  document.querySelectorAll("[data-playbook-tab]").forEach((button) => {
    const active = button.dataset.playbookTab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  document.querySelector("#playbook-edit-panel").hidden = tab !== "edit";
  document.querySelector("#playbook-versions-panel").hidden = tab !== "versions";
  const syntaxButton = document.querySelector("#check-playbook-syntax-button");
  const saveButton = document.querySelector("#save-playbook-button");
  syntaxButton.hidden = tab !== "edit";
  saveButton.hidden = tab !== "edit";
}

function sourceFromWorkflowResponse(body, fallback) {
  const candidate = body?.source || body?.profile?.source || body?.revision?.source || body?.draft?.source || {};
  const source = { ...fallback, ...(candidate && typeof candidate === "object" ? candidate : {}) };
  const review = body?.review;
  if (review && typeof review === "object") {
    source.branch = review.branch || source.branch;
    source.pullRequest = review.pullRequest || source.pullRequest;
    source.review = review.pullRequest?.state || review.state || source.review;
  }
  return source;
}

function historyFromWorkflowResponse(body) {
  if (Array.isArray(body)) return body;
  if (Array.isArray(body?.history)) return body.history;
  if (Array.isArray(body?.revisions)) return body.revisions;
  if (Array.isArray(body?.versions)) return body.versions;
  if (Array.isArray(body?.items)) return body.items;
  return [];
}

function updateModelSource(profile, model, response, { replaceVersions = false } = {}) {
  const source = sourceFromWorkflowResponse(response, sourceMetadata(profile));
  profile.source = source;
  profile.playbook = source.path || profile.playbook;
  profile.revision = source.commitSha || profile.revision;
  model.repository = source.repository || "Repository unavailable";
  model.path = source.path || profile.playbook || "Path unavailable";
  const current = normalizePlaybookVersion({ ...(response || {}), source }, source);
  const existingCurrent = model.versions.find((item) => item.sha && item.sha === current.sha);
  if (!current.content && existingCurrent?.content) current.content = existingCurrent.content;
  if (current.message === "Revision metadata" && existingCurrent?.message) current.message = existingCurrent.message;
  if (current.timestamp === "Not reported" && existingCurrent?.timestamp) current.timestamp = existingCurrent.timestamp;
  if (replaceVersions) {
    const existingVersions = new Map(model.versions.map((item) => [item.sha, item]));
    const history = historyFromWorkflowResponse(response)
      .map((item) => {
        const version = normalizePlaybookVersion(item, source);
        return { ...existingVersions.get(version.sha), ...version };
      })
      .filter((item) => item.sha || item.state !== "source-unavailable");
    const currentIndex = history.findIndex((item) => item.sha && item.sha === current.sha);
    if (currentIndex >= 0) {
      // History contains revision metadata only; retain the transient bytes fetched
      // for the selected immutable revision so it remains editable as a Git draft.
      history.splice(currentIndex, 1, { ...history[currentIndex], ...current });
    } else if (current.sha) {
      history.unshift(current);
    }
    model.versions = history.length ? history : [current];
  } else {
    const existingIndex = model.versions.findIndex((item) => item.sha && item.sha === current.sha);
    if (existingIndex >= 0) model.versions.splice(existingIndex, 1, { ...model.versions[existingIndex], ...current });
    else model.versions = [current, ...model.versions.filter((item) => item.sha !== current.sha)];
  }
  if (!model.selectedSha || model.versions.some((item) => item.sha === current.sha)) model.selectedSha = current.sha || model.versions[0]?.sha || "";
}

async function loadPlaybookWorkflow(profile) {
  const model = getPlaybookGitModel(profile);
  model.loading = true;
  renderPlaybookEditor(profile);
  const prefix = `/profiles/${encodeURIComponent(profile.id)}`;
  const [playbook, history, review] = await Promise.all([
    optionalApi(`${prefix}/playbook`),
    optionalApi(`${prefix}/playbook/history`),
    optionalApi(`${prefix}/playbook/review-status`)
  ]);
  model.loading = false;
  model.api.playbook = playbook.available;
  model.api.history = history.available;
  model.api.review = review.available;
  if (playbook.available) updateModelSource(profile, model, playbook.body);
  model.api.draft = model.api.playbook && sourceIsRunnable(sourceMetadata(profile));
  if (history.available) {
    updateModelSource(profile, model, history.body, { replaceVersions: true });
    model.historyMessage = "";
  } else {
    model.historyMessage = history.error?.status === 404 ? "Version history API is unavailable. Current source metadata is shown above." : "Version history could not be loaded.";
  }
  if (review.available) updateModelSource(profile, model, review.body);
  renderPlaybookEditor(profile);
  if (!playbook.available) setPlaybookEditorResult("error", "The Git playbook workflow API is unavailable. Browser edits will not be persisted.");
}

function renderPlaybookEditor(profile, preserveSource = false) {
  const model = getPlaybookGitModel(profile);
  const version = selectedPlaybookVersion(model);
  const sourceInput = document.querySelector("#playbook-editor-input");
  document.querySelector("#playbook-editor-title").textContent = profile.name;
  document.querySelector("#playbook-editor-repository").textContent = model.repository;
  document.querySelector("#playbook-editor-path").textContent = model.path;
  document.querySelector("#playbook-editor-revision").textContent = shortSha(version);
  document.querySelector("#playbook-editor-branch").textContent = version?.branch || "Unresolved";
  if (!preserveSource) sourceInput.value = version?.content || "";
  sourceInput.readOnly = !version?.content || !version?.runnable || model.loading;
  sourceInput.placeholder = version?.content ? "" : model.loading ? "Loading Git-owned playbook source..." : "Git playbook source is unavailable from the workflow API.";
  const saveButton = document.querySelector("#save-playbook-button");
  document.querySelector("#save-playbook-label").textContent = versionStateLabel(version).toLowerCase().includes("draft") ? "Update draft" : "Create draft";
  const draftAvailable = model.api.draft && Boolean(version?.content) && !model.loading;
  saveButton.disabled = !draftAvailable;
  saveButton.title = draftAvailable
    ? "Create or update a Git draft"
    : model.loading
      ? "Loading the selected Git revision"
      : !model.api.draft
        ? "The Git draft workflow is unavailable for this source"
        : "The selected Git revision source could not be loaded";
  document.querySelector("#check-playbook-syntax-button").disabled = !version?.runnable || model.loading;
  renderPlaybookEditorStatus(version);
  renderPlaybookVersionList(profile, model);
  selectPlaybookEditorTab(activePlaybookEditorTab);
}

async function openPlaybookEditor(id) {
  const profile = collectionProfiles.find((item) => item.id === id);
  if (!profile) return;
  clearTimeout(playbookSyntaxTimer);
  document.querySelector("#playbook-editor-dialog").dataset.profileId = profile.id;
  activePlaybookEditorTab = "edit";
  renderPlaybookEditor(profile);
  setPlaybookEditorResult();
  document.querySelector("#playbook-editor-dialog").showModal();
  await loadPlaybookWorkflow(profile);
}

function closePlaybookEditor() {
  clearTimeout(playbookSyntaxTimer);
  document.querySelector("#playbook-editor-dialog").close();
  document.querySelector("#playbook-editor-dialog").dataset.profileId = "";
  setPlaybookEditorResult();
}

async function checkPlaybookSyntax() {
  const button = document.querySelector("#check-playbook-syntax-button");
  const profile = activePlaybookEditorProfile();
  if (!profile) return;
  const version = selectedPlaybookVersion(getPlaybookGitModel(profile));
  if (!version.runnable) {
    setPlaybookEditorResult("error", "This source requires migration review, source resolution, or review approval before syntax checking or collection.");
    return;
  }
  if (!version.sha) {
    setPlaybookEditorResult("error", "The selected source has no immutable commit to check.");
    return;
  }
  button.disabled = true;
  button.innerHTML = `${icon("activity")}<span>Checking</span>`;
  setPlaybookEditorResult("testing", "Requesting a server check for the selected immutable revision…");
  try {
    const result = await api(`/profiles/${encodeURIComponent(profile.id)}/syntax-check`, {
      method: "POST",
      body: JSON.stringify({ commitSha: version.sha, revision: version.sha })
    });
    const checkedSha = result.checkedCommitSha || result.commitSha || result.revision;
    const selectedRevisionChecked = result.selectedRevision === true || result.revisionChecked === true || (checkedSha && checkedSha === version.sha && result.mode !== "basic-local-preflight");
    button.disabled = false;
    button.innerHTML = `${icon("check")}<span>Check syntax</span>`;
    if (!selectedRevisionChecked) {
      setPlaybookEditorResult("error", "Selected-revision syntax checking is unavailable. Sentinel did not confirm a check of the immutable Git commit.");
      return;
    }
    if (result.valid === false) {
      setPlaybookEditorResult("error", Array.isArray(result.issues) && result.issues.length ? result.issues.join(" ") : "The selected revision did not pass syntax validation.");
      return;
    }
    setPlaybookEditorResult("success", `Sentinel completed a syntax check for ${shortSha(version)}.`);
  } catch (error) {
    button.disabled = false;
    button.innerHTML = `${icon("check")}<span>Check syntax</span>`;
    setPlaybookEditorResult("error", error.status === 404 ? "Selected-revision syntax checking is unavailable." : error.message);
  }
}

async function savePlaybook() {
  const profile = activePlaybookEditorProfile();
  if (!profile) return;
  const model = getPlaybookGitModel(profile);
  const selected = selectedPlaybookVersion(model);
  const source = document.querySelector("#playbook-editor-input").value;
  if (!model.api.draft) {
    setPlaybookEditorResult("error", "The Git draft workflow API is unavailable. These browser edits have not been saved or reviewed.");
    return;
  }
  if (!selected?.sha || !source) {
    setPlaybookEditorResult("error", "A resolved immutable source is required before creating a Git draft.");
    return;
  }
  const button = document.querySelector("#save-playbook-button");
  button.disabled = true;
  setPlaybookEditorResult("testing", "Creating or updating a Git draft through Sentinel…");
  try {
    const result = await api(`/profiles/${encodeURIComponent(profile.id)}/playbook/draft`, {
      method: "POST",
      body: JSON.stringify({ source })
    });
    updateModelSource(profile, model, result);
    const draft = normalizePlaybookVersion(result?.draft || result, sourceMetadata(profile));
    if (draft.sha || draft.content) {
      model.versions = [draft, ...model.versions.filter((item) => item.sha !== draft.sha)];
      model.selectedSha = draft.sha || model.selectedSha;
    }
    model.detailMode = "summary";
    renderPlaybookEditor(profile);
    renderCollectionProfiles();
    renderPlaybookRepository();
    setPlaybookEditorResult("success", `${draft.pullRequest || "Draft"} was saved by Sentinel and requires review before collection.`);
    showToast(`Git draft saved for ${profile.playbook}.`);
  } catch (error) {
    button.disabled = false;
    setPlaybookEditorResult("error", error.status === 404 ? "The Git draft workflow API is unavailable. These browser edits have not been saved." : error.message);
  }
}

function selectPlaybookVersion(sha) {
  const profile = activePlaybookEditorProfile();
  if (!profile) return;
  const model = getPlaybookGitModel(profile);
  if (!model.versions.some((version) => version.sha === sha)) return;
  model.selectedSha = sha;
  model.detailMode = "summary";
  renderPlaybookEditor(profile);
  setPlaybookEditorResult();
}

function showPlaybookVersionDiff(sha) {
  const profile = activePlaybookEditorProfile();
  if (!profile) return;
  const model = getPlaybookGitModel(profile);
  model.selectedSha = sha;
  model.detailMode = "diff";
  renderPlaybookEditor(profile, true);
}

function showPlaybookReviewDetails(sha) {
  const profile = activePlaybookEditorProfile();
  if (!profile) return;
  const model = getPlaybookGitModel(profile);
  model.selectedSha = sha;
  model.detailMode = "review";
  renderPlaybookEditor(profile, true);
}

function createPlaybookDraftFromVersion(sha) {
  const profile = activePlaybookEditorProfile();
  if (!profile) return;
  const model = getPlaybookGitModel(profile);
  const source = model.versions.find((version) => version.sha === sha);
  if (!source) return;
  if (sourceState(source) === "draft") {
    model.selectedSha = source.sha;
    activePlaybookEditorTab = "edit";
    renderPlaybookEditor(profile);
    setPlaybookEditorResult("testing", `${source.pullRequest || "The draft"} is managed by Sentinel. Continue editing before it is reviewed.`);
    return;
  }
  if (!model.api.draft || !source.content) {
    setPlaybookEditorResult("error", "The Git draft workflow API or this revision's source content is unavailable. No browser-only draft was created.");
    return;
  }
  model.selectedSha = source.sha;
  activePlaybookEditorTab = "edit";
  renderPlaybookEditor(profile);
  setPlaybookEditorResult("testing", `Edit ${shortSha(source)} and save it through Sentinel to create a review-required draft.`);
}

function setCredentialFormResult(type, message) {
  const result = document.querySelector("#credential-form-result");
  result.className = "connection-result";
  if (!message) { result.innerHTML = ""; return; }
  result.classList.add(type);
  result.innerHTML = `${icon(type === "error" ? "alert" : "check-circle")}<span>${escapeHtml(message)}</span>`;
}

function resetCredentialForm() {
  const form = document.querySelector("#credential-form");
  form.reset();
  form.dataset.credentialId = "";
  renderCredentialTypeOptions("Username and password", "service");
  document.querySelector("#credential-reference-field").hidden = false;
  document.querySelector("#credential-reference-input").disabled = false;
  document.querySelector("#credential-type-field").hidden = false;
  document.querySelector("#credential-type-input").disabled = false;
  document.querySelector("#credential-submit-label").textContent = "Save credential";
  document.querySelector("#credential-form .form-note").textContent = "This form records a path reference only. It never reads, displays, or stores the secret value.";
  document.querySelector("#credential-state-input").value = "active";
  setCredentialFormResult();
}

function renderCredentialTypeOptions(selectedType, mode) {
  const types = mode.startsWith("ssh-") ? ["SSH key"] : ["Username and password", "OLVM API credential", "API token"];
  const select = document.querySelector("#credential-type-input");
  select.innerHTML = types.map((type) => `<option value="${escapeHtml(type)}">${escapeHtml(type)}</option>`).join("");
  select.value = types.includes(selectedType) ? selectedType : types[0];
}

function openCredentialDialog(id, defaultType = "") {
  const credential = id ? credentials.find((item) => item.id === id) : null;
  resetCredentialForm();
  const sshKey = credential ? isSshKeyCredential(credential) : defaultType === "SSH key";
  const managedSshKey = credential && isManagedSshKey(credential);
  credentialDialogMode = sshKey ? (credential ? (managedSshKey ? "ssh-managed" : "ssh-external") : "ssh-generate") : "service";
  renderCredentialTypeOptions(credential?.type || defaultType || "Username and password", credentialDialogMode);
  const generatedMode = credentialDialogMode === "ssh-generate" || credentialDialogMode === "ssh-managed";
  document.querySelector("#credential-reference-field").hidden = generatedMode;
  document.querySelector("#credential-reference-input").disabled = generatedMode;
  document.querySelector("#credential-type-field").hidden = sshKey;
  document.querySelector("#credential-type-input").disabled = sshKey;
  document.querySelector("#credential-dialog-eyebrow").textContent = generatedMode ? "Managed SSH key" : (sshKey ? "External SSH key reference" : "Service credential reference");
  document.querySelector("#credential-dialog-title").textContent = credential ? `Edit ${credential.name}` : (sshKey ? "Generate SSH key" : "Add service reference");
  document.querySelector("#credential-submit-label").textContent = credential ? "Save changes" : (sshKey ? "Generate key" : "Save credential");
  if (generatedMode) document.querySelector("#credential-form .form-note").textContent = "Sentinel generates an Ed25519 key pair. The private key stays in its restricted runtime store and is never shown in this portal.";
  if (credentialDialogMode === "ssh-external") document.querySelector("#credential-form .form-note").textContent = "This record points to a key managed outside Sentinel. Sentinel does not read or display its private key.";
  if (credential) {
    document.querySelector("#credential-form").dataset.credentialId = credential.id;
    document.querySelector("#credential-name-input").value = credential.name;
    document.querySelector("#credential-reference-input").value = credential.reference;
    document.querySelector("#credential-type-input").value = credential.type;
    document.querySelector("#credential-principal-input").value = credential.principal;
    document.querySelector("#credential-scope-input").value = credential.scope;
    document.querySelector("#credential-state-input").value = credential.state;
  }
  document.querySelector("#credential-dialog").showModal();
}

function closeCredentialDialog() {
  document.querySelector("#credential-dialog").close();
  resetCredentialForm();
}

function credentialFormValues() {
  return {
    id: document.querySelector("#credential-form").dataset.credentialId,
    name: document.querySelector("#credential-name-input").value.trim(),
    reference: document.querySelector("#credential-reference-input").value.trim(),
    type: document.querySelector("#credential-type-input").value,
    principal: document.querySelector("#credential-principal-input").value.trim(),
    scope: document.querySelector("#credential-scope-input").value.trim(),
    state: document.querySelector("#credential-state-input").value
  };
}

async function saveCredential(event) {
  event.preventDefault();
  const form = document.querySelector("#credential-form");
  if (!form.reportValidity()) return;
  const values = credentialFormValues();
  const generatedSshKey = credentialDialogMode === "ssh-generate";
  const sshKey = credentialDialogMode.startsWith("ssh-");
  const endpoint = generatedSshKey
    ? "/settings/ssh-keys/generate"
    : sshKey
      ? `/settings/ssh-keys/${encodeURIComponent(values.id)}`
      : `/credentials${values.id ? `/${encodeURIComponent(values.id)}` : ""}`;
  try {
    await api(endpoint, {
      method: values.id ? "PUT" : "POST",
      body: JSON.stringify(values)
    });
    await loadBackendState();
    closeCredentialDialog();
    showToast(`${values.name} ${generatedSshKey ? "generated" : values.id ? "updated" : "added"}.`);
  } catch (error) {
    setCredentialFormResult("error", error.message);
  }
}

async function copySshPublicKey(publicKey) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(publicKey);
    } else {
      const source = document.createElement("textarea");
      source.value = publicKey;
      source.setAttribute("readonly", "");
      source.style.position = "fixed";
      source.style.opacity = "0";
      document.body.append(source);
      source.select();
      const copied = document.execCommand("copy");
      source.remove();
      if (!copied) throw new Error("Clipboard is unavailable.");
    }
    showToast("Public key copied.");
  } catch (error) {
    showToast("Public key could not be copied.");
  }
}

function renderManagers() {
  const count = managers.length;
  document.querySelector("#manager-count").textContent = count;
  document.querySelector("#configured-manager-count").textContent = count;
  document.querySelector("#manager-list").innerHTML = managers.map((manager) => {
    const name = escapeHtml(manager.name);
    const address = escapeHtml(manager.address);
    const username = escapeHtml(manager.username);
    const state = managerStateName[manager.state] || "Ready";
    return `
      <article class="manager-row">
        <span class="manager-source-icon">${icon("network")}</span>
        <div class="manager-identity"><strong>${name}</strong><span>${address}</span><small>${escapeHtml(manager.credential || "Secret reference not set")}</small></div>
        <div class="manager-meta"><label>Connection</label><strong>${manager.ssl ? "HTTPS" : "HTTP"}</strong><span>${username}</span></div>
        <div class="manager-collection"><label>Last inventory</label><strong>${escapeHtml(manager.lastSync)}</strong><span>${escapeHtml(manager.inventory)}</span></div>
        <span class="manager-state ${manager.state}">${state}</span>
        <div class="manager-actions">
          <button class="outline-button compact-button" data-manager-test="${manager.id}" aria-label="Test ${name} connection" title="Test connection" ${manager.state === "testing" || manager.state === "syncing" ? "disabled" : ""}>${icon("activity")}<span class="button-label">Test</span></button>
          <button class="primary-button compact-button" data-manager-sync="${manager.id}" aria-label="Sync ${name} inventory" title="Sync inventory" ${manager.state === "testing" || manager.state === "syncing" ? "disabled" : ""}>${icon("refresh")}<span class="button-label">Sync</span></button>
          <button class="icon-button subtle manager-edit-button" data-manager-edit="${manager.id}" aria-label="Edit ${name}">${icon("edit")}</button>
        </div>
      </article>`;
  }).join("") || '<div class="empty-state">No OLVM managers are configured. Add a manager after creating its secret reference.</div>';
  updateActionAvailability();
}

function openHost(name) {
  const host = hosts.find((item) => item.name === name);
  if (!host) return;
  const isManual = hostSourceType(host) === "manual";
  document.querySelector("#dialog-title").textContent = host.name;
  document.querySelector("#dialog-status").innerHTML = `<span class="status-badge ${host.status}">${statusName[host.status]}</span>`;
  const fields = [["Source", isManual ? "Manual" : `OLVM · ${hostSourceName(host)}`], ["Role", host.role], ["Environment", host.environment], ["Operating system", host.os], ["Network addresses", renderIpList(host), "detail-item-wide"], ["Uptime", host.uptime], ["Disk utilization", host.disk], ["Memory utilization", host.memory], ["Last collection", host.collected]];
  if (isManual) fields.splice(5, 0, ["Lifecycle", lifecycleName[host.lifecycle || "active"]], ["SSH connection", `${host.connectionUser || "root"} · port ${host.connectionPort || "22"}`], ["Secret reference", host.credential || "Not set"]);
  else if (host.provenanceState === "legacy-unresolved") fields.splice(1, 0, ["Provenance", host.provenanceIssue || "Manager and Engine resource identity are not recorded."]);
  else fields.splice(1, 0, ["Manager identity", host.sourceManagerId || "Unavailable"], ["Engine resource", `${host.engineResourceType || "Unknown"} · ${host.engineResourceId || "Unavailable"}`], ["Reconciliation", `${host.reconciliationState || "Unavailable"} · authoritative run ${host.lastAuthoritativeRunId || "Unavailable"}`]);
  document.querySelector("#host-details").innerHTML = fields.map(([label, value, className = ""]) => `<div class="detail-item ${className}"><label>${label}</label><strong>${value}</strong></div>`).join("");
  document.querySelector("#collect-host-button").dataset.host = host.name;
  const editButton = document.querySelector("#edit-manual-host-button");
  editButton.hidden = !isManual;
  if (isManual) {
    editButton.dataset.manualHost = host.name;
  } else {
    delete editButton.dataset.manualHost;
  }
  document.querySelector("#host-dialog").showModal();
}

function resetManualHostForm() {
  const form = document.querySelector("#manual-host-form");
  form.reset();
  form.dataset.hostName = "";
  document.querySelector("#manual-host-user-input").value = "root";
  document.querySelector("#manual-host-port-input").value = "22";
  renderManualHostEnvironmentOptions();
  document.querySelector("#manual-host-lifecycle-input").value = "active";
  renderCredentialOptions();
  setManualHostConnectionResult();
}

function renderManualHostEnvironmentOptions(selectedEnvironment = "Production") {
  const select = document.querySelector("#manual-host-environment-input");
  const environment = typeof selectedEnvironment === "string" ? selectedEnvironment.trim() : "";
  const options = environment && !defaultManualHostEnvironments.includes(environment)
    ? [environment, ...defaultManualHostEnvironments]
    : defaultManualHostEnvironments;
  select.innerHTML = options.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
  select.value = environment || defaultManualHostEnvironments[0];
}

function setManualHostConnectionResult(type, message) {
  const result = document.querySelector("#manual-host-connection-result");
  result.className = "connection-result";
  if (!message) { result.innerHTML = ""; return; }
  result.classList.add(type);
  const stateIcon = type === "success" ? "check-circle" : type === "error" ? "alert" : "activity";
  result.innerHTML = `${icon(stateIcon)}<span>${escapeHtml(message)}</span>`;
}

function openManualHostDialog(name) {
  const host = name ? hosts.find((item) => item.name === name && hostSourceType(item) === "manual") : null;
  resetManualHostForm();
  document.querySelector("#manual-host-dialog-eyebrow").textContent = host ? "Manual inventory" : "New manual host";
  document.querySelector("#manual-host-dialog-title").textContent = host ? `Edit ${host.name}` : "Add physical host";
  if (host) {
    document.querySelector("#manual-host-form").dataset.hostName = host.name;
    document.querySelector("#manual-host-name-input").value = host.name;
    document.querySelector("#manual-host-address-input").value = host.ip;
    document.querySelector("#manual-host-user-input").value = host.connectionUser || "root";
    document.querySelector("#manual-host-port-input").value = host.connectionPort || "22";
    renderCredentialOptions(host.credential);
    document.querySelector("#manual-host-role-input").value = host.role;
    renderManualHostEnvironmentOptions(host.environment);
    document.querySelector("#manual-host-lifecycle-input").value = host.lifecycle || "active";
  }
  document.querySelector("#manual-host-dialog").showModal();
}

function closeManualHostDialog() {
  clearTimeout(manualHostTestTimer);
  document.querySelector("#manual-host-dialog").close();
  resetManualHostForm();
}

function manualHostFormValues() {
  return {
    originalName: document.querySelector("#manual-host-form").dataset.hostName,
    name: document.querySelector("#manual-host-name-input").value.trim(),
    address: document.querySelector("#manual-host-address-input").value.trim(),
    user: document.querySelector("#manual-host-user-input").value.trim(),
    port: document.querySelector("#manual-host-port-input").value.trim(),
    credential: document.querySelector("#manual-host-credential-input").value,
    role: document.querySelector("#manual-host-role-input").value.trim(),
    environment: document.querySelector("#manual-host-environment-input").value,
    lifecycle: document.querySelector("#manual-host-lifecycle-input").value
  };
}

function validateManualHostForm() {
  const form = document.querySelector("#manual-host-form");
  if (!form.reportValidity()) return null;
  const values = manualHostFormValues();
  const duplicateName = hosts.find((host) => host.name.toLowerCase() === values.name.toLowerCase() && host.name !== values.originalName);
  const duplicateAddress = hosts.find((host) => hostIps(host).some((address) => address.toLowerCase() === values.address.toLowerCase()) && host.name !== values.originalName);
  if (duplicateName) { setManualHostConnectionResult("error", `Host name ${values.name} is already in inventory.`); return null; }
  if (duplicateAddress) { setManualHostConnectionResult("error", `Management address ${values.address} is already in inventory.`); return null; }
  return values;
}

async function testManualHostConnection() {
  const values = validateManualHostForm();
  if (!values) return;
  const button = document.querySelector("#test-manual-host-button");
  button.disabled = true;
  button.innerHTML = `${icon("activity")}<span>Testing</span>`;
  setManualHostConnectionResult("testing", `Testing SSH connection to ${values.address}…`);
  try {
    const result = await api("/hosts/test", { method: "POST", body: JSON.stringify(values) });
    button.disabled = false;
    button.innerHTML = `${icon("activity")}<span>Test connection</span>`;
    setManualHostConnectionResult("success", result.message);
  } catch (error) {
    button.disabled = false;
    button.innerHTML = `${icon("activity")}<span>Test connection</span>`;
    setManualHostConnectionResult("error", error.message);
  }
}

async function saveManualHost(event) {
  event.preventDefault();
  const values = validateManualHostForm();
  if (!values) return;
  try {
    await api(values.originalName ? `/hosts/${encodeURIComponent(values.originalName)}` : "/hosts", {
      method: values.originalName ? "PUT" : "POST",
      body: JSON.stringify(values)
    });
    await loadBackendState();
    closeManualHostDialog();
    if (document.querySelector("#host-dialog").open) document.querySelector("#host-dialog").close();
    showToast(`${values.name} ${values.originalName ? "updated" : "added as a manual host"}.`);
  } catch (error) {
    setManualHostConnectionResult("error", error.message);
  }
}

function resetManagerForm() {
  const form = document.querySelector("#manager-form");
  form.reset();
  form.dataset.managerId = "";
  document.querySelector("#manager-ssl-input").checked = true;
  renderCredentialOptions();
  setConnectionResult();
}

function setConnectionResult(type, message) {
  const result = document.querySelector("#connection-result");
  result.className = "connection-result";
  if (!message) { result.innerHTML = ""; return; }
  result.classList.add(type);
  const stateIcon = type === "success" ? "check-circle" : type === "error" ? "alert" : "activity";
  result.innerHTML = `${icon(stateIcon)}<span>${escapeHtml(message)}</span>`;
}

function openManagerDialog(id) {
  const manager = id ? managers.find((item) => item.id === id) : null;
  resetManagerForm();
  document.querySelector("#manager-dialog-eyebrow").textContent = manager ? "Connection settings" : "New source";
  document.querySelector("#manager-dialog-title").textContent = manager ? `Edit ${manager.name}` : "Add OLVM manager";
  if (manager) {
    document.querySelector("#manager-form").dataset.managerId = manager.id;
    document.querySelector("#manager-name-input").value = manager.name;
  document.querySelector("#manager-address-input").value = manager.address;
  document.querySelector("#manager-username-input").value = manager.username;
    renderCredentialOptions(manager.credential);
    document.querySelector("#manager-ssl-input").checked = manager.ssl;
  }
  document.querySelector("#manager-dialog").showModal();
}

function closeManagerDialog() {
  clearTimeout(managerTestTimer);
  document.querySelector("#manager-dialog").close();
  resetManagerForm();
}

function managerFormValues() {
  return {
    id: document.querySelector("#manager-form").dataset.managerId,
    name: document.querySelector("#manager-name-input").value.trim(),
    address: document.querySelector("#manager-address-input").value.trim(),
    username: document.querySelector("#manager-username-input").value.trim(),
    credential: document.querySelector("#manager-credential-input").value,
    ssl: document.querySelector("#manager-ssl-input").checked
  };
}

function validateManagerForm() {
  const form = document.querySelector("#manager-form");
  if (!form.reportValidity()) return null;
  return managerFormValues();
}

async function testFormConnection() {
  const values = validateManagerForm();
  if (!values) return;
  const button = document.querySelector("#test-manager-button");
  button.disabled = true;
  button.innerHTML = `${icon("activity")}<span>Testing</span>`;
  setConnectionResult("testing", `Testing ${values.ssl ? "HTTPS" : "HTTP"} connection to ${values.address}…`);
  try {
    const result = await api("/managers/test", { method: "POST", body: JSON.stringify(values) });
    button.disabled = false;
    button.innerHTML = `${icon("activity")}<span>Test connection</span>`;
    setConnectionResult("success", result.message);
  } catch (error) {
    button.disabled = false;
    button.innerHTML = `${icon("activity")}<span>Test connection</span>`;
    setConnectionResult("error", error.message);
  }
}

async function saveManager(event) {
  event.preventDefault();
  const values = validateManagerForm();
  if (!values) return;
  try {
    await api(values.id ? `/managers/${encodeURIComponent(values.id)}` : "/managers", {
      method: values.id ? "PUT" : "POST",
      body: JSON.stringify(values)
    });
    await loadBackendState();
    closeManagerDialog();
    showToast(`${values.name} ${values.id ? "updated" : "added"}.`);
  } catch (error) {
    setConnectionResult("error", error.message);
  }
}

async function runManagerAction(id, action) {
  const manager = managers.find((item) => item.id === id);
  if (!manager || manager.state === "testing" || manager.state === "syncing") return;
  try {
    const result = await api(`/managers/${encodeURIComponent(id)}/${action}`, { method: "POST" });
    await loadBackendState();
    showToast(result.message);
  } catch (error) {
    showRequestError(error);
  }
}

async function syncAllManagers() {
  const button = document.querySelector("#sync-all-button");
  if (button.disabled) return;
  button.disabled = true;
  button.innerHTML = `${icon("refresh")}<span>Syncing all</span>`;
  try {
    const result = await api("/managers/sync-all", { method: "POST" });
    await loadBackendState();
    showToast(result.message);
  } catch (error) {
    showRequestError(error);
  } finally {
    button.disabled = false;
    button.innerHTML = `${icon("refresh")}<span>Sync all</span>`;
  }
}

async function runEntireCollection() {
  try {
    const result = await api("/collections/run", { method: "POST" });
    await loadBackendState();
    showToast(result.message);
  } catch (error) {
    showRequestError(error);
  }
}

async function collectHost(hostName) {
  try {
    const result = await api(`/hosts/${encodeURIComponent(hostName)}/collect`, { method: "POST" });
    await loadBackendState();
    showToast(result.message);
  } catch (error) {
    showRequestError(error);
  }
}

function showToast(message) {
  const toast = document.querySelector("#toast");
  const toastText = document.querySelector("#toast-text");
  toast.hidden = false;
  toast.setAttribute("aria-hidden", "false");
  toastText.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.classList.remove("show");
    toast.setAttribute("aria-hidden", "true");
    setTimeout(() => {
      if (!toast.classList.contains("show")) {
        toast.hidden = true;
        toastText.textContent = "";
      }
    }, 200);
  }, 3600);
}

function goTo(view) {
  const titles = { overview: "Fleet overview", dashboards: "Dashboards", inventory: "Inventory", managers: "OLVM managers", collections: "Collections", credentials: "Credentials", alerts: "Alerts", settings: "Settings" };
  document.querySelectorAll(".view").forEach((item) => item.classList.toggle("active", item.id === `${view}-view`));
  document.querySelectorAll(".nav-item[data-view]").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  document.querySelector("#page-title").textContent = titles[view];
  window.location.hash = view;
}

document.addEventListener("click", (event) => {
  const navigation = event.target.closest("[data-view]");
  if (navigation) { event.preventDefault(); goTo(navigation.dataset.view); return; }
  const jump = event.target.closest("[data-go-to]");
  if (jump) { goTo(jump.dataset.goTo); return; }
  const grafanaDashboard = event.target.closest("[data-open-grafana-dashboard]");
  if (grafanaDashboard) { openGrafana(grafanaDashboard.dataset.openGrafanaDashboard); return; }
  if (event.target.closest("#open-grafana-button")) { openGrafana(grafanaIntegration.url); return; }
  if (event.target.closest("#open-grafana-builder-button")) { openGrafana(grafanaIntegration.url); return; }
  const collectionTab = event.target.closest("[data-collection-tab]");
  if (collectionTab) { selectCollectionTab(collectionTab.dataset.collectionTab); return; }
  const playbookTab = event.target.closest("[data-playbook-tab]");
  if (playbookTab) { selectPlaybookEditorTab(playbookTab.dataset.playbookTab); return; }
  const collectionProfilePlaybook = event.target.closest("[data-collection-profile-playbook]");
  if (collectionProfilePlaybook) { openPlaybookEditor(collectionProfilePlaybook.dataset.collectionProfilePlaybook); return; }
  const collectionProfileRun = event.target.closest("[data-collection-profile-run]");
  if (collectionProfileRun) { runCollectionProfile(collectionProfileRun.dataset.collectionProfileRun); return; }
  const collectionProfileEdit = event.target.closest("[data-collection-profile-edit]");
  if (collectionProfileEdit) { openCollectionProfileDialog(collectionProfileEdit.dataset.collectionProfileEdit); return; }
  if (event.target.closest("#add-collection-profile-button")) { openCollectionProfileDialog(); return; }
  if (event.target.closest("#edit-profile-playbook-button")) { const id = event.target.closest("#edit-profile-playbook-button").dataset.profileId; if (id) { document.querySelector("#collection-profile-dialog").close(); openPlaybookEditor(id); } return; }
  if (event.target.closest("#close-collection-profile-dialog") || event.target.closest("#cancel-collection-profile-button")) { closeCollectionProfileDialog(); return; }
  if (event.target.closest("#check-playbook-syntax-button")) { checkPlaybookSyntax(); return; }
  if (event.target.closest("#save-playbook-button")) { savePlaybook(); return; }
  const playbookVersionSelect = event.target.closest("[data-playbook-version-select]");
  if (playbookVersionSelect) { selectPlaybookVersion(playbookVersionSelect.dataset.playbookVersionSelect); return; }
  const playbookDiff = event.target.closest("[data-playbook-view-diff]");
  if (playbookDiff) { showPlaybookVersionDiff(playbookDiff.dataset.playbookViewDiff); return; }
  const playbookReview = event.target.closest("[data-playbook-review-details]");
  if (playbookReview && !playbookReview.disabled) { showPlaybookReviewDetails(playbookReview.dataset.playbookReviewDetails); return; }
  const playbookDraft = event.target.closest("[data-playbook-create-draft]");
  if (playbookDraft) { createPlaybookDraftFromVersion(playbookDraft.dataset.playbookCreateDraft); return; }
  if (event.target.closest("#compare-playbook-versions-button")) {
    const profile = activePlaybookEditorProfile();
    if (profile) showPlaybookVersionDiff(selectedPlaybookVersion(getPlaybookGitModel(profile)).sha);
    return;
  }
  if (event.target.closest("#close-playbook-editor-dialog") || event.target.closest("#cancel-playbook-editor-button")) { closePlaybookEditor(); return; }
  const sshPublicKeyCopy = event.target.closest("[data-ssh-public-key]");
  if (sshPublicKeyCopy) { copySshPublicKey(sshPublicKeyCopy.dataset.sshPublicKey); return; }
  const credentialEdit = event.target.closest("[data-credential-edit]");
  if (credentialEdit) { openCredentialDialog(credentialEdit.dataset.credentialEdit); return; }
  if (event.target.closest("#add-credential-button")) { openCredentialDialog(); return; }
  if (event.target.closest("#add-ssh-key-button")) { openCredentialDialog("", "SSH key"); return; }
  if (event.target.closest("#reset-grafana-password-button")) { openGrafanaPasswordDialog(); return; }
  if (event.target.closest("#export-internal-audit-button")) { exportInternalAuditEvents(); return; }
  if (event.target.closest("#close-credential-dialog") || event.target.closest("#cancel-credential-button")) { closeCredentialDialog(); return; }
  if (event.target.closest("#close-grafana-password-dialog") || event.target.closest("#cancel-grafana-password-button")) { closeGrafanaPasswordDialog(); return; }
  if (event.target.closest("#add-manual-host-button")) { openManualHostDialog(); return; }
  if (event.target.closest("#edit-manual-host-button")) {
    const hostName = event.target.closest("#edit-manual-host-button").dataset.manualHost;
    document.querySelector("#host-dialog").close();
    openManualHostDialog(hostName);
    return;
  }
  const hostButton = event.target.closest("[data-host]");
  if (hostButton) { openHost(hostButton.dataset.host); return; }
  if (event.target.closest("#test-manual-host-button")) { testManualHostConnection(); return; }
  if (event.target.closest("#close-manual-host-dialog") || event.target.closest("#cancel-manual-host-button")) { closeManualHostDialog(); return; }
  const managerTest = event.target.closest("[data-manager-test]");
  if (managerTest) { runManagerAction(managerTest.dataset.managerTest, "test"); return; }
  const managerSync = event.target.closest("[data-manager-sync]");
  if (managerSync) { runManagerAction(managerSync.dataset.managerSync, "sync"); return; }
  const managerEdit = event.target.closest("[data-manager-edit]");
  if (managerEdit) { openManagerDialog(managerEdit.dataset.managerEdit); return; }
  if (event.target.closest("#add-manager-button")) { openManagerDialog(); return; }
  if (event.target.closest("#sync-all-button")) { syncAllManagers(); return; }
  if (event.target.closest("#test-manager-button")) { testFormConnection(); return; }
  if (event.target.closest("#close-manager-dialog") || event.target.closest("#cancel-manager-button")) { closeManagerDialog(); return; }
  if (event.target.closest("#collect-button") || event.target.closest("#collection-list-button")) { runEntireCollection(); return; }
  if (event.target.closest("#collect-host-button")) { const host = event.target.closest("#collect-host-button").dataset.host; document.querySelector("#host-dialog").close(); collectHost(host); return; }
  if (event.target.closest("#close-dialog") || event.target.closest("#close-detail-button")) document.querySelector("#host-dialog").close();
});

document.querySelector("#manager-form").addEventListener("submit", saveManager);
document.querySelector("#manual-host-form").addEventListener("submit", saveManualHost);
document.querySelector("#collection-profile-form").addEventListener("submit", saveCollectionProfile);
document.querySelector("#collection-profile-schedule-input").addEventListener("input", () => updateCronSchedulePreview());
document.querySelector("#credential-form").addEventListener("submit", saveCredential);
document.querySelector("#grafana-password-form").addEventListener("submit", resetGrafanaPassword);
document.querySelector("#internal-audit-toggle").addEventListener("change", updateInternalAuditSettings);
document.querySelector("#host-search").addEventListener("input", renderHosts);
document.querySelector("#host-status-filter").addEventListener("change", (event) => {
  activeFilter = event.target.value;
  renderHosts();
});
document.querySelector("#host-source-filter").addEventListener("change", (event) => {
  activeSourceFilter = event.target.value;
  renderHosts();
});

renderAll();
loadBackendState().catch(showRequestError);
loadSummary();
const route = window.location.hash.slice(1);
if (["overview", "dashboards", "inventory", "managers", "collections", "credentials", "alerts", "settings"].includes(route)) goTo(route);
window.addEventListener("hashchange", () => {
  const nextRoute = window.location.hash.slice(1);
  if (["overview", "dashboards", "inventory", "managers", "collections", "credentials", "alerts", "settings"].includes(nextRoute)) {
    goTo(nextRoute);
  }
});
