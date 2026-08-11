# Telegram Hindi Video Automation

Python automation that watches a Telegram channel, generates Hindi narration
with Gemini and edge-tts, renders YouTube and Instagram videos with FFmpeg,
and optionally publishes the Reel to Instagram.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `telegram_video_automation.py` — Telegram Bot API polling and media pipeline
- `requirements.txt` — Python dependencies
- `.env.example` — runtime configuration reference
- `AUTOMATION_README.md` — setup and operating instructions

## Architecture decisions

- Telegram uses Bot API `getUpdates` long polling only; no user-account client
  libraries or Telegram API ID/hash are required.
- Telegram update offsets are checkpointed atomically so restarts do not repeat
  already-consumed channel posts.
- Instagram publishing is opt-in with `PUBLISH_TO_INSTAGRAM=false` by default.
- Each summary produces separate landscape YouTube and vertical Reel outputs.

## Product

The automation turns new summaries from `@weyogitforyou` into Hindi narration,
subtitled MP4 videos, and optionally an Instagram Reel.

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

_Populate as you build — sharp edges, "always run X before Y" rules._

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
