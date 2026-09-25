"""Pure functions over podrygka.ru / Aplaut JSON: no I/O, no clock reads.

Everything that turns a raw answer into a labelled number lives here, so the
traps found while checking the API against the site are covered by offline
tests on recorded responses.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

SITE = "https://www.podrygka.ru"
MSK = timezone(timedelta(hours=3), "MSK")

# The chain's morning discount: +5 % "by loyalty card", 09:00–12:00 Moscow time
# (rules page /discount/). The site shows it to anonymous visitors too: seen
# live on 2026-09-25, every non-promo item on a page lost its −5 % at 12:00 MSK.
MORNING_START_HOUR = 9
MORNING_END_HOUR = 12
MORNING_PERCENT = 5
# Server and local clocks may disagree a little around the window edges.
MORNING_EDGE_TOLERANCE = timedelta(minutes=5)

# Card discounts (including the morning one) do not apply to these brands
# (rules page /lp/card.html).
NO_CARD_DISCOUNT_BRANDS = ("CATRICE", "ESSENCE", "GILLETTE")

PRICE_SCOPE = "онлайн, без входа"
CARD_RULE_NOTE = "по правилам сети; корзиной не проверено"
PRICE_NOTE = (
    "Цены — онлайн-цены podrygka.ru для покупателя без входа в аккаунт; от региона онлайн-цена "
    "не зависит (проверено на Москве и Свердловской обл.). Персональная скидка по карте лояльности "
    "(3–15 %) не входит и без входа не считается; по правилам сети она не суммируется с акциями "
    "(Выгодная цена, Распродажа, Выгодная пара, 1+1=3, 15 % 15-го) и не действует на CATRICE, "
    "ESSENCE, GILLETTE. Цена в магазине может отличаться от онлайн-цены (так пишет сам сайт)."
)
DELIVERY_COST_NOTE = "стоимость доставки сайт показывает только при оформлении заказа"

# Search tiles: 20 per page, fixed by the site (there is no page-size parameter).
PAGE_SIZE = 20

SORTS = {"popular": None, "rating": "rating", "price_asc": "price_asc", "price_desc": "price_desc"}

# Marks (shields) that change or condition the price.  Keyed by mark `type`;
# the SALE shield shares type "discount" with the discount badge and is told
# apart by its name.
PROMO_MARKS: dict[str, dict[str, Any]] = {
    "benefit": {
        "label": "Выгодная цена",
        "requires_loyalty_card": True,
        "price_effect": "скидка уже в price_online_rub",
        "rule": "«специальные акции только для владельцев дисконтных карт» (/lp/card.html); сайт показывает эту цену и без входа",
    },
    "sale": {
        "label": "SALE (распродажа)",
        "requires_loyalty_card": False,
        "price_effect": "скидка уже в price_online_rub",
        "rule": "в правилах сети не названа акцией только для владельцев карты",
    },
    "pair": {
        "label": "Выгодная пара",
        "requires_loyalty_card": True,
        "price_effect": "скидка (обычно 50 %) на второй товар из акции считается в корзине; в price_online_rub её нет",
        "rule": "«специальные акции только для владельцев дисконтных карт» (/lp/card.html)",
    },
    "3for2": {
        "label": "1+1=3 (три по цене двух)",
        "requires_loyalty_card": True,
        "price_effect": "третий товар бесплатно считается в корзине; в price_online_rub этого нет",
        "rule": "«специальные акции только для владельцев дисконтных карт» (/lp/card.html)",
    },
    "gift": {
        "label": "Подарок за покупку",
        "requires_loyalty_card": "unknown",
        "price_effect": "цена не меняется; подарок и его условия — на странице акции",
        "rule": "правила не говорят однозначно, нужна ли карта",
    },
}
# Promotions whose discount is already inside the shown price.
PRICE_IN_SHOWN = {"benefit", "sale"}
# Marks that carry no price condition.
PLAIN_LABELS = {"hit": "Хит продаж", "new": "Новинка"}
# Search `promo_only`: products with a price promotion (filters of one kind are OR-ed by the site).
PROMO_FILTER_SLUGS = ("mark_msk-benefit", "mark_msk-discount", "mark_msk-pair", "mark_msk-3for2")
PROMO_MARK_TYPES = {"benefit", "sale", "pair", "3for2"}

REVIEW_SORTS = {"newest": "created_at:desc", "helpful": "helpfulness:desc"}
APLAUT_MAX_PER_PAGE = 25


class ParseError(ValueError):
    """The answer does not have the shape the parser relies on."""


# ---------------------------------------------------------------------------
# small helpers

def norm(text: Any) -> str:
    """Case/ё/space-insensitive form for comparing names typed by people."""
    s = str(text or "").lower().replace("ё", "е")
    s = re.sub(r"[`'’\"«»]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def squash(text: Any) -> str:
    """Letters and digits only: 'L'Oreal' == 'LOREAL', 'I`CE' == 'ICE'."""
    return re.sub(r"[^0-9a-zа-я]", "", norm(text))


def num(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else float(value)
    try:
        f = float(str(value).replace(",", "."))
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def product_url(path: str | None) -> str | None:
    if not path:
        return None
    return path if path.startswith("http") else SITE + path


# Fields that stay even when null: their absence could read as "free" / "no price".
KEEP_NULL = {"cost_rub", "price_online_rub"}


def prune(obj: Any) -> Any:
    """Drop None, empty strings, lists and dicts (zeros and False are data)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            v = prune(v)
            if v is None and k in KEEP_NULL:
                out[k] = None
                continue
            if v is None or v == "" or v == [] or v == {}:
                continue
            out[k] = v
        return out
    if isinstance(obj, list):
        return [prune(v) for v in obj if v is not None]
    return obj


# ---------------------------------------------------------------------------
# time

def morning_window(now: datetime) -> dict[str, Any]:
    """Whether the morning −5 % window (09:00–12:00 MSK) is open at `now`."""
    msk = now.astimezone(MSK)
    active = MORNING_START_HOUR <= msk.hour < MORNING_END_HOUR
    info: dict[str, Any] = {
        "active": active,
        "window_msk": f"{MORNING_START_HOUR:02d}:00–{MORNING_END_HOUR:02d}:00",
        "now_msk": msk.strftime("%H:%M"),
        "until_msk": f"{MORNING_END_HOUR:02d}:00" if active else None,
    }
    if not active:
        info["next_start_msk"] = f"{MORNING_START_HOUR:02d}:00 " + ("сегодня" if msk.hour < MORNING_START_HOUR else "завтра")
    info["note"] = (
        "Утренняя скидка −5 %: с 9:00 до 12:00 МСК сайт показывает её и без входа на товары без акций "
        "(конец окна в 12:00 наблюдался, начало в 9:00 — по правилам сети). По правилам она «по карте "
        "лояльности / при авторизации»; начислит ли её корзина гостю — не проверено."
    )
    return info


def _near_morning_window(now: datetime) -> bool:
    msk = now.astimezone(MSK)
    start = msk.replace(hour=MORNING_START_HOUR, minute=0, second=0, microsecond=0)
    end = msk.replace(hour=MORNING_END_HOUR, minute=0, second=0, microsecond=0)
    return start - MORNING_EDGE_TOLERANCE <= msk < end + MORNING_EDGE_TOLERANCE


# ---------------------------------------------------------------------------
# city

@dataclass(frozen=True)
class City:
    name: str
    region: str
    fias: str
    region_fias: str
    id: str | None = None

    @classmethod
    def from_api(cls, raw: dict) -> "City":
        try:
            return cls(
                name=str(raw["city_name"]), region=str(raw.get("region") or ""),
                fias=str(raw["fias"]), region_fias=str(raw["region_fias"]),
                id=str(raw["id"]) if raw.get("id") is not None else None,
            )
        except KeyError as e:
            raise ParseError(f"city entry without {e}") from e

    def to_dict(self) -> dict:
        return asdict(self)


class CityChoiceError(ValueError):
    def __init__(self, message: str, candidates: list[City]):
        super().__init__(message)
        self.candidates = candidates


def city_candidates(data: Any) -> list[City]:
    if not isinstance(data, dict) or not isinstance(data.get("result"), list):
        raise ParseError("getCity answer has no result list")
    return [City.from_api(c) for c in data["result"] if isinstance(c, dict)]


def pick_city(candidates: list[City], query: str) -> City:
    """The one city the query names, or an error listing the candidates.

    `query` is a city name, optionally followed by ", <region>" to tell apart
    same-named towns ("Березовский, Свердловская область").
    """
    name, _, region = query.partition(",")
    exact = [c for c in candidates if norm(c.name) == norm(name)]
    if region.strip():
        exact = [c for c in exact if norm(region) in norm(c.region)]
    if len(exact) == 1:
        return exact[0]
    listed = "; ".join(f"{c.name}, {c.region}" for c in (exact or candidates)[:10])
    if not candidates:
        raise CityChoiceError(f"Город «{query}» не найден в справочнике podrygka.ru.", [])
    if len(exact) > 1:
        raise CityChoiceError(
            f"Городов «{name.strip()}» несколько: {listed}. Укажите регион: «{name.strip()}, <область>».", exact)
    raise CityChoiceError(f"Точного совпадения для «{query}» нет. Похожие: {listed}.", candidates)


# ---------------------------------------------------------------------------
# prices and marks

def _marks(offer: dict) -> list[dict]:
    return [m for m in offer.get("marks") or [] if isinstance(m, dict)]


def mark_kind(mark: dict) -> str:
    kind = str(mark.get("type") or "")
    if kind == "discount" and norm(mark.get("name")) == "sale":
        return "sale"
    return kind


def promo_labels(offer: dict) -> tuple[list[dict], list[str]]:
    """Price-related promotions (with the card rule) and plain labels."""
    promos: list[dict] = []
    labels: list[str] = []
    seen: set[str] = set()
    promotions = [p for p in offer.get("promotions") or [] if isinstance(p, dict)]
    for mark in _marks(offer):
        kind = mark_kind(mark)
        if kind in seen:
            continue
        seen.add(kind)
        if kind in PROMO_MARKS:
            spec = PROMO_MARKS[kind]
            entry = {
                "code": kind,
                "label": spec["label"],
                "requires_loyalty_card": spec["requires_loyalty_card"],
                "requires_loyalty_card_basis": f"{CARD_RULE_NOTE}: {spec['rule']}" if spec["requires_loyalty_card"] is True
                else spec["rule"],
                "price_effect": spec["price_effect"],
            }
            if kind == "pair" and mark.get("name"):
                entry["site_text"] = f"{mark['name']} {mark.get('title') or ''}".strip()
            if kind == "3for2":
                names = [p.get("name") for p in promotions if p.get("type") == "threeForTwo" and p.get("name")]
                if names:
                    entry["promotion"] = names[0]
            promos.append(entry)
        elif kind in PLAIN_LABELS:
            labels.append(PLAIN_LABELS[kind])
        elif kind == "discount":
            continue  # the "Персональная скидка −N %" badge: the discount itself, not a promotion
        elif mark.get("name"):
            # A shield this server does not know yet: pass it on, without guessing its terms.
            promos.append({
                "code": kind or "unknown", "label": str(mark["name"]).strip(),
                "requires_loyalty_card": "unknown", "requires_loyalty_card_basis": "метка не описана в правилах, известных серверу",
            })
    return promos, labels


def promotions(offer: dict) -> list[dict]:
    return [
        {"name": p.get("name"), "code": p.get("filterValue")}
        for p in offer.get("promotions") or [] if isinstance(p, dict) and p.get("name")
    ]


def _is_morning_shape(base: float, total: float, percent: Any) -> bool:
    if num(percent) == MORNING_PERCENT:
        return True
    return abs(total - base * (100 - MORNING_PERCENT) / 100) <= 1


def price(offer: dict, now: datetime) -> dict[str, Any]:
    """The shown online price with what kind of discount it contains."""
    p = offer.get("pricing") or {}
    total, base = num(p.get("totalPrice")), num(p.get("basePrice"))
    if not total or total <= 0:
        return {"price_online_rub": None, "price_note": "в ответе сайта нет цены (товар, вероятно, не продаётся онлайн)"}
    out: dict[str, Any] = {"price_online_rub": total}
    promos, _ = promo_labels(offer)
    if not base or base <= total:
        out["discount_kind"] = "none"
        return out
    out["price_before_discount_rub"] = base
    percent = num(p.get("discountPercent"))
    out["discount_percent"] = percent if percent is not None else round((base - total) / base * 100)
    kinds = {x["code"] for x in promos}
    brand = str(((offer.get("attributes") or {}).get("brand") or {}).get("value") or "").upper()
    if kinds & PRICE_IN_SHOWN:
        out["discount_kind"] = "promo"
    elif _is_morning_shape(base, total, percent) and _near_morning_window(now) and brand not in NO_CARD_DISCOUNT_BRANDS:
        out["discount_kind"] = "morning_5pct"
        out["price_after_morning_window_rub"] = base
        out["requires_loyalty_card"] = True
        out["requires_loyalty_card_basis"] = (
            f"{CARD_RULE_NOTE}: утренняя скидка действует «при предъявлении дисконтной карты … и при авторизации "
            "в интернет-магазине» (/discount/); сайт показывает её и без входа"
        )
        out["discount_note"] = "утренняя скидка −5 % до 12:00 МСК; потом цена вернётся к price_before_discount_rub"
    else:
        out["discount_kind"] = "unknown"
        note = "скидка без метки акции; сервер не может сказать, чья она и до когда действует"
        if now.astimezone(MSK).day == 15:
            note += ("; сегодня 15-е — по правилам сети скидка по карте 15 % (с 9 до 12 — 20 %), "
                     "если это она, то она для владельцев карты")
        out["discount_note"] = note
    return out


_VOLUME = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(мл|ml|л|г|гр|g|кг|шт)\.?(?=$|[\s,;)/])", re.IGNORECASE)
_UNIT = {"мл": ("мл", 1), "ml": ("мл", 1), "л": ("мл", 1000), "г": ("г", 1), "гр": ("г", 1), "g": ("г", 1),
         "кг": ("г", 1000), "шт": ("шт", 1)}


def unit_price(name: str | None, price_rub: float | None) -> dict | None:
    """Price per 100 ml / 100 g / 1 pc from the pack size in the name, if unambiguous."""
    if not name or not price_rub:
        return None
    found = _VOLUME.findall(name)
    if len(found) != 1:
        return None
    amount, unit = found[0]
    base_unit, factor = _UNIT[unit.lower()]
    qty = float(amount.replace(",", ".")) * factor
    if qty <= 0:
        return None
    per = 1 if base_unit == "шт" else 100
    return {
        "rub": round(price_rub / qty * per, 2),
        "per": f"{per} {base_unit}" if per != 1 else f"1 {base_unit}",
        "pack": f"{num(amount)} {unit}",
        "basis": "фасовка взята из названия товара",
    }


# ---------------------------------------------------------------------------
# stock, variants, lines

def stock(offer: dict) -> dict[str, Any]:
    a = offer.get("availability") or {}
    online = num(a.get("quantityOnline"))
    offline = num(a.get("quantityOffline"))
    shops = num(a.get("shopsNumber"))
    sold_online = bool(a.get("availableOnline")) and (online or 0) > 0
    out: dict[str, Any] = {
        "online_qty": online,
        "sold_online": sold_online,
        "stores_qty_in_region": offline,
        "shops_in_region": shops,
    }
    if not sold_online and (offline or 0) > 0:
        out["stock_note"] = "онлайн-склад пуст: товар есть только в магазинах региона"
    elif not sold_online and not offline:
        out["stock_note"] = "нет в наличии ни онлайн, ни в магазинах региона"
    return out


def line_articles(offer: dict) -> list[str]:
    arts = [str(offer.get("article"))]
    arts += [str(v.get("article")) for v in offer.get("offers") or [] if isinstance(v, dict) and v.get("article")]
    return list(dict.fromkeys(arts))


def variant_label(v: dict) -> str | None:
    attrs = v.get("attributes") or {}
    for key in ("tone", "color", "volume", "size"):
        value = (attrs.get(key) or {}).get("value") if isinstance(attrs.get(key), dict) else None
        if value:
            return str(value)
    return None


def variant_prices(offer: dict) -> list[float]:
    values = []
    for v in [offer, *[x for x in offer.get("offers") or [] if isinstance(x, dict)]]:
        total = num((v.get("pricing") or {}).get("totalPrice"))
        if total and total > 0:
            values.append(total)
    return values


def dedup_lines(offers: list[dict]) -> list[tuple[dict, list[str]]]:
    """Merge tiles of one product line (they share articles); keep first tile.

    Returns (tile, other tile articles merged into it).
    """
    kept: list[tuple[dict, set[str], list[str]]] = []
    for o in offers:
        arts = set(line_articles(o))
        for _, seen, merged in kept:
            if arts & seen:
                seen |= arts
                merged.append(str(o.get("article")))
                break
        else:
            kept.append((o, arts, []))
    return [(tile, merged) for tile, _, merged in kept]


def brand(offer: dict) -> tuple[str | None, str | None]:
    b = (offer.get("attributes") or {}).get("brand") or {}
    return (b.get("value") or None, b.get("filterValue") or None) if isinstance(b, dict) else (None, None)


def category(offer: dict) -> dict | None:
    sec = offer.get("bitrixSection") or {}
    if not isinstance(sec, dict) or not sec.get("namePath"):
        return None
    m = re.search(r"/(category_[^/]+)/?$", sec.get("url") or "")
    path = "/".join(part.strip() for part in str(sec["namePath"]).split("/"))
    return {"path": path, "code": m.group(1) if m else None}


def category_codes(offer: dict) -> set[str]:
    codes = set()
    cat = category(offer)
    if cat and cat["code"]:
        codes.add(cat["code"])
    for s in (offer.get("attributes") or {}).get("section") or []:
        if isinstance(s, dict) and s.get("filterValue"):
            codes.add(s["filterValue"])
    return codes


def rating_line(offer: dict) -> dict[str, Any]:
    r = offer.get("reviews") or {}
    amount = num(r.get("amount"))
    value = num(r.get("rating"))
    out: dict[str, Any] = {"reviews_line": amount if amount is not None else None}
    if amount and value:
        out["rating_line"] = value
    return out


def tile(offer: dict, now: datetime) -> dict[str, Any]:
    """One search/compare row: flat, every figure labelled by its field name."""
    promos, labels = promo_labels(offer)
    b_name, b_code = brand(offer)
    cat = category(offer)
    prices = variant_prices(offer)
    variants = len(line_articles(offer))
    row: dict[str, Any] = {
        "article": str(offer.get("article")),
        "name": offer.get("name"),
        "brand": b_name,
        "brand_code": b_code,
        "category": cat["path"] if cat else None,
        "category_code": cat["code"] if cat else None,
        "url": product_url(offer.get("url")),
    }
    row.update(price(offer, now))
    row["promo_labels"] = promos
    row["promotions"] = promotions(offer)
    row["labels"] = labels
    row["unit_price"] = unit_price(offer.get("name"), row.get("price_online_rub"))
    row.update(rating_line(offer))
    row["variants_count"] = variants
    if variants > 1 and prices:
        row["variant_price_range_rub"] = {"min": min(prices), "max": max(prices)}
    row.update(stock(offer))
    return row


def variant_rows(offer: dict, now: datetime, delivery_by_article: dict | None = None) -> list[dict]:
    rows = []
    for v in [offer, *[x for x in offer.get("offers") or [] if isinstance(x, dict)]]:
        row: dict[str, Any] = {
            "article": str(v.get("article")),
            "variant": variant_label(v),
            "name": v.get("name"),
            "url": product_url(v.get("url")),
        }
        row.update(price(v, now))
        row.update({k: val for k, val in stock(v).items()})
        if delivery_by_article is not None:
            row["delivery"] = delivery_brief(delivery_by_article.get(row["article"]))
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# characteristics

_SKIP_ATTRS = {"brand", "section", "tag", "marking"}


def characteristics(offer: dict) -> dict[str, str]:
    """Attribute name → value as the card shows it (the site's own labels)."""
    out: dict[str, str] = {}
    for key, attr in (offer.get("attributes") or {}).items():
        if key in _SKIP_ATTRS or not isinstance(attr, dict):
            continue
        value = attr.get("value")
        if value in (None, ""):
            continue
        out[str(attr.get("name") or key)] = str(value)
    return out


# ---------------------------------------------------------------------------
# delivery

_DELIVERY_NAMES = {"pickup": "самовывоз из магазина", "courier": "курьер", "pvz": "пункт выдачи"}


def delivery_map(data: Any) -> dict[str, list[dict]]:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ParseError("delivery_times answer has no items list")
    return {str(i.get("article")): i.get("delivery_time") or [] for i in data["items"] if isinstance(i, dict)}


def delivery(options: list[dict] | None, stores_qty_in_region: Any = None) -> list[dict]:
    out = []
    for opt in options or []:
        if not isinstance(opt, dict):
            continue
        row = {
            "method": opt.get("name") or _DELIVERY_NAMES.get(opt.get("code"), opt.get("code")),
            "code": opt.get("code"),
            "when": opt.get("value"),
            "min_days": num(opt.get("minDays")),
            "cost_rub": None,
            "cost_note": DELIVERY_COST_NOTE,
        }
        if opt.get("code") == "pickup" and num(opt.get("minDays")) == 0 and stores_qty_in_region == 0:
            row["note"] = ("в магазинах региона товара нет (0 шт.), а сайт пишет «сегодня» — "
                           "вероятно, заказ везут со склада; срок уточняется при оформлении")
        out.append(row)
    return out


def delivery_brief(options: list[dict] | None) -> str | None:
    parts = [f"{_DELIVERY_NAMES.get(o.get('code'), o.get('name'))}: {o.get('value')}"
             for o in options or [] if isinstance(o, dict) and o.get("value")]
    return "; ".join(parts) or None


# ---------------------------------------------------------------------------
# stores

_STREET_WORDS = re.compile(r"^(ул|улица|пр|пр-т|просп|проспект|пл|площадь|б-р|бульвар|ш|шоссе|пер|переулок|наб|мкр|тц|д)\b", re.I)


def _address_city(address: str) -> str | None:
    m = re.match(r"\s*(?:г\.\s*)?([А-ЯЁ][А-ЯЁа-яё]+(?:-[А-ЯЁа-яё]+)*)(?=[\s,.])", address or "")
    if not m or _STREET_WORDS.match(m.group(1)):
        return None
    return m.group(1)


def store_facet(data: Any) -> list[dict]:
    """Stores of the region from the catalog `store` filter (with product counts)."""
    if not isinstance(data, dict) or not isinstance((data.get("filter") or {}).get("items"), list):
        raise ParseError("filter answer has no filter.items")
    for item in data["filter"]["items"]:
        if isinstance(item, dict) and item.get("code") == "store":
            stores = []
            for v in item.get("variants") or []:
                code = str(v.get("filterValue") or "")
                m = re.fullmatch(r"store_(\d+)", code)
                if not m:
                    continue
                stores.append({
                    "store_id": code,
                    "name": v.get("name"),
                    "address": v.get("smallText"),
                    "products_in_stock": num(v.get("initialCount")),
                })
            return stores
    return []


def stores_by_city(stores: list[dict], city: str) -> dict[str, list[dict]]:
    """Split region stores by what their address says about the city."""
    groups: dict[str, list[dict]] = {"in_city": [], "elsewhere_in_region": [], "city_not_stated": []}
    for s in stores:
        named = _address_city(s.get("address") or "")
        if named is None:
            groups["city_not_stated"].append(s)
        elif norm(named) == norm(city):
            groups["in_city"].append(s)
        else:
            groups["elsewhere_in_region"].append({**s, "city": named})
    return groups


def looks_like_moscow_fallback(stores: list[dict], city: City) -> bool:
    """The API silently answers for Moscow when it ignores the region."""
    if not stores or "москв" in norm(city.region) or "москв" in norm(city.name):
        return False
    moscow = sum(1 for s in stores if "москв" in norm(s.get("address")))
    return moscow > len(stores) / 2


def shops(data: Any) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get("result"), dict) \
            or not isinstance(data["result"].get("shops"), list):
        raise ParseError("productAvailable answer has no result.shops")
    return [s for s in data["result"]["shops"] if isinstance(s, dict)]


def shop_row(s: dict) -> dict[str, Any]:
    hours = "; ".join(
        f"{w.get('day')}: {w.get('from')}–{w.get('to')}".strip()
        for w in s.get("working_time") or [] if isinstance(w, dict) and w.get("from")
    )
    return {
        "store_id": s.get("filterValue"),
        "name": s.get("name"),
        "address": s.get("address"),
        "city": s.get("city_name"),
        "qty": num(s.get("amount")),
        "level": s.get("amount_text"),
        "hours": hours or None,
        "lat": num(s.get("latitude")),
        "lon": num(s.get("longitude")),
    }


def split_shops(shop_list: list[dict], city: City, region_store_ids: set[str] | None) -> dict[str, Any]:
    """Stores holding the product: this city vs the rest of the region.

    `region_store_ids` is the region's store list from the catalog filter; a
    store outside it means the answer is for another region.
    """
    in_city, elsewhere, foreign = [], [], []
    for s in shop_list:
        row = shop_row(s)
        if region_store_ids is not None and row["store_id"] not in region_store_ids:
            foreign.append(row)
        elif norm(row["city"]) == norm(city.name):
            in_city.append(row)
        else:
            elsewhere.append(row)
    key = lambda r: (-(r["qty"] or 0), r["name"] or "")  # noqa: E731
    return {"in_city": sorted(in_city, key=key), "elsewhere_in_region": sorted(elsewhere, key=key), "foreign": foreign}


# ---------------------------------------------------------------------------
# catalog facets

def categories(data: Any) -> list[dict]:
    """Flatten the category tree into code / name / path rows."""
    if not isinstance(data, dict) or not isinstance(data.get("filter"), dict):
        raise ParseError("categories answer has no filter")
    rows: list[dict] = []

    def walk(variants: Iterable, path: list[str]) -> None:
        for v in variants or []:
            if not isinstance(v, dict):
                continue
            name = str(v.get("name") or "").strip()
            code = v.get("filterValue")
            here = [*path, name] if name else path
            if code and str(code).startswith("category_"):
                rows.append({"code": code, "name": name, "path": "/".join(here), "count": num(v.get("initialCount"))})
            walk(v.get("variants"), here)

    for item in data["filter"].get("items") or []:
        if isinstance(item, dict) and item.get("code") == "category":
            walk(item.get("variants"), [])
    return rows


def find_category(rows: list[dict], wanted: str) -> list[dict]:
    """Categories whose code, name or path suffix equals `wanted`."""
    w = norm(wanted).strip("/")
    code = w if w.startswith("category_") else f"category_{w}"
    by_code = [r for r in rows if norm(r["code"]) == code]
    if by_code:
        return by_code
    return [r for r in rows if norm(r["name"]) == w or norm(r["path"]).endswith(w) and "/" in w]


def brand_facet(data: Any) -> list[dict]:
    """All brands of the catalog `brand` filter (a tree grouped by first letter)."""
    if not isinstance(data, dict) or not isinstance((data.get("filter") or {}).get("items"), list):
        raise ParseError("filter answer has no filter.items")
    out: list[dict] = []

    def walk(variants: Iterable) -> None:
        for v in variants or []:
            if not isinstance(v, dict):
                continue
            code = str(v.get("filterValue") or "")
            if code.startswith("brand_"):
                out.append({"name": v.get("name"), "code": code, "count": num(v.get("initialCount"))})
            walk(v.get("variants"))

    for item in data["filter"]["items"]:
        if isinstance(item, dict) and item.get("code") == "brand":
            walk(item.get("variants"))
    return out


# ---------------------------------------------------------------------------
# listing

def listing(data: Any) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("listing"), dict):
        raise ParseError("no listing object")
    lst = data["listing"]
    if not isinstance(lst.get("navigation"), dict) or not isinstance(lst.get("offers"), list):
        raise ParseError("listing without navigation/offers")
    return lst


def listing_problem(data: Any) -> str | None:
    try:
        lst = listing(data)
    except ParseError as e:
        return str(e)
    for o in lst["offers"]:
        if not isinstance(o, dict) or not o.get("article") or not isinstance(o.get("pricing"), dict):
            return "listing tile without article/pricing"
    return None


def product_problem(data: Any, article: str) -> str | None:
    if not isinstance(data, dict) or not isinstance(data.get("product"), dict):
        return "no product object"
    got = str(data["product"].get("article"))
    if got != str(article):
        return f"answer is for article {got}, not {article}"
    if not isinstance(data["product"].get("pricing"), dict):
        return "product without pricing"
    return None


def products_problem(data: Any, articles: list[str]) -> str | None:
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        return "no products list"
    extra = {str(p.get("article")) for p in data["products"] if isinstance(p, dict)} - set(articles)
    if extra:
        return f"answer holds articles that were not asked for: {sorted(extra)[:3]}"
    return None


# ---------------------------------------------------------------------------
# reviews (Aplaut and the site's own backend)

def stars(buckets: Any) -> dict[str, int] | None:
    if not isinstance(buckets, list):
        return None
    out = {str(k): 0 for k in range(1, 6)}
    for b in buckets:
        if isinstance(b, dict) and num(b.get("key")) in (1, 2, 3, 4, 5):
            out[str(num(b["key"]))] = int(num(b.get("doc_count")) or 0)
    return out


def mean_from_stars(dist: dict[str, int] | None) -> tuple[float | None, int]:
    if not dist:
        return None, 0
    n = sum(dist.values())
    if not n:
        return None, 0
    return round(sum(int(k) * v for k, v in dist.items()) / n, 2), n


def aplaut_problem(data: Any, article: str) -> str | None:
    if not isinstance(data, dict):
        return "body is not a JSON object"
    ctx = data.get("context")
    if not isinstance(ctx, dict) or not isinstance(data.get("items"), list) or not isinstance(data.get("meta"), dict):
        return "no context/items/meta"
    alias = ctx.get("alias")
    if alias is not None and str(alias) != str(article):
        return f"Aplaut answered for article {alias}, not {article}"
    return None


def _time_usage(details: Any) -> str | None:
    """The only author detail kept: how long the buyer has used the product."""
    for d in details or []:
        if isinstance(d, dict) and d.get("name") == "time_usage" and isinstance(d.get("value"), str):
            return d["value"]
    return None


def aplaut_review(item: dict) -> dict[str, Any]:
    """An Aplaut review reduced to an allow-list of fields.

    Aplaut also returns the author's name, city, order number and — inside
    `author_details` — e-mail and phone; none of that is copied.
    """
    created = str(item.get("created_at") or "")[:10] or None
    rating = num(item.get("rating"))
    return {
        "date": created,
        "rating": rating,
        "text": (item.get("body") or None) if not item.get("wordless") else None,
        "pros": item.get("pros") or None,
        "cons": item.get("cons") or None,
        "verified": item.get("verified") if isinstance(item.get("verified"), bool) else None,
        "recommends": item.get("recommended") if isinstance(item.get("recommended"), bool) else None,
        "likes": num(item.get("likes")),
        "dislikes": num(item.get("dislikes")),
        "photos": len(item.get("photos") or []),
        "time_usage": _time_usage(item.get("author_details")),
        "comments": len(item.get("comments") or []) or None,
    }


def aplaut_summary(data: dict) -> dict[str, Any]:
    ctx = data.get("context") or {}
    meta = data.get("meta") or {}
    dist = stars(((data.get("aggs") or {}).get("rating") or {}).get("buckets"))
    mean, counted = mean_from_stars(dist)
    return {
        "total": num(meta.get("total_count")),
        "total_pages": num(meta.get("total_pages")),
        "stars": dist,
        "rating_from_stars": mean,
        "rated": counted,
        "line_rating_shown": num(ctx.get("rating")),
        "line_reviews_published": num(ctx.get("published_group_reviews_count")),
        "group_id": ctx.get("group_id"),
    }


def native_problem(data: Any) -> str | None:
    """Shape of /api/v3/content/reviews as the site's JS reads it (never seen live: it answers 500)."""
    if not isinstance(data, dict):
        return "body is not a JSON object"
    if not isinstance(data.get("reviews"), list) or not isinstance(data.get("pagination"), dict):
        return "no reviews/pagination"
    return None


def native_review(item: dict) -> dict[str, Any]:
    """Same allow-list for the site's own review backend (author name/e-mail dropped)."""
    return {
        "date": str(item.get("createdAt") or "")[:10] or None,
        "rating": num(item.get("rating")),
        "text": item.get("body") or None,
        "pros": item.get("pros") or None,
        "cons": item.get("cons") or None,
        "recommends": item.get("isRecommended") if isinstance(item.get("isRecommended"), bool) else None,
        "likes": num(item.get("likesCount")),
        "dislikes": num(item.get("dislikesCount")),
        "photos": len(item.get("media") or []) if isinstance(item.get("media"), list) else None,
        "time_usage": item.get("usagePeriod") or None,
        "variant": item.get("productVariant") or None,
        "comments": num(item.get("commentsCount")) or None,
    }


def aplaut_pages(page: int, limit: int) -> list[tuple[int, int, int, int]]:
    """Aplaut pages (page, per_page) covering items [(page-1)*limit, page*limit).

    Aplaut caps per_page at 25. Returns (aplaut_page, per_page, skip, take).
    """
    if limit <= APLAUT_MAX_PER_PAGE:
        return [(page, limit, 0, limit)]
    start = (page - 1) * limit
    end = start + limit
    per = APLAUT_MAX_PER_PAGE
    out = []
    for p in range(start // per + 1, (end - 1) // per + 2):
        lo, hi = (p - 1) * per, p * per
        skip = max(0, start - lo)
        take = min(hi, end) - max(lo, start)
        out.append((p, per, skip, take))
    return out
