import Icon, { type IconName } from './Icon';
import type { AppView } from './Sidebar';

/**
 * Bottom tab bar below the md breakpoint, where the sidebar is hidden: the
 * views someone checks from a phone. "More" is Settings, which lists the rest.
 */

const TABS: { id: AppView; label: string; icon: IconName }[] = [
  { id: 'dashboard', label: 'Room', icon: 'dashboard' },
  { id: 'agents', label: 'Feed', icon: 'feed' },
  { id: 'conflicts', label: 'Conflicts', icon: 'conflicts' },
  { id: 'timeline', label: 'Timeline', icon: 'timeline' },
  { id: 'settings', label: 'More', icon: 'more' },
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
  const hasTab = TABS.some((t) => t.id === view);
  return (
    <nav className="v3-mobile-nav md:hidden" aria-label="Views">
      <ul>
        {TABS.map((tab) => {
          const active = view === tab.id || (tab.id === 'settings' && !hasTab);
          return (
            <li key={tab.id}>
              <button type="button" aria-current={active ? 'page' : undefined} onClick={() => onNavigate(tab.id)}>
                <Icon name={tab.icon} size={19} />
                {tab.id === 'conflicts' && conflictCount > 0 ? <span className="v3-dot is-warn" aria-hidden="true" /> : null}
                <span>
                  {tab.label}
                  {tab.id === 'conflicts' && conflictCount > 0 ? <span className="sr-only"> ({conflictCount} active)</span> : null}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
