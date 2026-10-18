# PartRoute web (v0.4)

Browser client for the PartRoute cloud platform: projects, parts, files and members on top of the PartRoute API (`server/`). All engineering data lives on the server; the browser keeps only the sign-in token and preferences.

Stack: Vite, React 19, TypeScript (strict), react-router-dom 7 (`HashRouter`), three.js (landing hero, model viewer), @xyflow/react (workflow canvas), lucide-react, Vitest. No UI, state or i18n library.

## Run against the backend

```bash
# 1. Database + API (from the repo root; see server/README or design.md §9)
docker compose up -d db
cd server
uv sync && uv run alembic upgrade head
uv run python -m app.seed          # dev users + DEMO / OTHER projects (accounts listed in server/app/seed.py)
uv run uvicorn app.main:app --port 8000

# 2. Web
cd web
npm install
cp .env.example .env.local         # optional; VITE_API_BASE defaults to http://localhost:8000
npm run dev                        # http://localhost:5188
```

The API allows `http://localhost:5188` through CORS by default (`PARTROUTE_CORS_ORIGINS`). For a deployed API set `VITE_API_BASE=https://api.example.com` at build time.

| Command | What it does |
|---|---|
| `npm run dev` | Dev server on port 5188 |
| `npm run typecheck` | TypeScript only |
| `npm test` | Vitest (i18n key parity, API client error parsing) |
| `npm run build` | Type-check and build into `dist/` (`npm run preview` serves it on 5189) |

## What the app does today

- **Auth**: email + password against `POST /auth/login`; bearer token in `localStorage` (`partroute.token`, trade-off documented in `src/auth/AuthProvider.tsx`). Any 401 signs the user out; sign-out revokes the token server-side.
- **Shell**: top bar with breadcrumbs, a service-status pill from `GET /system/capabilities` (database / geometry kernel / AI key — "configured" never means "connected"), language and theme toggles, user menu. Left rail with project navigation; under 768 px a bottom bar and a menu drawer.
- **Projects** (`/projects`): search (`/` focuses it), show archived, cards or table, create (409 `project_code_taken` handled inline), archive / restore for owners.
- **Project** (`/projects/:id/:section`): title-block header, edit details, archive. Sections: Parts (search, status / tag filters, folder grouping, archived, create, archive / restore, copy to another project), Files (drag-and-drop upload with kind guess and part link, versions, new version, authenticated download, rename, archive), Members (add by email, change role, remove; 400 `last_owner` handled), Workflows (templates, open canvas), Runs (status, node counts).
- **Part workspace** (`/parts/:id/:tab`): header with number, name, editable status (designer / owner), material, owner, latest model, folder, tags; edit details, copy, archive. Tabs: **Model** (mesh viewer with picking and measuring, revisions with compare and STEP/GLB download, simplified CAD editor with live kernel preview, undo/redo and save-as-revision, STEP/mesh import, AI panel for create/modify with clarification answers), **Drawings** (generate from a revision, SVG view, SVG/DXF download), **Process** (status banner with staleness reasons, plans, gaps, approvals, manual drawing intake), **Quote** (tiers, cost lines with formulas, formal badge), Files, History, Runs.
- **Project management layer** (`docs/project-task-asset-design.zh-CN.md`): project nav is Overview / Tasks / Assets / Approvals / Workflow templates / Members (`VITE_LEGACY_NAV=1` adds Parts / Files / Runs back). Overview shows the objective, lifecycle, stages with stage reviews and completion blockers; tasks have criteria, baselines, runs, deliverables and submissions; the asset catalogue covers models, drawings, plans, quotes, documents, intakes and process modules; the approval centre (`/approvals`) handles engineering reviews, task acceptance and stage reviews; a bell shows notifications. The execution monitor (`/projects/:id/runs`) filters runs across tasks.
- **Workflow templates** (`/workflows/:id/:tab`): overview (contract, input policy, library source), configure (the draft canvas: node palette, typed ports, groups, parameter forms, save with version conflicts, validation), published versions (publish with gate checks, deprecate, share to the library), usage. Runs start from one dialog (`StartRunDialog`) that pins a published version and requires a task or "independent exploration". The public library is at `/template-library`.
- **Run page** (`/runs/:id`): live status over SSE, status-coloured canvas and timeline, node drawer with artifacts and AI steps, approvals, clarification answers, manual intake entry, cancel / retry.
- **Editor integration**: `src/features/editor/adapter.ts` defines the `EditorAdapter` a future complex editor implements; see `docs/editor-integration.md`.
- **Settings** (`/settings`, admins): live capabilities, preferences, read-only model configuration list (`GET /model-configs`).
- **Landing** (`/`): interactive three.js hero (lazy-loaded, static fallback without WebGL), bilingual product copy.

