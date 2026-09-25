"""HTTP client for podrygka.ru and its review provider Aplaut.

Plain `requests`, no browser: the site's own REST API lets any HTTP client in.

- ``/api/v3/*`` (catalog) needs a guest Bearer token from
  ``/api/v3/auth/guest-token`` and takes the region from the ``x-region-fias``
  header. Without the header it silently answers for Moscow, so the header is
  sent on every call. A rejected token (401/403 ``AuthToken*Error``) is
  re-issued once.
- JSON-RPC ``/api/v2/*``: ``getCity`` (city directory) and
  ``productAvailable`` (stock per store). The latter reads the region from
  Bitrix cookies and silently falls back to Moscow without them, so its
  answer is checked against the region's store list.
- Reviews come from Aplaut (``c-api.aplaut.io``) with the public key of the
  site's widget; the site's own ``/api/v3/content/reviews`` is tried first
  and skipped for an hour whenever it fails (it answers 500 at the moment).

Every answer is sanity-checked before use. Anything odd gets exactly one
retry; then the call fails with a reason instead of returning doubtful data.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

import requests

from . import parse
from .parse import City

log = logging.getLogger(__name__)

SITE = parse.SITE
GUEST_TOKEN_PATH = "/api/v3/auth/guest-token"
CITY_SEARCH_PATH = "/api/v2/location/city/search"
PRODUCT_AVAILABLE_PATH = "/api/v2/shops/product-available"
NATIVE_REVIEWS_PATH = "/api/v3/content/reviews"
APLAUT_APP_JS = "https://get.aplaut.io/f/v2/617a9a8e455212001bea2eb6/app.js"
APLAUT_REVIEWS = "https://c-api.aplaut.io/v2/reviews"

DEFAULT_CITY = "Екатеринбург"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140.0.0.0 Safari/537.36")
AUTH_ERRORS = {"AuthTokenInvalidError", "AuthTokenMissingError"}

TOKEN_MAX_AGE = 12 * 3600       # the site's own cookie lives 24 h; renew earlier
APLAUT_KEY_TTL = 6 * 3600
REGION_STORES_TTL = 6 * 3600
CATEGORIES_TTL = 24 * 3600
CITY_TTL = 30 * 24 * 3600
NATIVE_REVIEWS_BACKOFF = 3600
RETRY_PAUSE = 3.0
THROTTLE_PAUSE = 15.0
APLAUT_INTERVAL = 0.3


class PodrygkaError(RuntimeError):
    """Anything that stops a tool from returning trustworthy data."""


class RateLimited(PodrygkaError):
    pass


class Blocked(PodrygkaError):
    pass


class NotFound(PodrygkaError):
    pass


class Implausible(PodrygkaError):
    pass


class Unauthorized(PodrygkaError):
    """HTTP 401 outside the guest-token flow (e.g. a stale review-widget key)."""


class BadInput(PodrygkaError):
    """The request names something the shop does not know (city, brand, store…)."""


def cache_dir() -> Path:
    root = os.environ.get("PODRYGKA_CACHE_DIR") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "podrygka-mcp"
    )
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _problem_type(resp: Any) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    return body.get("type") if isinstance(body, dict) else None


def _is_html(resp: Any) -> bool:
    return "html" in (resp.headers.get("content-type") or resp.headers.get("Content-Type") or "").lower()


class PodrygkaClient:
    def __init__(self, http: Any = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self._lock = threading.RLock()
        self._sleep = sleep
        self._min_interval = float(os.environ.get("PODRYGKA_MIN_INTERVAL", "1.5"))
        self._timeout = float(os.environ.get("PODRYGKA_TIMEOUT", "25"))
        self._dir = cache_dir()
        if http is None:
            http = requests.Session()
            proxy = os.environ.get("PODRYGKA_PROXY")
            if proxy:
                http.proxies.update({"http": proxy, "https": proxy})
        self._http = http
        self._last: dict[str, float] = {}
        self.requests: dict[str, int] = {"shop": 0, "aplaut": 0}

        self._token: str | None = None
        self._token_at = 0.0
        self._load_token()
        self._cities: dict[str, dict] = self._read_json("cities.json") or {}
        self._city_lookups: dict[str, list[City]] = {}   # the city directory does not change during a session
        self._facets_cache: dict[str, tuple[float, dict[str, list[dict]]]] = {}
        self._categories: tuple[float, list[dict]] | None = None
        self._aplaut_key: tuple[float, str] | None = None
        self._native_down_until = 0.0

    # ---- small persistence -------------------------------------------------

    def _read_json(self, name: str) -> Any:
        try:
            return json.loads((self._dir / name).read_text())
        except (OSError, ValueError):
            return None

    def _write_json(self, name: str, data: Any) -> None:
        path = self._dir / name
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False))
            tmp.chmod(0o600)
            tmp.replace(path)
        except OSError as e:
            log.warning("Could not write %s: %s", path, e)

    def _load_token(self) -> None:
        data = self._read_json("session.json")
        if isinstance(data, dict) and isinstance(data.get("token"), str):
            self._token, self._token_at = data["token"], float(data.get("issued_at") or 0)

    # ---- transport ---------------------------------------------------------

    def _pace(self, kind: str) -> None:
        interval = self._min_interval if kind == "shop" else APLAUT_INTERVAL
        wait = interval + (random.uniform(0, 0.3) if kind == "shop" else 0) - (time.monotonic() - self._last.get(kind, 0.0))
        if wait > 0:
            self._sleep(wait)

    def _send(self, kind: str, method: str, url: str, **kw: Any) -> Any:
        self._pace(kind)
        headers = {"User-Agent": UA, "Accept": "application/json", "Accept-Language": "ru-RU",
                   "Referer": SITE + "/", **(kw.pop("headers", None) or {})}
        started = time.monotonic()
        status: Any = "error"
        try:
            resp = self._http.request(method, url, headers=headers, timeout=self._timeout, **kw)
            status = resp.status_code
            return resp
        finally:
            self._last[kind] = time.monotonic()
            self.requests[kind] = self.requests.get(kind, 0) + 1
            # Path only: query strings can carry the review widget key.
            log.debug("%s %s%s -> %s in %.2f s (#%d to %s)", method, _host(url), _path(url), status,
                      self._last[kind] - started, self.requests[kind], kind)

    def _call(self, kind: str, method: str, url: str, *, check: Callable[[Any], str | None] | None = None,
              auth: bool = False, region: str | None = None, not_found: str | None = None,
              retry: bool = True, **kw: Any) -> Any:
        """One request under the retry policy; returns the checked JSON body.

        At most one recovery attempt per call (plus one token re-issue for the
        catalog API): no retry loops against a shop that is pushing back.
        """
        reminted = False
        recovered = not retry
        while True:
            headers = dict(kw.get("headers") or {})
            if region:
                headers["x-region-fias"] = region
            if auth:
                headers["Authorization"] = f"Bearer {self._guest_token(region)}"
            try:
                resp = self._send(kind, method, url, **{**kw, "headers": headers})
            except requests.RequestException as e:
                if recovered:
                    raise PodrygkaError(f"Network error talking to {_host(url)}: {type(e).__name__}") from e
                recovered = True
                self._sleep(RETRY_PAUSE)
                continue
            status = resp.status_code

            if auth and status in (401, 403) and _problem_type(resp) in AUTH_ERRORS:
                if reminted:
                    raise Blocked("podrygka.ru rejects even a freshly issued guest token "
                                  f"(HTTP {status}). The API may have changed; try again later.")
                log.info("Guest token rejected (HTTP %s), issuing a new one", status)
                self._mint(region)
                reminted = True
                continue
            if status == 429:
                if recovered:
                    raise RateLimited(f"{_host(url)} is rate limiting this IP (HTTP 429). Wait several "
                                      "minutes before the next request; retrying in a loop makes it worse.")
                recovered = True
                self._sleep(THROTTLE_PAUSE)
                continue
            if status == 404 and not_found:
                raise NotFound(not_found)
            if status == 401:
                raise Unauthorized(f"{_host(url)} answered HTTP 401 for {_path(url)}")
            if status == 403 or (status == 200 and _is_html(resp)):
                if recovered:
                    raise Blocked(f"{_host(url)} refuses the request (HTTP {status}"
                                  f"{', an HTML page instead of JSON' if status == 200 else ''}). "
                                  "This looks like an anti-bot block: wait, do not retry in a loop. "
                                  "A VPN or foreign IP can cause it.")
                recovered = True
                self._sleep(RETRY_PAUSE * 2)
                continue
            if status >= 500:
                if recovered:
                    raise PodrygkaError(f"{_host(url)} answered HTTP {status} twice for {_path(url)}; "
                                        "the shop's API is failing right now, try later.")
                recovered = True
                self._sleep(RETRY_PAUSE)
                continue
            if status != 200:
                raise PodrygkaError(f"{_host(url)} answered HTTP {status} for {_path(url)}")
            try:
                data = resp.json()
            except ValueError:
                data = None
                problem = "the body is not JSON (empty or cut off)"
            else:
                problem = check(data) if check else None
            if problem is None:
                return data
            log.warning("Implausible answer from %s (%s)", _path(url), problem)
            if recovered:
                raise Implausible(f"{_host(url)} returned an implausible answer for {_path(url)} ({problem}), "
                                  "twice; not passing it on as data.")
            recovered = True
            self._sleep(RETRY_PAUSE)

    # ---- guest token -------------------------------------------------------

    def _guest_token(self, region: str | None) -> str:
        if self._token is None or time.time() - self._token_at > TOKEN_MAX_AGE:
            self._mint(region)
        assert self._token is not None
        return self._token

    def _mint(self, region: str | None) -> None:
        def check(d: Any) -> str | None:
            token = d.get("token") if isinstance(d, dict) else None
            return None if isinstance(token, str) and re.fullmatch(r"[\w/+=.-]{16,200}", token) else "no guest token in the answer"

        data = self._call("shop", "GET", SITE + GUEST_TOKEN_PATH, check=check, region=region)
        self._token, self._token_at = data["token"], time.time()
        self._write_json("session.json", {"token": self._token, "issued_at": self._token_at})

    def _api(self, path: str, city: City, params: dict | None = None, check: Callable | None = None,
             not_found: str | None = None, retry: bool = True) -> Any:
        return self._call("shop", "GET", SITE + path, params=params, check=check, auth=True,
                          region=city.region_fias, not_found=not_found, retry=retry)

    def _rpc(self, path: str, method: str, params: dict, check: Callable | None = None,
             cookies: dict | None = None) -> Any:
        body = {"jsonrpc": "2.0", "id": f"{method}-{int(time.time())}", "method": method, "params": params}

        def rpc_check(d: Any) -> str | None:
            if not isinstance(d, dict):
                return "body is not a JSON object"
            if d.get("error"):
                return f"JSON-RPC error {str(d['error'])[:120]}"
            return check(d) if check else None

        return self._call("shop", "POST", SITE + path, json=body, cookies=cookies, check=rpc_check)

    # ---- city --------------------------------------------------------------

    def resolve_city(self, city: str | None) -> tuple[City, str]:
        """The shop's city for a name, and where the name came from."""
        if city and city.strip():
            query, source = city.strip(), "param"
        elif os.environ.get("PODRYGKA_CITY", "").strip():
            query, source = os.environ["PODRYGKA_CITY"].strip(), "env PODRYGKA_CITY"
        else:
            query, source = DEFAULT_CITY, "default"
        with self._lock:
            key = parse.norm(query)
            hit = self._cities.get(key)
            if hit and time.time() - float(hit.get("at") or 0) < CITY_TTL:
                return City(**hit["city"]), source
            candidates = self.find_cities(query.partition(",")[0].strip())
            try:
                chosen = parse.pick_city(candidates, query)
            except parse.CityChoiceError as e:
                raise BadInput(f"{e} Список городов — find_city.") from e
            self._cities[key] = {"city": chosen.to_dict(), "at": time.time()}
            self._write_json("cities.json", self._cities)
            return chosen, source

    def find_cities(self, query: str) -> list[City]:
        def check(d: Any) -> str | None:
            try:
                parse.city_candidates(d)
            except parse.ParseError as e:
                return str(e)
            return None

        with self._lock:
            key = parse.norm(query)
            if key not in self._city_lookups:
                data = self._rpc(CITY_SEARCH_PATH, "getCity", {"search": query}, check=check)
                self._city_lookups[key] = parse.city_candidates(data)
            return list(self._city_lookups[key])

    # ---- catalog -----------------------------------------------------------

    def listing(self, city: City, slugs: list[str], params: dict) -> dict:
        path = "/api/v3/catalog" + "".join(f"/{s}" for s in slugs) + "/listing"
        with self._lock:
            return self._api(path, city, params, check=parse.listing_problem)

    def product(self, article: str, city: City) -> dict:
        with self._lock:
            data = self._api(f"/api/v3/catalog/product/{article}", city,
                             check=lambda d: parse.product_problem(d, article),
                             not_found=f"No product with article {article} on podrygka.ru (HTTP 404).")
            return data["product"]

    def products(self, articles: list[str], city: City) -> dict[str, dict]:
        with self._lock:
            data = self._api("/api/v3/catalog/products_by_articles", city, {"articles": ",".join(articles)},
                             check=lambda d: parse.products_problem(d, articles))
            return {str(p.get("article")): p for p in data["products"] if isinstance(p, dict)}

    def delivery_times(self, articles: list[str], city: City) -> dict[str, list[dict]]:
        """Delivery terms per article, as the product page shows them.

        `showAll=true` is what the product page sends; without it the pickup
        point (ПВЗ) option is left out.
        """
        if not articles:
            return {}

        def check(d: Any) -> str | None:
            try:
                got = parse.delivery_map(d)
            except parse.ParseError as e:
                return str(e)
            extra = set(got) - set(articles)
            return f"delivery terms for articles not asked for: {sorted(extra)[:3]}" if extra else None

        with self._lock:
            data = self._api("/api/v3/catalog/delivery_times", city,
                             {"articles": ",".join(articles), "showAll": "true"}, check=check)
            return parse.delivery_map(data)

    def shops(self, article: str, city: City) -> list[dict]:
        """Stores holding the article, from the JSON-RPC `productAvailable`.

        The region comes from Bitrix cookies here, not from a header; without
        them the answer is silently Moscow's. Callers check it against
        `region_stores`.
        """
        cookies = {"BITRIX_SM_FIAS": city.fias, "BITRIX_SM_REGION_FIAS": city.region_fias,
                   "city_fias": city.fias, "region_fias": city.region_fias, "city_checked": "1"}

        def check(d: Any) -> str | None:
            try:
                parse.shops(d)
            except parse.ParseError as e:
                return str(e)
            return None

        with self._lock:
            data = self._rpc(PRODUCT_AVAILABLE_PATH, "productAvailable", {"article": article},
                             check=check, cookies=cookies)
            return parse.shops(data)

    def _facets(self, city: City) -> dict[str, list[dict]]:
        """Stores and brands of the region from the whole-catalog filter, cached 6 h."""
        with self._lock:
            hit = self._facets_cache.get(city.region_fias)
            if hit is None:
                saved = self._read_json(f"facets-{city.region_fias}.json")
                if isinstance(saved, dict) and isinstance(saved.get("facets"), dict):
                    hit = (float(saved.get("at") or 0), saved["facets"])
            if hit and time.time() - hit[0] < REGION_STORES_TTL:
                self._facets_cache[city.region_fias] = hit
                return hit[1]

            def check(d: Any) -> str | None:
                try:
                    stores = parse.store_facet(d)
                    brands = parse.brand_facet(d)
                except parse.ParseError as e:
                    return str(e)
                if not stores or not brands:
                    return "no store or brand list in the catalog filter"
                if parse.looks_like_moscow_fallback(stores, city):
                    return f"the store list is Moscow's, not {city.region}'s"
                return None

            data = self._api("/api/v3/catalog/filter", city, check=check)
            facets = {"stores": parse.store_facet(data), "brands": parse.brand_facet(data)}
            self._facets_cache[city.region_fias] = (time.time(), facets)
            self._write_json(f"facets-{city.region_fias}.json", {"at": time.time(), "facets": facets})
            return facets

    def region_stores(self, city: City) -> list[dict]:
        """All stores of the city's region (catalog `store` filter)."""
        return self._facets(city)["stores"]

    def categories(self, city: City) -> list[dict]:
        with self._lock:
            if self._categories and time.time() - self._categories[0] < CATEGORIES_TTL:
                return self._categories[1]
            cached = self._read_json("categories.json")
            if isinstance(cached, dict) and time.time() - float(cached.get("at") or 0) < CATEGORIES_TTL:
                self._categories = (float(cached["at"]), cached["rows"])
                return cached["rows"]

            def check(d: Any) -> str | None:
                try:
                    return None if parse.categories(d) else "empty category tree"
                except parse.ParseError as e:
                    return str(e)

            rows = parse.categories(self._api("/api/v3/catalog/categories", city, check=check))
            self._categories = (time.time(), rows)
            self._write_json("categories.json", {"at": self._categories[0], "rows": rows})
            return rows

    def brand_code(self, name: str, city: City) -> str:
        """Filter code (brand_…) of a brand given by its name, from the catalog's brand list."""
        wanted = parse.squash(name)
        brands = self._facets(city)["brands"]
        match = [b for b in brands if parse.squash(b["name"]) == wanted]
        if match:
            return match[0]["code"]
        close = [b["name"] for b in brands if wanted and len(wanted) >= 3
                 and (wanted in parse.squash(b["name"]) or parse.squash(b["name"]) in wanted)]
        raise BadInput(f"No brand «{name}» among the {len(brands)} brands of podrygka.ru"
                       + (f"; similar: {', '.join(close[:12])}" if close else "")
                       + ". Pass the exact name or a brand_code from search results.")

    # ---- reviews -----------------------------------------------------------

    def _aplaut_token(self, refresh: bool = False) -> str:
        if not refresh and self._aplaut_key and time.time() - self._aplaut_key[0] < APLAUT_KEY_TTL:
            return self._aplaut_key[1]
        if not refresh:
            cached = self._read_json("aplaut.json")
            if isinstance(cached, dict) and time.time() - float(cached.get("at") or 0) < APLAUT_KEY_TTL:
                self._aplaut_key = (float(cached["at"]), cached["key"])
                return cached["key"]
        resp = self._send("aplaut", "GET", APLAUT_APP_JS, headers={"Accept": "*/*"})
        if resp.status_code != 200:
            raise PodrygkaError(f"The review widget script is unavailable (HTTP {resp.status_code}).")
        m = re.search(r"access_token\s*[=:]\s*[\"']([A-Za-z0-9_\-]{16,})[\"']", resp.text)
        if not m:
            raise PodrygkaError("The review widget script no longer contains its public key; "
                                "the review provider integration has changed.")
        self._aplaut_key = (time.time(), m.group(1))
        self._write_json("aplaut.json", {"key": m.group(1), "at": self._aplaut_key[0]})
        return m.group(1)

    def aplaut_reviews(self, article: str, merge_in_group: bool, page: int, per_page: int, sort: str) -> dict:
        params = {"context": "product", "context_id": article, "merge_in_group": str(merge_in_group).lower(),
                  "page": page, "per_page": per_page, "sort": sort}
        with self._lock:
            refreshed = False
            while True:
                try:
                    return self._call("aplaut", "GET", APLAUT_REVIEWS,
                                      params={**params, "access_token": self._aplaut_token(refreshed)},
                                      check=lambda d: parse.aplaut_problem(d, article),
                                      not_found=f"The review service (Aplaut) has no record of article {article} (HTTP 404): "
                                                "either there is no such article or it has no reviews on the site.",
                                      retry=True, headers={"Origin": SITE})
                except Unauthorized:
                    if refreshed:
                        raise PodrygkaError("The review service rejects the widget key even after "
                                            "re-reading it (HTTP 401); the integration has changed.") from None
                    refreshed = True

    def native_reviews(self, article: str, city: City, only_this_article: bool, page: int, limit: int) -> dict | None:
        """The site's own review backend; None when it is unavailable (then Aplaut is used)."""
        with self._lock:
            if time.time() < self._native_down_until:
                return None
            params = {"article": article, "page": page, "page_size": limit, "orderBy": "createdAtDesc",
                      "hasMedia": "false", "onlyRequestedArticle": str(only_this_article).lower()}
            try:
                return self._api(NATIVE_REVIEWS_PATH, city, params, check=parse.native_problem, retry=False)
            except PodrygkaError as e:
                log.info("Site's own review API unavailable (%s); using Aplaut for the next hour", e)
                self._native_down_until = time.time() + NATIVE_REVIEWS_BACKOFF
                return None


def _host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1) if m else url


def _path(url: str) -> str:
    m = re.match(r"https?://[^/]+(/[^?]*)", url)
    return m.group(1) if m else url
