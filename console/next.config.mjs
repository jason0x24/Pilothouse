/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The console runs as a thin client of the Pilothouse FastAPI server.
  // Set PILOTHOUSE_API to point at it (default http://127.0.0.1:8088).
  env: {
    PILOTHOUSE_API: process.env.PILOTHOUSE_API ?? "http://127.0.0.1:8088",
    // Exposed to the browser so live SSE + cancel hits the same API host.
    NEXT_PUBLIC_PILOTHOUSE_API:
      process.env.NEXT_PUBLIC_PILOTHOUSE_API ??
      process.env.PILOTHOUSE_API ??
      "http://127.0.0.1:8088",
  },
};

export default nextConfig;
