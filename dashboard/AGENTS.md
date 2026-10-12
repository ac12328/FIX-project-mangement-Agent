<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

# Dashboard Agent Instructions

Next.js 15 + TypeScript + Tailwind CSS + shadcn/ui.

## Commands

```bash
npm run dev          # Dev server (port 3000)
npx tsc --noEmit    # Type check
npx eslint .        # Lint
```

## Structure

| Path | Purpose |
|------|---------|
| `src/app/` | App Router pages (one folder per route) |
| `src/components/` | Shared UI components (shadcn/ui based) |
| `src/lib/api.ts` | API client functions (fetch to FastAPI at :8000) |

## Conventions

- All API calls go through `src/lib/api.ts` — add new functions there, don't inline fetch
- FastAPI backend runs on port 8000, CORS is wide open in dev
- SSE for real-time updates from `src/events/bus.py` (backend)
- Use shadcn/ui components from `src/components/ui/` — don't create custom equivalents
- Pages are server components by default; add `"use client"` only when needed
