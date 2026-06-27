import { defineConfig } from 'vite';

// L'app Activity est buildée (`npm run build`) puis servie par Flask, sur la MÊME
// origine que /api/token (architecture mono-serveur). Le bloc `server` ne sert que
// pour `npm run dev` (HMR), prévu plus tard.
export default defineConfig({
  envDir: '..',          // lit le .env À LA RACINE du projet (VITE_DISCORD_CLIENT_ID)
  base: './',            // chemins d'assets relatifs → servis par Flask sous /assets
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    proxy: {
      // Adapter le port si WEBAPP_PORT diffère (Flask).
      '/api': { target: 'http://localhost:8001', changeOrigin: true },
      '/.proxy/api': { target: 'http://localhost:8001', changeOrigin: true },
    },
    hmr: { clientPort: 443 },
  },
});
