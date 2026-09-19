import { useEffect, useState } from "react";
import { request } from "../../api";
type Usage = {run_id: string; skill_id: string; revision: string; source: string};
export default function SkillUsage({taskId, refresh}: {taskId: string; refresh: unknown}) {
 const [items,setItems] = useState<Usage[]>([]);
 useEffect(() => {let active = true; void request<Usage[]>(`/tasks/${taskId}/skill-usage`).then(v => {if(active) setItems(v);}).catch(() => {}); return () => {active=false;};}, [taskId,refresh]);
 return items.length ? <details className="skill-panel"><summary>已加载的 Skills（{items.length}）</summary>{items.map(s => <p key={`${s.run_id}-${s.skill_id}`}><strong>{s.skill_id}</strong> · {s.source === "manual" ? "用户选择" : "自动加载"}<br/><small>版本 {s.revision}</small></p>)}</details> : null;
}
