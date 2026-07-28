# spa-sitemap-generator

Generates `sitemap.xml` for a JavaScript-rendered site by crawling it in a real
browser.

Most sitemap tools fetch HTML and parse it. On a single-page application the
navigation is built by JavaScript after load, so a source-only crawler sees zero
links. This one drives headless Chrome, waits for the page to render, and reads the
links out of the live DOM.

Requires Python 3.11+ and Google Chrome. Selenium manages chromedriver itself.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Use

```bash
spa-sitemap new --url https://example.com/    # crawl from scratch
spa-sitemap export                             # write the sitemap
```

That writes `sites/example.com/sitemap.db` and `sites/example.com/sitemap.xml`.
There is nothing to configure.

| Command | |
|---------|--|
| `new`    | Crawl from scratch, discarding this site's previous crawl. |
| `update` | Resume — after Ctrl-C, a crash, or the site going down. |
| `export` | Write `sitemap.xml` from the pages crawled successfully. |
| `status` | Progress, and what failed or was skipped. |
| `sites`  | Everything crawled so far. |

State lives in SQLite, so a crawl of thousands of pages can be interrupted and
resumed rather than restarted. One Ctrl-C finishes the page in flight and commits;
a second aborts.

Exit codes: `0` success, `1` error, `2` bad usage, `130` interrupted. A crawl that
rendered nothing while failing URLs exits `1`, so cron cannot mistake a dead site
for a current sitemap.

## Several sites

Each URL gets its own directory, named after it. No flags:

```bash
spa-sitemap new --url https://a.example/
spa-sitemap new --url https://b.example/
spa-sitemap sites
```

```
site        done  queued  url
a.example   1204       0  https://a.example/
b.example     87      12  https://b.example/
```

One site: every command needs no arguments. Several: name one with
`--site a.example`, or identify it by `--url`. A bare `export` or `status` uses the
only site there is, and otherwise lists the candidates instead of guessing.

Delete a site with `rm -rf sites/a.example`.

The directory is named after the whole crawl scope, not the host, because
`example.com/docs/` and `example.com/blog/` are two different crawls — they get
`sites/example.com_docs/` and `sites/example.com_blog/`. Ports count too, so two dev
servers on `localhost` stay apart.

## Options

```
--url URL            the site to crawl; also decides where its files go
--site NAME          which crawl to act on, when several exist
--max-pages N        stop after N pages
--max-depth N        do not follow links deeper than N
--max-runtime SEC    stop after SEC seconds
--delay SEC          wait between pages
--no-headless        show the browser window
--wait-for SELECTOR  wait for a CSS selector before reading links
--ignore-robots      crawl URLs robots.txt disallows
--ignore-canonical   do not collapse duplicates onto their rel=canonical URL
--lastmod WHEN       export: 'visited', 'today', or YYYY-MM-DD
--allow-empty        export: write a sitemap even when nothing was crawled
-y                   new: do not ask before discarding
-v / -q              more or less logging
```

Trying a new site: `--max-pages 20 -v`, then `status`. Zero links found means the
page renders differently than expected — `--no-headless` shows you why.

## Config file

Optional. `config.json` is used when present; `-c other.json` must exist.
Command-line flags win. The keys:

| Key | Default | |
|-----|---------|--|
| `base_url` | — | Where to start; also the crawl scope. Or `--url`. |
| `delay` | `1.0` | Seconds between page loads. |
| `respect_robots` | `true` | Obey robots.txt, including `Crawl-delay`. |
| `user_agent` | `spa-sitemap-generator` | Sent by the browser and for robots.txt. |
| `headless` | `true` | Run Chrome without a window. |
| `window_size` | `[1440, 980]` | Viewport; changes what a responsive site renders. |
| `page_load_timeout` | `30.0` | Give up on a page load after this long. |
| `settle_timeout` | `8.0` | How long to wait for the DOM to stop changing. |
| `wait_for_selector` | `null` | Must appear before links are read. |
| `include_subdomains` | `false` | Treat `sub.example.com` as in scope. |
| `restrict_to_path` | `true` | A base of `/docs/` crawls only `/docs/`. |
| `keep_query` | `true` | Keep query strings, minus tracking parameters. |
| `hash_routing` | `false` | Treat `#/route` as a distinct page. |
| `respect_canonical` | `true` | Collapse duplicates onto their canonical URL. |
| `strip_query_params` | tracking params | Removed before URLs are compared. |
| `exclude_patterns` | `[]` | Regexes; a match is never crawled. |
| `max_pages`, `max_depth`, `max_runtime` | `null` | Termination guards. |
| `max_attempts` | `3` | Tries per URL before giving up on it. |
| `max_consecutive_failures` | `10` | Abandon the run after this many failures in a row. |
| `max_restarts` | `3` | Replacement browsers before giving up. |
| `sites_dir` | `sites` | Where the per-site directories live. |

