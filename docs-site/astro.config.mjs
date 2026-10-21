// @ts-check
// The Hivelane docs site (Astro Starlight). Its pages are generated from the
// repo's Markdown by scripts/sync-docs.mjs, which `npm run dev` and
// `npm run build` run first; sources.mjs says which file becomes which page.
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

import { BASE, BRANCH, REPO_PUBLIC, REPO_URL, SITE, starlightSidebar } from './sources.mjs';

export default defineConfig({
  site: SITE,
  base: BASE,
  integrations: [
    starlight({
      title: 'Hivelane',
      description: 'Live coordination for parallel AI coding agents.',
      logo: { src: './src/assets/logo.svg', alt: 'Hivelane' },
      favicon: '/favicon.svg',
      // No GitHub links while the repo is private: they 404 for visitors.
      social: REPO_PUBLIC ? [{ icon: 'github', label: 'GitHub', href: REPO_URL }] : [],
      // Synced pages carry their own editUrl (the real source under docs/);
      // this default only applies to pages written in docs-site/ itself.
      ...(REPO_PUBLIC ? { editLink: { baseUrl: `${REPO_URL}/edit/${BRANCH}/docs-site/` } } : {}),
      lastUpdated: false,
      sidebar: starlightSidebar(),
    }),
  ],
});
