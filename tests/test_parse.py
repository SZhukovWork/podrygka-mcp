"""Pure parsing on recorded podrygka.ru / Aplaut answers: every trap has a test here."""
import copy
from datetime import datetime, timezone

import pytest

from podrygka_mcp import parse
from podrygka_mcp.parse import City

from conftest import load, msk

EKB = City(name="Екатеринбург", region="Свердловская область", fias="2763c110-cb8b-416a-9dac-ad28a55b4402",
           region_fias="92b30014-4d52-4e2e-892d-928142b924bf", id="208757")


def offers(name):
    return {o["article"]: o for o in load(name)["listing"]["offers"] if isinstance(o, dict)}


# ---- morning window (09:00–12:00 MSK) ----------------------------------------

@pytest.mark.parametrize("hour, minute, active", [(8, 59, False), (9, 0, True), (11, 59, True), (12, 0, False), (23, 0, False)])
def test_morning_window_boundaries_in_msk(hour, minute, active):
    w = parse.morning_window(msk(hour, minute))
    assert w["active"] is active
    assert w["window_msk"] == "09:00–12:00"
    assert (w.get("until_msk") == "12:00") is active


def test_morning_window_is_computed_in_moscow_time():
    # 06:30 UTC is 09:30 MSK (and 11:30 in Yekaterinburg): open.
    assert parse.morning_window(datetime(2026, 9, 25, 6, 30, tzinfo=timezone.utc))["active"] is True
    assert parse.morning_window(datetime(2026, 9, 25, 9, 1, tzinfo=timezone.utc))["active"] is False
    assert parse.morning_window(msk(8))["next_start_msk"] == "09:00 сегодня"
    assert parse.morning_window(msk(13))["next_start_msk"] == "09:00 завтра"


def _offer_from_snapshot(snap, discount_titles):
    base, total = snap["basePrice"], snap["totalPrice"]
    pricing = {"basePrice": base, "totalPrice": total, "priceText": ""}
    if total < base:
        pricing["discountPercent"] = round((base - total) / base * 100)
    marks = [{"type": "discount", "name": "Персональная скидка", "title": t} for t in discount_titles]
    return {"article": "x", "pricing": pricing, "marks": marks, "attributes": {"brand": {"value": "KENSUKO"}}}


def test_morning_discount_before_and_after_noon():
    """Recorded live: at 11:25 MSK non-promo items had −5 %, at 12:00:57 they were back at basePrice."""
    fx = load("morning_discount_before_after")
    morning_items = [i for i in fx["items"] if i["before_11_25_msk"]["discount_marks"] == ["-5%"]]
    assert len(morning_items) == 5
    for item in morning_items:
        before = parse.price(_offer_from_snapshot(item["before_11_25_msk"], ["-5%"]), msk(11, 25))
        assert before["discount_kind"] == "morning_5pct"
        assert before["price_after_morning_window_rub"] == item["after_12_00_msk"]["totalPrice"]
        assert before["requires_loyalty_card"] is True and "корзиной не проверено" in before["requires_loyalty_card_basis"]
        after = parse.price(_offer_from_snapshot(item["after_12_00_msk"], []), msk(12, 1))
        assert after["discount_kind"] == "none"
        assert after["price_online_rub"] == item["after_12_00_msk"]["basePrice"]


def test_a_5pct_discount_outside_the_window_is_not_called_morning():
    tile = offers("listing_morning_1125msk")["213515"]      # −5 % at 11:25 MSK
    assert parse.price(tile, msk(11, 25))["discount_kind"] == "morning_5pct"
    late = parse.price(tile, msk(15))
    assert late["discount_kind"] == "unknown" and "price_after_morning_window_rub" not in late


def test_morning_discount_is_not_claimed_for_brands_without_card_discounts():
    tile = copy.deepcopy(offers("listing_morning_1125msk")["213515"])
    tile["attributes"]["brand"]["value"] = "ESSENCE"
    assert parse.price(tile, msk(10))["discount_kind"] == "unknown"


