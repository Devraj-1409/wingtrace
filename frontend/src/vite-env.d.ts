/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Backend URL when the site is hosted elsewhere, e.g. https://api.example.com. Optional. */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
