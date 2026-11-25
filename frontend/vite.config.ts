import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';

// Vite injects the entry <script> before the CSS <link>. Put stylesheets first so
// they're applied before the app starts measuring layout (Firefox otherwise warns
// "Layout was forced before the page was fully loaded" and can flash unstyled).
function stylesBeforeScripts(): Plugin {
  return {
    name: 'muuvies-styles-before-scripts',
    enforce: 'post',
    transformIndexHtml: {
      order: 'post',
      handler(html) {
        const links = html.match(/<link rel="stylesheet"[^>]*>\s*/g) ?? [];
        if (!links.length) return html;
        let out = html;
        for (const l of links) out = out.replace(l, '');
        return out.replace(/(\s*)<script type="module"/, `$1${links.join('').trim()}$1<script type="module"`);
      },
    },
  };
}

export default defineConfig({
  plugins: [react(), stylesBeforeScripts()],
  base: '/static/app/',
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/static/fonts': 'http://127.0.0.1:8000',
      '/static/media': 'http://127.0.0.1:8000',
    },
  },
  build: {
    outDir: '../backend/static/app',
    emptyOutDir: true,
    target: 'es2022',
    cssCodeSplit: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          'react-vendor': ['react', 'react-dom'],
        },
      },
    },
  },
});
