# spa-sitemap-generator

Generates a `sitemap.xml` for a JavaScript-rendered site by crawling it in a real
browser.

Most sitemap tools fetch HTML and parse it. On a single-page application that
returns almost nothing useful: the navigation is built by JavaScript after load, so
a source-only crawler sees zero links. This tool drives headless Chrome, waits for
the page to actually render, and reads the links out of the live DOM.

Crawl state lives in SQLite, so a crawl of thousands of pages can be interrupted and
resumed instead of started over.

## Requirements

- Python 3.11+
- Google Chrome (or Chromium) installed

Selenium 4.6+ downloads and manages the matching chromedriver itself — there is
nothing to put on your `PATH`.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .                 # or: pip install -r requirements.txt
```

Installing with `-e .` gives you the `spa-sitemap` command. Without it, use
`python -m spa_sitemap` or `python run.py`; all three are equivalent.

## Quick start

No config file needed -- the URL is an argument:

```bash
spa-sitemap new --url https://example.com/   # crawl from scratch
spa-sitemap export                           # write sitemap.xml
```

For a site you crawl repeatedly, put the settings in a file instead:

```bash
cp config.example.json config.json           # then set "base_url"
spa-sitemap new
```

## Commands

| Command  | Purpose |
|----------|---------|
| `new`    | Discard existing crawl data and crawl from scratch. Prompts first if there is data to lose (`-y` skips), and refuses outright if the database belongs to a *different* site (`--force` overrides). |
| `update` | Resume: visit whatever is still queued. Safe to run after a crash or Ctrl-C. |
| `export` | Write the successfully-crawled URLs to `sitemap.xml`. |
| `status` | Show counts, and the URLs that failed or were skipped. |

Useful flags (`spa-sitemap <command> --help` for the full list):

```
--url URL            override base_url for this run
--max-pages N        stop after N pages
--max-depth N        do not follow links deeper than N
--max-runtime SEC    stop after SEC seconds
--delay SEC          wait between pages
--no-headless        show the browser window (for debugging a stubborn site)
--wait-for SELECTOR  wait for a CSS selector on every page before reading links
--ignore-robots      crawl URLs robots.txt disallows
--ignore-canonical   do not collapse duplicates onto their rel=canonical URL
--lastmod WHEN       export: 'visited', 'today', or YYYY-MM-DD
--allow-empty        export: write a sitemap even when nothing was crawled
-v / -q              more or less logging
```

Exit codes: `0` success, `1` error, `2` bad usage, `130` interrupted. A crawl that
rendered nothing while failing URLs exits `1`, so a cron job cannot mistake a dead
site for an up-to-date sitemap.

Pressing Ctrl-C once finishes the page in flight and commits progress, so `update`
picks up cleanly. Pressing it twice aborts immediately.

## Crawling more than one site

One database holds one site. Give each site its own file:

```bash
spa-sitemap new --url https://a.example/ --database db/a.db
spa-sitemap export --database db/a.db -o a.xml

