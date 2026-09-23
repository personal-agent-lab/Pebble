import { useState } from "react";

import type { SkillUsageRecord } from "../api";
import { shortTime } from "../status";

/**
 * 任务页的技能加载面板：正文进入过上下文的技能与来源（手动装配或模型主动读取）。
 * 只反映装配事实，被加载不等于帮助任务成功。
 */
export default function SkillUsage({ usage }: { usage: SkillUsageRecord[] }) {
  const [open, setOpen] = useState(false);
  if (usage.length === 0) return null;
  // 同一技能同一轮只留最近一次：面板回答“这轮用了哪些技能”，不逐条罗列重复加载。
  const latest = new Map<string, SkillUsageRecord>();
  for (const record of usage) latest.set(`${record.run_id}:${record.skill_id}`, record);
  const rows = [...latest.values()];

  return (
    <div className="skill-usage">
      <button type="button" className="skill-usage-toggle" aria-expanded={open}
        onClick={() => setOpen((value) => !value)}>
        已加载技能 {rows.length} 个
      </button>
      {open && (
        <ul className="skill-usage-list">
          {rows.map((record) => (
            <li key={`${record.run_id}:${record.skill_id}`}>
              <span className="skill-usage-id">{record.skill_id}</span>
              <span className={`skill-usage-source ${record.source}`}>
                {record.source === "manual" ? "手动装配" : "自动读取"}
              </span>
              <span className="skill-usage-time">{shortTime(record.loaded_at)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
