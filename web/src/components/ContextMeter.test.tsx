// @vitest-environment jsdom

import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test } from "vitest";

import type { ContextReading } from "../api";
import ContextMeter, { formatPercentage } from "./ContextMeter";

afterEach(cleanup);

const reading = (overrides: Partial<ContextReading> = {}): ContextReading => ({
  used_percentage: 4.2,
  threshold_percentage: 83.5,
  auto_compact_enabled: false,
  categories: [
    { kind: "system_prompt", percentage: 0.4 },
    { kind: "system_tools", percentage: 0.7 },
    { kind: "messages", percentage: 3.1 },
  ],
  ...overrides,
});

test("标记只显示占用环，读数在无障碍名称与悬停提示里，默认不展开面板", () => {
  render(<ContextMeter reading={reading()} />);
  const trigger = screen.getByRole("button", { name: "上下文已用 4.2%" });
  // 环上不带文字：常驻的比例与「未记录」不占输入框。
  expect(trigger.textContent).toBe("");
  expect(trigger.getAttribute("title")).toBe("上下文已用 4.2%");
  expect(trigger.querySelector(".context-ring-fill")).toBeTruthy();
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("点开只列总占用与各类别占比", async () => {
  render(<ContextMeter reading={reading()} />);
  await userEvent.click(screen.getByRole("button", { name: "上下文已用 4.2%" }));

  const panel = screen.getByRole("dialog", { name: "上下文占用" });
  // 总量一行，之后是各类别；面板不再承载阈值与口径说明。
  expect(within(panel).getByText("上下文已用")).toBeTruthy();
  expect(within(panel).getByText("4.2%")).toBeTruthy();
  expect(within(panel).getByText("系统提示词")).toBeTruthy();
  expect(within(panel).getByText("0.4%")).toBeTruthy();
  expect(within(panel).getByText("工具定义")).toBeTruthy();
  expect(within(panel).getByText("对话消息")).toBeTruthy();
  expect(within(panel).getByText("3.1%")).toBeTruthy();
  expect(panel.querySelectorAll("li")).toHaveLength(3);
  expect(panel.textContent).not.toContain("阈值");
  expect(panel.textContent).not.toContain("token");
});

test("运行时不给分解时只显示占用比例", async () => {
  render(<ContextMeter reading={reading({ categories: [] })} />);
  await userEvent.click(screen.getByRole("button", { name: "上下文已用 4.2%" }));
  expect(screen.getByText("这个运行时不报上下文分解")).toBeTruthy();
});

test("未知类别沿用原始标识，不猜语义", async () => {
  render(<ContextMeter reading={reading({
    categories: [{ kind: "brand_new_kind", percentage: 2 }],
  })} />);
  await userEvent.click(screen.getByRole("button", { name: "上下文已用 4.2%" }));
  expect(screen.getByText("brand_new_kind")).toBeTruthy();
});

test("点面板以外或按 Esc 收起", async () => {
  render(<div>
    <ContextMeter reading={reading()} />
    <button type="button">外面</button>
  </div>);
  const trigger = screen.getByRole("button", { name: "上下文已用 4.2%" });
  await userEvent.click(trigger);
  expect(screen.getByRole("dialog")).toBeTruthy();
  await userEvent.click(screen.getByRole("button", { name: "外面" }));
  expect(screen.queryByRole("dialog")).toBeNull();

  await userEvent.click(trigger);
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(trigger);
});

test("占用达到压缩阈值时环转为告警色", () => {
  const { container } = render(<ContextMeter reading={reading({ used_percentage: 90 })} />);
  expect(container.querySelector(".context-ring-fill.near-limit")).toBeTruthy();
  expect(container.querySelector(".context-trigger-text")).toBeNull();
});

test("环走过的长度与占用比例一致", () => {
  const ring = (percentage: number) => {
    const { container } = render(<ContextMeter reading={reading({ used_percentage: percentage })} />);
    const circle = container.querySelector(".context-ring-fill") as SVGCircleElement;
    const circumference = Number(circle.getAttribute("stroke-dasharray"));
    return Number(circle.getAttribute("stroke-dashoffset")) / circumference;
  };
  // 返回值是「还空着的比例」：0% 全空、50% 半圈、100% 走满。
  expect(ring(0)).toBeCloseTo(1, 5);
  expect(ring(50)).toBeCloseTo(0.5, 5);
  expect(ring(100)).toBeCloseTo(0, 5);
  cleanup();
});

test("没有读数时环是空的，环上也不写未记录", async () => {
  const { container } = render(<ContextMeter reading={null} />);
  const circle = container.querySelector(".context-ring-fill") as SVGCircleElement;
  expect(circle.getAttribute("stroke-dashoffset"))
    .toBe(circle.getAttribute("stroke-dasharray"));

  const trigger = screen.getByRole("button", { name: "上下文已用 未记录" });
  expect(trigger.textContent).toBe("");
  // 没有读数时仍可打开面板。
  await userEvent.click(trigger);
  expect(screen.getByRole("dialog")).toBeTruthy();
});

test("读数变化时标记跟着更新", () => {
  const { rerender } = render(<ContextMeter reading={reading()} />);
  expect(screen.getByRole("button", { name: "上下文已用 4.2%" })).toBeTruthy();
  rerender(<ContextMeter reading={reading({ used_percentage: 11 })} />);
  expect(screen.getByRole("button", { name: "上下文已用 11%" })).toBeTruthy();
});

test("formatPercentage 处理未记录与小数", () => {
  expect(formatPercentage(null)).toBe("未记录");
  expect(formatPercentage(0)).toBe("0%");
  expect(formatPercentage(0.4)).toBe("0.4%");
  expect(formatPercentage(9.9)).toBe("9.9%");
  expect(formatPercentage(10)).toBe("10%");
});
