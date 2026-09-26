// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, expect, test, vi } from "vitest";

import type { RunObservation, TimelineItem } from "../api";
import TimelineFeed from "./TimelineFeed";

const scrollIntoView = vi.fn();
beforeAll(() => { Element.prototype.scrollIntoView = scrollIntoView; });
beforeEach(() => { scrollIntoView.mockClear(); });
afterEach(cleanup);

const draft: Extract<TimelineItem, { kind: "mail_draft" }> = {
  item_id: "card-1",
  kind: "mail_draft",
  run_id: "run-1",
  operation_id: "op-1",
  created_at: "2026-09-14T00:00:00Z",
  draft: {
    operation_id: "op-1", kind: "new", version: 1, status: "pending",
    to: ["office@example.edu"], subject: "证明文件", body: "请查收完整证明文件。",
  },
  execution: { operation_id: "op-1", version: 1, status: "pending", confirmation: null, result: null },
};

type SdkResult = NonNullable<RunObservation["sdk_result"]>;

/** 一轮的运行观测：折叠头只取时长，其余字段保留形状以便日后回填。 */
const observation = (sdk: SdkResult | null = null): RunObservation => ({
  run_id: "run-1",
  kind: "message",
  status: "done",
  model: "auto",
  created_at: "2026-09-24T10:00:00Z",
  started_at: "2026-09-24T10:00:01Z",
  finished_at: "2026-09-24T10:00:09Z",
  materials: null,
  sdk_result: sdk,
  usage_totals: { input_tokens: null, output_tokens: null, credits: null },
  context_before: null,
  context_after: null,
  steps: [],
});

test("按持久化顺序渲染 Agent 文字、完整邮件卡和后续文字", () => {
  const items: TimelineItem[] = [
    { item_id: "text-1", kind: "text", role: "assistant", run_id: "run-1", text: "前置说明", created_at: "2026-09-14T00:00:00Z" },
    draft,
    { item_id: "text-2", kind: "text", role: "assistant", run_id: "run-1", text: "后置说明", created_at: "2026-09-14T00:00:01Z" },
  ];
  const { container } = render(<TimelineFeed taskId="task-1" items={items} running={false}
    sendMessage={vi.fn()} onChanged={vi.fn()} />);
  const text = container.textContent ?? "";
  expect(text.indexOf("前置说明")).toBeLessThan(text.indexOf("证明文件"));
  expect(text.indexOf("证明文件")).toBeLessThan(text.indexOf("后置说明"));
  expect(screen.getByText("请查收完整证明文件。")).toBeTruthy();
});

test("只有时间线出现新内容才贴底，重渲染本身不滚动", () => {
  const items: TimelineItem[] = [
    { item_id: "text-1", kind: "text", role: "assistant", run_id: "run-1", text: "前置说明", created_at: "2026-09-14T00:00:00Z" },
  ];
  const props = { taskId: "task-1", running: false, sendMessage: vi.fn(), onChanged: vi.fn() };
  const { rerender } = render(<TimelineFeed {...props} items={items} />);
  expect(scrollIntoView).toHaveBeenCalledTimes(1);

  // 任务列表轮询、输入框打字都会让父组件重渲染，但时间线没变，不该动滚动位置。
  rerender(<TimelineFeed {...props} items={items} />);
  expect(scrollIntoView).toHaveBeenCalledTimes(1);

  rerender(<TimelineFeed {...props} items={[...items, {
    item_id: "text-2", kind: "text", role: "assistant", run_id: "run-1",
    text: "后置说明", created_at: "2026-09-14T00:00:01Z",
  }]} />);
  expect(scrollIntoView).toHaveBeenCalledTimes(2);
});

test("末条文字流式增长时继续贴底", () => {
  const props = { taskId: "task-1", running: false, sendMessage: vi.fn(), onChanged: vi.fn() };
  const partial: TimelineItem = {
    item_id: "text-1", kind: "text", role: "assistant", run_id: "run-1",
    text: "邮件草稿", created_at: "2026-09-14T00:00:00Z",
  };
  const { rerender } = render(<TimelineFeed {...props} items={[partial]} />);
  rerender(<TimelineFeed {...props} items={[{ ...partial, text: "邮件草稿已准备好" }]} />);
  expect(scrollIntoView).toHaveBeenCalledTimes(2);
});

const answer = (id: string, text: string, createdAt: string): TimelineItem =>
  ({ item_id: id, kind: "text", role: "assistant", run_id: "run-1", text, created_at: createdAt });

