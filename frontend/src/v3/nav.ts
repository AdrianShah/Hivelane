import type { IconName } from '../components/Icon';
import type { AppView } from '../components/Sidebar';

export interface NavItem {
  id: AppView;
  label: string;
  icon: IconName;
}

/** The live room, then the record of it: two groups split by one hairline. */
export const NAV_GROUPS: NavItem[][] = [
  [
    { id: 'dashboard', label: 'Dashboard', icon: 'dashboard' },
    { id: 'agents', label: 'Feed', icon: 'feed' },
    { id: 'tasks', label: 'Tasks', icon: 'tasks' },
    { id: 'conflicts', label: 'Conflicts', icon: 'conflicts' },
    { id: 'timeline', label: 'Timeline', icon: 'timeline' },
    { id: 'heatmap', label: 'Heat map', icon: 'heatmap' },
  ],
  [
    { id: 'history', label: 'History', icon: 'history' },
    { id: 'replay', label: 'Replay', icon: 'replay' },
    { id: 'analytics', label: 'Analytics', icon: 'analytics' },
    { id: 'invite', label: 'Invite', icon: 'invite' },
    { id: 'notifications', label: 'Notifications', icon: 'notifications' },
    { id: 'settings', label: 'Settings', icon: 'settings' },
  ],
];

export const NAV_ITEMS: NavItem[] = NAV_GROUPS.flat();

