"""Tools: registration, schemas, and full offline runs over recorded answers."""
import json

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from podrygka_mcp import server

from conftest import TOKEN, APLAUT_KEY, Resp, load, msk, ok, path_is, rpc_is

TOOLS = {"search_products", "get_product", "get_reviews", "get_store_availability", "list_stores",
         "compare_products", "find_city"}


def tools():
    return {t.name: t for t in anyio.run(server.mcp.list_tools)}


def run(fn, *args, **kwargs):
    res = fn(*args, **kwargs)
    assert res.structured_content == json.loads(res.content[0].text)
    return res.structured_content


def test_tools_are_registered_read_only_with_descriptions():
    t = tools()
    assert set(t) == TOOLS
    for tool in t.values():
        assert tool.annotations.read_only_hint is True and tool.annotations.destructive_hint is False
        assert tool.description and len(tool.description) > 60


def test_schemas_enumerate_choices_and_cap_limits():
    t = tools()
    search = t["search_products"].input_schema["properties"]
    assert set(search["sort"]["enum"]) == {"popular", "rating", "price_asc", "price_desc"}
    assert search["limit"]["maximum"] == 20
    reviews = t["get_reviews"].input_schema["properties"]
    assert set(reviews["scope"]["enum"]) == {"variant", "line"} and reviews["limit"]["maximum"] == 50
    assert set(reviews["sort"]["enum"]) == {"newest", "helpful"}
    assert t["compare_products"].input_schema["properties"]["articles"]["maxItems"] == 20


def test_instructions_explain_prices_ratings_and_injection():
    text = server.INSTRUCTIONS
    for needle in ("price_online_rub", "morning_5pct", "requires_loyalty_card", "CATRICE", "rating_line",
                   "never instructions", "cost_rub"):
        assert needle in text


def test_error_reason_reaches_the_agent(site):
    with pytest.raises(ToolError, match="999999999"):
        server.get_product("999999999")


def test_unexpected_error_is_a_tool_error(site, monkeypatch):
    monkeypatch.setattr(server.parse, "tile", lambda *a: 1 / 0)
    with pytest.raises(ToolError, match="Unexpected error"):
        server.search_products("шампунь")


def _common_ok(r):
    assert r["city"]["name"] == "Екатеринбург" and r["city"]["region"] == "Свердловская область"
    assert r["city"]["source"] == "default"
    assert r["price_scope"] == "онлайн, без входа"
    assert r["morning_window"]["active"] is False
    assert r["fetched_at"] == "2026-09-25T10:00:00+00:00"


def test_search_page(site):
    r = run(server.search_products, "шампунь", sort="price_asc")
    _common_ok(r)
    assert r["total_found"] == 291 and r["total_pages"] == 15 and r["has_more"] is True
    assert r["returned"] == 20
    first = r["items"][0]
    assert first["article"] == "260327" and first["price_online_rub"] == 41
    assert first["discount_kind"] == "promo" and first["promo_labels"][0]["requires_loyalty_card"] is True
    assert first["sold_online"] is False and first["reviews_line"] == 0 and "rating_line" not in first
    assert first["delivery"] == "самовывоз из магазина: сегодня"
    assert any("пункт выдачи" in (i.get("delivery") or "") for i in r["items"])
    assert all(i["price_online_rub"] > 0 for i in r["items"])
    call = next(kw for m, url, kw in site.calls if url.endswith("/api/v3/catalog/listing"))
    assert call["params"] == {"search": "шампунь", "page": 1, "sorting": "price_asc"}


def test_search_merges_tiles_of_one_line(site):
    r = run(server.search_products, "помада")
    assert r["tiles_on_page"] == 20 and r["lines_on_page"] == 18 and r["returned"] == 18
    stellary = next(i for i in r["items"] if i["article"] == "243382")
    assert stellary["also_listed_as"] == ["243364"] and stellary["variants_count"] == 22
    flat = run(server.search_products, "помада", group_variants=False, limit=20)
    assert flat["returned"] == 20