test("连续几条回答只在末尾留一处落款，复制拿到整段而不是最后一截", async () => {
  const writeText = vi.fn(async () => {});
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  const items: TimelineItem[] = [
    { item_id: "ask", kind: "text", role: "user", run_id: "run-1", text: "帮我排日程", created_at: "2026-09-14T00:00:00Z" },
    answer("text-1", "先查冲突", "2026-09-14T00:00:01Z"),
    answer("text-2", "没有冲突，已创建", "2026-09-14T00:00:02Z"),
  ];
  render(<TimelineFeed taskId="task-1" items={items} running={false} sendMessage={vi.fn()} onChanged={vi.fn()} />);

  const copy = screen.getByRole("button", { name: "复制回答" });
  await userEvent.click(copy);
  expect(writeText).toHaveBeenCalledWith("先查冲突\n\n没有冲突，已创建");
  expect(await screen.findByRole("button", { name: "已复制" })).toBeTruthy();
});

test("回答还在流式输出时不挂落款，避免复制到半截文字", () => {
  const items = [answer("text-1", "正在查冲突", "2026-09-14T00:00:01Z")];
  const { rerender } = render(<TimelineFeed taskId="task-1" items={items} running={true}
    sendMessage={vi.fn()} onChanged={vi.fn()} />);
  expect(screen.queryByRole("button", { name: "复制回答" })).toBeNull();

  rerender(<TimelineFeed taskId="task-1" items={items} running={false}
    sendMessage={vi.fn()} onChanged={vi.fn()} />);
  expect(screen.getByRole("button", { name: "复制回答" })).toBeTruthy();
});

test("用户消息与邮件卡不挂落款", () => {
  const items: TimelineItem[] = [
    { item_id: "ask", kind: "text", role: "user", run_id: "run-1", text: "帮我排日程", created_at: "2026-09-14T00:00:00Z" },
    draft,
  ];
  render(<TimelineFeed taskId="task-1" items={items} running={false} sendMessage={vi.fn()} onChanged={vi.fn()} />);
  expect(screen.queryByRole("button", { name: "复制回答" })).toBeNull();
});

test("只在最后一轮已中断的用户消息右下方显示重试", async () => {
  const retryMessage = vi.fn(async () => null);
  const items: TimelineItem[] = [
    { item_id: "old", kind: "text", role: "user", run_id: "run-old", text: "旧问题", created_at: "2026-09-14T00:00:00Z" },
    { item_id: "latest", kind: "text", role: "user", run_id: "run-latest", text: "最后问题", created_at: "2026-09-14T00:00:01Z" },
  ];
  render(<TimelineFeed taskId="task-1" items={items} running={false} retryRunId="run-latest"
    retryMessage={retryMessage} sendMessage={vi.fn()} onChanged={vi.fn()} />);

  const retry = screen.getByRole("button", { name: "重试这条消息" });
  expect(retry.textContent).toBe("");
  expect(retry.closest(".msg")?.textContent).toContain("最后问题");
  await userEvent.click(retry);
  expect(retryMessage).toHaveBeenCalledTimes(1);
});

test("重试进行中禁用按钮且不在非中断消息上显示", () => {
  const item: TimelineItem = {
    item_id: "latest", kind: "text", role: "user", run_id: "run-latest",
    text: "最后问题", created_at: "2026-09-14T00:00:01Z",
  };
  const props = { taskId: "task-1", items: [item], running: false,
    retryMessage: vi.fn(async () => null), sendMessage: vi.fn(), onChanged: vi.fn() };
  const { rerender } = render(<TimelineFeed {...props} retryRunId="run-latest" retrying />);
  expect(screen.getByRole("button", { name: "正在重试这条消息" }).hasAttribute("disabled")).toBe(true);

  rerender(<TimelineFeed {...props} retryRunId={null} />);
  expect(screen.queryByRole("button", { name: "重试这条消息" })).toBeNull();
});

