// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

const memoryModule = vi.hoisted(() => {
  let release!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  return { pending, release };
});

vi.mock("./api", async (importOriginal) => ({
  ...await importOriginal<typeof import("./api")>(),
  listTasks: () => Promise.resolve([]),
}));

vi.mock("./pages/MemoryPage", async () => {
  await memoryModule.pending;
  return { default: () => <h1>记忆内容</h1> };
});

const App = (await import("./App")).default;

afterEach(cleanup);

test("首次加载记忆页时保留导航外框", async () => {
  render(<MemoryRouter initialEntries={["/memory"]}><App /></MemoryRouter>);

  expect(document.querySelector(".main .loading")?.textContent).toBe("读取中…");
  expect(document.querySelector(".sidebar .brand-name")?.textContent).toBe("Pebble");

  memoryModule.release();
  expect(await screen.findByRole("heading", { name: "记忆内容" })).toBeTruthy();
});
