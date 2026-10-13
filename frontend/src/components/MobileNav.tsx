import type { AppView } from './Sidebar';

/**
 * Bottom tab bar below the md breakpoint, where the sidebar is hidden: the
 * read-only views someone checks from a phone. Settings holds the rest.
 */

const TABS: { id: AppView; label: string; icon: string }[] = [
  { id: 'agents', label: 'Lanes', icon: 'view_agenda' },
  { id: 'timeline', label: 'Timeline', icon: 'timeline' },
  { id: 'heatmap', label: 'Heat map', icon: 'local_fire_department' },
  { id: 'conflicts', label: 'Conflicts', icon: 'warning' },
  { id: 'settings', label: 'More', icon: 'more_horiz' },
];

export default function MobileNav({
  view,
  onNavigate,
  conflictCount,
}: {
  view: AppView;
  onNavigate: (view: AppView) => void;
  conflictCount: number;
}) {
  return (
    <nav className="mobile-nav md:hidden fixed bottom-0 inset-x-0 z-50" aria-label="Views">
      <ul className="list-none m-0 p-0 grid grid-cols-5">
        {TABS.map((tab) => {
          // "More" (settings) stands for every view without a tab of its own.
          const hasTab = TABS.some((t) => t.id === view);
          const active = view === tab.id || (tab.id === 'settings' && !hasTab);
          return (
            <li key={tab.id}>
              <button
                type="button"
                className="w-full flex flex-col items-center gap-0.5 pt-1.5 pb-1.5 text-[10px] font-semibold"
                aria-current={active ? 'page' : undefined}
                onClick={() => onNavigate(tab.id)}
              >
                <span className="mobile-nav__icon relative inline-grid place-items-center w-11 h-7 rounded-full">
                  <span className="material-symbols-outlined text-[20px]" aria-hidden="true">
                    {tab.icon}
                  </span>
                  {tab.id === 'conflicts' && conflictCount > 0 ? (
                    <span className="absolute top-0 right-1.5 min-w-4 h-4 px-1 rounded-full bg-error text-[9px] font-bold text-white grid place-items-center">
                      {conflictCount}
                    </span>
                  ) : null}
                </span>
                <span>
                  {tab.label}
                  {tab.id === 'conflicts' && conflictCount > 0 ? (
                    <span className="sr-only"> ({conflictCount} active)</span>
                  ) : null}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