test("附件单独消息刷新后仍按顺序显示图片与下载文件", () => {
  const items: TimelineItem[] = [{
    item_id: "ask", kind: "text", role: "user", run_id: "run-1", text: "",
    created_at: "2026-09-14T00:00:00Z", attachments: [
      { file_id: "image", filename: "photo.png", mime_type: "image/png", size: 12,
        sha256: "a", url: "/api/tasks/t/attachments/image" },
      { file_id: "text", filename: "notes.md", mime_type: "text/markdown", size: 1024,
        sha256: "b", url: "/api/tasks/t/attachments/text" },
    ],
  }];
  render(<TimelineFeed taskId="task-1" items={items} running={false}
    sendMessage={vi.fn()} onChanged={vi.fn()} />);

  expect(screen.getByRole("link", { name: "查看图片 photo.png" }).getAttribute("href"))
    .toBe("/api/tasks/t/attachments/image");
  const download = screen.getByText("notes.md").closest("a");
  expect(download?.getAttribute("href")).toBe("/api/tasks/t/attachments/text");
  expect(download?.hasAttribute("download")).toBe(true);
});

test("从搜索结果跳进来时滚到命中条目并高亮，之后的新内容不再拽回底部", () => {
  const items: TimelineItem[] = [
    { item_id: "text-1", kind: "text", role: "user", run_id: "run-1", text: "预算怎么定", created_at: "2026-09-14T00:00:00Z" },
    { item_id: "text-2", kind: "text", role: "assistant", run_id: "run-1", text: "提高一成", created_at: "2026-09-14T00:00:01Z" },
    draft,
  ];
  const props = { taskId: "task-1", running: false, sendMessage: vi.fn(), onChanged: vi.fn() };
  const { container, rerender } = render(<TimelineFeed {...props} items={items} focusItemId="text-2" />);

  const target = container.querySelector("#item-text-2");
  expect(target?.getAttribute("data-focus")).toBe("true");
  expect(scrollIntoView).toHaveBeenCalledTimes(1);
  expect(scrollIntoView.mock.contexts[0]).toBe(target);
  // 邮件卡也有可定位的锚点
  expect(container.querySelector("#item-card-1")).toBeTruthy();

  rerender(<TimelineFeed {...props} focusItemId="text-2" items={[...items, {
    item_id: "text-3", kind: "text", role: "assistant", run_id: "run-2",
    text: "新回答", created_at: "2026-09-14T00:00:02Z",
  }]} />);
  expect(scrollIntoView).toHaveBeenCalledTimes(1);
});

test("处理中显示当前步骤；没有步骤时只有跳动的点", () => {
  const props = { taskId: "task-1", items: [] as TimelineItem[], sendMessage: vi.fn(), onChanged: vi.fn() };
  const { rerender } = render(<TimelineFeed {...props} running activity={null} />);
  expect(screen.getByRole("status").textContent).toBe("Agent 正在处理");

  rerender(<TimelineFeed {...props} running activity="正在检索资料：星云验收" />);
  expect(screen.getByRole("status").textContent).toBe("正在检索资料：星云验收");

  rerender(<TimelineFeed {...props} running={false} activity="正在检索资料：星云验收" />);
  expect(screen.queryByRole("status")).toBeNull();
});

test("后台记忆整理提示带查看记忆入口，其他提示不带", () => {
  const notice = (item_id: string, text: string): TimelineItem => ({
    item_id, kind: "notice", run_id: "run-1", text, created_at: "2026-09-14T00:00:00Z",
  });
  const items = [
    notice("n-1", "已整理记忆"),
    notice("n-2", "已修改资料：周会纪要。位置：kb/inbox/周会.md。版本：a → b"),
  ];
  render(<MemoryRouter><TimelineFeed taskId="task-1" items={items} running={false}
    sendMessage={vi.fn()} onChanged={vi.fn()} /></MemoryRouter>);

  const links = screen.getAllByRole("link", { name: "查看记忆" });
  expect(links).toHaveLength(1);
  expect(links.every((link) => link.getAttribute("href") === "/memory")).toBe(true);
});

const toolCall = (item_id: string, name: string, status: "ok" | "error"): Extract<TimelineItem, { kind: "tool" }> => ({
  item_id, kind: "tool", run_id: "run-1", tool_call_id: "3f2a19c0" + item_id, name,
  arguments: { skill_id: "weekly-report" }, status,
  result: "# 周报整理\n\n适用场景：每周五汇总本周日程与邮件。",
  created_at: "2026-09-24T14:32:00Z",
});

