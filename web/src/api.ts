/**
 * 后端接口：契约类型、fetch 封装、错误体映射与 SSE 订阅。
 *
 * 字段与错误名称对应 docs/v1-mail-flow-contract.md；本模块不推导业务状态（见 status.ts），
 * 也不缓存任何内容，页面自行决定何时重新读取。
 */

export type RunStatus = "pending" | "running" | "done" | "error" | "interrupted";

export type OperationStatus = "pending" | "sending" | "sent" | "failed" | "unknown";

export type Run = {
  run_id: string;
  task_id: string;
  kind: "new_mail" | "message" | "execution_result";
  status: RunStatus;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export type Task = {
  task_id: string;
  goal: string;
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
  version: number;
  status: OperationStatus;
  source_message_id: string;
  thread_id: string;
  to: string[];
  subject: string;
  body: string;
};

export type SendResult =
  | { status: "sent"; message_id: string }
  | { status: "failed" | "unknown"; reason: string };

export type Execution = {
  operation_id: string;
  version: number;
  status: OperationStatus;
  confirmation: { task_id: string; version: number; confirmed_at: string } | null;
  result: SendResult | null;
};

export type HistoryMessage = { role: "user" | "assistant"; text: string };

export type History = {
  task_id: string;
  sdk_session_id: string | null;
  messages: HistoryMessage[];
};

export type FieldError = { field: string; message: string };

/** Agent 事件，均带所属调用标识；见契约第 3、9 节。 */
export type AgentEvent =
  | { type: "session"; run_id: string; sdk_session_id: string }
  | { type: "text"; run_id: string; text: string }
  | { type: "draft_saved"; run_id: string; operation_id: string; version: number }
  | { type: "done"; run_id: string }
  | { type: "error"; run_id: string; message: string };

/** 接口错误：保留后端返回的结构化字段，供页面区分处理。 */
export class ApiError extends Error {
  readonly name = "ApiError";

  constructor(
    readonly code: string,
    message: string,
    readonly httpStatus: number,
    readonly currentVersion?: number,
    readonly operationStatus?: string,
    readonly fieldErrors?: FieldError[],
  ) {
    super(message);
  }

  /** 依赖未接入（Agent、邮件校验或发送），页面应降级提示而不是当作故障。 */
  get unavailable(): boolean {
    return this.code === "unavailable";
  }

  /** 网络不可达或响应不可解析，与后端明确返回的业务错误区分。 */
  get offline(): boolean {
    return this.code === "offline";
  }
}

type ErrorBody = {
  error?: string;
  message?: string;
  current_version?: number;
  status?: string;
  errors?: FieldError[];
  detail?: unknown;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...init,
      headers: init?.body ? { "Content-Type": "application/json", ...init?.headers } : init?.headers,
    });
  } catch (error) {
    throw new ApiError("offline", `无法连接 Pebble 服务：${String(error)}`, 0);
  }

  const text = await response.text();
  const payload: unknown = text ? JSON.parse(text) : null;

  if (!response.ok) {
    const body = (payload ?? {}) as ErrorBody;
    // 网关错误没有业务错误体，说明服务没接上，与后端明确返回的业务错误分开提示；
    // FastAPI 的请求体校验错误同样没有 error 字段，统一归到 invalid_request。
    const gatewayFailure = body.error === undefined && [502, 503, 504].includes(response.status);
    const code = body.error ?? (gatewayFailure ? "offline" : "invalid_request");
    const message = gatewayFailure
      ? `无法连接 Pebble 服务（HTTP ${response.status}）`
      : (body.message ?? (body.detail ? JSON.stringify(body.detail) : response.statusText));
    throw new ApiError(
      code,
      message,
      response.status,
      body.current_version,
      body.status,
      body.errors,
    );
  }

  return payload as T;
}

export const listTasks = () => request<Task[]>("/tasks");

export const createTask = (goal: string) =>
  request<Task>("/tasks", { method: "POST", body: JSON.stringify({ goal }) });

export const getTask = (taskId: string) => request<TaskDetail>(`/tasks/${taskId}`);

export const listOperations = (taskId: string) =>
  request<OperationSummary[]>(`/tasks/${taskId}/operations`);

export const getHistory = (taskId: string) => request<History>(`/tasks/${taskId}/history`);

export const sendMessage = (taskId: string, message: string) =>
  request<Run>(`/tasks/${taskId}/messages`, { method: "POST", body: JSON.stringify({ message }) });

export const getDraft = (operationId: string, version?: number) =>
  request<Draft>(
    `/operations/${operationId}/draft${version === undefined ? "" : `?version=${version}`}`,
  );

export const editDraft = (
  operationId: string,
  expectedVersion: number,
  fields: { to: string[]; subject: string; body: string },
) =>
  request<{ operation_id: string; version: number }>(`/operations/${operationId}/draft`, {
    method: "PATCH",
    body: JSON.stringify({ expected_version: expectedVersion, ...fields }),
  });

export const confirmOperation = (taskId: string, operationId: string, version: number) =>
  request<Execution>(`/tasks/${taskId}/confirmations`, {
    method: "POST",
    body: JSON.stringify({ operation_id: operationId, version }),
  });

export const getExecution = (operationId: string) =>
  request<Execution>(`/operations/${operationId}/execution`);

const EVENT_TYPES = ["session", "text", "draft_saved", "done", "error"] as const;

/**
 * 订阅任务事件流。断线由浏览器自动重连，重连通过 onReconnect 通知调用方重新拉取
 * 历史与操作列表——SSE 不重放断线期间的事件。
 */
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
