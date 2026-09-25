import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { listSkills, type Selection, type Skill } from "./api";
export default function SkillPicker({value, onChange, open, onClose, disabled = false}: {value: Selection; onChange: (s: Selection) => void; open: boolean; onClose: () => void; disabled?: boolean}) {
  const [items, setItems] = useState<Skill[]>([]), [query, setQuery] = useState(""), [error, setError] = useState("");
  useEffect(() => { if (open) void listSkills().then(setItems).catch(e => setError(String(e))); }, [open]);
  const toggle = (s: Skill) => {
    const selected = value.skills.some(r => r.id === s.id);
    onChange({...value, skills: selected ? value.skills.filter(r => r.id !== s.id) : [...value.skills, {id: s.id, revision: s.content_hash}], excluded_skill_ids: selected ? [...new Set([...value.excluded_skill_ids, s.id])] : value.excluded_skill_ids.filter(id => id !== s.id)});
  };
  if (!open && value.skills.length === 0) return null;
  return <div className="skill-picker">
    {value.skills.map(ref => <button type="button" className="btn-secondary" disabled={disabled} key={ref.id} onClick={() => toggle(items.find(s => s.id === ref.id) ?? {id: ref.id} as Skill)}>{items.find(s => s.id === ref.id)?.name ?? ref.id} ×</button>)}
    {open && <div className="skill-panel"><div className="skill-panel-head"><strong>选择 Skill</strong><button type="button" onClick={onClose} aria-label="关闭 Skill 选择器">×</button></div><input aria-label="搜索 Skill" placeholder="搜索名称或描述" value={query} onChange={e => setQuery(e.target.value)} />
      {error && <p role="alert">{error}</p>}
      {items.filter(s => s.status === "approved" && `${s.name} ${s.description}`.toLowerCase().includes(query.toLowerCase())).map(s => <label key={s.id}><input type="checkbox" disabled={disabled} checked={value.skills.some(r => r.id === s.id)} onChange={() => toggle(s)} />{s.name}<small>{s.description}</small></label>)}
      <label><input type="checkbox" disabled={disabled} checked={value.auto_match_skills} onChange={e => onChange({...value, auto_match_skills: e.target.checked})} />允许自动匹配</label><Link to="/skills">管理 / 新建 Skill</Link>
    </div>}
  </div>;
}
