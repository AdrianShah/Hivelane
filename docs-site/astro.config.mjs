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
      logo: { light: './src/assets/logo-light.svg', dark: './src/assets/logo-dark.svg', alt: 'Hivelane' },
      favicon: '/favicon.svg',
      head: [
        { tag: 'link', attrs: { rel: 'preconnect', href: 'https://fonts.googleapis.com' } },
        { tag: 'link', attrs: { rel: 'preconnect', href: 'https://fonts.gstatic.com', crossorigin: true } },
        {
          tag: 'link',
          attrs: {
            rel: 'stylesheet',
            href: 'https://fonts.googleapis.com/css2?family=Google+Sans:wght@400..700&family=Google+Sans+Code:wght@400..600&display=swap',
          },
        },
      ],
      customCss: ['./src/styles/theme.css'],
      // Keep the theme in step with the dashboard (same origin, shared storage).
      components: {
        ThemeProvider: './src/components/ThemeProvider.astro',
        ThemeSelect: './src/components/ThemeSelect.astro',
      },
      expressiveCode: {
        // Plain frames: no empty terminal title bar over shell snippets.
        defaultProps: { frame: 'code' },
        themes: ['github-dark-default', 'github-light-default'],
        styleOverrides: {
          borderRadius: '12px',
          borderColor: 'var(--sl-color-hairline)',
          codeFontFamily: 'var(--sl-font-mono)',
          codeFontSize: '0.84rem',
          codeLineHeight: '1.7',
          uiFontFamily: 'var(--sl-font)',
          frames: { shadowColor: 'transparent', frameBoxShadowCssValue: 'none' },
        },
      },
      social: REPO_PUBLIC ? [{ icon: 'github', label: 'GitHub', href: REPO_URL }] : [],
      // Synced pages carry their own editUrl (the real source under docs/);
      // this default only applies to pages written in docs-site/ itself.
      ...(REPO_PUBLIC ? { editLink: { baseUrl: `${REPO_URL}/edit/${BRANCH}/docs-site/` } } : {}),
      lastUpdated: false,
      sidebar: starlightSidebar(),
    }),
  ],
});