def test_promo_price_is_promo_at_any_time():
    tile = offers("listing_morning_1125msk")["260327"]      # −29 % «Выгодная цена»
    for when in (msk(10), msk(15)):
        p = parse.price(tile, when)
        assert p["discount_kind"] == "promo" and p["discount_percent"] == 29
        assert p["price_online_rub"] == 41 and p["price_before_discount_rub"] == 58


# ---- promotions and their card rules ----------------------------------------------

def test_benefit_requires_card_by_the_rules_unverified_in_cart():
    promos, _ = parse.promo_labels(load("product_242804_ekb")["product"])
    assert [p["label"] for p in promos] == ["Выгодная цена"]
    assert promos[0]["requires_loyalty_card"] is True
    assert promos[0]["requires_loyalty_card_basis"].startswith("по правилам сети; корзиной не проверено")
    assert "только для владельцев дисконтных карт" in promos[0]["requires_loyalty_card_basis"]


def test_three_for_two_and_pair_are_cart_promotions_for_card_holders():
    socks = next(iter(offers("listing_promo_3for2").values()))
    promos, _ = parse.promo_labels(socks)
    assert promos[0]["label"].startswith("1+1=3") and promos[0]["requires_loyalty_card"] is True
    assert promos[0]["promotion"] == "НОСКИ: 1+1=3"
    assert "корзине" in promos[0]["price_effect"]
    assert parse.price(socks, msk(13))["discount_kind"] == "none"   # the shown price has no discount

    pair = next(iter(offers("listing_promo_pair").values()))
    promos, _ = parse.promo_labels(pair)
    assert promos[0]["label"] == "Выгодная пара" and promos[0]["requires_loyalty_card"] is True


def test_sale_shield_is_told_apart_from_the_discount_badge():
    sale = next(iter(offers("listing_promo_sale").values()))
    promos, _ = parse.promo_labels(sale)
    assert [p["code"] for p in promos] == ["sale"]
    assert promos[0]["requires_loyalty_card"] is False
    assert parse.price(sale, msk(10))["discount_kind"] == "promo"


def test_gift_card_rule_is_unknown_and_hit_is_a_plain_label():
    tiles = offers("listing_promo_gift")
    promos, labels = parse.promo_labels(tiles["84358"])
    kinds = {p["code"]: p for p in promos}
    assert kinds["gift"]["requires_loyalty_card"] == "unknown"
    assert kinds["benefit"]["requires_loyalty_card"] is True
    assert labels == ["Хит продаж"]


def test_unknown_shield_is_passed_on_without_guessing():
    promos, _ = parse.promo_labels({"marks": [{"type": "mystery", "name": "Новая акция"}]})
    assert promos == [{"code": "mystery", "label": "Новая акция", "requires_loyalty_card": "unknown",
                       "requires_loyalty_card_basis": "метка не описана в правилах, известных серверу"}]


# ---- ratings: line vs variant --------------------------------------------------

def test_catalog_rating_is_the_lines_and_zero_means_no_reviews():
    tiles = offers("listing_pomada_ekb")
    vs = tiles["210346"]
    assert {(v["reviews"]["rating"], v["reviews"]["amount"]) for v in vs["offers"]} == {(4.1, 92)}
    assert parse.rating_line(vs) == {"reviews_line": 92, "rating_line": 4.1}
    assert parse.rating_line(tiles["266871"]) == {"reviews_line": 0}     # rating 0 = no reviews, not 0 stars


def test_variant_without_reviews_has_no_rating_of_its_own():
    own = parse.aplaut_summary(load("aplaut_242804_variant"))
    assert own["total"] == 0 and own["rating_from_stars"] is None
    assert own["line_rating_shown"] == 4.5 and own["line_reviews_published"] == 29
    line = parse.aplaut_summary(load("aplaut_242804_line"))
    assert line["total"] == 29 and line["rating_from_stars"] == 4.48
    assert line["stars"] == {"1": 0, "2": 1, "3": 4, "4": 4, "5": 20}


