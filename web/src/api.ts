/** 后端 Interface：统一时间线、邮件草稿版本、确认执行与资料管理。 */

export type RunStatus = "pending" | "running" | "done" | "error" | "interrupted";
export type OperationStatus = "pending" | "sending" | "sent" | "creating" | "created" | "failed" | "unknown";

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
};

/** 会话是谁开的头：`mail` 是收到新邮件自动开始，`user` 是用户自己发起。 */
export type TaskSource = "mail" | "user";

export type Task = {
  task_id: string;
  goal: string;
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
    };

export type Timeline = { task_id: string; sdk_session_id: string | null; items: TimelineItem[] };
export type MessageTarget = { kind: "mail_draft"; operation_id: string };
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
  | { type: "error"; run_id: string; item_id: string; message: string }
  | { type: "notice"; run_id: string; item_id: string; text: string }
  | { type: "activity"; run_id: string; text: string };

export class ApiError extends Error {
  readonly name = "ApiError";
  constructor(
    readonly code: string,
    message: string,
    readonly httpStatus: number,
    readonly currentVersion?: number | string,
    readonly operationStatus?: string,
    readonly fieldErrors?: FieldError[],
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
  status?: string;
  errors?: FieldError[];
  detail?: unknown;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...init,
      headers: init?.body ? { "Content-Type": "application/json", ...init.headers } : init?.headers,
    });
  } catch (error) {
    throw new ApiError("offline", `无法连接 Pebble 服务：${String(error)}`, 0);
  }
  const text = await response.text();
  const payload: unknown = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const body = (payload ?? {}) as ErrorBody;
    const gatewayFailure = body.error === undefined && [502, 503, 504].includes(response.status);
    const code = body.error ?? (gatewayFailure ? "offline" : "invalid_request");
    const message = gatewayFailure
      ? `无法连接 Pebble 服务（HTTP ${response.status}）`
      : (body.message ?? (body.detail ? JSON.stringify(body.detail) : response.statusText));
    throw new ApiError(code, message, response.status, body.current_version, body.status, body.errors);
  }
  return payload as T;
}

export const listTasks = () => request<Task[]>("/tasks");
export const createTask = (goal: string) =>
  request<Task>("/tasks", { method: "POST", body: JSON.stringify({ goal }) });
export const getTask = (taskId: string) => request<TaskDetail>(`/tasks/${taskId}`);
export const deleteTask = (taskId: string) =>
  request<void>(`/tasks/${taskId}`, { method: "DELETE" });
export const listOperations = (taskId: string) =>
  request<OperationSummary[]>(`/tasks/${taskId}/operations`);
export const getTimeline = (taskId: string) => request<Timeline>(`/tasks/${taskId}/timeline`);

export const sendMessage = (
  taskId: string,
  message: string,
  target: MessageTarget | null = null,
) => request<Run>(`/tasks/${taskId}/messages`, {
  method: "POST",
  body: JSON.stringify({ message, target }),
});

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
export const verifyExecution = (operationId: string) =>
  request<Execution>(`/operations/${operationId}/verification`, { method: "POST" });

const EVENT_TYPES = ["session", "text", "draft_saved", "done", "error", "notice", "activity"] as const;
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
      onEvent(JSON.parse((event as MessageEvent<string>).data) as AgentEvent);
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
  tags: string[] | null;
  version: string;
};

export type KbDocument = {
  id: string;
  path: string;
  title: string;
  summary: string;
  tags: string[];
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
  title: string; summary: string; body: string; path?: string; tags: string[];
}) =>
  request<KbWriteResult>("/kb/documents", { method: "POST", body: JSON.stringify(fields) });
export const updateKbDocument = (
  path: string,
  expectedVersion: string,
  fields: { title: string; summary: string; body: string; tags: string[] },
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
