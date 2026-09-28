/** 后端 Interface：统一时间线、邮件草稿版本、确认执行、资料与长期记忆管理。 */

export type RunStatus = "pending" | "running" | "done" | "error" | "interrupted";
export type OperationStatus =
  | "pending" | "sending" | "sent" | "creating" | "created" | "failed" | "unknown" | "cancelled";

export type Run = {
  run_id: string;
  task_id: string;
  kind: "new_mail" | "message" | "execution_result";
  status: RunStatus;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  /** 进行中调用的当前步骤说明（如“正在检索资料：星云验收”）；只在任务详情里给出。 */
  activity?: string | null;
  /** 仅任务详情提供：最后一轮中断且未产生操作记录时可由用户重试。 */
  retryable?: boolean;
};

/** 会话是谁开的头：`mail` 是收到新邮件自动开始，`user` 是用户自己发起。 */
export type TaskSource = "mail" | "user";

export type Task = {
  task_id: string;
  goal: string;
  model: string;
  source: TaskSource;
  sdk_session_id: string | null;
  created_at: string;
};
export type TaskDetail = Task & { latest_run: Run | null };
export type OperationSummary = {
  operation_id: string;
  type: string;
  version: number;
  status: OperationStatus;
};

export type Draft = {
  operation_id: string;
  kind: "reply" | "new";
  version: number;
  status: OperationStatus;
  source_message_id?: string;
  thread_id?: string;
  to: string[];
  subject: string;
  body: string;
};

export type SendResult =
  | { status: "sent"; message_id: string }
  | { status: "created"; event_id: string }
  | { status: "failed" | "unknown"; reason: string };

export type Execution = {
  operation_id: string;
  version: number;
  status: OperationStatus;
  confirmation: { task_id: string; version: number; confirmed_at: string } | null;
  result: SendResult | null;
};

export type TimelineItem =
  | {
      item_id: string;
      kind: "text";
      role: "user" | "assistant";
      run_id: string;
      text: string;
      attachments?: Attachment[];
      created_at: string;
    }
  | {
      item_id: string;
      kind: "mail_draft";
      run_id: string;
      operation_id: string;
      draft: Draft;
      execution: Execution;
      created_at: string;
    }
  | {
      item_id: string;
      kind: "error";
      run_id: string;
      text: string;
      created_at: string;
    }
  | {
      item_id: string;
      kind: "notice";
      run_id: string;
      text: string;
      created_at: string;
    }
  | {
      item_id: string;
      kind: "tool";
      run_id: string;
      tool_call_id: string;
      name: string;
      arguments: Record<string, unknown>;
      status: "running" | "ok" | "error";
      result: string | null;
      created_at: string;
    };

export type Timeline = { task_id: string; sdk_session_id: string | null; items: TimelineItem[] };

export type ObservationStep = {
  step_id: string;
  kind: "tool" | "compact" | "degraded";
  code: string;
  status: "running" | "ok" | "error" | "denied";
  started_at: string | null;
  ended_at: string | null;
  item_id: string | null;
  tool_call_id: string | null;
  detail: Record<string, unknown> | null;
};

export type ObservationMaterial = { title: string; chars: number };
export type ObservationSkipped = { category: string; reason: string; skill_id?: string };

export type UsageEntry = {
  message_id: string | null;
  request_id: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  credits: number | null;
};

/** CLI `/context` 视图的一类：运行时只给占窗口百分比，不给绝对 token 数。 */
export type ContextCategory = { kind: string; percentage: number | null };

export type ContextReading = {
  used_percentage: number | null;
  threshold_percentage: number | null;
  auto_compact_enabled: boolean | null;
  categories?: ContextCategory[];
};

