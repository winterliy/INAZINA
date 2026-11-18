"""Algorithmic liquidity: independent 법인 whose accounts quote the order book under their own names.

Each provider company has a temperament (low / medium / high). Every cycle it re-centres limit quotes on the
market price with an Avellaneda-Stoikov style rule: the reservation price leans against its inventory (it sells
cheaper when it holds a lot, buys dearer when it holds none), the spread widens for a calmer temperament, and a
few price levels per side are laid out with growing size. The companies trade as ordinary accounts through the
normal order book, so every fill, tax and ledger row is the regular one. A provider stops trading when its
equity falls below half of the capital it started with.
"""
import hashlib
import math
import time

from functions.money import base_currency_code
from managers.company_account_manager import CompanyAccounts

# spread_bps: full quoted spread; skew_bps: how far the quote leans, either way, between an empty and a full inventory
# (kept under half the spread so a quote never crosses the market price); levels per side;
# size_pct: share of free cash (or held shares) one level quotes; assets: how many stocks it covers;
# cycle_seconds: how often it requotes; inventory_cap: stock value it will hold as a share of its equity.
PROFILES = {
    "low": {"spread_bps": 120, "skew_bps": 30, "levels": 1, "size_pct": 0.01, "assets": 3, "cycle_seconds": 180, "inventory_cap": 0.15},
    "medium": {"spread_bps": 60, "skew_bps": 20, "levels": 2, "size_pct": 0.01, "assets": 6, "cycle_seconds": 90, "inventory_cap": 0.3},
    "high": {"spread_bps": 25, "skew_bps": 5, "levels": 3, "size_pct": 0.01, "assets": 8, "cycle_seconds": 45, "inventory_cap": 0.5},
}
LEVEL_STEP_BPS = 15
HALT_EQUITY_RATIO = 0.5
RESERVE_FLOOR = 0.5            # free cash must stay above this share of equity for new bids
INITIAL_CAPITAL = 5_000_000_000
REQUOTE_TOLERANCE = 0.25        # keep a resting order while it is within this share of the half-spread
STALE_FRACTION = 0.25           # a quote this close to (or through) the price, as a share of the half-spread, is withdrawn

# Twelve independent companies: name, code, industry, temperament (four of each).
FIRMS = [
    ("한빛무역", "HNBT", "무역", "low"), ("도람산업", "DRMS", "제조", "low"),
    ("그랜트상사", "GRNT", "유통", "low"), ("새벽물산", "SBMS", "식품", "low"),
    ("별누리상사", "BNRS", "유통", "medium"), ("누리개발", "NRDB", "건설", "medium"),
    ("미래유통", "MRYT", "유통", "medium"), ("타온에너지", "TAON", "에너지", "medium"),
    ("솔라인더스트리", "SLIN", "제조", "high"), ("가람산업", "GRMS", "제조", "high"),
    ("편안상사", "PYAN", "무역", "high"), ("오대양물산", "ODYM", "무역", "high"),
]


def quote_prices(price, held_value_ratio, profile):
    """Bid/ask ladders around the inventory-skewed reservation price (integers, best price first)."""
    cfg = PROFILES[profile]
    ratio = max(0.0, min(1.0, held_value_ratio))
    reservation = price * (1 - (ratio - 0.5) * 2 * cfg["skew_bps"] / 10000)
    bids, asks = [], []
    for level in range(cfg["levels"]):
        half = (cfg["spread_bps"] / 2 + level * LEVEL_STEP_BPS) / 10000
        bids.append(max(1, math.floor(reservation * (1 - half))))
        asks.append(max(1, math.ceil(reservation * (1 + half))))
    return bids, asks


