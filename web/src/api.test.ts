// @vitest-environment jsdom

import { afterEach, expect, test, vi } from "vitest";

import type { AgentEvent } from "./api";
import { subscribeEvents } from "./api";

/** 最小 EventSource 替身：只提供订阅与关闭，测试手动投递事件。 */
class FakeEventSource {
  static latest: FakeEventSource | null = null;
  listeners = new Map<string, ((event: Event) => void)[]>();
  closed = false;

  constructor(public url: string) {
    FakeEventSource.latest = this;
  }

  addEventListener(type: string, handler: (event: Event) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), handler]);
  }

  close() {
    this.closed = true;
  }

  dispatch(type: string, event: Event) {
    for (const handler of this.listeners.get(type) ?? []) handler(event);
  }
}

afterEach(() => {
  FakeEventSource.latest = null;
  vi.unstubAllGlobals();
});

test("带数据的事件按 JSON 投递，连接 error 事件被忽略", () => {
  vi.stubGlobal("EventSource", FakeEventSource);
  const events: AgentEvent[] = [];
  const unsubscribe = subscribeEvents("task-1", (event) => events.push(event), () => undefined);
  const source = FakeEventSource.latest!;
  expect(source.url).toBe("/api/tasks/task-1/events");

  source.dispatch("text", { data: JSON.stringify({ type: "text", text: "解答" }) } as MessageEvent);
  expect(events).toEqual([{ type: "text", text: "解答" }]);

  // EventSource 的连接 error 事件与网关的 error 消息同名，但没有 data：不能抛出页面异常。
  expect(() => source.dispatch("error", new Event("error"))).not.toThrow();
  expect(events).toHaveLength(1);

  // 网关自己的 error 消息照常投递。
  source.dispatch("error", { data: JSON.stringify({ type: "error", message: "断线" }) } as MessageEvent);
  expect(events.at(-1)).toEqual({ type: "error", message: "断线" });

  unsubscribe();
  expect(source.closed).toBe(true);
});
