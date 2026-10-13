/**
 * Path helpers shared by the dashboard views. Agents on Windows report
 * backslash paths and some report "./src/x.ts"; one spelling per file keeps
 * the heat map, timeline and filters from treating them as different files.
 */

export function normPath(path: string): string {
  return path
    .replace(/\\/g, '/')
    .replace(/^\.\//, '')
    .replace(/\/{2,}/g, '/');
}

/** Path segments, ignoring empty ones from leading/trailing slashes. */
export function pathSegments(path: string): string[] {
  return normPath(path).split('/').filter(Boolean);
}

export function baseName(path: string): string {
  const parts = pathSegments(path);
  return parts[parts.length - 1] ?? path;
}
