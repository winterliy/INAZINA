import json
import sqlite3
import unittest

from web_api.search_indicators import search_indicators


class SearchIndicatorsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript("""
            CREATE TABLE country_currencies(nationality,currency_code,base_rate,legal_max_rate);
            CREATE TABLE market_exchanges(exchange_id,nationality,index_value,index_updated_epoch);
            CREATE TABLE saving(product_code,name,issuer,interest_rate,quote_currency,status);
            CREATE TABLE liability(product_code,name,issuer,interest_rate,quote_currency,status);
            INSERT INTO country_currencies VALUES ('한국','KRW',-0.5,20),('미국','USD',4.25,30);
            INSERT INTO market_exchanges VALUES ('KSE','한국',1234.56,100),('NYSE','미국',2000,200);
        """)
        self.addCleanup(self.conn.close)

    def test_rates_and_indices_are_separate_read_only_records(self):
        before = self.conn.total_changes
        result = search_indicators(self.conn)
        self.assertEqual(result['countries'][1]['baseRate'], -0.5)
        self.assertEqual(result['countries'][1]['legalMaxRate'], 20)
        self.assertEqual(result['exchanges'][0]['indexValue'], 1234.56)
        self.assertNotIn('code', result['exchanges'][0])
        self.assertEqual(self.conn.total_changes, before)
        json.dumps(result, allow_nan=False)

    def test_country_currency_exchange_and_literal_search(self):
        self.assertEqual(len(search_indicators(self.conn, '한국')['exchanges']), 1)
        self.assertEqual(search_indicators(self.conn, ' usd ')['countries'][0]['country'], '미국')
        self.assertEqual(search_indicators(self.conn, 'kse')['exchanges'][0]['exchangeId'], 'KSE')
        self.assertEqual(len(search_indicators(self.conn, '기준금리')['countries']), 2)
        self.assertEqual(search_indicators(self.conn, '%'), {'countries': [], 'exchanges': [], 'products': []})

    def test_non_finite_and_missing_values_serialize_as_null(self):
        self.conn.execute("UPDATE country_currencies SET base_rate=?,legal_max_rate=?", (float('inf'), None))
        self.conn.execute("UPDATE market_exchanges SET index_value=?,index_updated_epoch=?", ('NaN', float('-inf')))
        result = search_indicators(self.conn)
        self.assertIsNone(result['countries'][0]['baseRate'])
        self.assertIsNone(result['countries'][0]['legalMaxRate'])
        self.assertIsNone(result['exchanges'][0]['indexValue'])
        self.assertIsNone(result['exchanges'][0]['updatedEpoch'])
        json.dumps(result, allow_nan=False)
