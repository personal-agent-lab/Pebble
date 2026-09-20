import { CaretDown, Check } from "@phosphor-icons/react";
import { useEffect, useId, useRef, useState } from "react";

import type { ModelEntry } from "../api";

type Props = {
  value: string;
  models: ModelEntry[];
  disabled?: boolean;
  onChange: (model: string) => void;
};

const MENU_MAX_HEIGHT = 320;
const OPTION_HEIGHT = 34;
const GAP = 12;

function visibleBounds(node: HTMLElement): [number, number] {
  let top = 0;
  let bottom = window.innerHeight;
  for (let parent = node.parentElement; parent !== null; parent = parent.parentElement) {
    if (getComputedStyle(parent).overflowY === "visible") continue;
    const rect = parent.getBoundingClientRect();
    top = Math.max(top, rect.top);
    bottom = Math.min(bottom, rect.bottom);
  }
  return [top, bottom];
}

export default function ModelPicker({ value, models, disabled = false, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const [upward, setUpward] = useState(false);
  const [active, setActive] = useState(0);
  const [maxHeight, setMaxHeight] = useState(MENU_MAX_HEIGHT);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const listId = useId();
  const current = models.find((entry) => entry.id === value);

  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    list.current?.focus();
    return () => document.removeEventListener("pointerdown", close);
  }, [open]);

  useEffect(() => {
    if (open) list.current?.children[active]?.scrollIntoView?.({ block: "nearest" });
  }, [open, active]);

  const show = () => {
    const node = trigger.current;
    if (disabled || models.length === 0 || node === null) return;
    // 菜单会被最近的滚动容器裁掉：按它与视口的可见交集，选空间更大的一侧并限制高度。
    const rect = node.getBoundingClientRect();
    const [top, bottom] = visibleBounds(node);
    const below = bottom - rect.bottom - GAP;
    const above = rect.top - top - GAP;
    const needed = Math.min(models.length * OPTION_HEIGHT + 14, MENU_MAX_HEIGHT);
    const up = below < needed && above > below;
    setUpward(up);
    setMaxHeight(Math.max(120, Math.min(MENU_MAX_HEIGHT, up ? above : below)));
    setActive(Math.max(0, models.findIndex((entry) => entry.id === value)));
    setOpen(true);
  };

  const choose = (index: number) => {
    const entry = models[index];
    if (entry !== undefined) onChange(entry.id);
    setOpen(false);
    trigger.current?.focus();
  };

  return <div className="model-picker" ref={root}>
    <button ref={trigger} type="button" className="model-trigger" aria-label="选择模型"
      aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? listId : undefined}
      disabled={disabled || models.length === 0}
      onClick={() => (open ? setOpen(false) : show())}
      onKeyDown={(event) => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); show(); }
      }}>
      <span>{current?.label ?? (models.length === 0 ? "加载模型…" : value)}</span>
      <CaretDown size={12} weight="bold" />
    </button>

    {open && <ul ref={list} id={listId} role="listbox" tabIndex={-1} aria-label="模型"
      className={`model-menu${upward ? " upward" : ""}`} style={{ maxHeight }}
      aria-activedescendant={`${listId}-${active}`}
      onKeyDown={(event) => {
        if (event.key === "ArrowDown") { event.preventDefault(); setActive((index) => Math.min(index + 1, models.length - 1)); }
        else if (event.key === "ArrowUp") { event.preventDefault(); setActive((index) => Math.max(index - 1, 0)); }
        else if (event.key === "Home") { event.preventDefault(); setActive(0); }
        else if (event.key === "End") { event.preventDefault(); setActive(models.length - 1); }
        else if (event.key === "Enter" || event.key === " ") { event.preventDefault(); choose(active); }
        else if (event.key === "Escape" || event.key === "Tab") { setOpen(false); trigger.current?.focus(); }
      }}>
      {models.map((entry, index) => {
        const selected = entry.id === value;
        return <li key={entry.id} id={`${listId}-${index}`} role="option" aria-selected={selected}
          className={`model-option${index === active ? " active" : ""}`}
          onPointerMove={() => setActive(index)} onClick={() => choose(index)}>
          <span className="model-option-label">{entry.label}</span>
          {entry.kind === "custom" && <span className="model-option-kind">自定义</span>}
          <span className="model-option-check">{selected && <Check size={14} weight="bold" />}</span>
        </li>;
      })}
    </ul>}
  </div>;
}
