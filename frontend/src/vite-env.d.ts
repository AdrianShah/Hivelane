/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_WS_URL?: string;
  /** "1" on a static host with no relay behind it (the GitHub Pages build). */
  readonly VITE_STATIC_HOST?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
