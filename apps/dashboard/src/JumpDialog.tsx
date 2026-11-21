import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

export type JumpTarget = { key: string; label: string; hint: string; go: () => void };

/** ⌘K dialog: type to filter, ↑↓ to move, Enter to open, Esc to close. Focus is trapped inside and returns to where it was. */
export function JumpDialog({ targets, onClose }: { targets: JumpTarget[]; onClose: () => void }) {
  const [term, setTerm] = useState("");
  const [active, setActive] = useState(0);
  const panel = useRef<HTMLElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const id = useId();

  const shown = useMemo(() => {
    const needle = term.trim().toLocaleLowerCase();
    return targets.filter((item) => !needle || `${item.label} ${item.hint}`.toLocaleLowerCase().includes(needle)).slice(0, 12);
  }, [targets, term]);
  const index = Math.min(active, Math.max(0, shown.length - 1));

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    input.current?.focus();
    return () => { if (previous?.isConnected) previous.focus(); };
  }, []);
  useEffect(() => { list.current?.querySelector<HTMLElement>(`[id="${id}-${index}"]`)?.scrollIntoView({ block: "nearest" }); }, [index, id]);

  const pick = (target: JumpTarget | undefined) => { if (!target) return; target.go(); onClose(); };

  function onKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(); return; }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!shown.length) return;
      setActive((index + (event.key === "ArrowDown" ? 1 : shown.length - 1)) % shown.length);
      return;
    }
    if (event.key === "Enter" && event.target === input.current) { event.preventDefault(); pick(shown[index]); return; }
    if (event.key === "Tab") {
      const focusable = Array.from(panel.current?.querySelectorAll<HTMLElement>("input, button:not([tabindex='-1'])") ?? []);
      if (!focusable.length) { event.preventDefault(); return; }
      const position = focusable.indexOf(document.activeElement as HTMLElement);
      const next = focusable[(position + (event.shiftKey ? focusable.length - 1 : 1)) % focusable.length];
      event.preventDefault();
      next.focus();
    }
  }

  return <div className="jump-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section ref={panel} className="jump" role="dialog" aria-modal="true" aria-label="바로가기" onKeyDown={onKeyDown}>
      <input ref={input} role="combobox" aria-expanded="true" aria-controls={`${id}-list`} aria-autocomplete="list" aria-activedescendant={shown.length ? `${id}-${index}` : undefined}
        value={term} onChange={(event) => { setTerm(event.target.value); setActive(0); }} placeholder="페이지, 명령어 카테고리, $명령어 검색" aria-label="바로가기 검색" />
      <ul ref={list} id={`${id}-list`} role="listbox" aria-label="바로가기 결과">
        {shown.map((item, position) => <li key={item.key} role="presentation">
          <button type="button" tabIndex={-1} role="option" id={`${id}-${position}`} aria-selected={position === index} onMouseEnter={() => setActive(position)} onClick={() => pick(item)}><b>{item.label}</b><small>{item.hint}</small></button>
        </li>)}
      </ul>
      {!shown.length && <p className="empty" role="status">일치하는 항목이 없습니다.</p>}
      <div className="jump-foot"><span><kbd>↑↓</kbd> 이동</span><span>·</span><span><kbd>Enter</kbd> 열기</span><span>·</span><span><kbd>Esc</kbd> 닫기</span></div>
    </section>
  </div>;
}