export type RunObservation = {
  run_id: string;
  kind: string;
  status: string;
  model: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  materials: { assembled: ObservationMaterial[]; skipped: ObservationSkipped[] } | null;
  sdk_result: {
    duration_ms: number | null;
    duration_api_ms: number | null;
    num_turns: number | null;
    is_error: boolean;
    stop_reason?: string | null;
    usage: UsageEntry[];
    /** ResultMessage 的末次请求读数；CN 运行时只在结果层有真实标识与 token。 */
    result_usage?: {
      request_id: string | null;
      input_tokens: number | null;
      output_tokens: number | null;
      credits: number | null;
      context_usage_ratio: number | null;
    } | null;
    /** 轮末会话累计快照；读取面按相邻轮差值推算本轮消耗。 */
    session_totals?: {
      input_tokens: number | null;
      output_tokens: number | null;
      credits: number | null;
    } | null;
  } | null;
  usage_totals: { input_tokens: number | null; output_tokens: number | null; credits: number | null };
  /** delta＝相邻轮会话累计快照的差值推算；requests＝请求级条目求和；缺省＝无合计。 */
  usage_totals_source?: "delta" | "requests" | null;
  context_before: ContextReading | null;
  context_after: ContextReading | null;
  steps: ObservationStep[];
};

export type Observations = { runs: RunObservation[] };
export type MessageTarget = { kind: "mail_draft"; operation_id: string };
export type Attachment = {
  file_id: string;
  filename: string;
  mime_type: string;
  size: number;
  sha256: string;
  url: string;
};
export type ModelEntry = { id: string; label: string; kind: "managed" | "custom" };
export type ModelCatalog = {
  default_model: string;
  models: ModelEntry[];
  fetched_at: string | null;
  /** 服务端最近一次刷新目录失败，返回的是之前读到的目录。 */
  stale: boolean;
};
export type FieldError = { field: string; message: string };

export type AgentEvent =
  | { type: "session"; run_id: string; sdk_session_id: string }
  | { type: "text"; run_id: string; item_id: string; text: string }
  | {
      type: "draft_saved";
      run_id: string;
      item_id: string;
      operation_id: string;
      version: number;
    }
  | { type: "done"; run_id: string }
  /** 用户终止了这一轮：调用已按中断落库，页面重读即可。 */
  | { type: "interrupted"; run_id: string }
  | { type: "error"; run_id: string; item_id: string; message: string }
  | { type: "notice"; run_id: string; item_id: string; text: string }
  | { type: "activity"; run_id: string; text: string }
  | { type: "timeline_changed"; run_id: string };

export class ApiError extends Error {
  readonly name = "ApiError";
  constructor(
    readonly code: string,
    message: string,
    readonly httpStatus: number,
    readonly currentVersion?: number | string,
    readonly operationStatus?: string,
    readonly fieldErrors?: FieldError[],
    readonly currentRevision?: string,
  ) {
    super(message);
  }
  get unavailable(): boolean { return this.code === "unavailable"; }
  get offline(): boolean { return this.code === "offline"; }
}

type ErrorBody = {
  error?: string;
  message?: string;
  current_version?: number | string;
  current_revision?: string;
  status?: string;
  errors?: FieldError[];
  detail?: unknown;
};

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...init,
      headers: init?.body && !(init.body instanceof FormData)
        ? { "Content-Type": "application/json", ...init.headers }
        : init?.headers,
    });
  } catch (error) {
    throw new ApiError("offline", `无法连接 Pebble 服务：${String(error)}`, 0);
  }
  const text = await response.text();
  let payload: unknown = null;
  try { payload = text ? JSON.parse(text) : null; }
  catch { throw new ApiError("invalid_response", `服务返回了无效响应（HTTP ${response.status}）`, response.status); }
  if (!response.ok) {
    const body = (payload ?? {}) as ErrorBody;
    const gatewayFailure = body.error === undefined && [502, 503, 504].includes(response.status);
    const code = body.error ?? (gatewayFailure ? "offline" : "invalid_request");
    const message = gatewayFailure
      ? `无法连接 Pebble 服务（HTTP ${response.status}）`
      : (body.message ?? (body.detail ? JSON.stringify(body.detail) : response.statusText));
    throw new ApiError(code, message, response.status, body.current_version, body.status, body.errors,
      body.current_revision);
  }
  return payload as T;
}