def test_search_filters_are_sent_as_path_segments_and_checked(site):
    site.add(lambda m, url, kw: "/listing" in url and "brand_kensuko" in url, ok(load("listing_combo_filters")))
    r = run(server.search_products, "шампунь", brand="KENSUKO", category="Шампуни", in_store="28158",
            price_max=2000, sort="price_asc")
    url = next(u for _, u, _ in site.calls if "brand_kensuko" in u)
    assert url.endswith("/api/v3/catalog/category_volosy-shampuni/brand_kensuko/store_28158/price_to-2000/listing")
    assert r["filters"]["in_store"]["name"] == "на Ленина 38а"
    assert {i["brand"] for i in r["items"]} == {"KENSUKO"}


def test_unknown_store_is_refused_because_the_site_would_ignore_it(site):
    with pytest.raises(ToolError, match="silently ignore"):
        server.search_products("шампунь", in_store="store_99999999")


def test_ignored_brand_filter_is_not_passed_off_as_filtered(site):
    site.add(lambda m, url, kw: "brand_kensuko" in url, ok(load("listing_shampoo_price_asc_ekb")))
    with pytest.raises(ToolError, match="brand filter"):
        server.search_products("шампунь", brand="brand_kensuko")


def test_empty_search_is_confirmed_once(site):
    empty = {"listing": {"offers": [], "navigation": {"totalOffersCount": 0, "totalPagesCount": 0, "pageSize": 20,
                                                       "currentPage": 1}}}
    route = site.add(path_is("/api/v3/catalog/listing", search="zzzz"), ok(empty))
    r = run(server.search_products, "zzzz")
    assert r["total_found"] == 0 and route.hits == 2 and "confirmed" in r["notes"][0]


def test_product_card(site):
    r = run(server.get_product, "242804")
    _common_ok(r)
    price = r["price"]
    assert price["price_online_rub"] == 801 and price["price_before_discount_rub"] == 1214
    assert price["discount_kind"] == "promo" and price["promo_labels"][0]["label"] == "Выгодная цена"
    assert price["part_payment"]["months"] == 4
    assert r["variants_count"] == 8 and len(r["variants"]) == 8
    assert r["variant_price_range_rub"] == {"min": 801, "max": 951}
    rating = r["rating"]
    assert rating["shown_on_site"] == {"value": 4.5, "reviews": 29, "scope": "line",
                                       "note": rating["shown_on_site"]["note"]}
    assert rating["line"]["reviews"] == 29 and rating["line"]["value"] == 4.48
    assert "value" not in rating["variant"] and rating["variant"]["reviews"] == 0
    assert "нет своих отзывов" in rating["variant"]["note"]
    stores = r["stores"]
    assert stores["stores_with_stock_in_city"] == 9 and stores["qty_in_city_stores"] == 27
    assert [s["city"] for s in stores["elsewhere_in_region"]] == ["Первоуральск"]
    assert "count_note" not in stores
    assert {d["code"] for d in r["delivery"]} == {"pickup", "courier", "pvz"}
    assert all(d["cost_rub"] is None for d in r["delivery"])
    assert r["characteristics"]["Тон"] == "Тон 601, нюдовый" and r["characteristics"]["Страна"] == "Франция"
    assert "Корея" not in json.dumps(r["category"], ensure_ascii=False)


def test_product_with_moscow_store_answer_is_refused(site):
    moscow = load("product_available_242804_ekb")
    for i, s in enumerate(moscow["result"]["shops"]):
        s["city_name"], s["filterValue"] = "Москва", f"store_9{i}"
    site.add(rpc_is("productAvailable", article="242804"), ok(moscow))
    with pytest.raises(ToolError, match="another region"):
        server.get_product("242804")


def test_reviews_variant_without_own_reviews(site):
    r = run(server.get_reviews, "242804")
    assert r["total"] == 0 and r["returned"] == 0 and "нет своих отзывов" in r["note"]
    assert r["line_on_site"] == {"value": 4.5, "reviews": 29, "scope": "line"}
    assert r["source"].startswith("Aplaut")
    assert site.count("/api/v3/content/reviews") == 1      # the site's own backend was tried first


