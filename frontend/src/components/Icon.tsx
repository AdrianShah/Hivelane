/**
 * The dashboard's icon set: thin (1.6 stroke) inline SVG, no icon font. One
 * `name` prop; colour follows currentColor, size is the box in px.
 *
 * Paths are drawn on a 24 × 24 grid with round caps and joins so they sit
 * next to Geist at 13-14 px without looking heavier than the type.
 */
import type { ReactElement, SVGProps } from 'react';

const PATHS = {
  dashboard: (
    <>
      <rect x="3.5" y="3.5" width="7" height="8" rx="1.6" />
      <rect x="13.5" y="3.5" width="7" height="5" rx="1.6" />
      <rect x="13.5" y="11.5" width="7" height="9" rx="1.6" />
      <rect x="3.5" y="14.5" width="7" height="6" rx="1.6" />
    </>
  ),
  feed: (
    <>
      <rect x="3.5" y="4" width="17" height="6" rx="1.6" />
      <rect x="3.5" y="14" width="17" height="6" rx="1.6" />
    </>
  ),
  tasks: (
    <>
      <rect x="4" y="4" width="16" height="16" rx="3" />
      <path d="m8.5 12 2.5 2.5 4.5-5" />
    </>
  ),
  timeline: (
    <>
      <path d="M3.5 12h17" />
      <circle cx="7" cy="12" r="1.8" />
      <circle cx="13" cy="12" r="1.8" />
      <path d="M17.5 8v8" />
    </>
  ),
  heatmap: (
    <>
      <rect x="4" y="4" width="4.5" height="4.5" rx="1" />
      <rect x="9.75" y="4" width="4.5" height="4.5" rx="1" />
      <rect x="15.5" y="4" width="4.5" height="4.5" rx="1" />
      <rect x="4" y="9.75" width="4.5" height="4.5" rx="1" />
      <rect x="9.75" y="9.75" width="4.5" height="4.5" rx="1" />
      <rect x="4" y="15.5" width="4.5" height="4.5" rx="1" />
    </>
  ),
  conflicts: (
    <>
      <path d="M10.3 4.6 3.4 17a2 2 0 0 0 1.7 3h13.8a2 2 0 0 0 1.7-3L13.7 4.6a2 2 0 0 0-3.4 0Z" />
      <path d="M12 9.5v4" />
      <path d="M12 16.8v.01" />
    </>
  ),
  history: (
    <>
      <path d="M3.8 12a8.2 8.2 0 1 0 2.4-5.8L3.8 8.5" />
      <path d="M3.8 4.5v4h4" />
      <path d="M12 8v4.2l2.8 1.8" />
    </>
  ),
  replay: (
    <>
      <path d="M4 12a8 8 0 1 0 8-8" />
      <path d="m10 9.2 5 2.8-5 2.8Z" />
      <path d="M4 4v4h4" />
    </>
  ),
  analytics: (
    <>
      <path d="M4 20V10" />
      <path d="M10 20V4" />
      <path d="M16 20v-7" />
      <path d="M21 20H3" />
    </>
  ),
  invite: (
    <>
      <circle cx="9.5" cy="8.5" r="3.5" />
      <path d="M3.5 19.5a6 6 0 0 1 12 0" />
      <path d="M18.5 8v6" />
      <path d="M15.5 11h6" />
    </>
  ),
  notifications: (
    <>
      <path d="M6 9.5a6 6 0 0 1 12 0c0 5.5 2 7 2 7H4s2-1.5 2-7Z" />
      <path d="M10 19.5a2.2 2.2 0 0 0 4 0" />
    </>
  ),
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 3.5v2M12 18.5v2M3.5 12h2M18.5 12h2M6 6l1.4 1.4M16.6 16.6 18 18M6 18l1.4-1.4M16.6 7.4 18 6" />
    </>
  ),
  search: (
    <>
      <circle cx="11" cy="11" r="6.5" />
      <path d="m20 20-4.2-4.2" />
    </>
  ),
  chevron: <path d="m9.5 6 6 6-6 6" />,
  'chevron-down': <path d="m6 9.5 6 6 6-6" />,
  close: <path d="M6.5 6.5 17.5 17.5M17.5 6.5 6.5 17.5" />,
  copy: (
    <>
      <rect x="8.5" y="8.5" width="11.5" height="11.5" rx="2.2" />
      <path d="M15.5 8.5V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7.5a2 2 0 0 0 2 2h2.5" />
    </>
  ),
  share: (
    <>
      <path d="M12 15V4" />
      <path d="m8 7.5 4-4 4 4" />
      <path d="M6 11.5H5.5A1.5 1.5 0 0 0 4 13v5.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V13a1.5 1.5 0 0 0-1.5-1.5H18" />
    </>
  ),
  check: <path d="m5 12.5 4.5 4.5L19 7.5" />,
  link: (
    <>
      <path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1" />
      <path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  keyboard: (
    <>
      <rect x="3" y="6" width="18" height="12" rx="2.2" />
      <path d="M7 10h.01M11 10h.01M15 10h.01M7 14h10" />
    </>
  ),
  filter: <path d="M4 5.5h16l-6.2 7.3v5.4l-3.6 1.8v-7.2Z" />,
  'filter-off': (
    <>
      <path d="M4 5.5h16l-6.2 7.3v5.4l-3.6 1.8v-7.2Z" />
      <path d="M3.5 3.5 20.5 20.5" />
    </>
  ),
  folder: <path d="M3.5 7.5A1.5 1.5 0 0 1 5 6h4.2l2 2.2H19a1.5 1.5 0 0 1 1.5 1.5v8.3A1.5 1.5 0 0 1 19 19.5H5A1.5 1.5 0 0 1 3.5 18Z" />,
  'folder-open': (
    <>
      <path d="M3.5 17.5V7.5A1.5 1.5 0 0 1 5 6h4.2l2 2.2H18A1.5 1.5 0 0 1 19.5 9.7V11" />
      <path d="M3.5 17.5 6 12.2a1.5 1.5 0 0 1 1.4-1H20a1 1 0 0 1 .9 1.4l-2.3 5.4a1.5 1.5 0 0 1-1.4 1H5a1.5 1.5 0 0 1-1.5-1.5Z" />
    </>
  ),
  file: (
    <>
      <path d="M6.5 3.5h7l4.5 4.5v11a1.5 1.5 0 0 1-1.5 1.5h-10A1.5 1.5 0 0 1 5 19V5a1.5 1.5 0 0 1 1.5-1.5Z" />
      <path d="M13.5 3.5V8H18" />
    </>
  ),
  'thumb-up': (
    <>
      <path d="M7.5 10.5v9H5a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1Z" />
      <path d="M7.5 10.5 11 4a2 2 0 0 1 2.5 2.2l-.6 3.3h5.2a2 2 0 0 1 2 2.4l-1.2 6A2 2 0 0 1 17 19.5H7.5" />
    </>
  ),
  'thumb-down': (
    <>
      <path d="M16.5 13.5v-9H19a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1Z" />
      <path d="M16.5 13.5 13 20a2 2 0 0 1-2.5-2.2l.6-3.3H5.9a2 2 0 0 1-2-2.4l1.2-6A2 2 0 0 1 7 4.5h9.5" />
    </>
  ),
  timer: (
    <>
      <circle cx="12" cy="13.5" r="7" />
      <path d="M12 10v3.5l2.2 1.4" />
      <path d="M9.5 3.5h5" />
    </>
  ),
  bolt: <path d="M13 3.5 5.5 13.5H12l-1 7 7.5-10H12Z" />,
  bulb: (
    <>
      <path d="M9 17.5h6" />
      <path d="M10 20.5h4" />
      <path d="M12 3.5a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2V17.5h5v-1.1c0-.8.4-1.5 1-2A6 6 0 0 0 12 3.5Z" />
    </>
  ),
  target: (
    <>
      <circle cx="12" cy="12" r="7.5" />
      <circle cx="12" cy="12" r="3" />
      <path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3" />
    </>
  ),
  sliders: <path d="M5 7h9M18 7h1M5 17h3M12 17h7M14 4.5v5M8 14.5v5" />,
  diff: (
    <>
      <path d="M8 4v8M4 8h8" />
      <path d="M13.5 18h6.5" />
      <path d="M5 20 19 4" />
    </>
  ),
  branch: (
    <>
      <circle cx="7" cy="5.5" r="2" />
      <circle cx="7" cy="18.5" r="2" />
      <circle cx="17" cy="8.5" r="2" />
      <path d="M7 7.5v9" />
      <path d="M17 10.5c0 4-4.5 3.5-8.8 6.6" />
    </>
  ),
  plan: (
    <>
      <path d="M10 6.5h10M10 12h10M10 17.5h10" />
      <path d="m3.8 6.5 1.4 1.4 2.3-2.6M3.8 12l1.4 1.4 2.3-2.6" />
      <path d="M4.5 17.5h2" />
    </>
  ),
  claim: (
    <>
      <rect x="4.5" y="3.5" width="15" height="17" rx="2" />
      <circle cx="12" cy="10" r="2.6" />
      <path d="M8 17c.6-2 2.1-3 4-3s3.4 1 4 3" />
    </>
  ),
  block: (
    <>
      <circle cx="12" cy="12" r="8" />
      <path d="m6.4 6.4 11.2 11.2" />
    </>
  ),
  warning: (
    <>
      <path d="M10.3 4.6 3.4 17a2 2 0 0 0 1.7 3h13.8a2 2 0 0 0 1.7-3L13.7 4.6a2 2 0 0 0-3.4 0Z" />
      <path d="M12 9.5v4" />
      <path d="M12 16.8v.01" />
    </>
  ),
  more: <path d="M6 12h.01M12 12h.01M18 12h.01" />,
  sun: (
    <>
      <circle cx="12" cy="12" r="3.6" />
      <path d="M12 3v1.8M12 19.2V21M3 12h1.8M19.2 12H21M5.6 5.6l1.3 1.3M17.1 17.1l1.3 1.3M5.6 18.4l1.3-1.3M17.1 6.9l1.3-1.3" />
    </>
  ),
  moon: <path d="M19.5 14.5A7.5 7.5 0 0 1 9.5 4.5a7.5 7.5 0 1 0 10 10Z" />,
  monitor: (
    <>
      <rect x="3.5" y="4.5" width="17" height="11.5" rx="2" />
      <path d="M9 20h6M12 16v4" />
    </>
  ),
  external: (
    <>
      <path d="M14 4.5h5.5V10" />
      <path d="M19.5 4.5 11 13" />
      <path d="M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10" />
    </>
  ),
  ping: (
    <>
      <path d="M4.5 12a7.5 7.5 0 0 1 7.5-7.5" />
      <path d="M8 12a4 4 0 0 1 4-4" />
      <circle cx="12" cy="12" r="1.2" />
      <path d="M14.5 14.5 20 20" />
    </>
  ),
  release: (
    <>
      <rect x="5" y="11" width="14" height="9.5" rx="2" />
      <path d="M8.5 11V7.5a3.5 3.5 0 0 1 6.8-1.2" />
    </>
  ),
  lock: (
    <>
      <rect x="5" y="11" width="14" height="9.5" rx="2" />
      <path d="M8.5 11V7.5a3.5 3.5 0 0 1 7 0V11" />
    </>
  ),
  first: (
    <>
      <path d="M12 19.5V5" />
      <path d="m6.5 10.5 5.5-5.5 5.5 5.5" />
      <path d="M5 3.5h14" />
    </>
  ),
  play: <path d="M7.5 5.2v13.6a.8.8 0 0 0 1.2.7l10.6-6.8a.8.8 0 0 0 0-1.4L8.7 4.5a.8.8 0 0 0-1.2.7Z" />,
  pause: <path d="M8.5 5v14M15.5 5v14" />,
  arrow: <path d="M5 12h14m-5.5-5.5L19 12l-5.5 5.5" />,
  logout: (
    <>
      <path d="M14 4.5h4a1.5 1.5 0 0 1 1.5 1.5v12a1.5 1.5 0 0 1-1.5 1.5h-4" />
      <path d="M10.5 16.5 6 12l4.5-4.5" />
      <path d="M6 12h10" />
    </>
  ),
  refresh: (
    <>
      <path d="M19.5 12a7.5 7.5 0 0 1-13.4 4.6" />
      <path d="M4.5 12a7.5 7.5 0 0 1 13.4-4.6" />
      <path d="M18.5 3.5v4h-4M5.5 20.5v-4h4" />
    </>
  ),
  docs: (
    <>
      <path d="M5 4.5A1.5 1.5 0 0 1 6.5 3h11A1.5 1.5 0 0 1 19 4.5v15a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 5 19.5Z" />
      <path d="M9 8h6M9 12h6M9 16h3" />
    </>
  ),
  dot: <circle cx="12" cy="12" r="3.5" fill="currentColor" stroke="none" />,
  command: <path d="M9 9V6.5A2.5 2.5 0 1 0 6.5 9H9Zm0 0h6m-6 0v6m6-6V6.5A2.5 2.5 0 1 1 17.5 9H15Zm0 0v6m0 0h2.5a2.5 2.5 0 1 1-2.5 2.5V15Zm0 0H9m0 0v2.5A2.5 2.5 0 1 1 6.5 15H9Z" />,
  spark: <path d="M12 3.5 13.8 10.2 20.5 12l-6.7 1.8L12 20.5l-1.8-6.7L3.5 12l6.7-1.8Z" />,
  inbox: (
    <>
      <path d="M4 13.5h4.5l1.5 2.5h4l1.5-2.5H20" />
      <path d="M6.2 5.5h11.6L20 13.5V18a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 18v-4.5Z" />
    </>
  ),
  edit: (
    <>
      <path d="M4.5 19.5h4l10-10a2.1 2.1 0 0 0-3-3l-10 10Z" />
      <path d="m13.5 7.5 3 3" />
    </>
  ),
  help: (
    <>
      <circle cx="12" cy="12" r="8" />
      <path d="M9.8 9.6a2.3 2.3 0 0 1 4.4.9c0 1.6-2.2 1.9-2.2 3.3" />
      <path d="M12 16.8v.01" />
    </>
  ),
  shield: (
    <>
      <path d="M12 3.5 5 6v5.5c0 4.2 3 7.3 7 9 4-1.7 7-4.8 7-9V6Z" />
      <path d="m9 12 2.2 2.2L15 10.5" />
    </>
  ),
  'cloud-off': (
    <>
      <path d="M7 18.5h9.5a3.5 3.5 0 0 0 1.3-6.8 5.5 5.5 0 0 0-9.6-2.5A4.2 4.2 0 0 0 7 18.5Z" />
      <path d="M3.5 3.5 20.5 20.5" />
    </>
  ),
} satisfies Record<string, ReactElement>;

export type IconName = keyof typeof PATHS;

export interface IconProps extends Omit<SVGProps<SVGSVGElement>, 'name'> {
  name: IconName;
  /** Box size in px (default 16). */
  size?: number;
  /** Accessible name; without one the icon is decorative (aria-hidden). */
  label?: string;
}

export default function Icon({ name, size = 16, label, className, ...rest }: IconProps) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`v3-icon${className ? ` ${className}` : ''}`}
      role={label ? 'img' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      focusable="false"
      {...rest}
    >
      {PATHS[name]}
    </svg>
  );
}