export const listTasks = () => request<Task[]>("/tasks");
const MODEL_CATALOG_KEY = "pebble.models";

function storedCatalog(): ModelCatalog | null {
  try {
    const value = JSON.parse(localStorage.getItem(MODEL_CATALOG_KEY) ?? "null") as ModelCatalog | null;
    return value !== null && Array.isArray(value.models) && value.models.length > 0 ? value : null;
  } catch {
    return null;
  }
}

let modelCatalog: ModelCatalog | null = null;
/**
 * 每次读取都刷新内存与本机缓存。页面先用缓存立即显示目录，网络波动读不到时继续沿用，
 * 避免闪出型号标识或整页不可用；服务端校验仍以它自己的目录为准。
 */
export const listModels = async () => {
  modelCatalog = await request<ModelCatalog>("/models");
  try {
    localStorage.setItem(MODEL_CATALOG_KEY, JSON.stringify(modelCatalog));
  } catch {
    // 本机缓存只是加速显示，写不进去不影响使用。
  }
  return modelCatalog;
};
export const cachedCatalog = () => modelCatalog ?? storedCatalog();
export const cachedModels = () => cachedCatalog()?.models ?? null;

function messageForm(
  message: string,
  files: File[],
  target: MessageTarget | null = null,
  selection: SkillSelection | null = null,
): FormData {
  const body = new FormData();
  body.set("message", message);
  if (target !== null) body.set("target", JSON.stringify(target));
  for (const file of files) body.append("files", file);
  if (selection !== null) body.set("selection", JSON.stringify(selection));
  return body;
}

/** `taskId` 由前端生成时兼作幂等键：重复提交同一标识返回已创建的任务。 */
export const createTask = (
  message: string,
  model: string,
  files: File[],
  taskId?: string,
  selection: SkillSelection | null = null,
) => {
  const body = messageForm(message, files, null, selection);
  body.set("model", model);
  if (taskId !== undefined) body.set("task_id", taskId);
  return request<{ task: Task; run: Run }>("/tasks", { method: "POST", body });
};
export const getTask = (taskId: string) => request<TaskDetail>(`/tasks/${taskId}`);
export const deleteTask = (taskId: string) =>
  request<void>(`/tasks/${taskId}`, { method: "DELETE" });
export const listOperations = (taskId: string) =>
  request<OperationSummary[]>(`/tasks/${taskId}/operations`);
export const getTimeline = (taskId: string) => request<Timeline>(`/tasks/${taskId}/timeline`);
export const getObservations = (taskId: string) =>
  request<Observations>(`/tasks/${taskId}/observations`);

export const sendMessage = (
  taskId: string,
  message: string,
  target: MessageTarget | null = null,
  files: File[] = [],
  selection: SkillSelection | null = null,
) => request<Run>(`/tasks/${taskId}/messages`, {
  method: "POST",
  body: messageForm(message, files, target, selection),
});

export const retryLastMessage = (taskId: string) =>
  request<Run>(`/tasks/${taskId}/retry`, { method: "POST" });

/** 终止任务当前进行中的一轮：已流出的回答保留，调用记为已中断。 */
export const interruptTask = (taskId: string) =>
  request<Run>(`/tasks/${taskId}/interrupt`, { method: "POST" });

export const editDraft = (
  operationId: string,
  expectedVersion: number,
  fields: { to: string[]; subject: string; body: string },
) => request<{ operation_id: string; version: number; status: "pending" }>(
  `/operations/${operationId}/draft`,
  { method: "PATCH", body: JSON.stringify({ expected_version: expectedVersion, ...fields }) },
);