No sample or fake records are shown anywhere; empty, loading (skeleton) and error states are explicit, and API errors show a friendly message with the raw code.

## Extension points

| To add | Where |
|---|---|
| Model viewer / CAD editor, Drawings, Process, Quote, History, Runs panels | Replace the `component` of the tab in `src/pages/part/registry.tsx` (`PART_TABS`), ideally with `lazy(() => import(...))`. Panels receive `PartTabProps` (`part`, `project`, `caps`, `onPartChange`, `reloadPart`). |
| Workflow list / runs inside a project | Replace `workflows` / `runs` in `src/pages/project/registry.tsx` (`PROJECT_SECTION_COMPONENTS`, props `ProjectTabProps`). |
| New pages (workflow canvas `/workflows/:id`, run detail `/runs/:id`, …) | Add to `SHELL_ROUTES` in `src/app/routes.tsx` (rendered inside the authenticated shell). Use `useCrumbs()` and `useShellProject()` from `src/components/shell/ShellContext.tsx` for breadcrumbs and the project rail. |
| API calls | `src/api/endpoints.ts` already has a typed function for every server route (revisions, `cad/preview`, AI modeling, drawings, intakes, plans, quotes, approvals, part status, workflows, runs with `watchRun()` over SSE, model configs). Types in `src/api/types.ts` mirror the server's response builders. Blobs: `blobUrl()` / `downloadBlob()` in `src/api/blobs.ts`. |
| Permissions | `src/auth/permissions.ts` mirrors `ACTION_ROLES` from `server/app/security.py`; `projectCapabilities(project)` gives `design`, `process`, `quote`, `workflow`, `manage`. The server stays authoritative — still handle 403. |
| Strings | Add keys to `src/i18n/en.ts` and `src/i18n/zh.ts` (typecheck fails if one is missing). `useI18n()` gives `t`, `tn` (plurals), `fmt` (Intl dates / numbers / bytes); `<Trans>` interpolates React nodes; API error codes map to `error.<code>`. |

## i18n, theme, accessibility

- English is the default; Chinese via the English / Chinese language toggle. Stored in `partroute.lang`, applied to `<html lang>`. Switching re-renders in place: routes, open dialogs and drafts are kept.
- Theme: system / dark / light (`partroute.theme`, `<html data-theme>`), applied before first paint by an inline script in `index.html`. Tokens in `src/styles/tokens.css`.
- `prefers-reduced-motion` disables transitions and the hero's auto-rotation and stage auto-advance.
- Visible focus rings, `/` focuses search on list pages, dialogs trap focus, close on Esc and return focus to the opener; menus support arrow keys.

## File map

| Path | Contents |
|---|---|
| `src/app/` | `App.tsx` (providers + router), `routes.tsx` (shell routes), `nav.tsx` (paths, project sections) |
| `src/api/` | `client.ts` (fetch wrapper, `ApiError`), `types.ts`, `endpoints.ts`, `blobs.ts` |
| `src/auth/` | `AuthProvider`, `RequireAuth`, `permissions.ts` |
| `src/i18n/` | `en.ts` (source of truth), `zh.ts`, provider, Intl formatters, error-code mapping, `Trans` |
| `src/prefs/` | Theme provider, media-query hooks |
| `src/components/` | `shell/` (top bar, rail, status pill), `ui/` (dialog, menu, popover, toast, badges, states, form), `files/FilesPanel.tsx`, `ComingSoonPanel.tsx` |
| `src/pages/` | `Home`, `Login`, `Projects`, `project/*`, `part/*`, `Settings`, `NotFound` |
| `src/three/` | `hero.ts` (landing scene); `viewer.ts` + `geometry.ts` kept for the viewer task |
| `src/domain/` | Only the v0.3 bracket template files that `three/` still imports |
| `src/styles/` | `tokens.css`, `app.css`, `home.css` |

The v0.3 prototype (studios, planning, quote, local store) was removed; it remains in git history at commit `8f8123b`.

## Deploy

Static build: `VITE_API_BASE=https://<api-host> npm run build`, then serve `dist/` from Firebase Hosting (`firebase.json` targets the demo site `partroute`, https://partroute.web.app; the one-command deploy is `deploy/gcp/deploy.sh web`, see [docs/deploy-gcp.zh-CN.md](../docs/deploy-gcp.zh-CN.md)) or any static host. The app uses hash routing, so no rewrite rules are needed. Add the hosting origin to the API's `PARTROUTE_CORS_ORIGINS`. Frontend and backend deploy and roll back independently.
