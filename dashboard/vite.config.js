import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
    plugins: [vue()],
    server: {
        port: 3000,
        proxy: {
            // useApi.js calls these paths unmodified (no /api prefix) to
            // match the FastAPI server's real routes - proxy them as-is,
            // no rewrite needed.
            '/v1': {
                target: 'http://localhost:8000',
                changeOrigin: true,
            },
            '/health': {
                target: 'http://localhost:8000',
                changeOrigin: true,
            },
        }
    }
})
