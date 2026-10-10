import math
import random
import sqlite3
import time
import threading
from decimal import Decimal, ROUND_HALF_UP
from dataclasses import replace
from contextlib import contextmanager
from datetime import datetime, timezone
from database import DatabaseManager
from database.unified import UnifiedConnection
from managers.movement_history_manager import MovementHistoryManager
from managers.market_session_manager import MarketSessionManager
from managers.auto_target_engine import AutoTargetConfig, TargetCycle, cycle_bounds, sample_target, target_pull
from managers.stochastic_price_engine import (
    GarchConfig, MAX_PRICE, TRADING_MINUTES_PER_YEAR,
    next_variance, simulate_step, standardized_shock,
)


EXCHANGE_INDEX_PREFIX = "__exchange_index__:"
PROFILE_COLUMNS = (
    "liquidity", "trend_sensitivity", "sector_sensitivity",
    "jump_intensity", "mean_reversion",
)


class _MarketHalted(Exception):
    """A concurrent administrator pause rolls back the current price batch."""


class MarketManager:
    """시장 가격 변동, 자산 코드 조회, 거래소별 종목 목록을 관리합니다."""

    DEFAULT_VOLATILITY = 0.15
    MAX_VOLATILITY = 1.0
    # Targets are intentionally process-local.  Quotes and GARCH state are
    # persisted separately; restarting the bot keeps the last price but starts
    # a fresh ordinary target cycle and clears unfinished administrator events.
    _runtime_lock = threading.RLock()
    _runtime_auto_targets = {}
    _runtime_event_targets = {}

    def __init__(self, db: DatabaseManager):
        self.db = db
        self._runtime_key = str(db.assets.db_path)
        self.movement_history = MovementHistoryManager(db)
        self._tick = threading.local()

    def _runtime_auto(self):
        with self._runtime_lock:
            return self._runtime_auto_targets.setdefault(self._runtime_key, {})

    def _runtime_events(self):
        with self._runtime_lock:
            return self._runtime_event_targets.setdefault(self._runtime_key, {})

    def register_runtime_events(self, plans, *, event_type, volatility_percent, now, duration_seconds):
        """Install active admin-event paths in RAM only."""
        with self._runtime_lock:
            events = self._runtime_events()
            for code, (start_price, target_price) in plans.items():
                events[str(code)] = dict(asset_code=str(code), event_type=event_type,
                    volatility_percent=float(volatility_percent), start_price=int(start_price),
                    target_price=int(target_price), start_epoch=int(now), end_epoch=int(now + duration_seconds),
                    noise_fraction=0.0, last_tick_epoch=int(now), last_price=int(start_price),
                    jump_applied=False)

    def clear_runtime_targets(self, code=None):
        with self._runtime_lock:
            if code is None:
                self._runtime_events().clear()
                self._runtime_auto().clear()
            else:
                self._runtime_events().pop(str(code), None)
                self._runtime_auto().pop(str(code), None)

    @staticmethod
    def is_stock_type(asset_type):
        return str(asset_type or "").strip().casefold() in {"stock", "주식"}

    @staticmethod
    def normalize_market_price(price):
        if isinstance(price, int):
            return max(0, price)
        numeric_price = float(price or 0)
        if numeric_price <= 0:
            return 0
        whole = math.floor(numeric_price)
        return max(1, whole + int(numeric_price - whole >= .5))

    @classmethod
    def format_price(cls, price, asset_type):
        return f"{cls.normalize_market_price(price):,}"

    @staticmethod
    def type_label(asset_type):
        return {"stock_future": "주식 선물", "corporate_bond": "회사채", "mutual_fund": "일반 펀드",
                "index_fund": "인덱스 펀드", "commodity_spot": "현물 원자재", "option": "옵션", "els":"ELS"}.get(asset_type, asset_type)

    @contextmanager
    def _short_market_transaction(self, alias, *, now=None):
        """Use an isolated connection and a short lock timeout for background ticks."""
        aliases = (alias,) if isinstance(alias, str) else tuple(alias)
        active = getattr(self._tick, 'conn', None)
        if active is not None:
            yield active
            return
        conn = sqlite3.connect(self.db.assets.db_path, timeout=0.05, isolation_level=None, factory=UnifiedConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 50")
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Expired sidecar/circuit deadlines are processed for every
            # exchange.  Individual assets are filtered below, so pausing EX1
            # does not freeze EX2's price engine.
            MarketSessionManager.process_all_conn(conn, now)
            yield conn
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _to_market_asset(row, source):
        if row is None:
            return None
        if source == "company" and "total_shares" in row.keys() and "capital" in row.keys():
            return {
                "code": row["company_code"], "name": row["name"],
                "exchange_id": row["exchange_id"],
                "price": MarketManager.normalize_market_price(row["share_price"]),
                "type": "stock", "status": row["status"],
                "is_fixed": int(row["is_fixed"] or 0), "source": source,
                "total_supply": int(row["total_shares"] or 0),
                "quote_currency":row['quote_currency'],
            }
        if source == "investment":
            status = row["status"]
            if status == "active" and row['product_type']!='els' and MarketManager._investment_matured(row["created_at"], row["maturity_days"]):
                status = "matured"
            return {
                "code": row["product_code"], "name": row["name"],
                "exchange_id": row["exchange_id"],
                "price": MarketManager.normalize_market_price(row["unit_price"]),
                "type": row["product_type"], "status": status,
                "is_fixed": int(row["is_fixed"] or 0), "source": source,
                "total_supply": int(row["total_supply"] or 0),
                "quote_currency":row['quote_currency'],
                "maturity_days": int(row["maturity_days"] or 0),
                "underlying_exchange_id": row["underlying_exchange_id"],
                "leverage_multiplier": float(row["leverage_multiplier"] or 1),
                "rebalance_cycle_seconds": int(row["rebalance_cycle_seconds"] or 86400),
                "underlying_code": row["underlying_code"], "issuer_code": row["issuer_code"],
                "face_value": int(row["face_value"] or 0), "annual_rate": float(row["annual_rate"] or 0),
                "contract_size": int(row["contract_size"] or 1),
                "initial_margin": float(row["initial_margin"]), "maintenance_margin": float(row["maintenance_margin"]),
                "maturity_epoch": row["maturity_epoch"],
            }
        if source == "treasury_bond":
            return {
                "code": row["product_code"], "name": row["name"],
                "exchange_id": row["exchange_id"],
                "price": MarketManager.normalize_market_price(row["market_price"]),
                "type": "국채", "status": row["status"],
                "is_fixed": 1, "source": source,
                "total_supply": int(row["total_supply"] or 0)
                // max(1, int(row["face_value"] or 1)),
                "face_value": int(row["face_value"] or 1),
                "annual_rate": float(row["annual_rate"] or 0),
                "maturity_days": int(row["maturity_days"] or 0),
                "issued_amount": int(row["sold_amount"] or 0),
            }
        asset_type = row["type"]
        price = MarketManager.normalize_market_price(row["price"])
        return {
            "code": row["code"], "name": row["name"],
            "exchange_id": row["exchange_id"], "price": price,
            "type": asset_type, "status": row["status"],
            "is_fixed": int(row["is_fixed"] or 0), "source": source,
            "total_supply": int(row["total_supply"]) if row["total_supply"] is not None else None,
            "quote_currency":row['quote_currency'],
        }

    @staticmethod
    def _investment_matured(created_at, maturity_days):
        try:
            days = float(maturity_days or 0)
            if days <= 0 or not created_at:
                return False
            created = datetime.strptime(str(created_at), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            ).timestamp()
            return datetime.now(timezone.utc).timestamp() >= created + days * 86400
        except (TypeError, ValueError, OverflowError):
            return False

    def _matching_rows(self, code):
        return [
            (self.db.assets.get_asset_info(code), "market_assets"),
            (self.db.companies.get_company(code), "company"),
            (self.db.products.get_investment(code), "investment"),
            (self.db.products.get_treasury_bond(code), "treasury_bond"),
        ]

    def get_asset_info(self, code):
        asset = self._get_asset_info(code)
        if asset:
            quote = self.db.assets.execute_one("SELECT price FROM market_quote_overrides WHERE asset_code=?", (str(code),))
            if quote:
                asset['price'] = int(quote['price'])
        return asset

    def _get_asset_info(self, code):
        matches = [self._to_market_asset(row, source) for row, source in self._matching_rows(code) if row]
        locations = []
        checks = (
            (self.db.assets, "SELECT 1 FROM market_assets WHERE code = ?", "시장 자산"),
            (self.db.companies, "SELECT 1 FROM company WHERE company_code = ?", "회사 주식"),
            (self.db.products, "SELECT 1 FROM saving WHERE product_code = ?", "저축 상품"),
            (self.db.products, "SELECT 1 FROM liability WHERE product_code = ?", "대출 상품"),
            (self.db.products, "SELECT 1 FROM investment WHERE product_code = ?", "투자 상품"),
            (self.db.products, "SELECT 1 FROM treasury_bond_products WHERE product_code = ?", "국채 상품"),
            (self.db.assets, "SELECT 1 FROM option_contracts WHERE contract_code = ?", "옵션 계약"),
            (self.db.accounts, "SELECT 1 FROM accounts WHERE account_number = ?", "계좌 번호"),
            (self.db.products, "SELECT 1 FROM saving WHERE product_code = ? AND product_kind='currency_saving'", "외환통장 상품"),
            (self.db.accounts, "SELECT 1 FROM accounts WHERE account_number = ? AND account_type='currency_saving'", "외환계좌 번호"),
        )
        for database, query, label in checks:
            if database.execute_one(query, (code,)):
                locations.append(label)
        if len(locations) > 1:
            raise ValueError(f"코드 `{code}` 중복 등록({', '.join(locations)}) — SQL Browser에서 정리해주세요.")
        option_row = self.db.assets.execute_one(
            "SELECT * FROM option_contracts WHERE contract_code = ?", (str(code),)
        )
        if option_row:
            contract = dict(option_row)
            underlying = self.get_asset_info(contract["underlying_code"])
            if not underlying:
                return None
            from managers.option_manager import OptionManager

            now = int(time.time())
            years = max(0, int(contract["expiration_epoch"]) - now) / (365.0 * 86400)
            reference_price = OptionManager.theoretical_price(
                int(underlying["price"] or 0), int(contract["strike_price"]), years,
                float(contract["implied_volatility"]), contract["option_type"],
                float(self.db.system.get_setting("base_rate", "3")) / 100,
            )
            side_name = "콜" if contract["option_type"] == "call" else "풋"
            return {
                **contract,
                "code": str(code),
                "name": f"{underlying['name']} {side_name} {int(contract['strike_price']):,}",
                "type": "option",
                "price": reference_price,
                "status": "matured" if contract["status"] == "active" and int(contract["expiration_epoch"]) <= now else contract["status"],
                "is_fixed": 1,
                "exchange_id": underlying.get("exchange_id"),
                "total_supply": int(contract["issued_contracts"]),
            }
        return matches[0] if matches else None

    def _get_sql_volatility(self):
        raw_value = self.db.system.get_setting(
            "market_volatility", str(self.DEFAULT_VOLATILITY)
        )
        try:
            volatility = float(raw_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "system.db settings.market_volatility는 0~1 사이 숫자여야 합니다."
            ) from exc
        if not math.isfinite(volatility) or not 0 <= volatility <= self.MAX_VOLATILITY:
            raise ValueError(
                "system.db settings.market_volatility는 0~1 사이 숫자여야 합니다."
            )
        return volatility

    @staticmethod
    def _normalize_market_condition(raw_value):
        condition_aliases = {
            "bull": "bull", "상승장": "bull",
            "bear": "bear", "하락장": "bear",
            "flat": "flat", "평장": "flat",
        }
        condition = condition_aliases.get(str(raw_value or "").strip().casefold())
        if condition is None:
            raise ValueError(
                "system.db settings.market_condition은 bull/bear/flat "
                "(상승장/하락장/평장) 중 하나여야 합니다."
            )
        return condition

    def _get_sql_market_condition(self):
        raw_value = self.db.system.get_setting("market_condition", "flat")
        return self._normalize_market_condition(raw_value)

    @staticmethod
    def _finite_profile_value(row, key, default, low=0.0, high=10.0):
        try:
            value = row[key] if key in row.keys() else None
        except (AttributeError, KeyError, IndexError):
            value = None
        if value is None:
            return default
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return number if math.isfinite(number) and low <= number <= high else default

    @classmethod
    def _price_profile(cls, row, source):
        asset_type = str((row["product_type"] if "product_type" in row.keys() else row["type"] if "type" in row.keys() else "stock") or "stock")
        type_key = asset_type.strip().casefold()
        defaults = {
            "stock": dict(liquidity=1.0, trend_sensitivity=1.0, sector_sensitivity=0.0, jump_intensity=0.0, mean_reversion=1.0),
            "주식": dict(liquidity=1.0, trend_sensitivity=1.0, sector_sensitivity=0.0, jump_intensity=0.0, mean_reversion=1.0),
            "commodity_spot": dict(liquidity=1.0, trend_sensitivity=1.0, sector_sensitivity=0.0, jump_intensity=0.0, mean_reversion=1.0),
            "forex": dict(liquidity=1.0, trend_sensitivity=1.0, sector_sensitivity=0.0, jump_intensity=0.0, mean_reversion=1.0),
            "etf": dict(liquidity=1.0, trend_sensitivity=1.0, sector_sensitivity=0.0, jump_intensity=0.0, mean_reversion=1.0),
        }
        base = defaults.get(type_key, defaults["stock"]).copy()
        profile = {key: cls._finite_profile_value(row, key, base[key]) for key in PROFILE_COLUMNS}
        profile["liquidity"] = max(0.05, profile["liquidity"])
        return profile

    def _sector_shock(self, key, epoch, config):
        if not key:
            return 0.0
        batch = getattr(self._tick, 'batch', None)
        if batch is None:
            return standardized_shock(random, config.student_df)
        shocks = batch.setdefault('sector_shocks', {})
        if (key, epoch) not in shocks:
            shocks[(key, epoch)] = standardized_shock(random, config.student_df)
        return shocks[(key, epoch)]

    def _event_target_price(self, conn, code, price, now, legacy_events, *, sample_noise, context=None, batch=None):
        """Return an in-memory event bridge; events deliberately do not survive restart."""
        with self._runtime_lock:
            target = self._runtime_events().get(str(code))
            if target is not None:
                target = dict(target)
        if target is None:
            return None
        duration = max(1, int(target["end_epoch"]) - int(target["start_epoch"]))
        elapsed = max(0, min(duration, int(now) - int(target["start_epoch"])))
        baseline = (target["start_price"] * duration + (target["target_price"] - target["start_price"]) * elapsed) / duration
        previous = int(target["last_price"])
        delta = max(0, int(now) - int(target["last_tick_epoch"]))
        noise = float(target["noise_fraction"])
        if delta and elapsed < duration:
            shock, jump = sample_noise(target)
            if jump:
                with self._runtime_lock:
                    live = self._runtime_events().get(str(code))
                    if live is not None:
                        live["jump_applied"] = True
            span = abs(target["target_price"] - target["start_price"])
            radius = min(baseline * .10, max(span * .12, baseline * .02)) * (target["volatility_percent"] / 100)
            taper = math.sin(math.pi * elapsed / duration)
            band = max(0.0, min(radius * taper, abs(baseline-target["start_price"]) * .25,
                                abs(target["target_price"]-baseline) * .25))
            noise = max(-band, min(band, (noise * target["start_price"] * math.exp(-delta / 3)
                                           + band * math.tanh(shock * .6 + jump) * (1-math.exp(-delta / 3)))) / target["start_price"])
        value = baseline + target["start_price"] * noise
        next_price = min(MAX_PRICE, max(1, int(Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))))
        direction = 1 if target["target_price"] >= target["start_price"] else -1
        if (next_price - previous) * direction < 0:
            mean_step = abs(target["target_price"] - target["start_price"]) * delta / duration
            next_price = previous - direction * min(abs(next_price - previous), max(1, int(math.ceil(mean_step * .5))))
        if context is not None:
            context.update(event_id=f"ram:{code}", advanced=True, latent_price=float(next_price),
                           volatility_percent=float(target["volatility_percent"]))
        with self._runtime_lock:
            live = self._runtime_events().get(str(code))
            if live is not None:
                live.update(noise_fraction=noise, last_tick_epoch=int(now), last_price=next_price)
                if elapsed >= duration:
                    self._runtime_events().pop(str(code), None)
        return next_price

        """Legacy persisted-event implementation retained below only for source history."""
        for event in legacy_events:
            if event["asset_code"] not in (None, "", code) or not event["severity"]:
                continue
            exists = conn.execute("SELECT 1 FROM system.market_event_targets WHERE event_id = ? AND asset_code = ?",
                                  (event["event_id"], code)).fetchone()
            if exists or int(event["ends_epoch"]) <= now:
                continue
            # Older events have already contributed drift. Continue only their
            # remaining effect, anchoring at the current price without a jump.
            remaining = (int(event["ends_epoch"]) - now) / (int(event["ends_epoch"]) - int(event["starts_epoch"]))
            sign = 1 if event["event_type"] == "good" else -1
            target = max(1, MarketManager.normalize_market_price(price * math.exp(
                sign * math.log1p(.2 * int(event["severity"]) / 100) * remaining)))
            newer = conn.execute("SELECT 1 FROM system.market_event_targets WHERE asset_code = ? AND event_id > ?",
                                 (code, event["event_id"])).fetchone()
            if not newer:
                conn.execute("UPDATE system.market_event_targets SET state = 'superseded' "
                             "WHERE asset_code = ? AND state = 'active' AND event_id < ?", (code, event["event_id"]))
            conn.execute("INSERT INTO system.market_event_targets "
                "(event_id, asset_code, start_price, target_price, start_epoch, end_epoch, state, last_tick_epoch, last_price) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event["event_id"], code, price, target, now, event["ends_epoch"],
                 "superseded" if newer else "active", now, price))
        target = batch['events'].get(code) if batch is not None and not legacy_events else conn.execute("SELECT t.*, e.event_type, COALESCE(e.volatility_percent, "
            "CASE WHEN e.target_mode = 'percent' THEN e.target_value ELSE e.severity END) AS volatility_percent "
            "FROM system.market_event_targets t JOIN system.market_events e ON e.event_id = t.event_id "
            "WHERE t.asset_code = ? AND t.start_epoch <= ? "
            "AND (t.state = 'active' OR (t.state = 'completed' AND t.completed_epoch = ?)) "
            "ORDER BY t.event_id DESC LIMIT 1", (code, now, now)).fetchone()
        if not target:
            return None
        if context is not None:
            context.update(event_id=target["event_id"], advanced=False, latent_price=float(price),
                           volatility_percent=float(target["volatility_percent"] or 0))
        duration = target["end_epoch"] - target["start_epoch"]
        elapsed = max(0, min(duration, now - target["start_epoch"]))
        numerator = target["start_price"] * duration + (target["target_price"] - target["start_price"]) * elapsed
        next_price = max(1, (2 * numerator + duration) // (2 * duration))
        previous_tick = target["last_tick_epoch"]
        noise = float(target["noise_fraction"])
        if previous_tick is not None and now <= previous_tick:
            return target["last_price"] if target["last_price"] is not None else price
        latent_price = float(next_price)
        if elapsed < duration:
            if previous_tick is None:
                # Keep the live price when upgrading an already-running event.
                noise = (price - numerator / duration) / target["start_price"]
                next_price = price
                latent_price = float(price)
            else:
                delta = max(0, now - previous_tick)
                old_offset = float(target["noise_fraction"]) * target["start_price"]
                noise = 0.0
                if delta and target["volatility_percent"]:
                    shock, jump = sample_noise(target)
                    baseline = numerator / duration
                    amplitude = float(target["volatility_percent"]) / 100
                    span = abs(target["target_price"] - target["start_price"])
                    # The straight average-rate path is the centre line. A
                    # bounded, correlated deviation wanders around that line;
                    # one shock can no longer traverse the full event corridor.
                    radius = min(baseline * .10, max(span * .12, baseline * .02)) * amplitude
                    taper = math.sin(math.pi * elapsed / duration)
                    # Keep the noise local to the average-rate path. In
                    # particular, an early negative shock must not send a good
                    # event all the way back to its starting price (and vice
                    # versa). At most 25% of completed/remaining progress may
                    # be used as deviation.
                    completed_distance = abs(baseline - target["start_price"])
                    remaining_distance = abs(target["target_price"] - baseline)
                    band = max(0.0, min(radius * taper,
                                        completed_distance * .25,
                                        remaining_distance * .25))
                    desired_offset = band * math.tanh(shock * .6 + jump)
                    decay = math.exp(-delta / 3)
                    offset = old_offset * decay + desired_offset * (1-decay)
                    offset = max(-band, min(band, offset))
                    noise = offset / target["start_price"]
                    if jump:
                        conn.execute("UPDATE system.market_event_targets SET jump_applied_epoch = ? "
                                     "WHERE event_id = ? AND asset_code = ?", (now, target["event_id"], code))
                if noise:
                    value = Decimal(numerator) / duration + Decimal(target["start_price"]) * Decimal(str(noise))
                    next_price = min(MAX_PRICE, max(1, int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))))
                    latent_price = min(float(MAX_PRICE), max(1.0, float(value)))
                else:
                    latent_price = numerator / duration
                # A pullback is allowed, but only for part of the mean progress
                # made since the previous tick. The next ticks then continue
                # toward the target instead of cycling start↔goal.
                previous_price = int(target["last_price"] if target["last_price"] is not None else price)
                direction = 1 if target["target_price"] >= target["start_price"] else -1
                if (next_price - previous_price) * direction < 0:
                    mean_step = abs(target["target_price"] - target["start_price"]) * delta / duration
                    reverse_limit = max(1, int(math.ceil(mean_step * .5)))
                    next_price = previous_price - direction * min(abs(next_price-previous_price),reverse_limit)
                    latent_price = float(next_price)
        else:
            noise = 0.0
        conn.execute("UPDATE system.market_event_targets SET noise_fraction = ?, last_tick_epoch = ?, last_price = ? "
                     "WHERE event_id = ? AND asset_code = ?", (noise, now, next_price, target["event_id"], code))
        if elapsed == duration and target["state"] == "active":
            conn.execute("UPDATE system.market_event_targets SET state = 'completed', completed_epoch = ? "
                         "WHERE event_id = ? AND asset_code = ?", (now, target["event_id"], code))
        if context is not None:
            context.update(advanced=True, latent_price=latent_price)
        return next_price

    @staticmethod
    def _event_jump_fraction(target):
        if target.get("jump_applied") or target.get("jump_applied_epoch") is not None:
            return 0.0
        sign = 1 if target["event_type"] == "good" else -1
        return sign * .1 * float(target["volatility_percent"]) / 100

    def _market_factor(self, conn, epoch, config):
        """Every independent price source reuses this tick's common shock."""
        batch = getattr(self._tick, 'batch', None)
        factor = batch['factor'] if batch is not None else self._factor_from_settings(conn)
        if factor is not None and int(factor["last_epoch"]) == epoch:
            return float(factor["current_factor"])
        shock = standardized_shock(random, config.student_df)
        if batch is not None:
            batch['factor'] = dict(last_epoch=epoch, current_factor=shock)
        conn.executemany("INSERT INTO system.settings(key,value) VALUES (?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (
                             ('market_factor_last_epoch', str(int(epoch))),
                             ('market_factor_current', repr(float(shock))),
                         ))
        return shock

    @staticmethod
    def _factor_from_settings(conn):
        values = {row['key']: row['value'] for row in conn.execute(
            "SELECT key,value FROM system.settings "
            "WHERE key IN ('market_factor_last_epoch','market_factor_current')"
        )}
        try:
            return dict(last_epoch=int(values.get('market_factor_last_epoch', 0)),
                        current_factor=float(values.get('market_factor_current', 0)))
        except (TypeError, ValueError):
            return dict(last_epoch=0, current_factor=0.0)

    def _save_simulation_state(self, conn, code, variance, innovation, latent, epoch, price, event_id=None):
        batch = getattr(self._tick, 'batch', None)
        if batch is not None:
            batch['state_updates'][code] = (code, variance, innovation, latent, epoch, price, event_id)
            batch['states'][code] = dict(asset_code=code, variance=variance, innovation=innovation,
                latent_price=latent, last_epoch=epoch, last_display_price=price, event_id=event_id)
            return
        conn.execute("INSERT INTO system.market_simulation_state "
                     "(asset_code, variance, innovation, latent_price, last_epoch, last_display_price, event_id) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(asset_code) DO UPDATE SET "
                     "variance = excluded.variance, innovation = excluded.innovation, "
                     "latent_price = excluded.latent_price, last_epoch = excluded.last_epoch, "
                     "last_display_price = excluded.last_display_price, event_id = excluded.event_id",
                     (code, variance, innovation, latent, epoch, price, event_id))

    def _auto_target(self, conn, code, price, epoch, config):
        with self._runtime_lock:
            row = self._runtime_auto().get(str(code))
        if row is not None:
            try:
                cycle = TargetCycle(float(row["anchor_price"]), float(row["target_price"]),
                                    int(row["start_epoch"]), int(row["end_epoch"]))
                if cycle.start_epoch <= epoch < cycle.end_epoch:
                    return cycle
            except (TypeError, ValueError, OverflowError):
                pass
        cycle = sample_target(price, epoch, config, rng=random)
        with self._runtime_lock:
            self._runtime_auto()[str(code)] = dict(anchor_price=cycle.anchor_price,
                target_price=cycle.target_price, start_epoch=cycle.start_epoch, end_epoch=cycle.end_epoch)
        return cycle

        # Legacy SQLite-backed target state is intentionally unreachable.
        batch = getattr(self._tick, 'batch', None)
        row = batch['targets'].get(code) if batch is not None else conn.execute("SELECT * FROM system.market_auto_targets WHERE asset_code = ?", (code,)).fetchone()
        cycle = None
        if row is not None:
            try:
                cycle = TargetCycle(float(row["anchor_price"]), float(row["target_price"]),
                                    int(row["start_epoch"]), int(row["end_epoch"]))
                if epoch >= cycle.end_epoch or epoch < cycle.start_epoch:
                    cycle = None
            except (TypeError, ValueError, OverflowError):
                cycle = None
        if cycle is None:
            # Sample from the live fractional quote, without teleporting the
            # price to either the old or the new target at a cycle boundary.
            cycle = sample_target(price, epoch, config, rng=random)
            if batch is not None:
                batch['targets'][code] = dict(anchor_price=cycle.anchor_price, target_price=cycle.target_price,
                    start_epoch=cycle.start_epoch, end_epoch=cycle.end_epoch)
                batch['target_updates'][code] = (code, cycle.anchor_price, cycle.target_price, cycle.start_epoch, cycle.end_epoch)
                return cycle
            conn.execute("INSERT INTO system.market_auto_targets "
                         "(asset_code, anchor_price, target_price, start_epoch, end_epoch) VALUES (?, ?, ?, ?, ?) "
                         "ON CONFLICT(asset_code) DO UPDATE SET anchor_price = excluded.anchor_price, "
                         "target_price = excluded.target_price, start_epoch = excluded.start_epoch, end_epoch = excluded.end_epoch",
                         (code, cycle.anchor_price, cycle.target_price, cycle.start_epoch, cycle.end_epoch))
        return cycle

    def _stochastic_price(self, conn, code, price, now, settings, volatility, condition, legacy_events, profile=None, sector_key=None):
        profile = profile or {}
        liquidity = max(0.05, float(profile.get("liquidity", 1.0)))
        trend_sensitivity = max(0.0, float(profile.get("trend_sensitivity", 1.0)))
        sector_sensitivity = max(0.0, float(profile.get("sector_sensitivity", 0.0)))
        jump_intensity = max(0.0, float(profile.get("jump_intensity", 0.0)))
        mean_reversion = max(0.0, float(profile.get("mean_reversion", 1.0)))
        config = GarchConfig.from_settings(settings, volatility=volatility)
        config = replace(config, annual_volatility=config.annual_volatility / math.sqrt(liquidity))
        epoch = now // config.step_seconds * config.step_seconds
        batch = getattr(self._tick, 'batch', None)
        state = batch['states'].get(code) if batch is not None else conn.execute("SELECT * FROM system.market_simulation_state WHERE asset_code = ?", (code,)).fetchone()
        due = state is None or epoch > int(state["last_epoch"])
        edited = state is not None and price != int(state["last_display_price"])
        latent = float(price) if state is None or edited else float(state["latent_price"])
        variance = None if state is None or edited or not state["variance"] else float(state["variance"])
        innovation = 0.0 if state is None or edited else float(state["innovation"])
        auto_config = AutoTargetConfig.from_settings(settings)
        if edited or not auto_config.enabled or (state is not None and state["event_id"] is not None):
            self._delete_auto_target(conn, code)
        event_context = {}
        event_step = None

        def event_noise(target):
            nonlocal event_step
            if not due:
                return 0.0, 0.0
            # Events still fluctuate when ordinary market movement is paused.
            event_config = GarchConfig.from_settings(settings, volatility=max(volatility, self.DEFAULT_VOLATILITY))
            event_config = replace(event_config, annual_volatility=event_config.annual_volatility / math.sqrt(liquidity))
            jump = self._event_jump_fraction(target)
            event_step = simulate_step(latent, variance, innovation, event_config,
                market_shock=self._market_factor(conn, epoch, event_config), rng=random,
                jump=math.log1p(jump))
            # Keep the administrator's percent in charge of event amplitude;
            # GARCH increases or decreases the noise around that bridge.
            scale = min(3.0, math.sqrt(event_step.variance / event_config.step_variance))
            shock = max(-3.0, min(3.0, event_step.shock)) * scale
            return shock, jump

        event_price = self._event_target_price(conn, code, price, now, legacy_events,
                                              sample_noise=event_noise, context=event_context, batch=batch)
        if event_price is not None:
            self._delete_auto_target(conn, code)
            if not event_context["advanced"] and state is not None and not edited:
                return event_price
            if event_step is not None:
                variance, innovation = event_step.variance, event_step.innovation
            elif due:
                event_config = (GarchConfig.from_settings(settings, volatility=max(volatility, self.DEFAULT_VOLATILITY))
                                if event_context["volatility_percent"] else config)
                variance, innovation = next_variance(variance, innovation, event_config), 0.0
            self._save_simulation_state(conn, code, variance or 0.0, innovation,
                event_context["latent_price"], epoch, event_price, event_context["event_id"])
            return event_price

        if state is not None and state["event_id"] is not None:
            # Start free movement from the completed bridge's actual quote,
            # retaining its volatility memory but not its target trajectory.
            latent = float(price)
        if not due:
            if edited:
                self._save_simulation_state(conn, code, config.step_variance, 0.0, float(price),
                                            int(state["last_epoch"]), price)
            return price
        if not config.step_variance:
            # Pausing movement also pauses the variance recurrence, retaining
            # recent volatility for when the operator resumes it.
            self._save_simulation_state(conn, code, variance or 0.0, innovation, float(price), epoch, price)
            return price
        bias = {"bull": 1, "bear": -1, "flat": 0}[condition] * trend_sensitivity * math.log1p(.10) \
               * config.trading_minutes_per_step / TRADING_MINUTES_PER_YEAR
        if sector_sensitivity:
            bias += sector_sensitivity * math.sqrt(config.step_variance) * self._sector_shock(sector_key, epoch, config) * 0.25
        cycle = None
        if auto_config.enabled:
            cycle = self._auto_target(conn, code, latent, epoch, auto_config)
            bias += target_pull(latent, cycle, auto_config) * mean_reversion
            # Scale the GARCH target variance, not its sampled innovation.
            # This preserves alpha+beta stability and excludes target drift
            # from the stochastic innovation used by the next recurrence.
            config = replace(config, annual_volatility=config.annual_volatility * auto_config.noise_multiplier)
        jump = 0.0
        jump_probability = min(0.15, jump_intensity * config.trading_minutes_per_step / 390)
        if jump_probability and random.random() < jump_probability:
            direction = 1 if random.random() >= 0.5 else -1
            jump = direction * math.log1p(min(0.18, max(0.01, abs(standardized_shock(random, config.student_df)) * math.sqrt(config.step_variance) * 18)))
        step = simulate_step(latent, variance, innovation, config,
            market_shock=self._market_factor(conn, epoch, config), rng=random, directional_bias=bias, jump=jump)
        next_price, next_innovation = step.price, step.innovation
        if cycle is not None:
            lower, upper = cycle_bounds(cycle, auto_config)
            bounded = min(upper, max(lower, next_price))
            # Changing a band in SQL must still respect the existing 20%
            # single-step limit while the quote returns to its new band.
            next_price = min(float(MAX_PRICE), max(1.0, min(latent * 1.2, max(latent * .8, bounded))))
            next_innovation = math.log(next_price / latent) - bias + step.variance / 2
        new_price = min(MAX_PRICE, max(1, self.normalize_market_price(next_price)))
        self._save_simulation_state(conn, code, step.variance, next_innovation, next_price, epoch, new_price)
        return new_price

    @staticmethod
    def event_asset_eligible(asset):
        if asset.get('type')=='els':return False
        return (asset["status"] == "active" and asset["price"] > 0
                and (not asset["is_fixed"] or asset["type"] in {"option","국채"}))

    def get_event_assets(self):
        """Use identical eligibility rules for the modal and event registration."""
        return [asset for asset in self.get_market_assets(active_only=True) if self.event_asset_eligible(asset)]

    @staticmethod
    def _load_tick(conn, now):
        # Target paths are RAM-only.  Database reads here retain only the
        # persistent GARCH state and common market factor.
        return dict(states={r['asset_code']: r for r in conn.execute('SELECT * FROM market_simulation_state')},
            targets={}, factor=MarketManager._factor_from_settings(conn),
            events={}, state_updates={}, target_updates={}, target_deletes=set(), sector_shocks={})

        # Legacy SQLite-backed target state is intentionally unreachable.
        events = {}
        for row in conn.execute("SELECT t.*, e.event_type, COALESCE(e.volatility_percent, "
                "CASE WHEN e.target_mode='percent' THEN e.target_value ELSE e.severity END) volatility_percent "
                "FROM market_event_targets t JOIN market_events e USING(event_id) "
                "WHERE t.start_epoch<=? AND (t.state='active' OR (t.state='completed' AND t.completed_epoch=?)) "
                "ORDER BY t.event_id", (now, now)):
            events[row['asset_code']] = row
        return dict(states={r['asset_code']: r for r in conn.execute('SELECT * FROM market_simulation_state')},
            targets={r['asset_code']: r for r in conn.execute('SELECT * FROM market_auto_targets')},
            factor=MarketManager._factor_from_settings(conn),
            events=events, state_updates={}, target_updates={}, target_deletes=set(), sector_shocks={})

    def _delete_auto_target(self, conn, code):
        with self._runtime_lock:
            self._runtime_auto().pop(str(code), None)
        return

        # Legacy SQLite-backed target state is intentionally unreachable.
        batch = getattr(self._tick, 'batch', None)
        if batch is None:
            conn.execute('DELETE FROM market_auto_targets WHERE asset_code=?', (code,))
        elif code in batch['targets']:
            batch['targets'].pop(code)
            batch['target_updates'].pop(code, None)
            batch['target_deletes'].add(code)

    def _flush_tick(self, conn):
        batch = self._tick.batch
        conn.executemany('INSERT INTO market_simulation_state '
            '(asset_code,variance,innovation,latent_price,last_epoch,last_display_price,event_id) VALUES (?,?,?,?,?,?,?) '
            'ON CONFLICT(asset_code) DO UPDATE SET variance=excluded.variance,innovation=excluded.innovation, '
            'latent_price=excluded.latent_price,last_epoch=excluded.last_epoch, '
            'last_display_price=excluded.last_display_price,event_id=excluded.event_id', batch['state_updates'].values())
        return

        # Legacy SQLite-backed target state is intentionally unreachable.
        conn.executemany('DELETE FROM market_auto_targets WHERE asset_code=?',
                         [(code,) for code in batch['target_deletes']])
        conn.executemany('INSERT INTO market_auto_targets '
            '(asset_code,anchor_price,target_price,start_epoch,end_epoch) VALUES (?,?,?,?,?) '
            'ON CONFLICT(asset_code) DO UPDATE SET anchor_price=excluded.anchor_price, '
            'target_price=excluded.target_price,start_epoch=excluded.start_epoch,end_epoch=excluded.end_epoch',
            batch['target_updates'].values())
        conn.executemany('INSERT INTO market_simulation_state '
            '(asset_code,variance,innovation,latent_price,last_epoch,last_display_price,event_id) VALUES (?,?,?,?,?,?,?) '
            'ON CONFLICT(asset_code) DO UPDATE SET variance=excluded.variance,innovation=excluded.innovation, '
            'latent_price=excluded.latent_price,last_epoch=excluded.last_epoch, '
            'last_display_price=excluded.last_display_price,event_id=excluded.event_id', batch['state_updates'].values())

    def update_prices(self, market_trend=1.0, volatility=None, now=None, market_condition=None):
        now = int(time.time() if now is None else now)
        # Keep the background price loop's existing short-lock behaviour. A
        # busy DB skips a tick, and an automatic resume shares the same commit
        # as shifting suspended event/model clocks.
        try:
            with self._short_market_transaction("system", now=now) as conn:
                self._tick.conn = conn
                try:
                    self._tick.batch = self._load_tick(conn, now)
                    changed = self._update_prices_open(market_trend, volatility, now, market_condition)
                    if getattr(self.db,'els',None) is not None:
                        from managers.els_manager import ELSManager
                        ELSManager(self.db).observe(conn,now)
                    self._flush_tick(conn)
                finally:
                    self._tick.conn = None
                    self._tick.batch = None
            self.movement_history.cleanup(now)
            return changed
        except _MarketHalted:
            self.movement_history.cleanup(now)
            return 0
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
                return 0
            raise

    def _update_prices_open(self, market_trend=1.0, volatility=None, now=None, market_condition=None):
        """Refresh prices and append one sampled price per active code.

        GARCH variance, fractional prices and common shocks persist in inazina.db.
        A recurring nearby target adds mean reversion to ordinary movement;
        explicit administrator event goals take priority over those targets.
        One new epoch advances at most one game trading step, without replaying
        downtime. Reads settings in the tick transaction. Set
        ``is_fixed = 1`` to keep a SQL-edited price fixed while the loop runs.
        Every tradable price row can override global volatility; NULL follows
        settings. Company shares and investments can also override condition.
        A 50ms lock timeout makes a busy database skip a tick instead of holding up
        commands or other processes.
        """
        conn = self._tick.conn
        settings = {row['key']: row['value'] for row in conn.execute('SELECT key,value FROM settings')}
        volatility = float(settings.get('market_volatility', self.DEFAULT_VOLATILITY) if volatility is None else volatility)
        if not math.isfinite(volatility) or not 0 <= volatility <= self.MAX_VOLATILITY:
            raise ValueError("volatility는 0~1 사이 숫자여야 합니다.")
        market_condition = self._normalize_market_condition(
            settings.get('market_condition', 'flat') if market_condition is None else market_condition)
        market_trend = float(market_trend)
        if not math.isfinite(market_trend) or market_trend < 0:
            raise ValueError("market_trend는 0 이상의 유한한 숫자여야 합니다.")
        global_config = GarchConfig.from_settings(settings, volatility=volatility)
        AutoTargetConfig.from_settings(settings)
        now = int(time.time() if now is None else now)
        legacy_events = []
        changed = 0
        history = []
        specs = (
            ("assets", "market_assets", "code", "price", False, True, True, False),
            ("company", "company", "company_code", "share_price", False, True, True, False),
            ("product", "investment", "product_code", "unit_price", True, True, True, True),
        )
        for (
            schema, table, code_column, price_column,
            has_maturity, has_condition, has_volatility, has_derivative,
        ) in specs:
            schema_history = []
            schema_changed = 0
            price_updates = []
            optional_columns = []
            if has_maturity:
                optional_columns.extend(("maturity_days", "created_at"))
            if has_condition:
                optional_columns.append("market_condition")
            if has_volatility:
                optional_columns.append("market_volatility")
                optional_columns.extend(PROFILE_COLUMNS)
            if has_derivative:
                optional_columns.extend(("underlying_exchange_id", "leverage_multiplier", "product_type"))
            if schema == "company":
                optional_columns.extend(("industry", "total_shares", "capital"))
            extra = f", {', '.join(optional_columns)}" if optional_columns else ""
            try:
                with self._short_market_transaction((schema, "system")) as conn:
                    rows = conn.execute(
                        f"SELECT {code_column} AS code, {price_column} AS price, exchange_id, is_fixed{extra} "
                        f"FROM {schema}.{table} WHERE status = 'active'"
                    ).fetchall()
                    for row in rows:
                        if has_derivative and row['product_type']=='els':
                            continue
                        code = str(row["code"])
                        if MarketSessionManager(self.db).trading_error(
                            conn=conn, now=now, exchange_id=row["exchange_id"]
                        ):
                            continue
                        try:
                            raw_price = float(row["price"])
                        except (TypeError, ValueError, OverflowError):
                            continue
                        if not math.isfinite(raw_price) or not 0 < raw_price <= MAX_PRICE:
                            continue

                        price = self.normalize_market_price(raw_price)

                        asset_volatility = volatility
                        if has_volatility and row["market_volatility"] is not None:
                            try:
                                override_volatility = float(row["market_volatility"])
                                if (
                                    math.isfinite(override_volatility)
                                    and 0 <= override_volatility <= self.MAX_VOLATILITY
                                ):
                                    asset_volatility = override_volatility
                            except (TypeError, ValueError, OverflowError):
                                pass

                        matured = has_maturity and self._investment_matured(
                            row["created_at"], row["maturity_days"]
                        )
                        asset_condition = market_condition
                        if has_condition and row["market_condition"]:
                            try:
                                asset_condition = self._normalize_market_condition(
                                    row["market_condition"]
                                )
                            except ValueError:
                                # A bad per-asset SQL value must not stop the entire market tick.
                                asset_condition = market_condition
                        new_price = price
                        is_derivative = (
                            has_derivative and (bool(str(row["underlying_exchange_id"] or "").strip())
                                or row["product_type"] in {"stock_future", "corporate_bond", "mutual_fund", "index_fund"})
                        )
                        if not int(row["is_fixed"] or 0) and not matured and not is_derivative:
                            profile = self._price_profile(row, "company" if schema == "company" else "investment" if schema == "product" else "market_assets")
                            sector_key = row["industry"] if schema == "company" else row["product_type"] if has_derivative else row["type"] if "type" in row.keys() else None
                            new_price = self._stochastic_price(conn, code, price, now, settings,
                                min(self.MAX_VOLATILITY, asset_volatility * market_trend), asset_condition, legacy_events,
                                profile=profile, sector_key=sector_key)
                        else:
                            self._delete_auto_target(conn, code)
                        if int(row["is_fixed"] or 0) or matured:
                            # A price edited while fixed must become the new
                            # anchor when automatic movement is enabled again.
                            conn.execute("UPDATE system.market_simulation_state SET latent_price = ?, "
                                "last_display_price = ?, last_epoch = ?, variance = 0, innovation = 0, event_id = NULL "
                                "WHERE asset_code = ?", (price, price, now // global_config.step_seconds
                                                        * global_config.step_seconds, code))
                        if new_price != raw_price:
                            price_updates.append((new_price, code))
                        schema_history.append((code, new_price, now))
                    if price_updates:
                        cursor = conn.executemany(
                            f"UPDATE {schema}.{table} SET {price_column} = ? WHERE {code_column} = ?",
                            price_updates)
                        schema_changed = cursor.rowcount
                changed += schema_changed
                history.extend(schema_history)
            except sqlite3.OperationalError as exc:
                if getattr(self._tick, "conn", None) is not None:
                    raise
                if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
                    continue
                raise

        if history:
            try:
                with self._short_market_transaction("assets") as conn:
                    conn.executemany(
                        "INSERT INTO assets.market_price_history (asset_code, price, recorded_epoch) "
                        "VALUES (?, ?, ?) ON CONFLICT(asset_code, recorded_epoch) "
                        "DO UPDATE SET price = excluded.price",
                        history,
                    )
            except sqlite3.OperationalError as exc:
                if getattr(self._tick, "conn", None) is not None:
                    raise
                if "locked" not in str(exc).casefold() and "busy" not in str(exc).casefold():
                    raise
        self._update_exchange_indices(now)
        self._update_leveraged_etfs(now)
        self._update_linked_products(now)
        self._update_event_quotes(now, settings, volatility, market_condition)
        self._record_special_quotes(now)
        return changed

    def _update_event_quotes(self, now, settings, volatility, condition):
        # Run after NAV/index repricing, so linked prices cannot overwrite an
        # active event. Overlay contracts whose terms stay immutable.
        try:
            with self._short_market_transaction('assets') as conn:
                conn.execute("DELETE FROM market_quote_overrides WHERE asset_code IN (SELECT product_code FROM investment) "
                             "AND updated_epoch < ?", (now,))
        except sqlite3.OperationalError as exc:
            if getattr(self._tick, "conn", None) is not None:
                raise
            if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
                raise
        with self._short_market_transaction('assets') as conn:
            with self._runtime_lock:
                event_codes = tuple(self._runtime_events())
            quotes = self._batch_quotes(conn, now) if event_codes else {}
        for code in event_codes:
            asset = quotes.get(code)
            if not asset or asset['status'] != 'active':
                continue
            if MarketSessionManager(self.db).trading_error(
                conn=getattr(self._tick, 'conn', None), now=now,
                exchange_id=asset.get('exchange_id')
            ):
                continue
            if asset['type'] not in {'option','국채','stock_future','corporate_bond','mutual_fund','index_fund'} and not asset.get('underlying_exchange_id'):
                continue
            try:
                with self._short_market_transaction(('assets','system','product')) as conn:
                    value = self._stochastic_price(conn,asset['code'],asset['price'],now,settings,volatility,condition,[])
                    if asset['type'] == '국채':
                        conn.execute("UPDATE treasury_bond_products SET market_price=? WHERE product_code=?", (value,asset['code']))
                    elif asset['type'] != 'option':
                        conn.execute("UPDATE investment SET unit_price=? WHERE product_code=?", (value,asset['code']))
                    conn.execute("INSERT INTO market_quote_overrides VALUES (?,?,?,'event') ON CONFLICT(asset_code) DO UPDATE SET price=excluded.price,updated_epoch=excluded.updated_epoch", (asset['code'],value,now))
                    conn.execute("INSERT INTO market_price_history(asset_code,price,recorded_epoch) VALUES (?,?,?) ON CONFLICT(asset_code,recorded_epoch) DO UPDATE SET price=excluded.price",(asset['code'],value,now))
            except sqlite3.OperationalError as exc:
                if getattr(self._tick, "conn", None) is not None:
                    raise
                if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
                    raise

    def _record_special_quotes(self, now):
        # Keep observable option premiums and bond prices even with no fills.
        try:
            with self._short_market_transaction('assets') as conn:
                quotes = self._batch_quotes(conn, now)
                points = [(code, asset['price'], now) for code, asset in quotes.items()
                          if asset['type'] in {'option', '국채'} and asset['status'] == 'active']
                conn.executemany("INSERT INTO market_price_history(asset_code,price,recorded_epoch) VALUES (?,?,?) "
                    "ON CONFLICT(asset_code,recorded_epoch) DO UPDATE SET price=excluded.price", points)
        except sqlite3.OperationalError as exc:
            if getattr(self._tick, "conn", None) is not None:
                raise
            if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
                raise

    def _batch_quotes(self, conn, now):
        """Resolve current transaction quotes without recursive per-symbol SQL."""
        from managers.option_manager import OptionManager
        quotes = {}
        for table, source in (('market_assets', 'market_assets'), ('company', 'company'),
                              ('investment', 'investment'), ('treasury_bond_products', 'treasury_bond')):
            for row in conn.execute(f'SELECT * FROM {table}'):
                asset = self._to_market_asset(row, source)
                if asset['code'] in quotes:
                    raise ValueError(f"중복 종목 코드: {asset['code']}")
                quotes[asset['code']] = asset
        overrides = {r['asset_code']: r['price'] for r in conn.execute('SELECT * FROM market_quote_overrides')}
        for code, price in overrides.items():
            if code in quotes:
                quotes[code]['price'] = int(price)
        rate = float(conn.execute("SELECT value FROM settings WHERE key='base_rate'").fetchone()[0]) / 100
        for row in conn.execute('SELECT * FROM option_contracts'):
            underlying = quotes.get(row['underlying_code'])
            if underlying is None:
                continue
            code = row['contract_code']
            if code in quotes:
                raise ValueError(f'중복 종목 코드: {code}')
            price = overrides.get(code)
            if price is None:
                price = OptionManager.theoretical_price(int(underlying['price']), int(row['strike_price']),
                    max(0, row['expiration_epoch'] - now) / (365 * 86400),
                    float(row['implied_volatility']), row['option_type'], rate)
            quotes[code] = dict(code=code, price=int(price), type='option', is_fixed=1,
                exchange_id=underlying.get('exchange_id'),
                status='matured' if row['status']=='active' and row['expiration_epoch']<=now else row['status'])
        return quotes

    def _update_linked_products(self, now):
        from managers.investment_manager import InvestmentManager
        try:
            with self._short_market_transaction(("assets", "product", "company")) as conn:
                rate = float(conn.execute("SELECT value FROM settings WHERE key='base_rate'").fetchone()[0])
                prices = {r['code']: r for r in conn.execute('SELECT code,price FROM market_assets')}
                prices.update({r['code']: r for r in conn.execute('SELECT company_code code,share_price price FROM company')})
                components = {}
                for row in conn.execute('SELECT * FROM fund_components'):
                    components.setdefault(row['product_code'], []).append(row)
                updates, history = [], []
                products = conn.execute("SELECT * FROM product.investment WHERE status = 'active' "
                    "AND product_type IN ('stock_future', 'corporate_bond', 'mutual_fund')").fetchall()
                for product in products:
                    if MarketSessionManager(self.db).trading_error(
                        conn=conn, now=now, exchange_id=product['exchange_id']
                    ):
                        continue
                    if int(product["is_fixed"] or 0):
                        continue
                    price = InvestmentManager.linked_price(conn, product, rate, now,
                        prices=prices, fund_components=components)
                    if price is None:
                        continue
                    if price != product['unit_price']:
                        updates.append((price, product['product_code']))
                    history.append((product['product_code'], price, now))
                conn.executemany("UPDATE investment SET unit_price=? WHERE product_code=? AND is_fixed=0", updates)
                conn.executemany("INSERT INTO market_price_history(asset_code,price,recorded_epoch) VALUES (?,?,?) "
                    "ON CONFLICT(asset_code,recorded_epoch) DO UPDATE SET price=excluded.price", history)
        except sqlite3.OperationalError as exc:
            if getattr(self._tick, "conn", None) is not None:
                raise
            if "locked" not in str(exc).casefold() and "busy" not in str(exc).casefold():
                raise

    def _update_leveraged_etfs(self, now):
        """Price SQL-configured inverse/leveraged investment products from an exchange index."""
        try:
            with self._short_market_transaction(("assets", "product")) as conn:
                indices = {r['exchange_id']: r for r in conn.execute('SELECT * FROM market_exchanges')}
                states = {r['product_code']: r for r in conn.execute('SELECT * FROM leveraged_etf_state')}
                products = conn.execute(
                    "SELECT product_code, underlying_exchange_id, leverage_multiplier, "
                    "rebalance_cycle_seconds, unit_price, is_fixed "
                    "FROM product.investment WHERE status = 'active' "
                    "AND underlying_exchange_id IS NOT NULL "
                    "AND TRIM(underlying_exchange_id) <> ''"
                ).fetchall()
                for product in products:
                    code = str(product["product_code"])
                    exchange_id = str(product["underlying_exchange_id"]).strip()
                    if MarketSessionManager(self.db).trading_error(
                        conn=conn, now=now, exchange_id=exchange_id
                    ):
                        continue
                    try:
                        leverage = float(product["leverage_multiplier"])
                        raw_nav = float(product["unit_price"] or 0)
                        cycle = int(product["rebalance_cycle_seconds"] or 86400)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if (
                        not math.isfinite(leverage) or leverage == 0 or abs(leverage) > 10
                        or not math.isfinite(raw_nav) or raw_nav <= 0
                        or cycle <= 0 or cycle > 365 * 86400
                    ):
                        continue

                    index_row = indices.get(exchange_id)
                    if not index_row:
                        continue
                    current_index = float(index_row["index_value"])
                    if not math.isfinite(current_index) or current_index <= 0:
                        continue

                    state = states.get(code)
                    reset_state = (
                        not state
                        or str(state["underlying_exchange_id"]) != exchange_id
                        or float(state["leverage_multiplier"]) != leverage
                        or int(state["rebalance_cycle_seconds"] or 86400) != cycle
                    )
                    if reset_state:
                        base_index = current_index
                        base_nav = max(1.0, raw_nav)
                        last_reset = int(now)
                    else:
                        base_index = float(state["base_index_value"])
                        base_nav = max(1.0, float(state["base_nav"]))
                        last_reset = int(state["last_rebalance_epoch"])

                    if int(product["is_fixed"] or 0):
                        # SQL can pin/edit an ETF price with is_fixed=1. Re-anchor
                        # it while fixed so unfreezing does not apply stale returns.
                        base_index = current_index
                        base_nav = max(1.0, raw_nav)
                        last_reset = int(now)
                    else:
                        while int(now) >= last_reset + cycle:
                            break

                        theoretical_nav = base_nav * (
                            1 + leverage * (current_index / base_index - 1)
                        )
                        nav = max(1.0, theoretical_nav)
                        market_price = self.normalize_market_price(nav)
                        if market_price != self.normalize_market_price(raw_nav):
                            conn.execute(
                                "UPDATE product.investment SET unit_price = ? "
                                "WHERE product_code = ? AND is_fixed = 0",
                                (market_price, code),
                            )
                        conn.execute(
                            "INSERT INTO assets.market_price_history "
                            "(asset_code, price, recorded_epoch) VALUES (?, ?, ?) "
                            "ON CONFLICT(asset_code, recorded_epoch) DO UPDATE SET "
                            "price = excluded.price",
                            (code, market_price, int(now)),
                        )

                    conn.execute(
                        "INSERT INTO assets.leveraged_etf_state "
                        "(product_code, underlying_exchange_id, leverage_multiplier, "
                        "rebalance_cycle_seconds, "
                        "base_index_value, base_nav, last_rebalance_epoch) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(product_code) DO UPDATE SET "
                        "underlying_exchange_id = excluded.underlying_exchange_id, "
                        "leverage_multiplier = excluded.leverage_multiplier, "
                        "rebalance_cycle_seconds = excluded.rebalance_cycle_seconds, "
                        "base_index_value = excluded.base_index_value, base_nav = excluded.base_nav, "
                        "last_rebalance_epoch = excluded.last_rebalance_epoch",
                        (code, exchange_id, leverage, cycle, base_index, base_nav, last_reset),
                    )
        except Exception as exc:
            if getattr(self._tick, "conn", None) is not None:
                raise
            if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
                return
            raise

    def _update_exchange_indices(self, now):
        """Chain-link market-cap-weighted indexes for active company shares.

        New listings are seeded at their current price, so listing/removal does
        not itself create an index jump. Existing names contribute their prior
        market-cap weights to the tick's return.
        """
        try:
            with self._short_market_transaction("company") as company_conn:
                rows = company_conn.execute(
                    "SELECT company_code, exchange_id, share_price, total_shares "
                    "FROM company.company WHERE status = 'active' "
                    "AND exchange_id IS NOT NULL AND TRIM(exchange_id) <> ''"
                ).fetchall()
            constituents = {}
            for row in rows:
                exchange_id = str(row["exchange_id"]).strip()
                code = str(row["company_code"])
                try:
                    price = float(row["share_price"] or 0)
                    shares = max(0, int(row["total_shares"] or 0))
                except (TypeError, ValueError, OverflowError):
                    continue
                if not exchange_id or not code or not math.isfinite(price) or price <= 0:
                    continue
                constituents.setdefault(exchange_id, {})[code] = (price, shares)
            with self._short_market_transaction("assets") as conn:
                tracked_exchanges = conn.execute(
                    "SELECT exchange_id FROM assets.market_exchanges"
                ).fetchall()
                for tracked in tracked_exchanges:
                    if str(tracked["exchange_id"]) not in constituents:
                        conn.execute(
                            "DELETE FROM assets.market_exchange_index_components "
                            "WHERE exchange_id = ?", (tracked["exchange_id"],),
                        )
                for exchange_id, members in constituents.items():
                    if MarketSessionManager(self.db).trading_error(
                        conn=conn, now=now, exchange_id=exchange_id
                    ):
                        continue
                    previous_index = conn.execute(
                        "SELECT index_value FROM assets.market_exchanges "
                        "WHERE exchange_id = ?", (exchange_id,),
                    ).fetchone()
                    previous_members = {
                        str(row["company_code"]): (
                            float(row["previous_price"]), int(row["total_shares"] or 0)
                        )
                        for row in conn.execute(
                            "SELECT company_code, previous_price, total_shares "
                            "FROM assets.market_exchange_index_components WHERE exchange_id = ?",
                            (exchange_id,),
                        ).fetchall()
                    }

                    if previous_index is None:
                        index_value = 1000.0
                    else:
                        prior_index_value = float(previous_index["index_value"])
                        weighted_return = 0.0
                        total_weight = 0.0
                        unweighted_returns = []
                        for code, (price, _shares) in members.items():
                            previous = previous_members.get(code)
                            if not previous or previous[0] <= 0:
                                continue
                            previous_price, previous_shares = previous
                            asset_return = price / previous_price - 1.0
                            weight = previous_price * previous_shares
                            weighted_return += asset_return * weight
                            total_weight += weight
                            unweighted_returns.append(asset_return)
                        if total_weight > 0:
                            index_return = weighted_return / total_weight
                        elif unweighted_returns:
                            index_return = sum(unweighted_returns) / len(unweighted_returns)
                        else:
                            index_return = 0.0
                        index_value = max(0.01, round(prior_index_value * (1 + index_return), 2))

                    conn.execute(
                        "INSERT INTO assets.market_exchanges "
                        "(exchange_id, index_value, index_updated_epoch, status, updated_epoch, trading_halted) "
                        "VALUES (?, ?, ?, 'open', 0, 0) "
                        "ON CONFLICT(exchange_id) DO UPDATE SET "
                        "index_value = excluded.index_value, index_updated_epoch = excluded.index_updated_epoch",
                        (exchange_id, index_value, int(now)),
                    )
                    conn.execute(
                        "DELETE FROM assets.market_exchange_index_components WHERE exchange_id = ?",
                        (exchange_id,),
                    )
                    conn.executemany(
                        "INSERT INTO assets.market_exchange_index_components "
                        "(exchange_id, company_code, previous_price, total_shares) VALUES (?, ?, ?, ?)",
                        [
                            (exchange_id, code, price, shares)
                            for code, (price, shares) in members.items()
                        ],
                    )
        except sqlite3.OperationalError as exc:
            if getattr(self._tick, "conn", None) is not None:
                raise
            if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
                return
            raise

    def _prepare_movement_history_read(self):
        now = int(time.time())
        date, _ = self.movement_history.day_boundary(now)
        if self.db.system.get_setting(self.movement_history.DATE_KEY) != date:
            # Rebalance using the old index samples before deleting them, even
            # when a chart is opened before the first background tick of a day.
            self._update_leveraged_etfs(now)
            self.movement_history.cleanup(now)

    def get_price_history(self, asset_code, since_epoch):
        self._prepare_movement_history_read()
        return self.db.assets.execute(
            "SELECT recorded_epoch, price FROM market_price_history "
            "WHERE asset_code = ? AND recorded_epoch >= ? ORDER BY recorded_epoch",
            (str(asset_code), int(since_epoch)),
        )

    def get_trade_history(self, asset_code, since_epoch):
        self._prepare_movement_history_read()
        return self.db.assets.get_trade_history(asset_code, since_epoch)

    def get_exchange_indices(self):
        """List persisted indexes plus active exchanges awaiting their first tick."""
        rows = self.db.assets.execute(
            "SELECT exchange_id, index_value, index_updated_epoch AS updated_epoch "
            "FROM market_exchanges ORDER BY exchange_id"
        )
        indices = {str(row["exchange_id"]): dict(row) for row in rows}
        active_exchanges = self.db.companies.execute(
            "SELECT DISTINCT exchange_id FROM company WHERE status = 'active' "
            "AND exchange_id IS NOT NULL AND TRIM(exchange_id) <> ''"
        )
        for row in active_exchanges:
            exchange_id = str(row["exchange_id"]).strip()
            if exchange_id and exchange_id not in indices:
                indices[exchange_id] = {
                    "exchange_id": exchange_id,
                    "index_value": 1000.0,
                    "updated_epoch": 0,
                }
        return [indices[key] for key in sorted(indices, key=str.casefold)]

    def get_exchange_index_info(self, exchange_id):
        exchange_id = str(exchange_id)
        row = self.db.assets.execute_one(
            "SELECT exchange_id, index_value, index_updated_epoch AS updated_epoch "
            "FROM market_exchanges WHERE exchange_id = ?", (exchange_id,)
        )
        if row:
            return dict(row)
        exists = self.db.companies.execute_one(
            "SELECT 1 FROM company WHERE exchange_id = ? AND status = 'active' LIMIT 1",
            (exchange_id,),
        )
        if not exists:
            return None
        return {"exchange_id": exchange_id, "index_value": 1000.0, "updated_epoch": 0}

    def get_exchange_index_history(self, exchange_id, since_epoch):
        return []

    def get_exchange_trade_history(self, exchange_id, since_epoch):
        """Aggregate traded volume for every listed asset on an exchange."""
        return []

    @staticmethod
    def is_exchange_index_code(code):
        return str(code or "").startswith(EXCHANGE_INDEX_PREFIX)

    @staticmethod
    def exchange_index_code(exchange_id):
        return f"{EXCHANGE_INDEX_PREFIX}{exchange_id}"

    def get_exchange_assets(self, exchange_id):
        assets = []
        for row in self.db.assets.execute(
            "SELECT code FROM market_assets WHERE exchange_id = ? AND status = 'active'", (exchange_id,)
        ):
            asset = self.get_asset_info(row["code"])
            if asset and asset["status"] == "active":
                assets.append(asset)
        for row in self.db.companies.execute(
            "SELECT company_code AS code FROM company WHERE exchange_id = ? AND status = 'active'", (exchange_id,)
        ):
            asset = self.get_asset_info(row["code"])
            if asset and asset["status"] == "active":
                assets.append(asset)
        for row in self.db.products.execute(
            "SELECT product_code AS code FROM investment WHERE exchange_id = ? AND status = 'active'", (exchange_id,)
        ):
            asset = self.get_asset_info(row["code"])
            if asset and asset["status"] == "active":
                assets.append(asset)
        for row in self.db.products.execute(
            "SELECT product_code AS code FROM treasury_bond_products "
            "WHERE exchange_id = ? AND status = 'active'",
            (exchange_id,),
        ):
            asset = self.get_asset_info(row["code"])
            if asset and asset["status"] == "active":
                assets.append(asset)
        for row in self.db.assets.execute("SELECT contract_code FROM option_contracts WHERE status = 'active' AND expiration_epoch > ?",
                                           (int(time.time()),)):
            asset = self.get_asset_info(row["contract_code"])
            if asset and asset.get("exchange_id") == exchange_id:
                assets.append(asset)
        return sorted(assets, key=lambda item: item["code"])

    def get_market_assets(self, active_only=False):
        """Return tradable market assets from all supported price tables.

        Investment maturities are resolved through ``get_asset_info`` so the
        returned status reflects whether an asset has matured.
        """
        codes = []
        for rows in (
            self.db.assets.execute("SELECT code FROM market_assets"),
            self.db.companies.execute("SELECT company_code AS code FROM company"),
            self.db.products.execute("SELECT product_code AS code FROM investment"),
            self.db.products.execute("SELECT product_code AS code FROM treasury_bond_products"),
            self.db.assets.execute("SELECT contract_code AS code FROM option_contracts"),
        ):
            codes.extend(str(row["code"]) for row in rows if row["code"])

        assets = []
        for code in dict.fromkeys(codes):
            asset = self.get_asset_info(code)
            if asset and (not active_only or asset["status"] == "active"):
                assets.append(asset)
        return sorted(assets, key=lambda item: (str(item["name"]).casefold(), item["code"]))

    def get_tradeable_stocks(self):
        """Return active stock listings only, excluding funds and other assets."""
        return [
            asset for asset in self.get_market_assets(active_only=True)
            if str(asset["type"]).strip().casefold() in {"stock", "주식"}
        ]
