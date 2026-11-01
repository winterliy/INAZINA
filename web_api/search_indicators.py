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