export const confirmOperation = (taskId: string, operationId: string, version: number) =>
  request<Execution>(`/tasks/${taskId}/confirmations`, {
    method: "POST",
    body: JSON.stringify({ operation_id: operationId, version }),
  });
export const cancelOperation = (taskId: string, operationId: string, version: number) =>
  request<Execution>(`/tasks/${taskId}/cancellations`, {
    method: "POST",
    body: JSON.stringify({ operation_id: operationId, version }),
  });
export const verifyExecution = (operationId: string) =>
  request<Execution>(`/operations/${operationId}/verification`, { method: "POST" });

const EVENT_TYPES = ["session", "text", "draft_saved", "done", "interrupted", "error", "notice", "activity", "timeline_changed"] as const;
export function subscribeEvents(
  taskId: string,
  onEvent: (event: AgentEvent) => void,
  onReconnect: () => void,
): () => void {
  const source = new EventSource(`/api/tasks/${taskId}/events`);
  let opened = false;
  source.addEventListener("open", () => {
    if (opened) onReconnect();
    opened = true;
  });
  for (const type of EVENT_TYPES) {
    source.addEventListener(type, (event) => {
      // EventSource 自己的连接 error 事件与网关的 "error" 消息同名，且没有 data；
      // 只处理真正带数据帧的消息，别把连接故障变成页面异常。
      const data = (event as MessageEvent<string>).data;
      if (typeof data !== "string") return;
      onEvent(JSON.parse(data) as AgentEvent);
    });
  }
  return () => source.close();
}

/* ---------- 历史对话检索 ---------- */

/** 说话方：user 用户、assistant 助理、notice 程序提示、mail_draft 邮件草稿。 */
export type HistorySpeaker = "user" | "assistant" | "notice" | "mail_draft";

export type HistoryHit = {
  task_id: string;
  task_title: string;
  item_id: string;
  speaker: HistorySpeaker;
  created_at: string;
  snippet: string;
};

export const searchHistory = (q: string) =>
  request<{ query: string; results: HistoryHit[] }>(`/history/search?${new URLSearchParams({ q }).toString()}`);

/* ---------- 资料管理 ---------- */

/** 资料库列表项：`version` 是该资料当前的 Git 提交。 */
export type KbListItem = {
  id: string | null;
  path: string;
  title: string | null;
  summary?: string | null;
  updated_at?: string | null;
  version: string;
};

export type KbDocument = {
  id: string;
  path: string;
  title: string;
  summary: string;
  created_at: string | null;
  updated_at: string | null;
  version: string;
  body: string;
};

export type KbHit = {
  id: string;
  path: string;
  title: string | null;
  heading: string;
  snippet: string;
};

export type KbWriteResult = {
  id: string;
  path: string;
  title: string;
  version: string;
  index_status?: "ok" | "stale";
};

const query = (params: Record<string, string | undefined>) => {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value !== undefined) search.set(key, value);
  return search.toString();
};

export const listKbDocuments = () =>
  request<{ directory: string; documents: KbListItem[] }>("/kb/documents");
export const getKbDocument = (path: string) =>
  request<KbDocument>(`/kb/document?${query({ path })}`);
export const searchKb = (q: string) =>
  request<{ query: string; results: KbHit[] }>(`/kb/search?${query({ q })}`);
export const createKbDocument = (fields: {
  title: string; summary: string; body: string; path?: string; directory?: string;
}) =>
  request<KbWriteResult>("/kb/documents", { method: "POST", body: JSON.stringify(fields) });
/** 资料库全部子文件夹（含空文件夹），路径不带 `kb/` 前缀。 */
export const listKbFolders = () => request<{ folders: string[] }>("/kb/folders");
export const createKbFolder = (path: string) =>
  request<{ path: string }>("/kb/folders", { method: "POST", body: JSON.stringify({ path }) });
/** 按标题与正文起草一句话说明，只返回文本、不保存。 */
export type KbAsset = { path: string };

