import { expect, test } from "vitest";

import { freeName } from "./KbDialogs";

test("目标文件夹重名时依次编号，大小写不同也算重名", () => {
  expect(freeName("DVM.md", [])).toBe("DVM.md");
  expect(freeName("DVM.md", ["dvm.md", "DVM 2.md"])).toBe("DVM 3.md");
});
