# Clean-Checkout Acceptance — July 28, 2026

## Verdict

The integrated offline product installs and passes its complete backend and
dashboard gates from a fresh clone of GitHub `main`. This proves deterministic
offline acceptance; it does not substitute for the still-required retained live
Gemini run.

## Fresh-clone evidence

- Repository: `trace-watai/trace`
- Reviewed commit: `2520c4633990a578f146a879f7cb058711c66817`
- Source: a new clone from `origin`, not an existing developer checkout
- Python: fresh virtual environment followed by `pip install -e ".[dev]"`
- Node: `npm ci` from `apps/dashboard/package-lock.json`

The following commands passed:

```text
./scripts/check_repo.sh
TRACE_RUNS_DIR=<temporary-directory> ./scripts/demo.sh
npm run format:check
npm run lint
npm run typecheck
npm run test:run
npm run build
```

Observed results:

- Ruff lint and format checks passed.
- All 379 backend tests passed.
- The pipeline smoke run passed.
- The integrated offline demo produced both the expected harmful failure bundle
  and the valid sibling pass.
- All 30 dashboard tests passed.
- Dashboard formatting, linting, type checking, and the production build passed.

## Continuous acceptance

The `Integration CI` workflow repeats those backend and dashboard command
sequences on every pull request and every push to `main`. Its two independent
status checks are `Backend gate` and `Dashboard gate`.

## Dependency-audit boundary

The dashboard dependency graph was updated within the supported Next.js 15.5
line:

- Next.js and `eslint-config-next`: `15.5.22`
- PostCSS: `8.5.24`
- npm's compatible lockfile repairs, including `js-yaml`, `nanoid`, and
  `brace-expansion` patch releases

This removed the directly patchable Next.js server-action/SSRF/cache,
PostCSS, js-yaml, and earlier brace-expansion findings. The current npm audit
still reports high-severity nodes propagated from two root advisories:

1. Next.js 15.5.22 optionally installs Sharp 0.34.x, while the advisory requires
   Sharp 0.35.0 or later; that version is outside Next 15.5's declared supported
   range.
2. The Next/ESLint toolchain still reaches older `brace-expansion` through
   `minimatch`; npm offers only framework/tooling major changes as automatic
   remediation.

No `npm audit fix --force`, framework downgrade, or unsupported transitive
override was used. Those remaining upstream-compatible upgrades require a
separate reviewed compatibility change and must not be represented as resolved.

## Explicitly outside this proof

- No `GEMINI_API_KEY` was available, so no real provider call was made.
- This clean-checkout proof does not establish broad task-family coverage, five
  distinct failure bundles, or arbitrary stored-run dashboard loading.