def test_variant_rating_is_computed_from_its_own_stars():
    own = parse.aplaut_summary(load("aplaut_205410_variant"))
    assert own["total"] == 7 and own["rating_from_stars"] == 4.57
    assert own["line_rating_shown"] == 4.5


def test_review_count_comes_from_the_published_feed_not_the_context():
    data = load("aplaut_84361_line_helpful")
    assert data["context"]["reviews_count"] == 117
    assert parse.aplaut_summary(data)["total"] == 104


# ---- variants and duplicate tiles ---------------------------------------------------

def test_duplicate_tiles_of_one_line_are_merged():
    tiles = [o for o in load("listing_pomada_ekb")["listing"]["offers"]]
    lines = parse.dedup_lines(tiles)
    assert len(tiles) == 20 and len(lines) == 18
    merged = {t["article"]: m for t, m in lines if m}
    assert merged == {"243382": ["243364"], "215543": ["215544"]}


def test_variants_are_separate_articles_with_their_own_prices_and_stock():
    product = load("product_242804_ekb")["product"]
    rows = parse.variant_rows(product, msk(13))
    assert len(rows) == 8 and rows[0]["article"] == "242804"
    assert sorted({r["price_online_rub"] for r in rows}) == [801, 828, 856, 927, 951]
    tone_570 = next(r for r in rows if r["article"] == "242803")
    assert tone_570["variant"] == "Тон 570, нюдовый" and tone_570["online_qty"] == 0 and tone_570["sold_online"] is False
    row = parse.tile(product, msk(13))
    assert row["variants_count"] == 8 and row["variant_price_range_rub"] == {"min": 801, "max": 951}


# ---- stock ---------------------------------------------------------------------------

def test_available_without_online_stock_means_stores_only():
    tiles = offers("listing_shampoo_price_asc_ekb")
    s = parse.stock(tiles["260327"])
    assert s["sold_online"] is False and s["online_qty"] == 0 and s["stores_qty_in_region"] == 3
    assert "только в магазинах" in s["stock_note"]
    online_only = parse.stock(tiles["223187"])
    assert online_only["sold_online"] is True and online_only["shops_in_region"] == 0


def test_shops_number_counts_the_region_not_the_city():
    shops = parse.shops(load("product_available_242804_ekb"))
    region_ids = {s["store_id"] for s in parse.store_facet(load("filter_all_ekb"))}
    split = parse.split_shops(shops, EKB, region_ids)
    assert len(split["in_city"]) == 9 and [s["city"] for s in split["elsewhere_in_region"]] == ["Первоуральск"]
    assert not split["foreign"]
    card = load("product_242804_ekb")["product"]["availability"]
    assert card["shopsNumber"] == 10 == len(shops)
    assert card["quantityOffline"] == sum(s["qty"] for s in split["in_city"] + split["elsewhere_in_region"])
    assert split["in_city"][0]["hours"] == "Ежедневно: 10:00–22:00"


def test_moscow_answer_from_the_store_endpoint_is_foreign():
    shops = parse.shops(load("product_available_242804_ekb"))
    moscow = [{**s, "city_name": "Москва", "filterValue": f"store_9{i}"} for i, s in enumerate(shops)]
    region_ids = {s["store_id"] for s in parse.store_facet(load("filter_all_ekb"))}
    split = parse.split_shops(moscow, EKB, region_ids)
    assert len(split["foreign"]) == 10 and not split["in_city"]


