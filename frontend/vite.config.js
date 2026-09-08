import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Port 5173 is not incidental: it is one of the origins the API allows in
// CORS_ORIGINS, so moving it means updating the backend's .env too.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, strictPort: true },
});
