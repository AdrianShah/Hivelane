import { useEffect, useId, useRef, useState, type ReactNode, type RefObject } from 'react';
import { copyText } from '../clipboard';
import { SCOPE_LABEL } from '../detection';
import {
  FILTER_SCOPES,
  TIMELINE_KINDS,
  activeFilterCount,
  sameFilterText,
  toggleValue,
  type DashboardFilters,
} from '../dashboard/filters';
import { agentLabel, personColor } from '../dashboard/people';
import type { TimelineKind } from '../dashboard/sessionLog';
import { currentViewLink, type FiltersUpdate } from '../dashboard/useUrlState';

/**
 * Filters and search for lanes, the collision timeline and the heat map.
 * The state lives in the URL (see dashboard/filters.ts); this bar only edits
 * it. Text fields keep a local draft and commit after a short pause, so typing
 * stays smooth while the address bar catches up.
 */

interface FilterBarProps {
  filters: DashboardFilters;
  onChange: (update: FiltersUpdate) => void;
  people: string[];
  agentKinds: string[];
  /** Focused by the "/" shortcut. */
  searchRef: RefObject<HTMLInputElement | null>;
  /** e.g. "3 of 5 lanes", announced politely to screen readers. */
  resultLabel?: string;
}

const KIND_LABEL: Record<TimelineKind, { label: string; icon: string }> = {
  block: { label: 'Blocks', icon: 'block' },
  warn: { label: 'Warnings', icon: 'warning' },
  conflict: { label: 'Conflicts', icon: 'call_merge' },
};

const COMMIT_DELAY_MS = 220;

/** A text field's draft, committed after a pause and reset by outside changes. */
function useDraft(value: string, commit: (next: string) => void) {
  const [draft, setDraft] = useState(value);
  const committed = useRef(value);
  const commitRef = useRef(commit);

  useEffect(() => {
    commitRef.current = commit;
  });

  useEffect(() => {
    // The URL keeps filters trimmed: "login " comes back as "login". That is
    // our own commit, not a change from outside (Back/Forward, a pasted
    // link), so it must not reset the draft and eat the trailing space.
    if (!sameFilterText(value, committed.current)) {
      committed.current = value;
      setDraft(value);
    }
  }, [value]);

  useEffect(() => {
    if (draft === committed.current) return;
    const id = window.setTimeout(() => {
      committed.current = draft;
      commitRef.current(draft);
    }, COMMIT_DELAY_MS);
    return () => window.clearTimeout(id);
  }, [draft]);

  return [draft, setDraft] as const;
}

function FacetGroup({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div role="group" aria-label={label} className="flex flex-wrap items-center gap-1.5 min-w-0">
      <span className="section-label w-[4.5rem] shrink-0" aria-hidden="true">
        {label}
      </span>
      {children}
    </div>
  );
}

function Chip({
  pressed,
  onClick,
  children,
  title,
}: {
  pressed: boolean;
  onClick: () => void;
  children: ReactNode;
  title?: string;
}) {
  return (
    <button type="button" className="filter-chip" aria-pressed={pressed} onClick={onClick} title={title}>
      {pressed ? (
        <span className="material-symbols-outlined chip-check" aria-hidden="true">
          check
        </span>
      ) : null}
      {children}
    </button>
  );
}

