/** 后端 Interface：统一时间线、邮件草稿版本与确认执行。 */

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
};

export type Task = { task_id: string; goal: string; sdk_session_id: string | null; created_at: string };
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

export type CalendarPreview = {
  operation_id: string; version: number; status: OperationStatus; calendar_id: "primary";
  summary: string; start: string; end: string; all_day: boolean;
  location: string | null; description: string;
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
      item_id: string; kind: "calendar_preview"; run_id: string; operation_id: string;
      preview: CalendarPreview; execution: Execution; created_at: string;
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
    };

export type Timeline = { task_id: string; sdk_session_id: string | null; items: TimelineItem[] };
export type MessageTarget = { kind: "mail_draft" | "calendar_preview"; operation_id: string };
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
  | { type: "error"; run_id: string; item_id: string; message: string };

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
  get unavailable(): boolean { return this.code === "unavailable"; }
  get offline(): boolean { return this.code === "offline"; }
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

export const editCalendarPreview = (
  operationId: string,
  expectedVersion: number,
  fields: Omit<CalendarPreview, "operation_id" | "version" | "status">,
) => request<{ operation_id: string; version: number; status: "pending" }>(
  `/operations/${operationId}/calendar-preview`,
  { method: "PATCH", body: JSON.stringify({ expected_version: expectedVersion, ...fields }) },
);

export const confirmOperation = (taskId: string, operationId: string, version: number) =>
  request<Execution>(`/tasks/${taskId}/confirmations`, {
    method: "POST",
    body: JSON.stringify({ operation_id: operationId, version }),
  });
export const verifyExecution = (operationId: string) =>
  request<Execution>(`/operations/${operationId}/verification`, { method: "POST" });

const EVENT_TYPES = ["session", "text", "draft_saved", "done", "error"] as const;
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
