// Which repo files become which docs pages, and where they sit in the sidebar.
//
// The Markdown under ../docs (plus a few README sections) is the single source
// of truth: GitHub renders it in place, and scripts/sync-docs.mjs copies it into
// src/content/docs/ with Starlight frontmatter at build time. Nothing under
// src/content/docs/ is committed. To add a page, write it in ../docs and list it
// in SIDEBAR; a docs/*.md file left out of SIDEBAR still gets a page, under
// "More". A listed file that does not exist (yet) is skipped, not an error.
//
// Imported by astro.config.mjs (the sidebar) and scripts/sync-docs.mjs (the
// copy), so the two cannot disagree about which pages exist.

import { existsSync, readdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const SITE_ROOT = dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = resolve(SITE_ROOT, '..');
export const OUT_DIR = join(SITE_ROOT, 'src', 'content', 'docs');

// The site is published by .github/workflows/pages.yml to GitHub Pages, under
// the dashboard: https://adrianshah.github.io/Hivelane/docs/. Links between
// pages are built from BASE, so changing these two is enough to move the site
// (DOCS_SITE / DOCS_BASE override them for a one-off build elsewhere).
export const SITE = process.env.DOCS_SITE || 'https://adrianshah.github.io';
export const BASE = process.env.DOCS_BASE || '/Hivelane/docs';

export const REPO_URL = 'https://github.com/AdrianShah/Hivelane';
export const BRANCH = 'main';

// The repository is private for now, so "Edit page" and GitHub links on the
// public Pages site would 404 for every visitor. Flip this (or set
// DOCS_REPO_PUBLIC=1) when the repo goes public.
export const REPO_PUBLIC = process.env.DOCS_REPO_PUBLIC === '1';

/**
 * Each item becomes one page at <BASE>/<slug>/, built from one of:
 *   file:     a whole Markdown file; its first `# heading` becomes the title
 *   section:  one `## heading` section of a file, up to the next `## ` heading
 * `label` overrides the sidebar text (the page title is used otherwise), and
 * `title` the page title (needed for sections, which have no `# heading`).
 * A missing file is skipped, but a missing section fails the build, so a
 * renamed README heading is caught by the pull request that renamed it.
 */
export const SIDEBAR = [
  {
    label: 'Getting started',
    items: [
      { slug: 'quick-start', section: { file: 'README.md', heading: 'Quick start' }, title: 'Quick start' },
      { slug: 'install', file: 'docs/install.md', label: 'Install' },
    ],
  },
  {
    label: 'Concepts',
    items: [
      {
        slug: 'architecture',
        section: { file: 'README.md', heading: 'Architecture' },
        title: 'Architecture',
        description: 'How the agent hooks, the per-machine daemon, the relay and the dashboard fit together.',
      },
      { slug: 'conflict-detection', file: 'docs/conflict-detection.md', label: 'Conflict detection' },
      { slug: 'security', file: 'docs/security.md', label: 'Security' },
    ],
  },
  {
    label: 'Integrations',
    items: [
      { slug: 'adapters', file: 'docs/adapters.md', label: 'Agent adapters' },
      { slug: 'vscode', file: 'editors/vscode/README.md', label: 'VS Code / Cursor extension' },
      { slug: 'notifications', file: 'docs/notifications.md', label: 'Notifications and replay' },
    ],
  },
  {
    label: 'Self-hosting',
    // Every deploy/<target>/README.md is appended as self-hosting/<target>, so
    // a new deployment target needs no edit here.
    deployGuides: true,
    items: [{ slug: 'self-hosting', file: 'docs/self-hosting.md', label: 'Self-hosting the relay' }],
  },
  {
    label: 'Reference',
    items: [
      { slug: 'protocol', file: 'docs/protocol.md', label: 'Wire protocol' },
      { slug: 'cli', file: 'docs/cli.md', label: 'CLI' },
      { slug: 'faq', file: 'docs/faq.md', label: 'FAQ' },
      { slug: 'comparison', file: 'docs/comparison.md', label: 'Approaches compared' },
    ],
  },
];

// Pages that exist only on the site, under docs-site/pages/. `{{BASE}}` in
// them is replaced with BASE at sync time.
export const SITE_PAGES = [
  { slug: 'index', page: 'pages/index.mdx' },
  { slug: '404', page: 'pages/404.md' },
];

/** The repo-relative Markdown file an item is built from. */
export function sourceOf(item) {
  return item.file ?? item.section?.file ?? null;
}

function exists(repoPath) {
  return existsSync(join(REPO_ROOT, repoPath));
}

function subdirs(dir) {
  return readdirSync(dir, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .sort();
}

/**
 * deploy/<target>/README.md as self-hosting/<target>. A target whose README
 * sits one level down (deploy/helm/<chart>/README.md) counts too.
 */
function deployGuides() {
  const deployDir = join(REPO_ROOT, 'deploy');
  if (!existsSync(deployDir)) return [];
  return subdirs(deployDir).flatMap((name) => {
    const candidates = [`deploy/${name}/README.md`, ...subdirs(join(deployDir, name)).map((sub) => `deploy/${name}/${sub}/README.md`)];
    const file = candidates.find(exists);
    return file ? [{ slug: `self-hosting/${name}`, file }] : [];
  });
}

/** docs/*.md files that no SIDEBAR item mentions, as pages named after the file. */
function unlistedDocs(listed) {
  const docsDir = join(REPO_ROOT, 'docs');
  if (!existsSync(docsDir)) return [];
  return readdirSync(docsDir)
    .filter((name) => name.endsWith('.md') && !listed.has(`docs/${name}`))
    .sort()
    .map((name) => ({ slug: name.slice(0, -3).toLowerCase(), file: `docs/${name}` }));
}

/**
 * The pages to build and the sidebar that links them, both limited to sources
 * that exist right now. `missing` lists the sources that were skipped.
 */
export function resolvePages() {
  const groups = SIDEBAR.map(({ deployGuides: withGuides, ...group }) => ({
    ...group,
    items: withGuides ? [...group.items, ...deployGuides()] : [...group.items],
  }));
  const listed = new Set(groups.flatMap((group) => group.items.map(sourceOf)));
  const more = unlistedDocs(listed);
  if (more.length) groups.push({ label: 'More', items: more });

  const missing = [];
  const sidebar = [];
  for (const group of groups) {
    const items = group.items.filter((item) => {
      const ok = exists(sourceOf(item));
      if (!ok) missing.push(sourceOf(item));
      return ok;
    });
    if (items.length) sidebar.push({ ...group, items });
  }
  return { pages: sidebar.flatMap((group) => group.items), sidebar, missing };
}

/** The sidebar in Starlight's config format. */
export function starlightSidebar() {
  return resolvePages().sidebar.map((group) => ({
    label: group.label,
    items: group.items.map((item) => (item.label ? { slug: item.slug, label: item.label } : { slug: item.slug })),
  }));
}
