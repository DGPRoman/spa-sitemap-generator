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

```bash
cp config.example.json config.json     # then set "base_url"
spa-sitemap new                        # crawl from scratch
spa-sitemap export                     # write sitemap.xml
```

## Commands

| Command  | Purpose |
|----------|---------|
| `new`    | Discard existing crawl data and crawl from scratch. Prompts first if there is data to lose (`-y` skips). |
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

Exit codes: `0` success, `1` error, `2` bad usage, `130` interrupted.

Pressing Ctrl-C once finishes the page in flight and commits progress, so `update`
picks up cleanly. Pressing it twice aborts immediately.

## Configuration

`config.json`, or any file passed with `-c`. Only `base_url` is required.

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
| `base_url` | *required* | Where to start. Also defines the crawl scope. |
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
