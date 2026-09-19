// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { useState } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import SkillPicker from "./SkillPicker";
import { emptySelection } from "./api";
afterEach(() => {cleanup(); vi.restoreAllMocks();});
it("pins selected revisions and excludes removed skills", async () => {
 vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify([{id:"sk_one",name:"整理",description:"整理步骤",status:"approved",content_hash:"sha256:one"},{id:"sk_off",name:"停用项",status:"disabled"}]), {headers:{"Content-Type":"application/json"}}));
 function Harness(){const [value,set] = useState(emptySelection);return <><SkillPicker value={value} onChange={set}/><output>{JSON.stringify(value)}</output></>;}
 render(<MemoryRouter><Harness/></MemoryRouter>);
 fireEvent.click(screen.getByText("添加 Skill"));
 await waitFor(() => expect(screen.getByText("整理")).toBeTruthy());
 expect(screen.queryByText("停用项")).toBeNull();
 fireEvent.click(screen.getByRole("checkbox", {name:/整理/}));
 expect(screen.getByRole("status").textContent).toContain("sha256:one");
 fireEvent.click(screen.getByText("整理 ×"));
 expect(screen.getByRole("status").textContent).toContain('"excluded_skill_ids":["sk_one"]');
});
