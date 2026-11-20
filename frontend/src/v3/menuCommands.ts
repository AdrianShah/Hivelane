import type { IconName } from '../components/Icon';
import type { AppView } from '../components/Sidebar';
import type { Command } from '../dashboard/commands';
import { NAV_ITEMS } from './nav';
import type { V3Actions } from './model';

export interface MenuCommand extends Command {
  icon: IconName;
  run: () => void;
}

export function buildCommands(actions: V3Actions, extra: { toggleTheme: () => void; isOwner: boolean }): MenuCommand[] {
  const views: MenuCommand[] = NAV_ITEMS.map((item) => ({
    id: `view:${item.id}`,
    label: item.label,
    group: 'Go to',
    icon: item.icon,
    keywords: item.id === 'agents' ? ['lanes', 'agents'] : item.id === 'conflicts' ? ['overlaps', 'held', 'resolve'] : [],
    run: () => actions.navigate(item.id as AppView),
  }));
  const acts: MenuCommand[] = [
    { id: 'invite', label: 'Invite teammates', group: 'Actions', icon: 'invite', keywords: ['share', 'setup', 'join'], run: actions.openInvite },
    { id: 'copy', label: 'Copy room link', group: 'Actions', icon: 'link', keywords: ['share', 'url'], run: actions.copyLink },
    { id: 'theme', label: 'Toggle light or dark', group: 'Actions', icon: 'moon', keywords: ['theme', 'appearance', 'dark', 'light'], run: extra.toggleTheme },
    { id: 'share', label: 'Share the prevented-collisions card', group: 'Actions', icon: 'share', keywords: ['image', 'stats'], run: actions.openShare },
    { id: 'shortcuts', label: 'Keyboard shortcuts', group: 'Actions', icon: 'keyboard', hint: '?', run: actions.openShortcuts },
    { id: 'new', label: 'New room', group: 'Actions', icon: 'plus', keywords: ['create'], run: actions.newRoom },
    { id: 'leave', label: 'Leave room', group: 'Actions', icon: 'logout', keywords: ['exit', 'start'], run: actions.leaveRoom },
  ];
  return [...views, ...acts];
}

