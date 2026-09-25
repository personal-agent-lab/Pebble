import { useEffect, useState } from "react";
import AppShell from "../../components/AppShell";
import { action, getSkill, listDrafts, listSkills, restore, saveSkill, versions, type Skill, type Version } from "./api";
const labels: Record<string,string> = {approved: "已启用", draft: "待审核", disabled: "已停用", archived: "已归档"};
export default function SkillsPage() {
  const [items, setItems] = useState<Skill[]>([]), [drafts, setDrafts] = useState<Skill[]>([]), [tab, setTab] = useState("approved"), [query, setQuery] = useState(""), [editing, setEditing] = useState<Partial<Skill> | null>(null), [history, setHistory] = useState<Version[]>([]), [error, setError] = useState(""), [busy, setBusy] = useState(false), [base, setBase] = useState("");
  const reload = async () => { const [s,d] = await Promise.all([listSkills(), listDrafts()]); setItems(s); setDrafts(d); };
  useEffect(() => { void reload().catch(e => setError(String(e))); }, []);
  const run = async (fn: () => Promise<unknown>) => { setBusy(true); setError(""); try { await fn(); await reload(); } catch(e) {setError(String(e));} finally {setBusy(false);} };
  const open = (s: Skill) => void run(async () => {setEditing(s.draft_id ? s : await getSkill(s.id)); setHistory(s.draft_id ? [] : await versions(s.id)); setBase(s.base_revision && s.skill_id ? (await getSkill(s.skill_id)).body : "");});
  const shown = (tab === "draft" ? drafts : items.filter(s => s.status === tab)).filter(s => `${s.name} ${s.description}`.toLowerCase().includes(query.toLowerCase()));
  return <AppShell><div className="skills-page"><header><h1>Skills</h1><p>保存常用流程，在对话中选择或自动按需使用。也可以在完成一项工作后说“把刚才的工作总结为 Skill”，审核草稿后启用。</p><button className="btn" onClick={() => {setEditing({name: "", description: "", body: "", triggers: []}); setHistory([]); setBase("");}}>新建 Skill</button></header>
    <nav className="skill-tabs">{Object.entries(labels).map(([key,label]) => <button className="btn-secondary" aria-pressed={tab === key} key={key} onClick={() => setTab(key)}>{label}{key === "draft" ? ` (${drafts.length})` : ""}</button>)}</nav>
    <input aria-label="搜索 Skills" placeholder="搜索名称或描述" value={query} onChange={e => setQuery(e.target.value)} />
    {error && <p role="alert">{error} <button onClick={() => void run(reload)}>重试</button></p>}
    <div className="skill-grid">{shown.map(s => <button className="skill-card" key={s.draft_id ?? s.id} onClick={() => open(s)}><strong>{s.name}</strong><p>{s.description}</p><small>{labels[tab]} · 查看详情</small></button>)}{shown.length === 0 && <p>这里还没有 Skill。</p>}</div>
    {editing && <section className="skill-editor" aria-label="Skill 编辑器"><h2>{editing.draft_id ? "审核草稿" : editing.id ? "编辑 Skill" : "新建 Skill"}</h2>
      <form onSubmit={e => {e.preventDefault(); void run(async () => {await saveSkill(editing, !!editing.draft_id); setEditing(null);});}}>
        <fieldset disabled={busy}>
        <label>名称<input required value={editing.name ?? ""} onChange={e => setEditing({...editing, name: e.target.value})} /></label>
        <label>描述<input required value={editing.description ?? ""} onChange={e => setEditing({...editing, description: e.target.value})} /></label>
        <label>提示词正文<textarea required rows={12} value={editing.body ?? ""} onChange={e => setEditing({...editing, body: e.target.value})} /></label>
        <label>适用条件（每行一条）<textarea rows={3} value={(editing.triggers ?? []).join("\n")} onChange={e => setEditing({...editing, triggers: e.target.value.split("\n").filter(Boolean)})} /></label>
        {editing.evidence && <p>来源任务：{editing.evidence.tasks.join("、")}</p>}
        {base && <details><summary>查看当前生效正文，与草稿对比</summary><pre>{base}</pre></details>}
        <div className="skill-tabs"><button disabled={busy || editing.status === "archived"} className="btn">{editing.draft_id ? "保存草稿" : "保存并启用"}</button>
        <button type="button" className="btn-secondary" onClick={() => setEditing(null)}>关闭</button>
        {editing.draft_id ? <><button type="button" className="btn-secondary" disabled={busy} onClick={() => void run(async () => {await saveSkill(editing, true); const fresh = (await listDrafts()).find(d => d.draft_id === editing.draft_id); if(fresh) await action(fresh, "approve"); setEditing(null);})}>保存并批准</button><button type="button" className="btn-secondary" disabled={busy} onClick={() => void run(async () => {await action(editing as Skill, "reject"); setEditing(null);})}>驳回</button></> : editing.id && editing.status !== "archived" && <>{[editing.status === "disabled" ? "enable" : "disable", "archive"].map(verb => <button type="button" className="btn-secondary" disabled={busy} key={verb} onClick={() => void run(async () => {await action(editing as Skill, verb); setEditing(null);})}>{verb === "enable" ? "启用" : verb === "disable" ? "停用" : "归档"}</button>)}</>}</div>
        </fieldset>
      </form>
      {history.length > 0 && <details><summary>版本历史与恢复</summary>{history.map((v,i) => <article key={i}><p>{v.created_at}</p><pre>{v.body}</pre><button className="btn-secondary" disabled={busy} onClick={() => void run(async () => {await restore(editing.id!, v.revision); setTab("draft"); setEditing(null);})}>恢复为待审核草稿</button></article>)}</details>}
    </section>}
  </div></AppShell>;
}