def test_region_store_list_and_city_split():
    stores = parse.store_facet(load("filter_all_ekb"))
    assert len(stores) == 12 and stores[0]["store_id"].startswith("store_")
    groups = parse.stores_by_city(stores, "Екатеринбург")
    assert len(groups["in_city"]) == 10
    assert [s["city"] for s in groups["elsewhere_in_region"]] == ["Первоуральск"]
    assert [s["address"] for s in groups["city_not_stated"]] == ["пр-т Ленина, д. 38а"]
    assert not parse.looks_like_moscow_fallback(stores, EKB)
    moscow = [{**s, "address": "Москва, " + s["address"]} for s in stores]
    assert parse.looks_like_moscow_fallback(moscow, EKB)


# ---- delivery ------------------------------------------------------------------------

def test_delivery_terms_include_pickup_points_only_with_show_all():
    full = parse.delivery_map(load("delivery_times_showall_ekb"))
    short = parse.delivery_map(load("delivery_times_no_showall_ekb"))
    assert "pvz" in {d["code"] for d in full["242804"]}
    assert "pvz" not in {d["code"] for d in short["242804"]}


def test_delivery_cost_is_null_with_a_reason_and_stale_pickup_is_flagged():
    rows = parse.delivery(parse.delivery_map(load("delivery_times_showall_ekb"))["223187"], stores_qty_in_region=0)
    assert all(r["cost_rub"] is None and "оформлении" in r["cost_note"] for r in rows)
    pickup = next(r for r in rows if r["code"] == "pickup")
    assert pickup["when"] == "сегодня" and "0 шт." in pickup["note"]


# ---- city ------------------------------------------------------------------------------

def test_city_exact_match():
    city = parse.pick_city(parse.city_candidates(load("city_ekb")), "екатеринбург")
    assert city == EKB


def test_same_named_towns_need_a_region():
    cands = parse.city_candidates(load("city_berezovsky"))
    with pytest.raises(parse.CityChoiceError, match="Укажите регион"):
        parse.pick_city(cands, "Березовский")
    assert parse.pick_city(cands, "Березовский, Кемеровская").region == "Кемеровская область"


def test_no_guessing_without_an_exact_city_match():
    with pytest.raises(parse.CityChoiceError, match="Каменск-Уральский"):
        parse.pick_city(parse.city_candidates(load("city_kamensk")), "Каменск")
    with pytest.raises(parse.CityChoiceError, match="не найден"):
        parse.pick_city(parse.city_candidates(load("city_empty")), "Qwertyzzz")


# ---- facets ------------------------------------------------------------------------------

def test_category_lookup_by_name_code_and_path():
    rows = parse.categories(load("categories"))
    assert [r["code"] for r in parse.find_category(rows, "Шампуни")] == ["category_volosy-shampuni"]
    assert parse.find_category(rows, "category_volosy-shampuni")[0]["name"] == "Шампуни"
    assert parse.find_category(rows, "volosy-shampuni")[0]["code"] == "category_volosy-shampuni"
    assert parse.find_category(rows, "нет такой категории") == []


def test_brand_facet_and_category_of_a_tile():
    brands = parse.brand_facet(load("filter_all_ekb"))
    assert brands and all(b["code"].startswith("brand_") for b in brands)
    tile = load("product_242804_ekb")["product"]
    assert parse.brand(tile) == ("LOREAL", "brand_loreal")
    assert parse.category(tile) == {"path": "Макияж/Губы/Помада", "code": "category_makiyazh-guby-pomada"}


# ---- unit price ------------------------------------------------------------------------------

@pytest.mark.parametrize("name, price, expected", [
    ("Шампунь для волос `ORGANIC SHOP` MILK 250 мл", 215, {"rub": 86.0, "per": "100 мл"}),
    ("Гель для душа 1 л", 300, {"rub": 30.0, "per": "100 мл"}),
    ("Патчи 60 шт", 600, {"rub": 10.0, "per": "1 шт"}),
    ("Набор: шампунь 250 мл + бальзам 200 мл", 500, None),
    ("Помада для губ тон 601", 801, None),
])
def test_unit_price_only_from_an_unambiguous_pack_size(name, price, expected):
    got = parse.unit_price(name, price)
    if expected is None:
        assert got is None
    else:
        assert {k: got[k] for k in expected} == expected and got["basis"] == "фасовка взята из названия товара"


