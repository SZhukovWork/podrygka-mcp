"""Offline test harness: a fake HTTP transport that replays recorded answers."""
from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pytest

from podrygka_mcp import client as client_mod
from podrygka_mcp import parse, server

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "1234567890/" + "ab" * 16
APLAUT_KEY = "FakeWidgetKey0123456789abc"


def load(name: str) -> Any:
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    if isinstance(data, dict):
        data.pop("_note", None)
    return data


def msk(hour: int, minute: int = 0, day: int = 25) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=parse.MSK)


class Resp:
    def __init__(self, status: int, body: Any = None, content_type: str = "application/json"):
        self.status_code = status
        self._body = body
        self.headers = {"content-type": content_type}
        self.text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        self.content = self.text.encode()

    def json(self) -> Any:
        if isinstance(self._body, str):
            return json.loads(self._body)
        return copy.deepcopy(self._body)


def ok(body: Any) -> Resp:
    return Resp(200, body)


class Route:
    def __init__(self, match: Callable[[str, str, dict], bool], answers: Any):
        self.match = match
        self.answers = answers if isinstance(answers, list) else [answers]
        self.hits = 0

    def next(self, method: str, url: str, kw: dict) -> Any:
        self.hits += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer(method, url, kw) if callable(answer) else answer


class FakeHTTP:
    """Answers requests from routes (first match wins) and records them."""

    def __init__(self, routes: list[Route] | None = None):
        self.routes = list(routes or [])
        self.calls: list[tuple[str, str, dict]] = []

    def add(self, match: Callable[[str, str, dict], bool], answers: Any, first: bool = True) -> Route:
        route = Route(match, answers)
        if first:
            self.routes.insert(0, route)
        else:
            self.routes.append(route)
        return route

    def request(self, method: str, url: str, **kw: Any) -> Any:
        self.calls.append((method, url, kw))
        for route in self.routes:
            if route.match(method, url, kw):
                return route.next(method, url, kw)
        raise AssertionError(f"unexpected request {method} {url} params={kw.get('params')} json={kw.get('json')}")

    def count(self, part: str) -> int:
        return sum(1 for _, url, _ in self.calls if part in url)


def path_is(path: str, **params: Any) -> Callable[[str, str, dict], bool]:
    def match(method: str, url: str, kw: dict) -> bool:
        if not url.split("?")[0].endswith(path):
            return False
        got = kw.get("params") or {}
        return all(str(got.get(k)) == str(v) for k, v in params.items())
    return match


def rpc_is(method_name: str, **params: Any) -> Callable[[str, str, dict], bool]:
    def match(method: str, url: str, kw: dict) -> bool:
        body = kw.get("json") or {}
        if body.get("method") != method_name:
            return False
        return all(body.get("params", {}).get(k) == v for k, v in params.items())
    return match


def _asked(kw: dict) -> list[str]:
    return str((kw.get("params") or {}).get("articles") or "").split(",")


def delivery_answer(method: str, url: str, kw: dict) -> Resp:
    """Recorded delivery terms, only for the articles asked for (like the site)."""
    data = load("delivery_times_showall_ekb")
    data["items"] = [i for i in data["items"] if i["article"] in _asked(kw)]
    return ok(data)


def products_answer(method: str, url: str, kw: dict) -> Resp:
    data = load("products_by_articles_ekb")
    data["products"] = [p for p in data["products"] if p["article"] in _asked(kw)]
    return ok(data)


def site_routes() -> list[Route]:
    """The recorded Yekaterinburg answers, wired like the live site."""
    page2 = load("aplaut_242804_line")
    page2["items"] = page2["items"][:4]
    page2["meta"]["page"] = 2
    return [
        Route(path_is("/api/v3/auth/guest-token"), ok({"token": TOKEN})),
        Route(rpc_is("getCity", search="Екатеринбург"), ok(load("city_ekb"))),
        Route(rpc_is("getCity", search="Березовский"), ok(load("city_berezovsky"))),
        Route(rpc_is("getCity", search="Каменск"), ok(load("city_kamensk"))),
        Route(rpc_is("getCity", search="Москва"), ok(load("city_moscow"))),
        Route(rpc_is("productAvailable", article="242804"), ok(load("product_available_242804_ekb"))),
        Route(rpc_is("productAvailable", article="223187"), ok(load("product_available_223187_ekb"))),
        Route(path_is("/api/v3/catalog/listing", search="шампунь"), ok(load("listing_shampoo_price_asc_ekb"))),
        Route(path_is("/api/v3/catalog/listing", search="помада"), ok(load("listing_pomada_ekb"))),
        Route(path_is("/category_volosy-shampuni/listing"), ok(load("listing_combo_filters"))),
        Route(path_is("/api/v3/catalog/product/242804"), ok(load("product_242804_ekb"))),
        Route(path_is("/api/v3/catalog/product/999999999"), Resp(404, load("product_missing")["body"], "application/problem+json")),
        Route(path_is("/api/v3/catalog/products_by_articles"), products_answer),
        Route(path_is("/api/v3/catalog/delivery_times"), delivery_answer),
        Route(path_is("/api/v3/catalog/categories"), ok(load("categories"))),
        Route(path_is("/api/v3/catalog/filter"), ok(load("filter_all_ekb"))),
        Route(path_is("/api/v3/content/reviews"), Resp(500, {"title": "Internal server error", "status": 500, "type": "error"},
                                                      "application/problem+json")),
        Route(path_is("/app.js"), Resp(200, f'const access_token = "{APLAUT_KEY}";', "application/javascript")),
        Route(path_is("/v2/reviews", context_id="242804", merge_in_group="false"), ok(load("aplaut_242804_variant"))),
        Route(path_is("/v2/reviews", context_id="242804", merge_in_group="true", page=2), ok(page2)),
        Route(path_is("/v2/reviews", context_id="242804", merge_in_group="true"), ok(load("aplaut_242804_line"))),
        Route(path_is("/v2/reviews", context_id="205410", merge_in_group="false"), ok(load("aplaut_205410_variant"))),
        Route(path_is("/v2/reviews", context_id="84361"), ok(load("aplaut_84361_line_helpful"))),
    ]


@pytest.fixture
def sleeps():
    got: list[float] = []
    return got


@pytest.fixture
def make_client(monkeypatch, tmp_path, sleeps):
    monkeypatch.setenv("PODRYGKA_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("PODRYGKA_CITY", raising=False)
    monkeypatch.delenv("PODRYGKA_PROXY", raising=False)

    def _make(routes: list[Route] | None = None, with_site: bool = True) -> tuple[client_mod.PodrygkaClient, FakeHTTP]:
        http = FakeHTTP([*(routes or []), *(site_routes() if with_site else [])])
        c = client_mod.PodrygkaClient(http=http, sleep=sleeps.append)
        return c, http
    return _make


@pytest.fixture
def site(make_client, monkeypatch):
    """The MCP server wired to the recorded site, at 13:00 MSK (after the morning window)."""
    c, http = make_client()
    monkeypatch.setattr(server, "_client", c)
    monkeypatch.setattr(server, "_now", lambda: msk(13))
    return http
