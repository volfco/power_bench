# Decision log

## 2026-08-21 — Single LFS-driven build+deploy workflow [minor]
- Choice: `.github/workflows/deploy-report.yml` (checkout with `lfs: true` → pip duckdb → exporter self-check + export → upload-pages-artifact → deploy-pages); old `publish-data-report.yml`/`publish-report.yml` deleted; `power_meter.duckdb` tracked in `.gitattributes` via LFS.
- Why: user asked for one workflow that loads the DB from git LFS and deploys the static site; prior workflows referenced abandoned scripts.

## 2026-08-21 — SPA report replaces prior dashboard/report/GH-action implementations [major]
- Choice: single-file vanilla-JS SPA (`site/index.html`) + Chart.js CDN + Python exporter (`scripts/export_report_data.py` → `site/data.json`). Hash routing between Hosts / Configurations / Runs views.
- Why: user requested a fresh single-page GitHub-Pages report; no build step is the shortest path that works.

## 2026-08-21 — Config scale mapping [minor]
- Choice: optimization strings bucketed onto 0/25/75/100 via substring match (`map_scale`, most-powersave keys checked first so `epp=power` wins before `performance` sees `balance_performance`); unknown values render as "—".
- Why: user picked "map known names" option; DB currently empty so canonical labels are defined ahead of data.

## 2026-08-21 — All runs/readings exported into one data.json [minor]
- Choice: full dump (runs scalar fields + per-run timestamp/power/phase arrays), no server-side aggregation.
- Why: dataset size unknown but meter samples are ~1 Hz; client-side aggregation keeps the exporter dumb. Revisit if data.json exceeds a few MB.