spa-sitemap new --url https://b.example/ --database db/b.db
spa-sitemap export --database db/b.db -o b.xml
```

Pointing `new` at a database that already holds another site is an error rather
than a silent wipe, and that check is deliberately not waived by `-y` — under cron
there is nobody to prompt, which is exactly where a crawl would otherwise vanish.
A "site" here is the whole scope, not just the host: `example.com/docs/` and
`example.com/blog/` are two sites and cannot share a database.

## When things go wrong mid-crawl

A crawl that runs for hours will meet a dropped connection, a rate limit, or a
browser that dies. What happens next depends on who is at fault, which the
renderer works out from what the browser said:

| Fault | Examples | The URL pays | The run pays |
|-------|----------|--------------|--------------|
| The URL | `404`, `410`, a page that never settles | one attempt, then a growing wait; `failed` once they run out | nothing |
| The site | DNS failure, refused or reset connection, expired certificate | nothing — its attempt is refunded | counts towards `max_consecutive_failures` |
| The browser | crashed tab, dead chromedriver, invalid session | nothing — its attempt is refunded | costs one of `max_restarts` |

Only `408`, `425`, `429` and `5xx` are worth another navigation; other `4xx` are
not. Treating `429` as permanent used to drop real pages from the sitemap of any
site that rate-limits mid-crawl.

Three limits bound the damage:

- **Per URL**, `max_attempts` (default 3) retries a transient failure before the
  URL is recorded as `failed`.
- **Per run**, `max_consecutive_failures` (default 10) abandons the whole crawl
  once that many renders fail in a row with no success in between. An unreachable
  site fails *every* page it is handed, so without this the crawl would walk the
  entire frontier converting it into permanent failures — and since only `queued`
  URLs are ever claimed, `update` could not recover them. Stopping early leaves
  the frontier intact, so `update` resumes once the site is back.
- **Per browser**, `max_restarts` (default 3) bounds how many replacement Chromes
  one run will start. A dead browser is bounded here rather than by the failure
  streak, because a crash says nothing about the site.

A retry is *scheduled*, not slept on. The wait is stored on the row, so the loop
moves straight on to other URLs instead of blocking on one sick page — and a wait
kept only in memory would be erased by exactly the crash it exists to survive,
sending the next run straight back at the same URL. Waits grow 2s, 8s, 32s… to a
five-minute ceiling, with ±25% jitter so a wave of URLs that failed together does
not return in lockstep. A site-wide failure is paced rather than escalated:
deciding when to give up on a site is `max_consecutive_failures`' job, and an
escalating curve there fights it — measured against a real dead server it reached
the ceiling within a handful of failures and stalled the run for minutes before
abandoning a site it already knew was unreachable.

The refund matters more than it sounds. `claim` counts an attempt *before*
rendering, which is what stops a page that crashes Chrome from being retried for
ever — but when the browser or the site was at fault the URL never got a fair
navigation, so charging it is how an outage exhausts pages that were perfectly
fine. A crawl cut short by a dead chromedriver or a site outage now costs the
frontier nothing: everything not yet rendered is still `queued`.

Browser restarts are counted in the crawl summary, because an automatic recovery
is a quiet degradation — a run that replaced Chrome forty times should not read as
identical to one that never did.

A crawl that ends with every remaining URL still waiting out a backoff stops with
`still-backing-off` and exits `1`, rather than claiming the frontier was empty.
Reporting otherwise would recreate the contradiction where one command insists
work remains and another insists it does not.

## The database

`db/sitemap.db` carries a schema version. An older file is migrated forward in
place on open — add-column only, so a migration never rewrites a table full of
your crawl — and a file written by a *newer* build is refused rather than opened,
because the alternative is silently downgrading a database somebody is still
using.

## Configuration

Entirely optional. `--url` alone is enough to run, so a target URL never has to be
written into a file to be crawlable. When `config.json` exists it is used; a file
named with `-c` must exist, so a mistyped path fails instead of being ignored.
Command-line flags override file values.

```json
{
  "base_url": "https://example.com/",
  "delay": 1.0,
  "max_pages": null,
  "exclude_patterns": ["/logout\\b", "\\?action=delete"]
}
```

| Key | Default | Meaning |
|-----|---------|---------|
| `base_url` | *required* | Where to start; also defines the crawl scope. Or pass `--url`. |
| `delay` | `1.0` | Seconds between page loads. |
| `respect_robots` | `true` | Obey robots.txt, including `Crawl-delay`. |
| `user_agent` | `spa-sitemap-generator` | Sent by the browser and when fetching robots.txt. |
| `headless` | `true` | Run Chrome without a window. |
| `window_size` | `[1440, 980]` | Viewport, which can change what a responsive site renders. |
| `page_load_timeout` | `30.0` | Give up on a page load after this long. |
| `settle_timeout` | `8.0` | How long to wait for the DOM to stop changing. |
| `wait_for_selector` | `null` | CSS selector that must appear before links are read. |
| `include_subdomains` | `false` | Treat `sub.example.com` as in scope. |
| `restrict_to_path` | `true` | A base of `/docs/` crawls only `/docs/`. |
| `keep_query` | `true` | Keep query strings (minus tracking parameters). |
| `hash_routing` | `false` | Treat `#/route` fragments as distinct pages — see below. |
| `respect_canonical` | `true` | Collapse duplicates onto their `rel="canonical"` URL. |
| `strip_query_params` | tracking params | Parameters removed before a URL is compared. |
| `exclude_patterns` | `[]` | Regexes; a matching URL is never crawled. |
| `max_pages`, `max_depth`, `max_runtime` | `null` | Termination guards. |
| `max_attempts` | `3` | Tries per URL before giving up on it. |
| `max_consecutive_failures` | `10` | Abandon the run after this many failures in a row; `null` disables. |
| `max_restarts` | `3` | Replacement browsers to try before giving up on the run. |
| `database_path` | `db/sitemap.db` | Crawl state. |
| `output_path` | `sitemap.xml` | Sitemap destination. |

