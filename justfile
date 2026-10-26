set shell := ["bash", "-euo", "pipefail", "-c"]

site := "https://skupper.io/"
# Preserve the existing default output; other origins get their own directory.
out := if site == "https://skupper.io/" { "out" } else { "out/" + replace(replace_regex(site, "^https?://([^/]+).*$", "$1"), ":", "_") }

# List available commands.
default:
    @just --list

# Install or synchronize Python dependencies.
setup:
    uv sync

# Crawl the selected site, reusing cached HTML (optional page limit).
scan max_pages="1000":
    uv run skupper-audit scan --seed {{quote(site)}} --output-dir {{quote(out)}} --max-pages {{quote(max_pages)}} > /dev/null

# Download fresh HTML from the selected site (optional page limit).
refresh max_pages="1000":
    uv run skupper-audit scan --seed {{quote(site)}} --output-dir {{quote(out)}} --max-pages {{quote(max_pages)}} --refresh > /dev/null

# Generate a Markdown audit report from the existing inventory.
report:
    uv run skupper-audit report --input {{quote(out + "/pages.jsonl")}} > {{quote(out + "/report.md")}}

# Create or replace the SQLite inventory database.
db:
    uv run python create_db.py --input {{quote(out + "/pages.jsonl")}} --output {{quote(out + "/pages.db")}}

# Query the SQLite database (requires the sqlite3 CLI).
query sql="SELECT status, COUNT(*) AS pages FROM page_summary GROUP BY status;":
    sqlite3 -header -column {{quote(out + "/pages.db")}} {{quote(sql)}}

# Restore cached HTML into a locally browsable site, without network access.
restore:
    uv run python recreate_site.py --input {{quote(out + "/pages.jsonl")}} --output-dir {{quote(out + "/site")}}

# Restore pages and download same-origin assets from the live site.
restore-assets:
    uv run python recreate_site.py --input {{quote(out + "/pages.jsonl")}} --output-dir {{quote(out + "/site")}} --fetch-assets

# Serve the restored site on localhost (stop with Ctrl-C).
serve port="8000":
    uv run python -m http.server {{quote(port)}} --bind 127.0.0.1 --directory {{quote(out + "/site")}}

# Build report, database and restored HTML from an existing scan.
build: report db restore

# Scan the selected site, then build local outputs; stops if the scan fails.
audit: scan build

# Run the offline test suite.
test:
    uv run python -m unittest discover -s tests -v

# Compare two existing crawl directories by path (defaults: skupper.io vs Netlify).
diff left="out" right="out/skupper-docs.netlify.app":
    uv run python compare_sites.py --left {{quote(left + "/pages.jsonl")}} --right {{quote(right + "/pages.jsonl")}}

# Include detailed unified diffs of extracted Markdown.
diff-text left="out" right="out/skupper-docs.netlify.app":
    uv run python compare_sites.py --left {{quote(left + "/pages.jsonl")}} --right {{quote(right + "/pages.jsonl")}} --text-diff
