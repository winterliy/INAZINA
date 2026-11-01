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
    countries = []
    for row in connection.execute(
        "SELECT nationality,currency_code,base_rate,legal_max_rate "
        "FROM country_currencies ORDER BY nationality,currency_code"
    ):
        country, currency, base, legal = row
        if not term or any(term in str(value).casefold() for value in
                           (country, currency, "기준금리 법정 최고이자율 금리")):
            countries.append({"country": country, "currencyCode": currency,
                              "baseRate": _finite(base), "legalMaxRate": _finite(legal)})
    exchanges = []
    for row in connection.execute(
        "SELECT exchange_id,nationality,index_value,index_updated_epoch "
        "FROM market_exchanges ORDER BY exchange_id"
    ):
        exchange, country, value, updated = row
        name = f"{exchange} 거래소 지수"
        if not term or any(term in str(item).casefold() for item in (name, country)):
            exchanges.append({"exchangeId": exchange, "name": name, "country": country,
                              "indexValue": _finite(value), "updatedEpoch": _finite(updated)})
    return {"countries": countries, "exchanges": exchanges}
