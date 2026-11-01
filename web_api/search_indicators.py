"""Read-only simulation indicators for search, separate from tradable instruments."""
import math


def _finite(value):
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def search_indicators(connection, search=""):
    """Return country policy rates and exchange indices; never modify the database.

    Names and currency/exchange codes are matched literally, case-insensitively.
    No instrument codes are exposed: these records cannot become order targets.
    """
    term = str(search or "").strip().casefold()
    compact_term = "".join(term.split())

    def matches(values):
        return not term or any(
            term in str(value).casefold()
            or compact_term in "".join(str(value).casefold().split())
            for value in values
        )

    countries = []
    products = []
    for table, product_type in (("saving", "저축상품"), ("liability", "대출상품")):
        for row in connection.execute(
            f"SELECT product_code,name,issuer,interest_rate,quote_currency FROM {table} "
            "WHERE status='active' ORDER BY product_code"
        ):
            code, name, issuer, rate, currency = row
            if matches((code, name or "", issuer or "", currency or "")):
                products.append({"code": str(code), "name": str(name or code), "issuer": str(issuer or ""),
                                 "productType": product_type, "interestRate": _finite(rate),
                                 "currencyCode": str(currency or "")})
    for row in connection.execute(
        "SELECT nationality,currency_code,base_rate,legal_max_rate "
        "FROM country_currencies ORDER BY nationality,currency_code"
    ):
        country, currency, base, legal = row
        if matches((country, currency, "기준금리", "법정최대이자", "법정 최고이자",
                    "법정최고이자율", "최대이자율", "최고이자", "금리", "이자율")):
            countries.append({"country": country, "currencyCode": currency,
                              "baseRate": _finite(base), "legalMaxRate": _finite(legal)})
    exchanges = []
    for row in connection.execute(
        "SELECT exchange_id,nationality,index_value,index_updated_epoch "
        "FROM market_exchanges WHERE UPPER(exchange_id) != 'CURRENCY' ORDER BY exchange_id"
    ):
        exchange, country, value, updated = row
        name = f"{exchange} 거래소 지수"
        if matches((name, country)):
            exchanges.append({"exchangeId": exchange, "name": name, "country": country,
                              "indexValue": _finite(value), "updatedEpoch": _finite(updated)})
    return {"countries": countries, "exchanges": exchanges, "products": products}


def search_indicator_chart(connection, kind, key):
    """Return historical points for a country-rate pair or exchange index."""
    kind, key = str(kind or ""), str(key or "").strip()
    series = []
    if kind == "exchange":
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_exchange_index_history'"
        ).fetchone()
        if not exists:
            raise ValueError("exchange_index_history_unavailable")
        rows = connection.execute(
            "SELECT recorded_epoch,index_value FROM market_exchange_index_history "
            "WHERE exchange_id=? ORDER BY recorded_epoch", (key,),
        ).fetchall()
        current = connection.execute(
            "SELECT index_value,index_updated_epoch FROM market_exchanges WHERE exchange_id=? "
            "AND UPPER(exchange_id)!='CURRENCY'", (key,),
        ).fetchone()
        points = [{"time": int(row[0]), "value": _finite(row[1])} for row in rows]
        if current and current[1] and (not points or points[-1]["value"] != _finite(current[0])):
            points.append({"time": int(current[1]), "value": _finite(current[0])})
        points.sort(key=lambda point: point["time"])
        if len(points) > 600:
            stride = math.ceil(len(points) / 600)
            points = points[::stride] + ([points[-1]] if points[-1] != points[::stride][-1] else [])
        if points:
            series.append({"name": "거래소 지수", "color": "#2563eb", "points": points})
        title = f"{key} 거래소 지수"
    elif kind == "country":
        country = connection.execute(
            "SELECT 1 FROM country_currencies WHERE nationality=?", (key,),
        ).fetchone()
        if not country:
            raise ValueError("country_not_found")
        labels = {"base_rate": ("기준금리", "#2563eb"),
                  "legal_max_rate": ("법정최대이자", "#dc2626")}
        for rate_key, (name, color) in labels.items():
            rows = connection.execute(
                "SELECT effective_epoch,annual_percent FROM country_rate_history "
                "WHERE nationality=? AND rate_key=? ORDER BY effective_epoch", (key, rate_key),
            ).fetchall()
            points = [{"time": int(float(row[0])), "value": _finite(row[1])} for row in rows]
            if points:
                series.append({"name": name, "color": color, "points": points})
        title = f"{key} 금리"
    else:
        raise ValueError("unsupported_indicator_chart")
    return {"title": title, "series": series}
