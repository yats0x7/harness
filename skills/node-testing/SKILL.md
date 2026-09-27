---
name: node-testing
description: Running and writing tests in JavaScript/TypeScript projects (node --test, tsx, jest, vitest, mocha). Use for any package.json repository.
---
# JavaScript and TypeScript tests

## Find the runner
- Read `package.json` `scripts.test` first; use exactly that runner. Common ones: `node --test`,
  `tsx --test` (TypeScript without a build step), `vitest run`, `jest`, `mocha`.
- If `node_modules` is missing, install with `npm ci` (or `pnpm i --frozen-lockfile`, `yarn --frozen-lockfile`
  when that lockfile exists).
- When `scripts.test` lists test files explicitly, a new test file will not run unless you add it there.
  Adding a case to an existing listed test file is simpler.

## Reproduce
- ESM projects (`"type": "module"` or `.mjs`): import by absolute path from a scratch script:
  `const m = await import(process.env.REPO + "/src/x.js")`.
- TypeScript: run scratch scripts with `npx tsx $SCRATCH/repro.ts`.

## Verify
- Run the single test file first (`npx tsx --test src/x.test.ts` or `node --test test/x.test.js`), then the
  full `npm test`. Also run `npm run type-check` or `npx tsc --noEmit` when the project has it.
