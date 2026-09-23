/**
 * 新建中的任务：按下发送就进入任务页，创建请求在后台完成。
 *
 * 任务标识由前端生成并随请求提交，服务端以它去重，所以失败后重试不会建出第二个任务。
 * 记录只在内存里（附件是浏览器里的 File），刷新后只剩会话存储里的文字草稿，
 * 用于在任务页读不到该任务时把文字放回首页输入框。
 */

import { useSyncExternalStore } from "react";

import { ApiError, createTask } from "./api";

export type PendingTask = {
  taskId: string;
  message: string;
  model: string;
  files: File[];
  status: "sending" | "failed" | "created";
  error: ApiError | null;
};

/** 退回首页输入框的未发出消息；`reason` 说明为什么没发出，用户不在任务页时才需要。 */
export type UnsentDraft = { message: string; files: File[]; lostFiles: number; reason: string | null };

const STORAGE_KEY = "pebble.pendingTasks";

let pending = new Map<string, PendingTask>();
let restored: UnsentDraft | null = null;
const listeners = new Set<() => void>();
// 正在任务页查看的新建任务：失败时留在页面上处理，否则直接退回首页输入框。
const viewing = new Set<string>();

const toApiError = (error: unknown) =>
  error instanceof ApiError ? error : new ApiError("offline", String(error), 0);

function update(taskId: string, next: PendingTask | null): void {
  pending = new Map(pending);
  if (next === null) pending.delete(taskId);
  else pending.set(taskId, next);
  for (const listener of listeners) listener();
}

type Stored = { message: string; files: number };

function readStored(): Record<string, Stored> {
  try {
    return JSON.parse(window.sessionStorage.getItem(STORAGE_KEY) ?? "{}") as Record<string, Stored>;
  } catch { return {}; }
}

function writeStored(taskId: string, value: Stored | null): void {
  try {
    const stored = readStored();
    if (value === null) delete stored[taskId];
    else stored[taskId] = value;
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(stored));
  } catch { /* 会话存储不可用时只少了刷新后的草稿恢复 */ }
}

/** 服务端拒绝了输入（模型、附件、格式），原样重试没有意义，只能改了再发。 */
export const retryable = (error: ApiError) =>
  error.offline || error.unavailable || error.httpStatus === 0 || error.httpStatus >= 500;

async function submit(taskId: string): Promise<void> {
  const entry = pending.get(taskId);
  if (entry === undefined) return;
  update(taskId, { ...entry, status: "sending", error: null });
  try {
    await createTask(entry.message, entry.model, entry.files, taskId);
    writeStored(taskId, null);
    const current = pending.get(taskId);
    if (current !== undefined) update(taskId, { ...current, status: "created" });
  } catch (failure) {
    const current = pending.get(taskId);
    if (current === undefined) return;
    const error = toApiError(failure);
    update(taskId, { ...current, status: "failed", error });
    if (!viewing.has(taskId)) returnToComposer(taskId, error.message);
  }
}

/** 登记并开始创建任务，立即返回任务标识供页面跳转。 */
export function startTask(message: string, model: string, files: File[]): string {
  const taskId = crypto.randomUUID();
  update(taskId, { taskId, message, model, files, status: "sending", error: null });
  writeStored(taskId, { message, files: files.length });
  void submit(taskId);
  return taskId;
}

export function retryTask(taskId: string): void {
  if (pending.get(taskId)?.status === "failed") void submit(taskId);
}

function returnToComposer(taskId: string, reason: string | null): void {
  const entry = pending.get(taskId);
  if (entry === undefined) return;
  restored = { message: entry.message, files: entry.files, lostFiles: 0, reason };
  writeStored(taskId, null);
  update(taskId, null);
}

/**
 * 任务页挂载期间登记查看；离开时这条若已失败，放弃它并把文字与附件交回首页输入框。
 * 仍在发送的继续在后台完成，成功就出现在任务列表，失败再退回输入框。
 */
export function viewTask(taskId: string): () => void {
  viewing.add(taskId);
  return () => {
    viewing.delete(taskId);
    if (pending.get(taskId)?.status === "failed") returnToComposer(taskId, null);
  };
}

/** 刷新后读不到任务：会话存储里还有这条的文字，就作为草稿交回首页。 */
export function reviseLostTask(taskId: string): boolean {
  const stored = readStored()[taskId];
  if (stored === undefined) return false;
  restored = { message: stored.message, files: [], lostFiles: stored.files, reason: null };
  writeStored(taskId, null);
  return true;
}

/** 任务页读到服务端数据后不再需要本地占位。 */
export function forgetTask(taskId: string): void {
  writeStored(taskId, null);
  if (pending.get(taskId)?.status === "created") update(taskId, null);
}

/** 首页输入框取一次待恢复的草稿。 */
export function takeDraft(): UnsentDraft | null {
  const draft = restored;
  restored = null;
  return draft;
}

/** 订阅记录变化；首页用它接住在自己停留期间退回的消息。 */
export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function usePendingTask(taskId: string): PendingTask | null {
  return useSyncExternalStore(subscribe, () => pending.get(taskId) ?? null);
}

/** 仅供测试：清空内存与会话存储里的记录。 */
export function resetPendingTasks(): void {
  pending = new Map();
  restored = null;
  viewing.clear();
  try { window.sessionStorage.removeItem(STORAGE_KEY); } catch { /* 无需处理 */ }
  for (const listener of listeners) listener();
}