test("工具分隔的一轮回答只在结束后出现一个复制按钮", async () => {
  const writeText = vi.fn(async () => {});
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  const items: TimelineItem[] = [
    answer("before-tool", "先读取技能", "2026-09-24T14:31:00Z"),
    toolCall("tool-1", "skill_view", "ok"),
    answer("after-tool", "按技能完成周报", "2026-09-24T14:33:00Z"),
  ];
  const props = { taskId: "task-1", items, activeRunId: "run-1",
    sendMessage: vi.fn(), onChanged: vi.fn() };
  const { rerender } = render(<TimelineFeed {...props} running={true} />);
  expect(screen.queryByRole("button", { name: "复制回答" })).toBeNull();

  rerender(<TimelineFeed {...props} running={false} />);
  const buttons = screen.getAllByRole("button", { name: "复制回答" });
  expect(buttons).toHaveLength(1);
  await userEvent.click(buttons[0]);
  // 复制的是收起后仍可见的回答，不把折叠起来的过程叙述混进正文。
  expect(writeText).toHaveBeenCalledWith("按技能完成周报");
});

test("工具调用渲染为可折叠细行，展开显示完整参数与返回", async () => {
  const items: TimelineItem[] = [
    { item_id: "ask", kind: "text", role: "user", run_id: "run-1", text: "整理周报", created_at: "2026-09-24T14:31:00Z" },
    toolCall("tool-1", "skill_view", "ok"),
    toolCall("tool-2", "calendar_query", "error"),
    answer("text-1", "本周有 3 个日程", "2026-09-24T14:33:00Z"),
  ];
  render(<TimelineFeed taskId="task-1" items={items} running={false}
    sendMessage={vi.fn()} onChanged={vi.fn()} />);

  // 已结束的轮默认折叠，先展开已思考再检查工具行。
  await userEvent.click(screen.getByRole("button", { name: /^已思考/ }));
  // 已识别工具用动作与对象，未知工具回退原名和短参数。
  expect(screen.getByText("已读取技能")).toBeTruthy();
  expect(screen.getByText("「weekly-report」")).toBeTruthy();
  expect(screen.getByText("calendar_query")).toBeTruthy();
  expect(screen.getByText("失败")).toBeTruthy();
  expect(screen.queryByText("返回")).toBeNull();

  await userEvent.click(screen.getByRole("button", { name: "展开已读取技能 weekly-report" }));
  expect(screen.getByText("返回")).toBeTruthy();
  expect(screen.getByText("skill_view")).toBeTruthy();
  expect(screen.getByText(/适用场景：每周五汇总本周日程与邮件/)).toBeTruthy();
  expect(screen.getByText("weekly-report")).toBeTruthy();

  await userEvent.click(screen.getByRole("button", { name: "收起已读取技能 weekly-report" }));
  expect(screen.queryByText("返回")).toBeNull();
});

test("未完成工具在运行中和中断后给出不同说明", async () => {
  const pending: TimelineItem = {
    ...toolCall("tool-pending", "WebFetch", "ok"), status: "running", result: null,
  };
  const props = { taskId: "task-1", items: [pending], activeRunId: "run-1",
    sendMessage: vi.fn(), onChanged: vi.fn() };
  const { rerender } = render(<TimelineFeed {...props} running={true} />);
  expect(screen.getByText("正在读取网页")).toBeTruthy();
  await userEvent.click(screen.getByRole("button", { name: "展开正在读取网页" }));
  expect(screen.getByText("工具仍在运行")).toBeTruthy();

  rerender(<TimelineFeed {...props} running={false} />);
  // 中断后该轮折叠，展开分组才能看到中断语义。
  await userEvent.click(screen.getByRole("button", { name: /^已思考/ }));
  expect(screen.queryByText("正在读取网页")).toBeNull();
  expect(screen.getByText("读取网页")).toBeTruthy();
  expect(screen.getByText("未记录结果")).toBeTruthy();
  expect(screen.getByText("中断时未记录结果")).toBeTruthy();
});

test("依据跳转定位到工具行时自动展开并短暂高亮", async () => {
  const items: TimelineItem[] = [
    { item_id: "ask", kind: "text", role: "user", run_id: "run-1", text: "整理周报", created_at: "2026-09-24T14:31:00Z" },
    toolCall("tool-1", "skill_view", "ok"),
  ];
  const { container } = render(<TimelineFeed taskId="task-1" items={items} running={false}
    focusItemId="tool-1" sendMessage={vi.fn()} onChanged={vi.fn()} />);

  expect(container.querySelector("#item-tool-1")?.getAttribute("data-focus")).toBe("true");
  // 折叠条目要先展开分组再滚动，滚动排在下一帧。
  await waitFor(() => expect(scrollIntoView).toHaveBeenCalledTimes(1));
  expect(scrollIntoView.mock.contexts[0]).toBe(container.querySelector("#item-tool-1"));
  // 跳转落点自动展开，完整调用一眼可见。
  expect(screen.getByText("返回")).toBeTruthy();
});