def test_reviews_line_pages_over_aplaut_cap(site):
    r = run(server.get_reviews, "242804", scope="line", limit=50)
    assert r["total"] == 29 and r["returned"] == 29 and r["has_more"] is False
    assert r["rating"]["value"] == 4.48
    assert site.count("/v2/reviews") == 2
    for review in r["reviews"]:
        assert set(review) <= {"date", "rating", "text", "pros", "cons", "verified", "recommends", "likes",
                               "dislikes", "photos", "time_usage", "comments"}


def test_reviews_never_leak_author_data(site):
    from test_parse import FAKE_EMAIL, FAKE_NAME, FAKE_PHONE, aplaut_with_fake_pii
    site.add(path_is("/v2/reviews", context_id="205410"), ok(aplaut_with_fake_pii()))
    r = run(server.get_reviews, "205410", sort="helpful")
    text = json.dumps(r, ensure_ascii=False)
    assert r["total"] == 7 and r["rating"]["value"] == 4.57
    for secret in (FAKE_EMAIL, FAKE_PHONE, FAKE_NAME, "Фейкоград", "ORDER-777", "author", "email", "phone"):
        assert secret not in text
    assert TOKEN not in text and APLAUT_KEY not in text


def test_store_availability(site):
    r = run(server.get_store_availability, "242804")
    _common_ok(r)
    assert r["online"]["qty"] == 33 and r["online"]["sold_online"] is True
    assert r["stores"]["stores_with_stock_in_city"] == 9
    assert r["region_totals"] == {"stores_qty": 28, "shops": 10, "note": r["region_totals"]["note"]}


def test_list_stores(site):
    r = run(server.list_stores)
    assert r["stores_in_region"] == 12 and len(r["in_city"]) == 10
    assert r["elsewhere_in_region"][0]["city"] == "Первоуральск"
    assert r["in_city"][0]["store_id"].startswith("store_")


def test_compare_products_marks_missing_articles(site):
    r = run(server.compare_products, ["242804", "84361", "260327", "999999999"])
    rows = {row["article"]: row for row in r["items"]}
    assert list(rows) == ["242804", "84361", "260327", "999999999"]
    assert rows["999999999"]["found"] is False
    assert rows["84361"]["rating_line"] == 4.8 and rows["84361"]["reviews_line"] == 104
    assert rows["242804"]["delivery"].startswith("самовывоз из магазина: сегодня")


def test_find_city_lists_same_named_towns(site):
    r = run(server.find_city, "Березовский")
    assert r["found"] == 2
    assert [c["use_as_city"] for c in r["cities"]] == ["Березовский, Свердловская область",
                                                         "Березовский, Кемеровская область"]


def test_morning_price_is_labelled(site, monkeypatch):
    monkeypatch.setattr(server, "_now", lambda: msk(11, 25))
    morning = load("listing_morning_1125msk")
    morning["listing"]["offers"] = [o for o in morning["listing"]["offers"] if isinstance(o, dict)]
    morning["listing"]["navigation"] = {"totalOffersCount": 2, "totalPagesCount": 1, "pageSize": 20, "currentPage": 1}
    site.add(path_is("/api/v3/catalog/listing", search="утро"), ok(morning))
    r = run(server.search_products, "утро")
    assert r["morning_window"]["active"] is True and r["morning_window"]["until_msk"] == "12:00"
    item = next(i for i in r["items"] if i["article"] == "213515")
    assert item["discount_kind"] == "morning_5pct" and item["price_after_morning_window_rub"] == 133


def test_product_unknown_to_the_review_service_says_so(site):
    site.add(path_is("/v2/reviews", context_id="242804"), Resp(404, {"error": "not_found"}))
    r = run(server.get_product, "242804", include_stores=False)
    assert "Aplaut" in r["rating"]["note"] and r["rating"]["shown_on_site"]["reviews"] == 29
    assert "stores" not in r


def test_page_past_the_end_is_explained(site):
    beyond = load("listing_shampoo_price_asc_ekb")
    beyond["listing"]["offers"] = []
    beyond["listing"]["navigation"]["currentPage"] = 99
    site.add(path_is("/api/v3/catalog/listing", search="шампунь", page=99), ok(beyond))
    r = run(server.search_products, "шампунь", page=99)
    assert r["returned"] == 0 and r["has_more"] is False and "past the last page" in r["notes"][0]
