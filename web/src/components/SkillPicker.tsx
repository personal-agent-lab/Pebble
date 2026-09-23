import { useEffect, useRef, useState } from "react";

import type { SkillSelection, SkillSummary } from "../api";

type Props = {
  skills: SkillSummary[];
  selection: SkillSelection;
  onChange: (selection: SkillSelection) => void;
  disabled?: boolean;
};

/**
 * 随消息提交的技能选择：每个技能三态（默认 / 手动加入正文 / 本轮排除），
 * 外加自动匹配开关。手动项只带标识，内容版本由服务端装配时绑定。
 */
export default function SkillPicker({ skills, selection, onChange, disabled = false }: Props) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (root.current !== null && !root.current.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const picked = selection.skills.length;
  const excluded = selection.excluded_skill_ids.length;
  const touched = picked > 0 || excluded > 0 || !selection.auto_match;

  const included = (id: string) => selection.skills.some((item) => item.id === id);
  const banned = (id: string) => selection.excluded_skill_ids.includes(id);

  const toggleInclude = (id: string) => {
    if (included(id)) {
      onChange({ ...selection, skills: selection.skills.filter((item) => item.id !== id) });
      return;
    }
    onChange({
      ...selection,
      skills: [...selection.skills, { id }],
      excluded_skill_ids: selection.excluded_skill_ids.filter((item) => item !== id),
    });
  };

  const toggleExclude = (id: string) => {
    if (banned(id)) {
      onChange({ ...selection, excluded_skill_ids: selection.excluded_skill_ids.filter((item) => item !== id) });
      return;
    }
    onChange({
      ...selection,
      excluded_skill_ids: [...selection.excluded_skill_ids, id],
      skills: selection.skills.filter((item) => item.id !== id),
    });
  };

  return (
    <div className="skill-picker" ref={root}>
      <button type="button" className={`composer-skill-toggle${touched ? " on" : ""}`}
        aria-haspopup="menu" aria-expanded={open} disabled={disabled}
        aria-label="选择技能"
        title="选择随本条消息装配的技能"
        onClick={() => setOpen((value) => !value)}>
        技能{picked > 0 && <span className="skill-picker-count">{picked}</span>}
      </button>
      {open && (
        <div className="skill-picker-menu" role="menu" aria-label="技能选择">
          {skills.length === 0 && (
            <div className="skill-picker-empty">还没有启用中的技能，可在“技能”页创建</div>
          )}
          {skills.map((skill) => (
            <div className="skill-picker-row" key={skill.skill_id}>
              <div className="skill-picker-copy">
                <strong title={skill.skill_id}>{skill.name}</strong>
                <span>{skill.description}</span>
              </div>
              <div className="skill-picker-actions">
                <button type="button" role="menuitemcheckbox" aria-checked={included(skill.skill_id)}
                  className={included(skill.skill_id) ? "on" : ""}
                  disabled={disabled}
                  onClick={() => toggleInclude(skill.skill_id)}>加入</button>
                <button type="button" role="menuitemcheckbox" aria-checked={banned(skill.skill_id)}
                  className={banned(skill.skill_id) ? "on danger" : ""}
                  disabled={disabled}
                  onClick={() => toggleExclude(skill.skill_id)}>排除</button>
              </div>
            </div>
          ))}
          <div className="skill-picker-foot">
            <label className="skill-picker-auto">
              <input type="checkbox" checked={selection.auto_match} disabled={disabled}
                onChange={(event) => onChange({ ...selection, auto_match: event.target.checked })} />
              自动匹配
            </label>
            {touched && (
              <button type="button" className="skill-picker-reset" disabled={disabled}
                onClick={() => onChange({ skills: [], excluded_skill_ids: [], auto_match: true })}>
                重置
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
