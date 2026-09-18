// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";

import Markdown from "./Markdown";

afterEach(cleanup);

test("行内与块级公式排成 KaTeX，金额里的 $ 保持文字", () => {
  const { container } = render(
    <Markdown text={"距离 $\\rho_i = c\\,t$，费用从 $5 到 $10。\n\n$$\nE = mc^2\n$$"} />,
  );
  expect(container.querySelectorAll(".katex")).toHaveLength(2);
  expect(container.querySelectorAll(".katex-display")).toHaveLength(1);
  expect(container.textContent).toContain("费用从 $5 到 $10。");
});

test("公式里的 \\href 不生成链接", () => {
  const { container } = render(<Markdown text={"$\\href{javascript:alert(1)}{x}$"} />);
  expect(container.querySelector("a")).toBeNull();
});