export const uploadKbAsset = (file: File) => {
  const body = new FormData();
  body.append("file", file);
  return request<KbAsset>("/kb/assets", { method: "POST", body });
};

/** 由轻量模型看图生成说明，不写入文件；生成失败时为空串。 */
export const describeKbAsset = (path: string) =>
  request<{ description: string }>("/kb/assets/describe", { method: "POST", body: JSON.stringify({ path }) })
    .then((result) => result.description);

/** 正文里的 `assets/…` 相对资料库根目录，显示时换成读取接口的地址；其他地址原样返回。 */
export const kbAssetUrl = (src: string) => {
  const match = /^assets\/([^/]+)$/.exec(src);
  return match ? `/api/kb/assets/${encodeURIComponent(match[1])}` : src;
};

export const draftKbSummary = (title: string, body: string) =>
  request<{ summary: string }>("/kb/summary", { method: "POST", body: JSON.stringify({ title, body }) });
export const updateKbDocument = (
  path: string,
  expectedVersion: string,
  // 只传要改的字段；改了标题时文件名随之更新，返回的 path 是新位置。
  fields: { title?: string; summary?: string; body?: string },
) => request<KbWriteResult>("/kb/document/update", {
  method: "POST",
  body: JSON.stringify({ path, expected_version: expectedVersion, ...fields }),
});
export const moveKbDocument = (path: string, expectedVersion: string, newPath: string) =>
  request<KbWriteResult & { previous_path: string }>("/kb/document/move", {
    method: "POST",
    body: JSON.stringify({ path, expected_version: expectedVersion, new_path: newPath }),
  });
export const deleteKbDocument = (path: string, expectedVersion: string) =>
  request<{ path: string }>("/kb/document/delete", {
    method: "POST",
    body: JSON.stringify({ path, expected_version: expectedVersion }),
  });

/* ---------- 长期记忆 ---------- */

export type MemoryTarget = "user" | "memory";

/** 一块长期记忆：整份 Markdown 全文；`version` 是内容哈希，保存时带回用于冲突检查。 */
export type MemorySection = {
  content: string;
  usage: { chars: number; limit: number };
  version: string;
};

export type MemorySnapshot = Record<MemoryTarget, MemorySection>;
export type MemoryWriteResult = MemorySection & { target: MemoryTarget; changed: boolean };

export const getMemory = () => request<MemorySnapshot>("/memory");
export const saveMemory = (target: MemoryTarget, content: string, expectedVersion: string) =>
  request<MemoryWriteResult>(`/memory/${target}`, {
    method: "PUT",
    body: JSON.stringify({ content, expected_version: expectedVersion }),
  });

/* ---------- 技能 ---------- */

export type SkillOrigin = "user" | "explicit" | "review";
export type SkillState = "active" | "stale" | "archived";

/** 目录条目；`revision` 是内容的哈希版本，保存时带回用于冲突检查。 */
export type SkillSummary = {
  skill_id: string;
  name: string;
  description: string;
  origin: SkillOrigin;
  managed: boolean;
  state: SkillState;
  revision: string;
  updated_at: string;
  last_loaded_at: string | null;
};

export type SkillFileEntry = { path: string; hash: string };
export type SkillUsageRecord = {
  run_id: string;
  skill_id: string;
  task_id?: string;
  revision: string;
  source: "auto" | "manual";
  loaded_at: string;
};

export type SkillDetail = SkillSummary & {
  created_at: string;
  body: string;
  files: SkillFileEntry[];
  usage: SkillUsageRecord[];
};

export type SkillVersion = {
  revision: string;
  created_at: string;
  change_id: string | null;
  actor: string | null;
  reason: string | null;
};