test("末条是工具行时贴底判断不崩溃，新增内容照常滚动", () => {
  const items: TimelineItem[] = [
    { item_id: "ask", kind: "text", role: "user", run_id: "run-1", text: "整理周报", created_at: "2026-09-24T14:31:00Z" },
    toolCall("tool-1", "skill_view", "ok"),
  ];
  const props = { taskId: "task-1", running: false, sendMessage: vi.fn(), onChanged: vi.fn() };
  const { rerender } = render(<TimelineFeed {...props} items={items} />);
  expect(scrollIntoView).toHaveBeenCalledTimes(1);

  rerender(<TimelineFeed {...props} items={[...items,
    answer("text-1", "本周有 3 个日程", "2026-09-24T14:33:00Z"),
  ]} />);
  expect(scrollIntoView).toHaveBeenCalledTimes(2);
});

test("已结束的轮折叠为用时分组，展开还原过程行", async () => {
  const observed = observation({
    duration_ms: 8200, duration_api_ms: null, num_turns: 2, is_error: false, usage: [],
  });
  const items: TimelineItem[] = [
    { item_id: "ask", kind: "text", role: "user", run_id: "run-1", text: "整理周报", created_at: "2026-09-24T10:00:00Z" },
    toolCall("tool-1", "skill_view", "ok"),
    answer("text-1", "按技能完成周报", "2026-09-24T14:33:00Z"),
  ];
  render(<TimelineFeed taskId="task-1" items={items} observations={[observed]}
    running={false} sendMessage={vi.fn()} onChanged={vi.fn()} />);
  // 默认折叠：回答可见，工具行收在分组里。
  expect(screen.getByText("按技能完成周报")).toBeTruthy();
  expect(screen.queryByText("已读取技能")).toBeNull();
  // 折叠头只报用时，材料与用量不再单独成卡片。
  const header = screen.getByRole("button", { name: "已思考 8.2 秒" });
  expect(header).toBeTruthy();

  await userEvent.click(header);
  expect(screen.getByText("已读取技能")).toBeTruthy();
  expect(screen.queryByText("材料")).toBeNull();
  expect(screen.queryByText("用量")).toBeNull();
  // 工具行本身仍可继续展开：骨架行给出动作，展开后能看到完整参数与返回。
  expect(screen.getByRole("button", { name: "展开已读取技能 weekly-report" })).toBeTruthy();
});

test("进行中的轮实时显示过程，结束折叠为分组", async () => {
  const observed: RunObservation = {
    run_id: "run-1",
    kind: "message",
    status: "done",
    model: "auto",
    created_at: "2026-09-24T10:00:00Z",
    started_at: "2026-09-24T10:00:01Z",
    finished_at: "2026-09-24T10:00:09Z",
    materials: { assembled: [], skipped: [] },
    sdk_result: null,
    usage_totals: { input_tokens: null, output_tokens: null, credits: null },
    context_before: null,
    context_after: null,
    steps: [{
      step_id: "s1", kind: "tool", code: "skill_view", status: "running",
      started_at: "2026-09-24T10:00:02Z", ended_at: null,
      item_id: "tool-1", tool_call_id: "c1", detail: { source: "mcp" },
    }],
  };
  const items: TimelineItem[] = [
    { item_id: "ask", kind: "text", role: "user", run_id: "run-1", text: "整理周报", created_at: "2026-09-24T10:00:00Z" },
    toolCall("tool-1", "skill_view", "ok"),
  ];
  const props = { taskId: "task-1", items, observations: [observed], activeRunId: "run-1",
    sendMessage: vi.fn(), onChanged: vi.fn() };
  const { rerender } = render(<TimelineFeed {...props} running={true} />);
  // 运行中：过程实时可见，没有折叠组头。
  expect(screen.getByText("已读取技能")).toBeTruthy();
  expect(screen.queryByRole("button", { name: /^已思考/ })).toBeNull();

  rerender(<TimelineFeed {...props} running={false} />);
  // 结束后折叠：工具行收进分组，只留用户消息。
  expect(screen.queryByText("已读取技能")).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: /^已思考/ }));
  // 展开后工具行按时间顺序还原。
  expect(screen.getByText("已读取技能")).toBeTruthy();
});