Unknown keys are rejected with a suggestion, so a typo cannot silently disable a
setting.

If your routes look like `example.com/#/products`, set `"hash_routing": true`.
Fragments are normally stripped, but under hash routing the fragment *is* the page.
Plain anchors like `#top` are stripped either way.

## How URLs are treated

Two URLs that render the same document should produce one sitemap entry, so each
link is canonicalised: `http`/`https` unified, host lowercased, default ports and
fragments dropped, tracking parameters removed, remaining query sorted. Off-scope
links, non-`http(s)` schemes and asset extensions are dropped.

- **Redirects** — a URL that redirects is not a page. It is recorded as `redirected`
  and the content attributed to the target, so `/about` and `/about/` do not both
  appear.
- **`rel="canonical"`** — the canonical URL becomes the page, the requested one a
  `duplicate`. The only way to tell `/products/` from `/products/index.html` when
  both return 200.
- **HTTP status** — read from the Chrome DevTools log, since WebDriver does not
  expose it. Without this every 404 lands in the sitemap as a real URL.

Each URL is in exactly one state: `queued`, `done`, `failed`, `skipped`,
`redirected` or `duplicate`. Only `done` is exported.

## When things break mid-crawl

What happens depends on who is at fault, which the renderer works out from what the
browser said:

| Fault | Examples | The URL pays | The run pays |
|-------|----------|--------------|--------------|
| The URL | `404`, a page that never settles | an attempt, then a growing wait | nothing |
| The site | DNS failure, refused connection, bad certificate | nothing — its attempt is refunded | one of `max_consecutive_failures` |
| The browser | crashed tab, dead chromedriver | nothing — its attempt is refunded | one of `max_restarts` |

Only `408`, `425`, `429` and `5xx` are retried; other `4xx` are not.

Retries are scheduled, not slept on: the wait is stored on the row, so the loop
moves on to other URLs and a crash cannot erase it. Waits grow 2s, 8s, 32s… to a
five-minute ceiling, jittered so URLs that failed together do not return in
lockstep.

The refund matters most. An attempt is counted *before* rendering, so a page that
crashes Chrome cannot be retried forever — but when the browser or the site was at
fault, charging the URL is how an outage exhausts pages that were perfectly fine. A
crawl cut short by either now costs the frontier nothing: everything not yet
rendered is still `queued`, and `update` finishes it.

## Output

A standard urlset. Past 50 000 URLs or 50 MB it splits into `sitemap-1.xml`,
`sitemap-2.xml`, … with `sitemap.xml` becoming the index, as the protocol requires.
Writes are atomic, so a failure cannot truncate a sitemap you are already serving.

The database carries a schema version: an older file is migrated forward in place,
and one written by a newer build is refused rather than silently downgraded.

## Layout

```
spa_sitemap/
  urls.py        canonicalisation + scope rules (pure, no I/O)
  sites.py       where each site's files live
  store.py       SQLite frontier and results
  renderer.py    Renderer protocol + ChromeRenderer (the only Selenium code)
  crawler.py     the crawl loop
  sitemap.py     sitemap.xml / index writing
  robots.py      robots.txt
  config.py      config loading and validation
  cli.py         arguments, wiring, exit codes
```

The crawl loop depends on a `Renderer` protocol rather than on Selenium, and takes
its store, clock and sleep as arguments. That is what lets retries, redirects,
backoff, browser restarts and interruption all be tested in milliseconds with no
browser.

## Development

```bash
pip install -e ".[dev]"

pytest                      # everything (starts Chrome for the end-to-end tests)
pytest -m "not browser"     # no browser needed
ruff check . && mypy
```

`tests/fixtures/site/` is a small site whose links are injected by JavaScript after
load. `tests/test_end_to_end.py` asserts its raw HTML contains no `<a>` tags at all,
so if the browser layer stops working that test fails rather than quietly producing
an empty sitemap.

## Limitations

- One page at a time in one browser. Rendering is the bottleneck, parallel Chromes
  multiply memory, and a polite crawler wants a delay anyway.
- Only `<a href>` links are found. Routes reachable only by a JavaScript click, or
  listed only in an existing sitemap, are not discovered.
- Duplicate content is detected via redirects and `rel="canonical"` only. Two URLs
  serving identical content with no canonical tag will both be exported; use
  `exclude_patterns` to suppress one.