# ---- reviews: privacy and paging ------------------------------------------------------------------

FAKE_EMAIL = "fake.person@example.org"
FAKE_PHONE = "+7 900 111-22-33"
FAKE_NAME = "Фейкова Анна"


def aplaut_with_fake_pii():
    data = load("aplaut_205410_variant")
    for it in data["items"]:
        it["author_name"] = FAKE_NAME
        it["location_name"] = "Фейкоград"
        it["order_number"] = "ORDER-777"
        it["author_details"] = [
            {"_id": "1", "label": "Email:", "name": "email", "value": FAKE_EMAIL},
            {"_id": "2", "label": "Телефон", "name": "phone", "value": FAKE_PHONE},
            {"_id": "3", "label": "Срок использования", "name": "time_usage", "value": "Больше года"},
        ]
    data["aggs"]["author_details"] = {"doc_count": 3, "by_name": {"buckets": [
        {"key": "email", "label": "Email:", "doc_count": 1, "distribution": {"buckets": [{"key": FAKE_EMAIL, "doc_count": 1}]}},
        {"key": "phone", "label": "Телефон", "doc_count": 1, "distribution": {"buckets": [{"key": FAKE_PHONE, "doc_count": 1}]}},
    ]}}
    return data


def test_review_keeps_only_allowed_fields():
    data = aplaut_with_fake_pii()
    out = [parse.aplaut_review(r) for r in data["items"]]
    text = repr(out) + repr(parse.aplaut_summary(data))
    for secret in (FAKE_EMAIL, FAKE_PHONE, FAKE_NAME, "Фейкоград", "ORDER-777"):
        assert secret not in text
    assert out[0]["time_usage"] == "Больше года"
    assert set(out[0]) <= {"date", "rating", "text", "pros", "cons", "verified", "recommends", "likes",
                           "dislikes", "photos", "time_usage", "comments"}


def test_native_review_drops_author_fields():
    row = parse.native_review({"id": "r1", "authorName": FAKE_NAME, "authorEmail": FAKE_EMAIL, "rating": 5,
                               "body": "ok", "createdAt": "2026-09-01T10:00:00+03:00", "likesCount": 2})
    assert FAKE_NAME not in repr(row) and FAKE_EMAIL not in repr(row) and row["date"] == "2026-09-01"


@pytest.mark.parametrize("page, limit, expected", [
    (1, 20, [(1, 20, 0, 20)]),
    (1, 50, [(1, 25, 0, 25), (2, 25, 0, 25)]),
    (2, 30, [(2, 25, 5, 20), (3, 25, 0, 10)]),
])
def test_aplaut_pages_respect_its_25_per_page_cap(page, limit, expected):
    assert parse.aplaut_pages(page, limit) == expected


def test_aplaut_answer_for_another_product_is_rejected():
    assert parse.aplaut_problem(load("aplaut_205410_variant"), "242804") == "Aplaut answered for article 205410, not 242804"
    assert parse.aplaut_problem(load("aplaut_205410_variant"), "205410") is None


# ---- plausibility and pruning ----------------------------------------------------------------------

def test_listing_and_product_checks():
    assert parse.listing_problem(load("listing_shampoo_price_asc_ekb")) is None
    assert parse.listing_problem({"listing": {"offers": []}}) == "listing without navigation/offers"
    assert parse.product_problem(load("product_242804_ekb"), "242804") is None
    assert "not 205406" in parse.product_problem(load("product_242804_ekb"), "205406")
    assert parse.products_problem({"products": [{"article": "1"}]}, ["2"]).startswith("answer holds")


def test_prune_keeps_zeros_false_and_explicit_nulls():
    assert parse.prune({"a": None, "b": 0, "c": False, "d": [], "cost_rub": None, "e": {"f": None}}) == \
        {"b": 0, "c": False, "cost_rub": None}
