# Frontend audit — InfraSpine AI (HEAD ca956c2, 2026-11-28)

Owner feedback: "software ka frontend bahut bura hai" — the frontend looks very poor.
Scope: `frontend/src` only. No backend changes, no API behavior changes, no removed
features or routes. Baseline before any change: `npm test` 22 passed, typecheck clean,
`npm run build` clean (11.1s).

## Inventory
- ~80 `.tsx` files. Shell: `App.tsx` → `AdminConsole.tsx` (hash router, ~45 views) +
  `AppSidebar.tsx` (6 nav groups, command palette, theme + RTL switch).
- Shared UI: `components/ui/` (Tailwind v4 primitives: Card, Badge, Alert, Button, Tabs,
  Skeleton, PageHeader) — modern. Legacy: `styles.css` (1650 lines of bespoke classes)
  used by most pages — the two systems coexist and disagree.
- Biggest pages: `KnowledgePage.tsx` (1477 lines, the owner's main screen),
  `ModuleWorkspaces.tsx`, `FinancePage.tsx`, `HomePage`/`HomeDashboard.tsx`,
  `SettingsPage.tsx`, `LoginPage.tsx`.

## Findings (worst first)
1. **Brand colour clash (visual).** Primary brand is green (`--primary: #126b4e`,
   default `--brand`), but the logo mark (`.brand-mark`), the provider logo
   (`.provider-logo`) and the whole login hero use hardcoded blue-teal gradients
   (`#3f7893 → #254e67`, `#142b3a → #245569`). The first screen a user sees is a
   different colour from the rest of the app. `CompanySetupPage` also defaults the
   brand colour to `#146B61` (teal) instead of `#126b4e`.
2. **Tiny, cramped type (readability).** Base font is 13px/1.5; nav items 12.5px,
   document titles 12px, metadata 10.5px, table headers 11px. Dense grey-on-pale
   text is the main reason the app looks dated and hard to read.
3. **Two radius scales that disagree.** `styles.css`: sm 8 / base 10 / lg 12.
   `index.css` Tailwind theme: sm 6 / md 8 / lg 10 / xl 12. Same-named radii render
   differently depending on which system a component uses.
4. **Undefined token (bug).** `styles.css:261` uses `var(--radius-md)` but `:root`
   never defines `--radius-md` — the rule is dropped, so that element has no radius.
5. **Asymmetric page padding.** `.main { padding: 26px max(32px, calc(100% - 1088px)) 12px 32px; }`
   pushes content far from the right edge on wide screens while the left stays at 32px.
6. **Duplicate navigation (admin).** "Knowledge" and "AI Agents" appear in both the
   Workspace group and the numbered "InfraSpine modules" group (01/02); "Process
   intelligence" appears in Operations and as module 04. Same destination, two entries.
7. **Knowledge page density.** Doc list rows are 12px titles with 10.5px metadata,
   chat bubbles 13px, composer and starter prompts cramped — the most-used screen
   is the most cramped one.
8. **Tables are small and inconsistent.** Finance tables use unstyled `<table>` with
   12.5px text and 11px headers; other pages use `.parser-table` — no shared table
   style, no row hover, no sticky header.
9. **Sidebar is narrow and dense.** 252px wide, 34px nav rows, 12.5px labels; on
   <1280px it auto-collapses to an icon rail, which hides labels on common laptops.
10. **Login form is flat.** The sign-in form sits on a plain surface with no card,
    while the hero carries all the visual weight — the split feels unfinished.

## Redesign plan (small commits, tests green after each)
1. Tokens + base: one radius scale (8/12/16/20), base font 13→14px, line-height
   1.55, `--radius-md` defined, brand-derived gradients (no hardcoded blue),
   `.main` padding fixed, default brand `#126b4e` everywhere.
2. Shell + nav: taller nav rows, deduplicated admin nav, sidebar 252→264px.
3. Knowledge page: larger type, roomier chat + doc list, clearer empty state.
4. Login: brand-consistent hero + a real card for the form.
5. Tables + cards: one shared table style, consistent card padding, stat values.
6. Regression test `frontend/test/designTokens.test.mjs`: tokens exist, no
   hardcoded blue gradients, `--radius-md` defined.

## Out of scope (deliberately not touched)
- No API calls, routes, permissions, or backend code.
- No page removed; every view keeps its current destination.
- The 4 live PPTX-export files are untouched (they are live-only changes).
