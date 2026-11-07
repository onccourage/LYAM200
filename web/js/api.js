// Единственная точка доступа к бэкенду. API_BASE_URL: window.CQA_CONFIG.API_BASE_URL → ?api= → тот же origin.
const cfg = window.CQA_CONFIG || {};
const qp = new URLSearchParams(location.search).get("api");
export const API_BASE_URL = String(cfg.API_BASE_URL ?? qp ?? "").replace(/\/+$/, "");

export class ApiError extends Error {
  constructor(status, detail) {
    super(formatDetail(detail) || `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

function formatDetail(detail) {
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
  if (detail.errors) return detail.errors.join("; ");
  return JSON.stringify(detail);
}

async function request(method, path, { body, form, query } = {}) {
  let url = `${API_BASE_URL}/api${path}`;
  if (query) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null) q.set(k, String(v));
    const s = q.toString();
    if (s) url += `?${s}`;
  }
  const init = { method, headers: {} };
  if (form) init.body = form;
  else if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(url, init);
  } catch (e) {
    throw new ApiError(0, `Сервер недоступен: ${e.message}`);
  }
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!resp.ok) throw new ApiError(resp.status, data && data.detail !== undefined ? data.detail : data);
  return data;
}

const enc = encodeURIComponent;

export const api = {
  config: () => request("GET", "/config"),
  listDatasets: () => request("GET", "/datasets"),
  loadDataset: (groupId, refresh = true) => request("POST", `/datasets/${enc(groupId)}/load`, { query: { refresh } }),
  listConversations: (groupId) => request("GET", `/datasets/${enc(groupId)}/conversations`),
  getConversation: (groupId, convId) => request("GET", `/conversations/${enc(groupId)}/${enc(convId)}`),
  uploadConversations: (file, groupId) => {
    const form = new FormData();
    form.append("file", file);
    form.append("group_id", groupId);
    return request("POST", "/conversations/upload", { form });
  },
  getEvaluation: (groupId, convId) => request("GET", `/evaluations/${enc(groupId)}/${enc(convId)}`),
  evaluate: (groupId, convId, { checklistId, checklistVersion, full = false } = {}) =>
    request("POST", `/evaluations/${enc(groupId)}/${enc(convId)}`,
      { query: { checklist_id: checklistId, checklist_version: checklistVersion, full } }),
  analytics: (groupId, checklistId) => request("GET", `/datasets/${enc(groupId)}/analytics`, { query: { checklist_id: checklistId } }),
  datasetStats: (groupId) => request("GET", `/datasets/${enc(groupId)}/stats`),
  startBatch: (payload) => request("POST", "/batches", { body: payload }),
  getBatch: (id) => request("GET", `/batches/${id}`),
  listBatches: (groupId, limit = 1) => request("GET", "/batches", { query: { group_id: groupId, limit } }),
  cancelBatch: (id) => request("POST", `/batches/${id}/cancel`),
  resumeBatch: (id) => request("POST", `/batches/${id}/resume`),
  createAppeal: (payload) => request("POST", "/appeals", { body: payload }),
  listAppeals: (status, groupId) => request("GET", "/appeals", { query: { status, group_id: groupId } }),
  appealsSummary: () => request("GET", "/appeals/summary"),
  getAppeal: (id) => request("GET", `/appeals/${id}`),
  resolveAppeal: (id, payload) => request("POST", `/appeals/${id}/resolve`, { body: payload }),
  listChecklists: () => request("GET", "/checklists"),
  checklistVersions: (id) => request("GET", `/checklists/${enc(id)}/versions`),
  checklistDiff: (id, fromVersion, toVersion) => request("GET", `/checklists/${enc(id)}/diff`, { query: { from_version: fromVersion, to_version: toVersion } }),
  validateChecklist: (checklist) => request("POST", "/checklists/validate", { body: checklist }),
  saveChecklist: (checklist) => request("POST", "/checklists", { body: checklist }),
  getChecklist: (id, version) => request("GET", `/checklists/${enc(id)}`, { query: { version } }),
};
