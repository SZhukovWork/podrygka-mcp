# podrygka-mcp

**English** · [Русский](README.ru.md)

An [MCP](https://modelcontextprotocol.io) server that gives LLM agents live,
honestly-labelled data from [podrygka.ru](https://www.podrygka.ru), the online
shop of the «Подружка» cosmetics chain: catalog search with the site's filters,
product cards with every discount explained, stock in each store of your city,
delivery terms, and reviews with the variant's own rating kept apart from the
product line's.

> Buyer side, anonymous: no account, login or cart. The server reads the same
> JSON API the site's pages use, with plain HTTP — no browser.

## Why this server

The shop's data is easy to fetch and easy to misread. Everything below was
found by checking the API against the pages a person sees:

| Pitfall on podrygka.ru | What this server does |
|---|---|
| The catalog API answers for **Moscow** unless a region is sent; the store-stock endpoint takes the region from cookies and silently falls back to Moscow | Resolves your city through the shop's own city directory, sends the region with every request, checks store answers against the region's store list and refuses foreign ones |
| "Available in 12 stores" counts the whole **region** (Yekaterinburg + Pervouralsk), not the city | Stores of the city and of the rest of the region are listed separately, with units, the site's level (мало/средне/много) and opening hours |
| The rating and review count in the catalog belong to the **whole product line**: all 20 shades of a lipstick show "4.1 · 92" | `rating_line` is labelled as such; `get_product` computes the variant's own rating from its own reviews and says "no own reviews" instead of borrowing the line's |
| The review widget's own counter is higher than the published reviews (117 vs 104) | Counts come from the published feed |
| Shades and volumes are separate articles with **different prices and stock** (801–951 ₽ within one lipstick line); one line can take several search tiles | Tiles of one line are merged (`group_variants`); every variant is listed with its own price and stock |
| From 09:00 to 12:00 Moscow time the site shows a **−5 % morning discount** to everyone, and it is gone at 12:00 | `discount_kind: morning_5pct`, `price_after_morning_window_rub` and a `morning_window` block in every answer |
| «Выгодная цена», «Выгодная пара» and «1+1=3» are **card-holder promotions** by the chain's rules, yet the site shows their prices to anonymous visitors | Each promotion carries `requires_loyalty_card` and the rule it rests on, marked "cart not checked" |
| «Выгодная пара» and «1+1=3» discounts are applied only in the cart | `price_effect` says the shown price does not include them |
| Delivery cost appears only at checkout | `cost_rub: null` with the reason, never 0 |
| Without `showAll=true` the delivery endpoint drops the pickup-point (ПВЗ) option the product page shows | Asks the way the product page does |
| An unknown store id in the filter is **silently ignored** — the "filtered" search returns everything | Store ids are checked against the region's store list; brand, category and promotion filters are verified on the results |
| The review provider (Aplaut) returns buyers' **e-mails and phone numbers** | Reviews pass through an allow-list: stars, text, pros, cons, date, verified, likes, photo count, time of use; covered by a test with fake personal data |
| A rating of 0 means "no reviews", not zero stars | No rating field then, `reviews_line: 0` |
| The price in a physical store can differ from the online price (the site says so) | Stated in `price_note` next to every price |

Every answer carries `fetched_at` (UTC), the `city` it was computed for (as the
shop's directory names it, and where the choice came from), `price_scope` and
`morning_window`.

## Tools

| Tool | What it returns |
|---|---|
| `search_products(query, page, sort, price_min, price_max, brand, category, in_store, promo_only, city, group_variants, limit)` | 20 tiles per page (fixed by the site); sort `popular` / `rating` / `price_asc` / `price_desc`; `total_found`, `total_pages`, `has_more`. Filters: price window, brand (name or `brand_code`), category (name or `category_code`), a store (`in_store`, ids from `list_stores`), price promotions only. Per item: online price, pre-discount price, `discount_kind`, promotions with their card rules, price per 100 ml/g, line rating and review count, number of variants and their price range, online stock, stock and stores in the region, delivery terms for the city, URL |
| `get_product(article, city, include_variants, include_stores, include_description)` | Everything above for one article plus: instalment split, every variant (shade/volume) with its own price, stock and delivery terms, stock per store (city vs rest of the region), delivery options with `cost_rub: null`, ratings — `shown_on_site` (line), `line` (computed from stars) and `variant` (its own reviews) — and characteristics (country, tone, texture, composition, usage, package dimensions and weight) |
| `get_reviews(article, scope, page, limit, sort)` | `scope=variant` (this article's own reviews) or `line`; `sort=newest` or `helpful`; up to 50 per page. Date, stars, text, pros, cons, `verified`, likes/dislikes, photo count, time of use; star split and average for the scope; the line rating the site shows. No author data |
| `get_store_availability(article, city)` | Units in each store of the city (address, level, hours, coordinates), stores elsewhere in the region, the online warehouse separately |
| `list_stores(city)` | Stores of the region with ids for `in_store`, addresses and how many catalog items each has in stock |
| `compare_products(articles, city)` | Up to 20 articles side by side in two requests; articles the site does not return are listed with a reason |
| `find_city(query)` | Cities in the shop's directory with their regions; tells how to name same-named towns (`"Березовский, Свердловская область"`) |

## Requirements

Measured on Linux (CachyOS), Python 3.12, from a residential IP in Russia:

| | |
|---|---|
| Python | ≥ 3.10, with [uv](https://docs.astral.sh/uv/) (or pip) |
| Browser | **Not needed**: plain HTTP (`requests`) |
| Docker, display | Not needed |
| Disk | ≈33 MB for the environment (`mcp`, `requests` and their dependencies); ≈0.1 MB of cache in `~/.cache/podrygka-mcp` |
| Memory | ≈73 MB RSS after start, ≈82 MB after a search and a product card |
| Start | The server is ready in ≈0.4 s (≈1.6 s via `uvx` from a local checkout, including the build) |
| Latency | Requests to the shop are spaced 1.5 s apart, so time is mostly pauses: first search with an empty cache ≈7 s (city lookup, guest token, results, delivery terms), next searches ≈4 s, a full product card ≈6–8 s. Each HTTP call itself takes 0.1–0.8 s |
| Network | Works from a Russian residential IP. Foreign, VPN and datacenter IPs were not tested; the shop sits behind the NGENIX CDN, which can block them. `PODRYGKA_PROXY` sets a proxy |

## Install

Claude Code:

```bash
claude mcp add podrygka -- uvx --from git+https://github.com/SZhukovWork/podrygka-mcp podrygka-mcp
```

Any MCP client (`claude_desktop_config.json`, `.mcp.json`, …):

```json
{
  "mcpServers": {
    "podrygka": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/SZhukovWork/podrygka-mcp", "podrygka-mcp"]
    }
  }
}
```

From a checkout: `uv venv && uv pip install -e . && .venv/bin/podrygka-mcp`.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `PODRYGKA_CITY` | `Екатеринбург` | Default city for stock and delivery (any tool also takes `city`). Use the name from `find_city`; add `, <region>` for same-named towns |
| `PODRYGKA_MIN_INTERVAL` | `1.5` | Seconds between requests to podrygka.ru. Keep it: the shop has no published limits and the IP is yours |
| `PODRYGKA_PROXY` | — | Proxy URL, e.g. `http://user:pass@host:3128` |
| `PODRYGKA_CACHE_DIR` | `~/.cache/podrygka-mcp` | Guest token, review-widget key, city, category and store lists (files `0600`, directory `0700`) |
| `PODRYGKA_TIMEOUT` | `25` | HTTP timeout, seconds |
| `PODRYGKA_LOG` | `INFO` | `DEBUG` logs every request (path, status, time) to stderr; tokens and keys are never logged |

## What the numbers mean

- **`price_online_rub`** — the big price on the product page for a visitor who
  is not signed in. It was the same for Moscow and the Sverdlovsk region on all
  184 positions compared; the price in a physical store may differ.
  **`price_before_discount_rub`** — the crossed-out price.
- **`discount_kind`** — `promo`: a chain promotion («Выгодная цена», SALE);
  `morning_5pct`: the morning −5 % (only between 09:00 and 12:00 MSK, then the
  price becomes `price_after_morning_window_rub`); `unknown`: a discount
  without a promotion shield that the server cannot attribute; `none`.
- **`requires_loyalty_card`** — what the chain's rules say: `true` for
  «Выгодная цена», «Выгодная пара», «1+1=3» and the morning discount, `false`
  for SALE, `"unknown"` for gifts. The site shows these prices to anonymous
  visitors; what the cart charges a guest was not checked.
- **Personal card discount** (3–15 %) is not included and cannot be computed
  without logging in; by the rules it does not stack with promotions and does
  not apply to CATRICE, ESSENCE, GILLETTE.
- **Ratings** — `rating_line` / `reviews_line` in search and
  `rating.shown_on_site` in the card are the line's (rounded to 0.1, as on the
  site). `rating.line` is the same from the star split; `rating.variant` is
  computed from the article's own reviews (`computed: true`).
- **Stock** — `online_qty` is the single online warehouse of the chain;
  `stores_qty_in_region` and `shops_in_region` cover every store of the region;
  `stores.in_city` lists this city's stores. `level` is the site's own label.
- **Delivery** — `when` is the site's wording ("сегодня", "завтра",
  "2 - 3 дня"); `cost_rub` is always `null` (known only at checkout). When
  the site promises pickup "today" but no store of the region has the item,
  the option carries a note.
- **`unit_price`** — price per 100 ml / 100 g / 1 pc, only when the pack size
  appears exactly once in the product name.

## Limitations

- Unofficial: relies on the site's internal API (`/api/v3`, JSON-RPC
  `/api/v2`) and on the Aplaut review widget, which can change at any time. The
  site is mid-migration (Bitrix → Next.js); parsers are isolated in `parse.py`
  and covered by tests on recorded answers.
- Anonymous only: no personal card price, promo codes, bonuses or cart totals.
  «Выгодная пара», «1+1=3» and gifts are computed in the cart and are not
  priced here.
- The morning window's end (12:00 MSK) was observed; its start (09:00) comes
  from the chain's rules. Whether a guest's cart honours the morning price or
  «Выгодная цена» was not checked.
- The shop's own review API (`/api/v3/content/reviews`) answers 500 at the
  moment; reviews come from Aplaut. The server tries the shop's API first
  (once per hour) with a parser written from the site's JavaScript, not from a
  real answer. In the `line` scope a review is not tied to a shade.
- The store list (`list_stores`) has no opening hours and not every address
  names its town; hours come with `get_store_availability`.
- No price history, barcodes (EAN) or manufacturer codes: the site has none.
- Only Moscow and the Sverdlovsk region were checked in detail.
- The catalog API does not echo the region it answered for. The server sends the
  region with every call and cross-checks what can be checked: the city name from
  the shop's directory, the towns of the stores in stock answers against the
  region's store list, and the card's store count against the stores listed.
- `robots.txt` disallows `/api/v3`. Use the server for personal price research
  at a human pace.

## Roadmap

- **Later: optional account mode** (off by default): log in once to see your
  personal card price and exact delivery cost.
- **Later: cart** on top of the account mode.
- Checkout, payment and changing the delivery address are deliberately out of
  scope.

## Development

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest            # offline tests on recorded answers (no network)
.venv/bin/pytest -m live    # end-to-end over MCP stdio against live podrygka.ru (~25 requests, ~40 s)
```

Layout: `client.py` (request policy: pacing, guest token, one retry, sanity
checks, caches), `parse.py` (pure functions over the shop's JSON: prices,
promotions, ratings, stock, stores, reviews), `server.py` (tools).
Fixtures in `tests/fixtures/` are recorded answers, trimmed; review authors are
removed.

## Disclaimer

Not affiliated with «Подружка» or Aplaut. The server uses undocumented internal
endpoints of the shop's website; it is intended for personal use at a human
request rate. Respect the shop's terms of use.

License: MIT.


## Standalone lazy startup

The normal console command and `python -m podrygka_mcp` now start a lightweight
stdio frontend. No Buyer plugin, external launcher, absolute installation path,
or user configuration change is required. Initialization and tool/resource/prompt
listing use the packaged catalog; the first other request starts this connection's
original server. Tool schemas and results are unchanged, and client sessions are
never shared. The backend remains alive until the client disconnects, preserving
state and ongoing work. Closing stdin stops it; this is not an idle-eviction policy.

Catalogs are checked against package source and installed schema-library versions.
If a catalog is missing or stale, startup falls back to the original server.
Supported catalogs cover MCP SDK 2.2.0 and 2.3.0 and handshake protocols
2024-11-05, 2025-03-26, 2025-06-18 and 2025-11-25. Other versions/protocols continue
working through the original server. Set `MCP_EAGER_START=1` to bypass lazy startup
for diagnostics. Existing CLI subcommands retain their original behavior.

This reduces memory/import CPU when a host eagerly initializes unused connections;
it cannot prevent the host from spawning the initial process. To get zero store
processes in unrelated projects, disable the MCP in those projects in the host.
On POSIX an inherited negative nice priority is normalized to zero.

Maintainers: after changing package sources, regenerate catalogs with
`python scripts/update_lazy_catalog.py` in each supported SDK environment, then
run `pytest -q tests/test_lazy_stdio.py` and build/test the wheel. Generation only
initializes and lists declarations; it does not call storefront tools. The
schema/version tests deliberately fail if declarations and packaged catalogs drift.
