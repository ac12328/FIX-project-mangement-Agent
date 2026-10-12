const configuredApiBase = process.env.NEXT_PUBLIC_API_URL?.trim() ?? "";

// Keep browser requests same-origin by default. next.config.ts proxies /api to FastAPI.
export const API_BASE = configuredApiBase.replace(/\/+$/, "");
