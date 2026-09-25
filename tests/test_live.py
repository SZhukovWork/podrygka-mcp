"""End-to-end check over the real MCP stdio transport against live podrygka.ru (Yekaterinburg).

Deselected by default; run with:  pytest -m live
About 25 requests (about 20 to the shop, spaced by PODRYGKA_MIN_INTERVAL = 1.5 s, the rest to the
review service): takes about 40 s.
"""
import json
import os
import sys

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

pytestmark = pytest.mark.live

LIPSTICK = "242804"   # L'Oreal Color Riche Nude Intense 601: a line of 8 shades sharing one rating
SHAMPOO = "84361"     # KENSUKO: >100 reviews, sold in most stores of the region
PII_KEYS = ("email", "phone", "author", "order_number", "location_name")


async def _call_all():
    env = {**os.environ, "PODRYGKA_CITY": "Екатеринбург"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "podrygka_mcp"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name: t for t in (await session.list_tools()).tools}
            results = {}

            async def call(key, name, args):
                res = await session.call_tool(name, args)
                assert not res.is_error, (name, res.content)
                results[key] = res.structured_content or json.loads(res.content[0].text)
                return results[key]

            await call("city", "find_city", {"query": "Екатеринбург"})
            await call("search", "search_products", {"query": "шампунь", "sort": "price_asc"})
            await call("search_filtered", "search_products",
                       {"query": "шампунь", "brand": "KENSUKO", "category": "Шампуни", "price_max": 3000,
                        "sort": "price_desc", "limit": 10})
            stores = await call("stores", "list_stores", {})
            await call("search_store", "search_products",
                       {"query": "шампунь", "in_store": stores["in_city"][0]["store_id"], "limit": 5})
            await call("product", "get_product", {"article": LIPSTICK})
            await call("reviews_line", "get_reviews", {"article": LIPSTICK, "scope": "line", "limit": 30})
            await call("reviews_variant", "get_reviews", {"article": SHAMPOO, "sort": "helpful", "limit": 5})
            await call("availability", "get_store_availability", {"article": SHAMPOO})
            await call("compare", "compare_products", {"articles": [LIPSTICK, SHAMPOO, "999999999"]})
            return tools, results


def _in_ekb(r):
    assert r["city"]["name"] == "Екатеринбург" and r["city"]["region"] == "Свердловская область"
    assert r["price_scope"] == "онлайн, без входа" and "active" in r["morning_window"]
    assert r["fetched_at"].endswith("+00:00")


def test_all_tools_over_stdio():
    tools, r = anyio.run(_call_all)
    assert set(tools) == {"search_products", "get_product", "get_reviews", "get_store_availability",
                          "list_stores", "compare_products", "find_city"}

    assert r["city"]["cities"][0]["name"] == "Екатеринбург"

    search = r["search"]
    _in_ekb(search)
    assert search["total_found"] > 100 and search["has_more"] and search["returned"] >= 15
    prices = [i["price_online_rub"] for i in search["items"]]
    assert all(p > 0 for p in prices) and prices == sorted(prices)
    assert all(i["discount_kind"] in ("none", "promo", "morning_5pct", "unknown") for i in search["items"])

    filtered = r["search_filtered"]
    assert filtered["items"] and {i["brand"] for i in filtered["items"]} == {"KENSUKO"}
    assert all(i["price_online_rub"] <= 3000 for i in filtered["items"])
    assert filtered["filters"]["category_code"] == "category_volosy-shampuni"

    stores = r["stores"]
    assert stores["stores_in_region"] >= len(stores["in_city"]) >= 5
    assert r["search_store"]["filters"]["in_store"]["store_id"] == stores["in_city"][0]["store_id"]
    assert r["search_store"]["total_found"] < search["total_found"]

    product = r["product"]
    _in_ekb(product)
    assert product["price"]["price_online_rub"] > 0 and product["variants_count"] > 1
    assert len({v["article"] for v in product["variants"]}) == product["variants_count"]
    rating = product["rating"]
    assert rating["shown_on_site"]["scope"] == "line" and rating["line"]["reviews"] > 0
    assert "variant" in rating
    shop_cities = {s["city"] for s in product["stores"].get("in_city", []) + product["stores"].get("elsewhere_in_region", [])}
    assert shop_cities and "Москва" not in shop_cities
    assert all(d["cost_rub"] is None for d in product["delivery"])

    line = r["reviews_line"]
    assert line["total"] > 0 and line["returned"] > 0 and line["scope"] == "line"
    text = json.dumps([line, r["reviews_variant"]], ensure_ascii=False)
    assert not any(f'"{k}' in text for k in PII_KEYS)

    availability = r["availability"]
    _in_ekb(availability)
    assert availability["stores"]["stores_with_stock_in_city"] >= 1

    rows = {row["article"]: row for row in r["compare"]["items"]}
    assert rows["999999999"]["found"] is False and rows[LIPSTICK]["price_online_rub"] > 0
