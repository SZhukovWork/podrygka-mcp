"""Request policy of the client, with the network replaced by recorded answers."""
import json
import logging
import stat

import pytest
import requests

from podrygka_mcp import client
from podrygka_mcp.parse import City

from conftest import TOKEN, Resp, load, ok, path_is, rpc_is

EKB = City(name="Екатеринбург", region="Свердловская область", fias="2763c110-cb8b-416a-9dac-ad28a55b4402",
           region_fias="92b30014-4d52-4e2e-892d-928142b924bf", id="208757")
PRODUCT = "/api/v3/catalog/product/242804"


def problem(status, kind):
    return Resp(status, {"type": kind, "status": status, "title": "x"}, "application/problem+json")


def test_guest_token_is_issued_once_and_stored_privately(make_client, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    c, http = make_client()
    c.product("242804", EKB)
    c.product("242804", EKB)
    assert http.count("guest-token") == 1
    _, _, kw = http.calls[-1]
    assert kw["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert kw["headers"]["x-region-fias"] == EKB.region_fias
    session = tmp_path / "session.json"
    assert json.loads(session.read_text())["token"] == TOKEN
    assert stat.S_IMODE(session.stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert TOKEN not in caplog.text


def test_saved_token_is_reused_by_the_next_process(make_client):
    c, http = make_client()
    c.product("242804", EKB)
    c2, http2 = make_client()
    c2.product("242804", EKB)
    assert http2.count("guest-token") == 0


@pytest.mark.parametrize("status, kind", [(401, "AuthTokenInvalidError"), (403, "AuthTokenMissingError")])
def test_rejected_token_is_reissued_once(make_client, status, kind):
    c, http = make_client()
    http.add(path_is(PRODUCT), [problem(status, kind), ok(load("product_242804_ekb"))])
    assert c.product("242804", EKB)["article"] == "242804"
    assert http.count("guest-token") == 2 and http.count(PRODUCT) == 2


def test_token_rejected_twice_is_an_error_not_a_loop(make_client):
    c, http = make_client()
    http.add(path_is(PRODUCT), problem(401, "AuthTokenInvalidError"))
    with pytest.raises(client.Blocked, match="freshly issued guest token"):
        c.product("242804", EKB)
    assert http.count(PRODUCT) == 2


def test_throttling_gets_one_pause_then_a_clear_error(make_client, sleeps):
    c, http = make_client()
    http.add(path_is(PRODUCT), [Resp(429, {}), ok(load("product_242804_ekb"))])
    assert c.product("242804", EKB)["article"] == "242804"
    assert client.THROTTLE_PAUSE in sleeps

    c, http = make_client()
    http.add(path_is(PRODUCT), Resp(429, {}))
    with pytest.raises(client.RateLimited, match="Wait several minutes"):
        c.product("242804", EKB)
    assert http.count(PRODUCT) == 2


def test_challenge_page_and_foreign_403_are_reported_as_blocked(make_client):
    c, http = make_client()
    http.add(path_is(PRODUCT), Resp(200, "<html>checking your browser</html>", "text/html"))
    with pytest.raises(client.Blocked, match="anti-bot"):
        c.product("242804", EKB)
    assert http.count(PRODUCT) == 2

    c, http = make_client()
    http.add(path_is(PRODUCT), [Resp(403, "<html>denied</html>", "text/html"), ok(load("product_242804_ekb"))])
    assert c.product("242804", EKB)["article"] == "242804"


def test_server_error_is_retried_once(make_client):
    c, http = make_client()
    http.add(path_is(PRODUCT), Resp(500, {"title": "Internal server error"}))
    with pytest.raises(client.PodrygkaError, match="HTTP 500 twice"):
        c.product("242804", EKB)
    assert http.count(PRODUCT) == 2


def test_network_error_is_retried_once(make_client):
    c, http = make_client()
    http.add(path_is(PRODUCT), [requests.ConnectionError("reset"), ok(load("product_242804_ekb"))])
    assert c.product("242804", EKB)["article"] == "242804"


def test_answer_for_another_article_is_never_data(make_client):
    c, http = make_client()
    other = load("product_242804_ekb")
    other["product"]["article"] = "205406"
    http.add(path_is(PRODUCT), ok(other))
    with pytest.raises(client.Implausible, match="not 242804"):
        c.product("242804", EKB)
    assert http.count(PRODUCT) == 2


def test_cut_off_body_is_retried(make_client):
    c, http = make_client()
    http.add(path_is(PRODUCT), [Resp(200, '{"product": {"arti'), ok(load("product_242804_ekb"))])
    assert c.product("242804", EKB)["article"] == "242804"


def test_missing_product_is_not_found(make_client):
    c, _ = make_client()
    with pytest.raises(client.NotFound, match="999999999"):
        c.product("999999999", EKB)


def test_requests_are_spaced(make_client, sleeps, monkeypatch):
    monkeypatch.setenv("PODRYGKA_MIN_INTERVAL", "1.5")
    c, _ = make_client()
    c.product("242804", EKB)
    c.product("242804", EKB)
    assert any(s >= 1.4 for s in sleeps)


# ---- city ------------------------------------------------------------------

def test_city_is_resolved_once_and_cached(make_client):
    c, http = make_client()
    city, source = c.resolve_city(None)
    assert city == EKB and source == "default"
    assert c.resolve_city("Екатеринбург") == (EKB, "param")
    assert http.count("city/search") == 1


def test_find_city_then_resolve_asks_the_directory_once(make_client):
    c, http = make_client()
    assert c.find_cities("Екатеринбург")[0] == EKB
    assert c.resolve_city("Екатеринбург")[0] == EKB
    assert http.count("city/search") == 1


def test_city_from_environment(make_client, monkeypatch):
    monkeypatch.setenv("PODRYGKA_CITY", "Москва")
    c, _ = make_client()
    city, source = c.resolve_city(None)
    assert city.name == "Москва" and source == "env PODRYGKA_CITY"


def test_ambiguous_city_is_an_error_with_candidates(make_client):
    c, _ = make_client()
    with pytest.raises(client.BadInput, match="Свердловская область.*Кемеровская область"):
        c.resolve_city("Березовский")
    city, _ = c.resolve_city("Березовский, Свердловская область")
    assert city.region_fias == EKB.region_fias


def test_city_echo_must_match(make_client):
    """getCity answering with a different city is not taken as the one asked for."""
    c, http = make_client()
    http.add(rpc_is("getCity", search="Екатеринбург"), ok(load("city_moscow")))
    with pytest.raises(client.BadInput, match="Точного совпадения"):
        c.resolve_city("Екатеринбург")


# ---- region data -----------------------------------------------------------------

def test_store_list_for_moscow_instead_of_the_region_is_refused(make_client):
    c, http = make_client()
    moscow = load("filter_all_ekb")
    for item in moscow["filter"]["items"]:
        if item["code"] == "store":
            for v in item["variants"]:
                v["smallText"] = "Москва, " + (v["smallText"] or "")
    http.add(path_is("/api/v3/catalog/filter"), ok(moscow))
    with pytest.raises(client.Implausible, match="Moscow"):
        c.region_stores(EKB)


def test_facets_are_cached_and_brands_resolved_by_name(make_client):
    c, http = make_client()
    assert len(c.region_stores(EKB)) == 12
    assert c.brand_code("Kensuko", EKB) == "brand_kensuko"
    assert c.brand_code("L'Oreal", EKB) == "brand_loreal"
    assert http.count("/api/v3/catalog/filter") == 1
    with pytest.raises(client.BadInput, match="No brand"):
        c.brand_code("Nonexistentbrandzz", EKB)
    c2, http2 = make_client()                 # next process: from the disk cache
    assert len(c2.region_stores(EKB)) == 12 and http2.count("/api/v3/catalog/filter") == 0


def test_store_cookies_carry_the_region(make_client):
    c, http = make_client()
    shops = c.shops("242804", EKB)
    assert len(shops) == 10
    _, _, kw = http.calls[-1]
    assert kw["cookies"]["BITRIX_SM_FIAS"] == EKB.fias and kw["cookies"]["BITRIX_SM_REGION_FIAS"] == EKB.region_fias


def test_delivery_terms_are_asked_like_the_product_page(make_client):
    c, http = make_client()
    got = c.delivery_times(["242804", "84361"], EKB)
    _, _, kw = http.calls[-1]
    assert kw["params"]["showAll"] == "true"
    assert "pvz" in {d["code"] for d in got["242804"]}


# ---- reviews ----------------------------------------------------------------------

def test_stale_widget_key_is_reread_once(make_client, tmp_path):
    c, http = make_client()
    http.add(path_is("/v2/reviews", context_id="205410"), [Resp(401, {"error": "unauthorized"}), ok(load("aplaut_205410_variant"))])
    data = c.aplaut_reviews("205410", False, 1, 5, "created_at:desc")
    assert data["meta"]["total_count"] == 7
    assert http.count("app.js") == 2
    assert stat.S_IMODE((tmp_path / "aplaut.json").stat().st_mode) == 0o600


def test_review_answer_for_another_product_is_refused(make_client):
    c, http = make_client()
    http.add(path_is("/v2/reviews", context_id="242804"), ok(load("aplaut_205410_variant")))
    with pytest.raises(client.Implausible, match="not 242804"):
        c.aplaut_reviews("242804", False, 1, 5, "created_at:desc")


def test_failing_native_review_api_is_skipped_for_an_hour(make_client):
    c, http = make_client()
    assert c.native_reviews("242804", EKB, True, 1, 20) is None
    assert c.native_reviews("242804", EKB, True, 1, 20) is None
    assert http.count("/api/v3/content/reviews") == 1