export type SkillChangeView = {
  id: string;
  review_job_id: string | null;
  skill_id: string | null;
  action: "create" | "patch" | "write_file" | "remove_file";
  payload: Record<string, unknown>;
  base_revision: string | null;
  reason: string;
  evidence_item_ids: string[];
  actor: "user" | "foreground" | "review";
  status: "proposed" | "applied" | "rejected" | "conflict";
  created_at: string;
  applied_at: string | null;
};

/** 手动选择的一项：只带标识，内容版本在装配时由服务端绑定。 */
export type SkillPick = { id: string };

/** 随消息提交的技能选择，形状见 `docs/skills.md` §3。 */
export type SkillSelection = {
  skills: SkillPick[];
  excluded_skill_ids: string[];
  auto_match: boolean;
};

export const DEFAULT_SKILL_SELECTION: SkillSelection = {
  skills: [], excluded_skill_ids: [], auto_match: true,
};

export const listSkills = (state: SkillState = "active", q?: string) =>
  request<SkillSummary[]>(`/skills?${query({ state, q })}`);
export const getSkill = (skillId: string) => request<SkillDetail>(`/skills/${skillId}`);
export const createSkill = (fields: {
  skill_id: string; name: string; description: string; body: string;
  attachments?: Record<string, string>;
}) => request<{ status: string; skill: SkillDetail }>("/skills", {
  method: "POST", body: JSON.stringify(fields),
});
export const updateSkill = (
  skillId: string,
  expectedRevision: string,
  fields: { name?: string; description?: string; body?: string },
) => request<{ status: string; skill: SkillDetail }>(`/skills/${skillId}`, {
  method: "PUT", body: JSON.stringify({ expected_revision: expectedRevision, ...fields }),
});
export const archiveSkill = (skillId: string) =>
  request<void>(`/skills/${skillId}/archive`, { method: "POST" });
export const restoreSkill = (skillId: string) =>
  request<{ skill_id: string; state: SkillState }>(`/skills/${skillId}/restore`, { method: "POST" });
export const setSkillManaged = (skillId: string, value: boolean) =>
  request<{ skill_id: string; managed: boolean }>(`/skills/${skillId}/managed`, {
    method: "POST", body: JSON.stringify({ value }),
  });
export const listSkillVersions = (skillId: string) =>
  request<SkillVersion[]>(`/skills/${skillId}/versions`);
export const getSkillVersion = (skillId: string, revision: string) =>
  request<{ body: string; files: SkillFileEntry[] }>(`/skills/${skillId}/versions/${revision}`);
export const restoreSkillVersion = (skillId: string, revision: string) =>
  request<{ status: string }>(`/skills/${skillId}/restore-version`, {
    method: "POST", body: JSON.stringify({ revision }),
  });
export const listSkillChanges = (status?: string) =>
  request<SkillChangeView[]>(`/skill-changes?${query({ status })}`);
export const approveSkillChange = (changeId: string, expectedRevision: string) =>
  request<{ status: string }>(`/skill-changes/${changeId}/approve`, {
    method: "POST", body: JSON.stringify({ expected_revision: expectedRevision }),
  });
export const rejectSkillChange = (changeId: string) =>
  request<void>(`/skill-changes/${changeId}/reject`, { method: "POST" });
export const readSkillFile = (skillId: string, path: string) =>
  request<{ path: string; content: string }>(
    `/skills/${skillId}/files/${path.split("/").map(encodeURIComponent).join("/")}`);
export const writeSkillFile = (
  skillId: string, path: string, content: string, expectedRevision: string,
) => request<{ status: string; skill: SkillDetail }>(`/skills/${skillId}/files`, {
  method: "POST", body: JSON.stringify({ relative_path: path, content, expected_revision: expectedRevision }),
});
export const removeSkillFile = (skillId: string, path: string, expectedRevision: string) =>
  request<{ status: string; skill: SkillDetail }>(`/skills/${skillId}/files/remove`, {
    method: "POST", body: JSON.stringify({ relative_path: path, expected_revision: expectedRevision }),
  });