test("收起时只留用户消息、卡片与最终回答，叙述与工具行都在分组里", async () => {
  const observed: RunObservation = {
    run_id: "run-1",
    kind: "message",
    status: "done",
    model: "auto",
    created_at: "2026-09-24T10:00:00Z",
    started_at: "2026-09-24T10:00:01Z",
    finished_at: "2026-09-24T10:00:09Z",
    materials: { assembled: [], skipped: [] },
    sdk_result: {
      duration_ms: 8200, duration_api_ms: null, num_turns: 2, is_error: false,
      usage: [],
    },
    usage_totals: { input_tokens: null, output_tokens: null, credits: null },
    context_before: null,
    context_after: null,
    steps: [],
  };
  const items: TimelineItem[] = [
    { item_id: "ask", kind: "text", role: "user", run_id: "run-1", text: "整理周报并发邮件", created_at: "2026-09-24T10:00:00Z" },
    answer("narr-1", "我先读取技能说明", "2026-09-24T10:00:01Z"),
    toolCall("tool-1", "skill_view", "ok"),
    draft,
    answer("final-1", "周报已起草好，等你确认", "2026-09-24T10:02:00Z"),
  ];
  render(<TimelineFeed taskId="task-1" items={items} observations={[observed]} running={false}
    sendMessage={vi.fn()} onChanged={vi.fn()} />);
  expect(screen.getByText("整理周报并发邮件")).toBeTruthy();
  expect(screen.getByText("周报已起草好，等你确认")).toBeTruthy();
  expect(screen.getByText("证明文件")).toBeTruthy();
  expect(screen.queryByText("我先读取技能说明")).toBeNull();
  expect(screen.queryByText("已读取技能")).toBeNull();

  await userEvent.click(screen.getByRole("button", { name: /^已思考/ }));
  expect(screen.getByText("我先读取技能说明")).toBeTruthy();
  expect(screen.getAllByText("已读取技能").length).toBeGreaterThan(0);
});

test("过程叙述与正式回答分开渲染：前者带过程标记，只有后者按回答样式", async () => {
  const items: TimelineItem[] = [
    { item_id: "ask", kind: "text", role: "user", run_id: "run-1", text: "整理周报", created_at: "2026-09-24T14:31:00Z" },
    answer("narr-1", "我来查一下最近一周的动态。", "2026-09-24T14:31:30Z"),
    toolCall("tool-1", "skill_view", "ok"),
    answer("narr-2", "我再抓几个具体来源核实。", "2026-09-24T14:32:00Z"),
    toolCall("tool-2", "skill_view", "ok"),
    answer("final-1", "本周有 3 个日程", "2026-09-24T14:33:00Z"),
  ];
  const { container } = render(<TimelineFeed taskId="task-1" items={items} running={false}
    sendMessage={vi.fn()} onChanged={vi.fn()} />);

  // 运行结束后叙述收进折叠分组，展开才回到过程区。过程标记挂在外层条目包装上，
  // 消息本身也带 .process，两处都要能看出来。
  await userEvent.click(screen.getByRole("button", { name: /^已思考/ }));
  const narrationRow = container.querySelector("#item-narr-1");
  expect(narrationRow?.classList.contains("process")).toBe(true);
  const narration = screen.getByText("我来查一下最近一周的动态。").closest(".msg");
  expect(narration?.classList.contains("process")).toBe(true);
  expect(screen.getByText("我再抓几个具体来源核实。").closest(".msg")?.classList.contains("process"))
    .toBe(true);
  // 正式回答留在分组外，也不能带上过程样式：样式不同正是判断“哪段是答案”的依据。
  const finalRow = container.querySelector("#item-final-1");
  expect(finalRow?.classList.contains("process")).toBe(false);
  const final = finalRow?.querySelector(".msg");
  expect(final?.classList.contains("process")).toBe(false);
  expect(within(final as HTMLElement).getByText("本周有 3 个日程")).toBeTruthy();
});

test("没有观测数据的轮次仍折叠过程，折叠头只显示「已思考」", async () => {
  const items: TimelineItem[] = [
    answer("narr-1", "我先查一下", "2026-09-24T14:31:00Z"),
    toolCall("tool-1", "skill_view", "ok"),
    answer("text-1", "没有观测", "2026-09-24T14:33:00Z"),
  ];
  render(<TimelineFeed taskId="task-1" items={items} observations={[]} running={false}
    sendMessage={vi.fn()} onChanged={vi.fn()} />);
  expect(screen.queryByText("我先查一下")).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "已思考 未记录" }));
  expect(screen.getByText("我先查一下")).toBeTruthy();
});