export default function FilterBar({
  filters,
  onChange,
  people,
  agentKinds,
  searchRef,
  resultLabel,
}: FilterBarProps) {
  const facetsId = useId();
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState<'ok' | 'fail' | null>(null);
  const [q, setQ] = useDraft(filters.q, (next) => onChange((prev) => ({ ...prev, q: next })));
  const [file, setFile] = useDraft(filters.file, (next) =>
    onChange((prev) => ({ ...prev, file: next })),
  );
  const count = activeFilterCount(filters);

  const copiedTimer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(copiedTimer.current), []);
  const copyLink = async () => {
    const ok = await copyText(currentViewLink());
    setCopied(ok ? 'ok' : 'fail');
    window.clearTimeout(copiedTimer.current);
    copiedTimer.current = window.setTimeout(() => setCopied(null), 1600);
  };

  // People in a filter link who have not shown up (yet) still get a chip.
  const personOptions = [...new Set([...people, ...filters.people])];
  const agentOptions = [...new Set([...agentKinds, ...filters.agents])];

  return (
    <section className="filter-bar rounded-2xl p-3 mb-5" aria-label="Filters and search">
      <div className="flex flex-wrap items-center gap-2">
        <label className="filter-input flex-1 min-w-[min(100%,14rem)]">
          <span className="material-symbols-outlined text-[18px] text-on-dim" aria-hidden="true">
            search
          </span>
          <span className="sr-only">Search intents, captions and reasons</span>
          <input
            ref={searchRef}
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') {
                if (q) setQ('');
                else e.currentTarget.blur();
              }
            }}
            placeholder="Search intents, captions, reasons…"
            autoComplete="off"
            spellCheck={false}
          />
          <span className="hidden sm:inline-flex" aria-hidden="true">
            <kbd>/</kbd>
          </span>
        </label>
        <label className="filter-input w-full sm:w-60">
          <span className="material-symbols-outlined text-[18px] text-on-dim" aria-hidden="true">
            folder_open
          </span>
          <span className="sr-only">File path contains</span>
          <input
            type="search"
            value={file}
            onChange={(e) => setFile(e.target.value)}
            placeholder="File path contains…"
            autoComplete="off"
            spellCheck={false}
          />
        </label>
        {/* Wrapped: .filter-chip sets display, which would fight sm:hidden. */}
        <span className="sm:hidden">
          <button
            type="button"
            className="filter-chip"
            aria-expanded={open}
            aria-controls={facetsId}
            onClick={() => setOpen((v) => !v)}
          >
            <span className="material-symbols-outlined text-[15px]" aria-hidden="true">
              tune
            </span>
            Filters{count ? ` (${count})` : ''}
          </button>
        </span>
        {count > 0 ? (
          <button
            type="button"
            className="filter-chip"
            onClick={() => {
              setQ('');
              setFile('');
              onChange({ people: [], file: '', agents: [], types: [], scopes: [], q: '' });
            }}
          >
            <span className="material-symbols-outlined text-[15px]" aria-hidden="true">
              filter_alt_off
            </span>
            Clear all
          </button>
        ) : null}
        <button
          type="button"
          className="filter-chip"
          onClick={() => void copyLink()}
          title="Copy a link to this view with these filters"
        >
          <span className="material-symbols-outlined text-[15px]" aria-hidden="true">
            {copied === 'ok' ? 'check' : 'link'}
          </span>
          {copied === 'ok' ? 'Link copied' : copied === 'fail' ? 'Copy failed' : 'Copy view link'}
        </button>
      </div>

      <div id={facetsId} className={`${open ? 'flex' : 'hidden'} sm:flex flex-col gap-2 mt-3`}>
        {personOptions.length > 0 ? (
          <FacetGroup label="People">
            {personOptions.map((person) => (
              <Chip
                key={person}
                pressed={filters.people.includes(person)}
                onClick={() => onChange((prev) => ({ ...prev, people: toggleValue(prev.people, person) }))}
              >
                <span className="person-dot" style={{ background: personColor(person) }} aria-hidden="true" />
                {person}
              </Chip>
            ))}
          </FacetGroup>
        ) : null}
        {agentOptions.length > 0 ? (
          <FacetGroup label="Agents">
            {agentOptions.map((agent) => (
              <Chip
                key={agent}
                pressed={filters.agents.includes(agent)}
                onClick={() => onChange((prev) => ({ ...prev, agents: toggleValue(prev.agents, agent) }))}
              >
                {agentLabel(agent)}
              </Chip>
            ))}
          </FacetGroup>
        ) : null}
        <FacetGroup label="Decision">
          {TIMELINE_KINDS.map((kind) => (
            <Chip
              key={kind}
              pressed={filters.types.includes(kind)}
              onClick={() => onChange((prev) => ({ ...prev, types: toggleValue(prev.types, kind) }))}
            >
              <span className="material-symbols-outlined text-[14px]" aria-hidden="true">
                {KIND_LABEL[kind].icon}
              </span>
              {KIND_LABEL[kind].label}
            </Chip>
          ))}
        </FacetGroup>
        <FacetGroup label="Scope">
          {FILTER_SCOPES.map((scope) => (
            <Chip
              key={scope}
              pressed={filters.scopes.includes(scope)}
              onClick={() => onChange((prev) => ({ ...prev, scopes: toggleValue(prev.scopes, scope) }))}
              title={`Decisions made at ${scope} scope`}
            >
              {SCOPE_LABEL[scope]}
            </Chip>
          ))}
        </FacetGroup>
      </div>

      {resultLabel ? (
        <p className="m-0 mt-2 text-[11px] text-on-dim" role="status" aria-live="polite">
          {resultLabel}
        </p>
      ) : null}
    </section>
  );
}
