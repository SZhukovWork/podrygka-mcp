# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org): patch releases fix breakage caused
by changes on podrygka.ru's side, minor releases add features.

## [0.1.0] — 2026-09-25

### Added
- `search_products`: the site's search with pages of 20, four sort orders,
  price window, brand, category, store and promotion filters; tiles of one
  product line merged; delivery terms for the city on every item.
- `get_product`: online price with the kind of discount (promotion, morning
  −5 %, unknown), promotions with the chain's loyalty-card rules, price per
  100 ml/g from the pack size, instalment split, every shade/variant with its
  own price and stock, stock per store of the city and the rest of the region,
  delivery terms (cost only at checkout), characteristics, line rating next to
  the variant's own rating computed from its reviews.
- `get_reviews` (variant or whole line, newest or most helpful, pages over
  Aplaut's 25-per-page cap), `get_store_availability`, `list_stores`,
  `compare_products` (up to 20 articles), `find_city`.
- City resolved through the shop's own directory and sent with every request;
  answers for another region (the store-stock endpoint silently falls back to
  Moscow) are refused.
- Guest token issued automatically and re-issued once when rejected; every
  answer sanity-checked, one retry, then a clear error; requests spaced
  1.5 s apart.
- Reviewer names, e-mails, phones and order numbers from the review provider
  are never returned (allow-list, covered by a test with fake personal data).
