import type { IconName } from './Icon';

/**
 * Material Symbols names the older views keep in their lookup tables, mapped
 * onto the inline icon set (the icon font is gone). Unknown names fall back
 * to a dot rather than rendering nothing.
 */
const MATERIAL: Record<string, IconName> = {
  warning: 'warning',
  call_split: 'branch',
  call_merge: 'branch',
  my_location: 'target',
  bolt: 'bolt',
  assignment_ind: 'claim',
  checklist: 'plan',
  check: 'check',
  block: 'block',
  difference: 'diff',
  chevron_right: 'chevron',
  search: 'search',
  folder_open: 'folder-open',
  folder: 'folder',
  tune: 'sliders',
  filter_alt_off: 'filter-off',
  link: 'link',
  close: 'close',
  ios_share: 'share',
  lightbulb: 'bulb',
  thumb_up: 'thumb-up',
  thumb_down: 'thumb-down',
  contrast: 'monitor',
  dark_mode: 'moon',
  light_mode: 'sun',
  hourglass_top: 'timer',
  inbox: 'inbox',
  task_alt: 'tasks',
  bedtime: 'moon',
  cloud_off: 'cloud-off',
  edit_note: 'edit',
  edit: 'edit',
  help: 'help',
  verified_user: 'shield',
};

export function fromMaterial(name: string): IconName {
  return MATERIAL[name] ?? 'dot';
}