The original `{"url": ..., "delay": ...}` config still works — `url` is accepted as
an alias for `base_url`. Unknown keys are rejected with a suggestion, so a typo
cannot silently disable a setting.

### Hash-routed SPAs

If your routes look like `example.com/#/products`, set `"hash_routing": true`.
Fragments are normally stripped (so `/a#top` and `/a` are one page), but under hash
routing the fragment *is* the page, and stripping it would collapse the whole site
to a single URL. Plain anchors like `#top` are still stripped either way.

## How URLs are treated

Two URLs that render the same document should produce one sitemap entry. To that
end, each discovered link is canonicalised: the scheme is normalised (`http` and
`https` are the same origin), the host is lowercased, default ports and fragments
are dropped, tracking parameters are removed, and the remaining query parameters are
sorted.

Links are dropped when they are off-scope, not `http(s)` (`mailto:`, `tel:`,
`javascript:`), or point at an asset extension (`.pdf`, `.jpg`, `.zip`, …).

Beyond that:

- **Redirects.** A URL that redirects is not itself a page. It is recorded as
  `redirected` and the content is attributed to the target, so `/about` and
  `/about/` do not both appear.
- **`rel="canonical"`.** When a page names a different URL as canonical, the
  canonical URL becomes the page and the requested one is recorded as `duplicate`.
  This is the only way to detect that `/products/` and `/products/index.html` are one
  document, since both return 200. Self-referencing and out-of-scope canonicals are
  ignored, so a misconfigured tag cannot empty your sitemap.
- **HTTP status.** WebDriver does not expose status codes, so the Chrome DevTools
  performance log is read to find them. A 4xx page is never exported — without this,
  every 404 lands in the sitemap as a real URL. If the log is unavailable the crawl
  continues without status detection.

Each URL is in exactly one state: `queued`, `done`, `failed`, `skipped`,
`redirected`, or `duplicate`. Only `done` URLs are exported.

## Output

A standard urlset. Past 50 000 URLs or 50 MB the output is split into
`sitemap-1.xml`, `sitemap-2.xml`, … with `sitemap.xml` becoming the sitemap index,
as the protocol requires. Writes are atomic, so a failure cannot truncate the
sitemap you are already serving.

`--lastmod visited` uses the date each page was actually crawled.

## Layout

```
spa_sitemap/
  urls.py        canonicalisation + scope rules (pure functions, no I/O)
  store.py       SQLite frontier and results
  renderer.py    Renderer protocol + ChromeRenderer (the only Selenium code)
  crawler.py     the crawl loop
  sitemap.py     sitemap.xml / sitemap index writing
  robots.py      robots.txt
  config.py      config loading and validation
  cli.py         argument parsing, command wiring, exit codes
```

The crawl loop depends on a `Renderer` protocol rather than on Selenium, and takes
its store, clock and sleep as arguments. That is what lets the entire loop — retries,
redirects, limits, interruption — be tested in milliseconds with no browser.

## Development

```bash
pip install -e ".[dev]"

pytest                      # everything (starts Chrome for the end-to-end tests)
pytest -m "not browser"     # no browser needed
ruff check .
mypy
```

`tests/fixtures/site/` is a small site whose links are injected by JavaScript after
load. `tests/test_end_to_end.py` asserts that its raw HTML contains no `<a>` tags at
all — if the browser layer stops working, that test fails rather than quietly
producing an empty sitemap.

## Limitations

- One page at a time in one browser. Rendering is the bottleneck and parallel Chrome
  instances multiply memory use; a polite crawler wants a delay between requests
  anyway.
- Only `<a href>` links are found. Routes reachable solely by clicking a JavaScript
  handler, or listed only in an existing sitemap, are not discovered.
- Duplicate content is detected via redirects and `rel="canonical"` only. Two
  distinct URLs serving identical content with no canonical tag will both be
  exported; use `exclude_patterns` to suppress one.
