# Skupper website content audit

A small Trafilatura-based CLI for collecting and analysing the **current `skupper.io` site**. It excludes `/v1/` and its descendants. The default seed is the homepage so the crawl can reach the current docs, getting-started material, examples, and CLI references—not just `/docs/`.

**What it does:** discover URLs through Trafilatura's sitemap discovery and HTML links; fetch HTML politely; extract Markdown and metadata; produce a JSONL inventory and a Markdown content report. Raw HTML is cached so extraction settings can be changed without downloading every page again.

**What it does not do:** execute JavaScript, prove every published page was found, validate conceptual correctness, or perform semantic duplicate detection. This is a content-audit starting point, not a production web crawler.

## Requirements

- Python 3.10 or later
- [`uv`](https://docs.astral.sh/uv/) (recommended; a standard Python virtual environment also works)
- Network access to `skupper.io` for the initial scan

## Install and run

```bash
uv sync

# Inspect the options
uv run skupper-audit --help
uv run skupper-audit scan --help

# Crawl all discoverable current HTML pages; JSONL goes to stdout.
uv run skupper-audit scan --output-dir out | tee out-scan.jsonl

# Produce a readable report (stdout Markdown).
uv run skupper-audit report --input out/pages.jsonl > out/report.md
```

By default, the scanner tries the site's sitemap and then follows internal page links, using a **0.5-second minimum delay between page HTTP requests** (sitemap discovery has its own delay). It stops at 1,000 pages to avoid an accidental unbounded crawl. Raise the limit if `out/run.json` reports `"truncated": true`:

```bash
uv run skupper-audit scan --max-pages 3000 --output-dir out > out-scan.jsonl
```

The scanner writes diagnostics to **stderr**, not stdout. Exit code **0** means no recorded errors or crawl truncation; **1** signals errors; **2** signals an incomplete run due to page limit (or extraction/fetch errors). Failed URLs remain recorded in the manifest.

## Shortcuts with just

With `just` and `uv` installed, use the included `justfile`:

```bash
just                     # List commands
just setup               # Install Python dependencies
just audit               # Scan skupper.io, then generate report, DB and local HTML

# Work with the existing out/pages.jsonl without another crawl:
just build               # Generate report, DB and local HTML
just query               # Show page counts by crawl status
just query 'SELECT title, words FROM page_summary ORDER BY words DESC LIMIT 10;'
just restore-assets      # Also download live site assets
just serve               # Preview at http://localhost:8000/ (Ctrl-C to stop)
just test                # Run offline tests
```

Individual commands include `just scan`, `just refresh`, `just report`, `just db`,
and `just restore`. Use `just scan 3000` to raise the page limit or
`just serve 8080` to change the preview port. `refresh` downloads fresh page HTML;
`scan` reuses cached HTML but still makes discovery and robots requests.
`query` requires the `sqlite3` CLI and an existing database.

All output defaults to `out/`; override it with, for example,
`just --set out sample scan`. The `audit` command stops if the crawler reports
errors or truncation. Inspect the scan results, then run `just build` to work
with the captured pages even if some URLs failed. The default `build` restores
HTML only; run `restore-assets` to fetch stylesheets, images, and scripts.

## Results

```text
out/
├── html/             # One cached original HTML file per canonical URL
├── markdown/         # One extracted Markdown file per successful page
├── pages.jsonl       # Machine-readable metadata and metrics for each page
├── run.json          # Discovery, completion, and error counters
└── report.md         # Generated in the second command
```

The HTML/Markdown filenames are deterministic URL hashes. Use `pages.jsonl` to map a page URL to its files. Each successful record contains URL, title, date, author, word count, heading count, code-block count, extracted-body hash, internal links, selected terminology counts and relative file paths. Failed/blocked pages contain an error or reason.

Example `jq` queries:

```bash
# Page URLs and title
jq -r 'select(.status == "ok") | [.url, .title] | @tsv' out/pages.jsonl

# Short content candidates
jq -r 'select(.status == "ok" and .words < 200) | [.words, .url] | @tsv' out/pages.jsonl | sort -n

# Find errors
jq -r 'select(.status != "ok") | [.status, .url, .error] | @tsv' out/pages.jsonl

# Where does "routing key" occur in the actual extracted content?
rg -n -i 'routing key' out/markdown/
```

`report` includes site sections, terminology frequency, short pages, **exact** duplicates, unresolved discovered URLs, and failures. A page listed as *discovered but not scanned* is **not necessarily a broken link**: it may simply be outside a limited scan.

## Query the inventory with SQL

Create a SQLite database using Python's standard library (no extra packages):

```bash
python create_db.py --input out/pages.jsonl --output out/pages.db

# Open an interactive SQL session (requires the sqlite3 CLI).
sqlite3 -header -column out/pages.db
```

Run the import again after a new scan to replace the inventory. Repeated imports
do not accumulate duplicates. Input validation and transactional updates protect
the previous inventory if an import fails. Failed/blocked crawl records are
included, with unavailable fields represented as SQL `NULL`.

The `pages` table retains each complete record in its `data` JSON column. Three
views provide convenient columns: `page_summary` for metadata and metrics,
`page_terms(url, term, mentions)` for terminology counts, and
`page_links(source_url, target_url)` for discovered internal links. These views
require SQLite JSON functions, included in current Python SQLite builds.

Paste these queries into the SQL session or a SQLite database browser:

```sql
-- Shortest successful pages
SELECT title, url, words
FROM page_summary
WHERE status = 'ok'
ORDER BY words
LIMIT 20;

-- Crawl failures and blocked pages
SELECT url, status, error, reason
FROM page_summary
WHERE status <> 'ok';

-- Pages mentioning TLS; use term = 'routing key' for that phrase
SELECT url, mentions
FROM page_terms
WHERE term = 'tls' AND mentions > 0
ORDER BY mentions DESC;

-- Terminology totals across the captured site
SELECT term, SUM(mentions) AS total_mentions
FROM page_terms
GROUP BY term
ORDER BY total_mentions DESC;

-- Pages with the same extracted-body hash
SELECT body_sha256, COUNT(*) AS page_count, GROUP_CONCAT(url, char(10)) AS urls
FROM page_summary
WHERE status = 'ok' AND body_sha256 IS NOT NULL
GROUP BY body_sha256
HAVING COUNT(*) > 1;

-- Links whose targets have no successful crawl record (not proof of a broken link)
SELECT DISTINCT l.source_url, l.target_url
FROM page_links AS l
LEFT JOIN page_summary AS p ON p.url = l.target_url AND p.status = 'ok'
WHERE p.url IS NULL;

-- Access any original JSON field directly
SELECT json_extract(data, '$.url') AS url,
       json_extract(data, '$.terms."routing key"') AS mentions
FROM pages
WHERE json_extract(data, '$.terms."routing key"') > 0;
```

This database contains inventory records and metrics, not cached HTML or Markdown
text. Use the `html_file` and `markdown_file` columns to locate those files relative
to the input manifest's directory.

## Recreate the captured website

Restore the cached original HTML to its original URL paths:

```bash
python recreate_site.py --input out/pages.jsonl --output-dir out/site

# Also download same-origin stylesheets, scripts, images and CSS dependencies.
python recreate_site.py --input out/pages.jsonl --output-dir out/site --fetch-assets

# Open http://localhost:8000/ after starting this server.
python -m http.server 8000 --directory out/site
```

The script uses only the Python standard library. Keep `pages.jsonl` alongside
its `html/` cache. It maps directory URLs to `index.html`, preserves HTML file
paths, and rewrites same-origin HTML links for local browsing. Existing page
files in the destination are overwritten; existing assets are reused. Failed
crawl records are skipped and counted. Asset download failures return exit code 1.

Without `--fetch-assets`, restoration is entirely offline. Downloaded assets come
from the current live site and may differ from the captured version. External
fonts and other external resources still require network access. This is a
best-effort static restoration: missing pages, `srcset` variants, assets loaded
by JavaScript, and absolute URLs inside CSS or JavaScript are not reconstructed.

## Re-extract without downloading pages

Run the scan again against the same output directory. Previously fetched HTML is reused unless you supply `--refresh`. The tool still checks `robots.txt` and, by default, the sitemap; use `--no-sitemap` to skip sitemap requests when experimenting with extraction options:

```bash
# Compare the precision preset against the standard extractor.
# Use separate directories if you want to keep both Markdown outputs.
cp -a out out-precision
uv run skupper-audit scan --no-sitemap --precision --output-dir out-precision > precision.jsonl
uv run skupper-audit report --input precision.jsonl > out-precision/report.md
```

The `--precision` option reduces boilerplate but may remove useful text. The default uses Trafilatura's normal extraction cascade. The source HTML is always retained, and the script deliberately avoids Trafilatura's cross-document deduplication, since identifying duplicates is part of this analysis.

## Input through stdin or a file

Passing an explicit URL list disables link traversal, which makes one-page jobs deterministic and suitable for pipelines or GNU Parallel. URLs outside `skupper.io`, `/v1/` links, non-HTML resources, and duplicate canonical URLs are ignored.

```bash
# Single-page smoke test
printf '%s\n' 'https://skupper.io/docs/' | \
  uv run skupper-audit scan --urls - --output-dir sample | \
  tee sample-pages.jsonl

# List from a file (one URL per line)
uv run skupper-audit scan --urls pages.txt --output-dir subset > subset.jsonl

# Parallel extraction: separate output directories prevent collisions.
# Keep request rates modest and respect the site's robots policy.
cat pages.txt | parallel --pipe -N 10 \
  'uv run skupper-audit scan --urls - --output-dir "worker-{#}"' \
  > combined.jsonl
uv run skupper-audit report --input combined.jsonl > combined-report.md
```

For large sites, don't increase concurrency aggressively. The crawler only accepts the exact `skupper.io` host and obeys `robots.txt` by default. `--ignore-robots` exists for environments where permission has been explicitly established.

## Tests

Offline unit tests cover canonical URLs, v1 exclusion, HTML link discovery, a three-page test crawl, cache reuse, and a golden-file Markdown report.

```bash
uv run python -m unittest discover -s tests -v

# Explicitly compare the fixture report against its golden file
uv run skupper-audit report --input tests/fixtures/pages.jsonl | \
  diff -u tests/fixtures/report.md -
```

To change the report intentionally, update `tests/fixtures/report.md` after reviewing the difference. No network access is needed for tests.

## Known limitations and next steps

1. **Coverage:** sitemap discovery plus link traversal is best effort, not a guarantee. JavaScript-generated routes, unlinked pages and pages blocked by `robots.txt` may be missing. Always inspect `run.json` and errors in `pages.jsonl`.
2. **Duplicate detection:** exact hash comparisons after case/whitespace normalization; near-duplicate prose and repeated paragraphs need a separate similarity pass.
3. **Navigation:** the `links` field is extracted from *all HTML anchors*, including site navigation, while text and terminology metrics come from Trafilatura's extracted main content.
4. **Missing embedded content:** JavaScript widgets and dynamically loaded content are not rendered. Inspect cached HTML and the live page if Markdown looks incomplete.
5. **Source comparison:** a useful second phase is joining URLs with the Markdown/AsciiDoc sources in `skupperproject/skupper-docs` to identify missing or duplicated topics.

Relevant documentation: [Trafilatura quickstart](https://trafilatura.readthedocs.io/en/stable/quickstart.html), [Python usage](https://trafilatura.readthedocs.io/en/stable/usage-python.html), [core functions](https://trafilatura.readthedocs.io/en/stable/corefunctions.html), [Skupper docs](https://skupper.io/docs/).