class LiquidityManager:
    def __init__(self, db, order_book):
        self.db = db
        self.book = order_book

    # ------------------------------------------------------------------ roster
    def providers(self):
        """Every provider with its cash, inventory value, equity and result against starting capital."""
        with self.db.readonly() as conn:
            rows = conn.execute(
                "SELECT p.*, c.name, a.user_id FROM liquidity_providers p "
                "JOIN company c ON c.company_code=p.company_code "
                "LEFT JOIN company_accounts a ON a.company_code=p.company_code ORDER BY p.company_code").fetchall()
            result = []
            for row in rows:
                cash, holdings = self._standing(conn, row["user_id"]) if row["user_id"] else (0, 0)
                equity = cash + holdings
                result.append({
                    "companyCode": row["company_code"], "name": row["name"], "profile": row["profile"],
                    "enabled": bool(row["enabled"]), "halted": bool(row["halted"]),
                    "initialCapital": int(row["initial_capital_minor_units"]), "cash": cash, "holdingsValue": holdings,
                    "equity": equity, "pnl": equity - int(row["initial_capital_minor_units"]),
                    "lastRunEpoch": int(row["last_run_epoch"]),
                    "openOrders": conn.execute(
                        "SELECT COUNT(*) FROM market_orders WHERE user_id=? AND status IN ('open','partial')",
                        (row["user_id"],)).fetchone()[0] if row["user_id"] else 0})
            return result

    def detail(self, company_code):
        """One provider's stocks: price, shares held, resting buy/sell quotes, and why it is (not) quoting."""
        with self.db.readonly() as conn:
            row = conn.execute(
                "SELECT p.*, a.user_id FROM liquidity_providers p JOIN company_accounts a ON a.company_code=p.company_code "
                "WHERE p.company_code=?", (str(company_code),)).fetchone()
            if not row:
                raise ValueError("유동성 공급 회사가 아닙니다.")
            user_id, now = row["user_id"], int(time.time())
            cfg = PROFILES[row["profile"]]
            assets = {}
            for order in self._open_orders(conn, user_id):
                assets.setdefault(order["asset_code"], {"buy": [], "sell": []})[order["side"]].append(int(order["price"]))
            for code, _price in self._targets(conn, row["company_code"], cfg["assets"]):
                assets.setdefault(code, {"buy": [], "sell": []})
            for code, held in conn.execute("SELECT asset_code, SUM(quantity) FROM user_assets WHERE user_id=? GROUP BY asset_code", (user_id,)):
                assets.setdefault(code, {"buy": [], "sell": []})
            result = []
            for code, quotes in sorted(assets.items()):
                company = conn.execute("SELECT name, share_price FROM company WHERE company_code=?", (code,)).fetchone()
                held = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM user_assets WHERE user_id=? AND asset_code=?", (user_id, code)).fetchone()[0]
                evented = self._event_running(conn, code, now)
                quoting = bool(quotes["buy"] or quotes["sell"])
                state = ("halted" if row["halted"] else "off" if not row["enabled"] else "event" if evented
                         else "quoting" if quoting else "waiting")
                result.append({"assetCode": code, "name": company["name"] if company else code,
                               "price": int(company["share_price"]) if company and company["share_price"] else 0,
                               "held": int(float(held)), "buys": sorted(quotes["buy"], reverse=True),
                               "sells": sorted(quotes["sell"]), "state": state})
            return result

    @staticmethod
    def _standing(conn, user_id):
        cash = int(conn.execute("SELECT cash FROM users WHERE user_id=?", (user_id,)).fetchone()[0] or 0)
        # Cash set aside for resting buy quotes still belongs to the company.
        cash += int(conn.execute(
            "SELECT COALESCE(SUM(reserved_cash_minor_units),0) FROM market_orders WHERE user_id=? AND status IN ('open','partial') "
            "AND order_kind='asset'", (user_id,)).fetchone()[0] or 0)
        holdings = 0
        for row in conn.execute(
                "SELECT h.asset_code, SUM(h.quantity) q, COALESCE(c.share_price, 0) price FROM user_assets h "
                "JOIN company c ON c.company_code=h.asset_code WHERE h.user_id=? GROUP BY h.asset_code", (user_id,)):
            holdings += int(float(row["q"]) * int(row["price"] or 0))
        return cash, holdings

    def add_provider(self, company_code, profile, *, enabled=True):
        """Put an existing company into the liquidity set (its capital account is opened if missing)."""
        if profile not in PROFILES:
            raise ValueError("유동성 성향은 low, medium, high 중에서 선택해주세요.")
        now = int(time.time())
        with self.db.atomic(("users", "accounts", "assets", "product", "company", "tax")) as conn:
            company = conn.execute("SELECT * FROM company WHERE company_code=? AND status='active'", (str(company_code),)).fetchone()
            if not company:
                raise ValueError("활성 상태의 회사만 유동성 공급 회사로 넣을 수 있습니다.")
            user_id = CompanyAccounts.account_id(conn, company["company_code"])
            cash, holdings = self._standing(conn, user_id)
            conn.execute(
                "INSERT INTO liquidity_providers(company_code,profile,enabled,initial_capital_minor_units,created_epoch,updated_epoch) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(company_code) DO UPDATE SET profile=excluded.profile, "
                "enabled=excluded.enabled, halted=0, updated_epoch=excluded.updated_epoch",
                (company["company_code"], profile, int(bool(enabled)), cash + holdings, now, now))
        return True

    def set_profile(self, company_code, profile):
        if profile not in PROFILES:
            raise ValueError("유동성 성향은 low, medium, high 중에서 선택해주세요.")
        with self.db.atomic(("company",)) as conn:
            if not conn.execute("UPDATE liquidity_providers SET profile=?, updated_epoch=? WHERE company_code=?",
                                (profile, int(time.time()), str(company_code))).rowcount:
                raise ValueError("유동성 공급 회사가 아닙니다.")

    def set_enabled(self, company_code, enabled):
        with self.db.atomic(("company",)) as conn:
            if not conn.execute("UPDATE liquidity_providers SET enabled=?, halted=CASE WHEN ?=1 THEN 0 ELSE halted END, "
                                "updated_epoch=? WHERE company_code=?",
                                (int(bool(enabled)), int(bool(enabled)), int(time.time()), str(company_code))).rowcount:
                raise ValueError("유동성 공급 회사가 아닙니다.")
        if not enabled:
            self.cancel_all(company_code)

    def remove_provider(self, company_code):
        """Take a company out of the set: its quotes are withdrawn, the company itself stays."""
        self.cancel_all(company_code)
        with self.db.atomic(("company",)) as conn:
            conn.execute("DELETE FROM liquidity_providers WHERE company_code=?", (str(company_code),))

    # ------------------------------------------------------------------ orders
    def _open_orders(self, conn, user_id, code=None):
        query = "SELECT order_id, asset_code, side, limit_price_minor_units price FROM market_orders " \
                "WHERE user_id=? AND status IN ('open','partial') AND order_kind='asset'"
        return conn.execute(query + (" AND asset_code=?" if code else ""), (user_id, code) if code else (user_id,)).fetchall()

    def cancel_all(self, company_code):
        with self.db.readonly() as conn:
            user = conn.execute("SELECT user_id FROM company_accounts WHERE company_code=?", (str(company_code),)).fetchone()
            orders = self._open_orders(conn, user[0]) if user else []
        for order in orders:
            self.book.cancel_order(user[0], int(order["order_id"]))

    def _targets(self, conn, firm_code, count):
        """The stocks a firm covers: a stable pseudo-random pick among tradable NWX stocks."""
        base = base_currency_code(conn)
        rows = conn.execute(
            "SELECT company_code, share_price FROM company WHERE status='active' AND trading_status='active' "
            "AND exchange_id IS NOT NULL AND share_price>0 AND COALESCE(quote_currency,?)=? AND Fractal=0 "
            "AND company_code<>?", (base, base, firm_code)).fetchall()
        rows = [row for row in rows if not conn.execute("SELECT 1 FROM liquidity_providers WHERE company_code=?", (row["company_code"],)).fetchone()]
        rows.sort(key=lambda row: hashlib.sha256(f"{firm_code}:{row['company_code']}".encode()).hexdigest())
        return [(row["company_code"], int(row["share_price"])) for row in rows[:count]]

    @staticmethod
    def _event_running(conn, asset, now):
        return bool(conn.execute(
            "SELECT 1 FROM market_event_targets WHERE asset_code=? AND state='active' AND start_epoch<=? LIMIT 1",
            (asset, now)).fetchone())      # 'active' lasts until the price engine has finished the event, even past its end time

    def withdraw_stale(self, now):
        """Pull quotes that a price jump has made unsafe, every tick (not only when a firm is due to requote):
        every quote on a stock while a price event runs on it, and any quote the price has moved onto or past."""
        pulled = {}
        with self.db.readonly() as conn:
            firms = [dict(row) for row in conn.execute(
                "SELECT p.company_code, p.profile, a.user_id FROM liquidity_providers p "
                "JOIN company_accounts a ON a.company_code=p.company_code WHERE p.enabled=1")]
            doomed = []
            for firm in firms:
                half = PROFILES[firm["profile"]]["spread_bps"] / 2 / 10000
                evented = {}
                for order in self._open_orders(conn, firm["user_id"]):
                    asset = order["asset_code"]
                    if asset not in evented:
                        evented[asset] = self._event_running(conn, asset, now)
                    price = conn.execute("SELECT share_price FROM company WHERE company_code=?", (asset,)).fetchone()
                    price = int(price[0]) if price and price[0] else 0
                    limit = int(order["price"])
                    through = price > 0 and ((order["side"] == "sell" and limit < price * (1 + STALE_FRACTION * half))
                                             or (order["side"] == "buy" and limit > price * (1 - STALE_FRACTION * half)))
                    if evented[asset] or through:
                        doomed.append((firm["company_code"], firm["user_id"], int(order["order_id"])))
        for code, user_id, order_id in doomed:
            self.book.cancel_order(user_id, order_id)
            pulled[code] = pulled.get(code, 0) + 1
        return pulled

    def run_cycle(self, now=None):
        """Requote every due provider. Returns {company_code: orders placed} for the cycle."""
        now = int(now if now is not None else time.time())
        placed = {}
        self.withdraw_stale(now)
        with self.db.readonly() as conn:
            due = [dict(row) for row in conn.execute(
                "SELECT * FROM liquidity_providers WHERE enabled=1 AND halted=0 ORDER BY company_code")]
        for provider in due:
            cfg = PROFILES[provider["profile"]]
            if now - int(provider["last_run_epoch"]) < cfg["cycle_seconds"]:
                continue
            try:
                placed[provider["company_code"]] = self._requote(provider, cfg, now)
            except Exception as exc:  # one firm's failure never stops the others
                print(f"[Liquidity] {provider['company_code']}: {type(exc).__name__}: {exc}")
        return placed

    def _requote(self, provider, cfg, now):
        code = provider["company_code"]
        with self.db.readonly() as conn:
            user = conn.execute("SELECT user_id FROM company_accounts WHERE company_code=?", (code,)).fetchone()
            if not user:
                return 0
            user_id = str(user[0])
            cash, holdings = self._standing(conn, user_id)
            targets = self._targets(conn, code, cfg["assets"])
        equity = cash + holdings
        if equity < HALT_EQUITY_RATIO * int(provider["initial_capital_minor_units"]):
            self.cancel_all(code)
            with self.db.atomic(("company",)) as conn:
                conn.execute("UPDATE liquidity_providers SET halted=1, updated_epoch=? WHERE company_code=?", (now, code))
            return 0
        count = 0
        for asset, price in targets:
            with self.db.readonly() as conn:
                if self._event_running(conn, asset, now):
                    continue                  # no quotes on a stock while a price event is moving it
                held = int(float(conn.execute(
                    "SELECT COALESCE(SUM(quantity),0) FROM user_assets WHERE user_id=? AND asset_code=?", (user_id, asset)).fetchone()[0]))
                resting = [dict(row) for row in self._open_orders(conn, user_id, asset)]
                cash = int(conn.execute("SELECT cash FROM users WHERE user_id=?", (user_id,)).fetchone()[0] or 0)
            held_ratio = (held * price) / max(1, equity * cfg["inventory_cap"])
            bids, asks = quote_prices(price, held_ratio, provider["profile"])
            half = cfg["spread_bps"] / 2 / 10000
            wanted = []
            for level, bid in enumerate(bids):
                size = int(cash * cfg["size_pct"] * (1 + level) // max(1, bid))
                if held_ratio < 1 and size >= 1 and cash > RESERVE_FLOOR * equity:   # never tie up more than half the capital in bids
                    wanted.append(("buy", bid, size))
            sellable = held
            for level, ask in enumerate(asks):
                size = min(sellable, max(1, int(held * cfg["size_pct"] * 10 * (1 + level)))) if sellable else 0
                if size >= 1:
                    wanted.append(("sell", ask, size))
                    sellable -= size
            keep = set()
            for order in resting:
                match = next((w for w in wanted if w[0] == order["side"] and w not in keep
                              and abs(w[1] - int(order["price"])) <= max(1, REQUOTE_TOLERANCE * half * price)), None)
                if match:
                    keep.add(match)
                else:
                    self.book.cancel_order(user_id, int(order["order_id"]))
            for side, limit, size in wanted:
                if (side, limit, size) in keep:
                    continue
                ok, _message = self.book.place_limit_order(user_id, asset, side, size, limit)
                count += 1 if ok else 0
        with self.db.atomic(("company",)) as conn:
            conn.execute("UPDATE liquidity_providers SET last_run_epoch=? WHERE company_code=?", (now, code))
        return count

    # ------------------------------------------------------------------ provisioning
    def provision_firms(self, treasury_user_id=None, capital=INITIAL_CAPITAL):
        """Create the twelve independent 법인 (unlisted, not stock companies, nationality 낙원), fund each
        from the 낙원 treasury and register it as a provider. Safe to run again: existing firms are kept."""
        from managers.currency_exchange_manager import CurrencyExchangeManager
        created = []
        for name, code, industry, profile in FIRMS:
            with self.db.atomic(("users", "accounts", "assets", "product", "company", "tax")) as conn:
                if conn.execute("SELECT 1 FROM company WHERE company_code=?", (code,)).fetchone():
                    continue
                from database import DatabaseManager
                if DatabaseManager.code_locations(conn, code):
                    raise ValueError(f"코드 {code}가 이미 다른 상품에 쓰이고 있습니다.")
                treasury = treasury_user_id or self._treasury(conn)
                conn.execute(
                    "INSERT INTO company(company_code,name,industry,capital,total_shares,share_price,max_shareholder_id,"
                    "exchange_id,nationality,legal_form) VALUES (?,?,?,?,?,?,?,NULL,'낙원','limited_company')",
                    (code, name, industry, capital, 1_000_000, max(1, capital // 1_000_000), treasury))
                account = CompanyAccounts.account_id(conn, code)
            CurrencyExchangeManager(self.db).transfer(treasury, account, capital)
            self.add_provider(code, profile)
            created.append(code)
        return created

    @staticmethod
    def _treasury(conn):
        from managers.currency_manager import CurrencyManager
        return CurrencyManager.treasury_id(conn, "낙원")
