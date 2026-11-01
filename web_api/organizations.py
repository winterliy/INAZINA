"""Read and administer organization, share-class and enterprise-group data."""
from decimal import Decimal, InvalidOperation
import json
import re
import time


def _base_amount(conn, amount_minor, currency):
    from functions.money import currency_precision, from_minor_units, to_minor_units
    from managers.currency_manager import CurrencyManager
    currency = str(currency or "").upper()
    base = str(conn.execute("SELECT base_currency_code() ").fetchone()[0] or "").upper()
    places = currency_precision(conn, currency)
    native = from_minor_units(int(amount_minor or 0), places)
    units = to_minor_units(native, 6)
    if currency != base:
        units = CurrencyManager.convert(conn, units, currency, base)
    return Decimal(str(units)).scaleb(-6)


def _metrics(conn, code):
    entity = conn.execute(
        "SELECT currency,decimal_places FROM financial_entities WHERE company_code=?", (code,)
    ).fetchone()
    if not entity:
        return {"revenue": None, "assets": None, "employees": None}
    currency = str(entity[0] or "")
    year = time.strftime("%Y", time.gmtime())
    revenue = conn.execute(
        "SELECT COALESCE(-SUM(l.amount),0) FROM financial_entries e "
        "JOIN financial_lines l USING(entry_id) JOIN financial_accounts a ON a.code=l.account "
        "WHERE e.company_code=? AND e.status='posted' AND e.period>=? AND a.category='revenue'",
        (code, year),
    ).fetchone()[0]
    assets = conn.execute(
        "SELECT COALESCE(SUM(l.amount),0) FROM financial_entries e "
        "JOIN financial_lines l USING(entry_id) JOIN financial_accounts a ON a.code=l.account "
        "WHERE e.company_code=? AND e.status='posted' AND e.period<=? "
        "AND a.category IN ('current_asset','noncurrent_asset')",
        (code, time.strftime("%Y-%m", time.gmtime())),
    ).fetchone()[0]
    try:
        base_revenue = _base_amount(conn, revenue, currency)
        base_assets = _base_amount(conn, assets, currency)
    except (ValueError, ArithmeticError):
        base_revenue = base_assets = None
    return {"revenue": base_revenue, "assets": base_assets, "employees": None}


def directory(conn, organization_code=None, now=None, min_percent=1):
    from managers.organization_manager import OrganizationManager
    now = int(time.time() if now is None else now)
    due_changes = OrganizationManager.apply_due_size_changes(conn, now)
    settings = conn.execute(
        "SELECT announcement_seconds,rule_json FROM organization_size_settings WHERE singleton=1"
    ).fetchone()
    rows = conn.execute(
        "SELECT o.*,c.exchange_id,c.quote_currency,c.total_shares,c.share_price "
        "FROM organizations o JOIN company c ON c.company_code=o.organization_code "
        "WHERE c.share_class='common' ORDER BY o.name,o.organization_code"
    ).fetchall()
    organizations = []
    size_changes = [{"organization_code": code, "status": "applied", "size": size,
                     "effective_epoch": now} for code, size in due_changes]
    for row in rows:
        metrics = _metrics(conn, row["organization_code"])
        transition = OrganizationManager.refresh_auto_size(conn, row["organization_code"], metrics, now)
        if transition["status"] in {"announced", "applied"}:
            size_changes.append({"organization_code": row["organization_code"], **transition})
        row = conn.execute(
            "SELECT o.*,c.exchange_id,c.quote_currency,c.total_shares,c.share_price "
            "FROM organizations o JOIN company c ON c.company_code=o.organization_code "
            "WHERE o.organization_code=?", (row["organization_code"],)
        ).fetchone()
        item = dict(row)
        item["display_size"] = OrganizationManager.displayed_size(row)
        item["metrics"] = metrics
        issues = conn.execute(
            "SELECT s.issuance_id,s.legacy_product_code,s.class_name,s.share_class,s.voting_rights_per_share,"
            "s.economic_interest_per_share,s.rights_json,c.total_shares,c.share_price,c.status,"
            "c.dividend_mode,c.dividend_per_share,c.dividend_percent,c.dividend_priority,"
            "c.dividend_cycle_seconds,c.dividend_is_active,c.cumulative_dividend,"
            "c.liquidation_value_per_share,c.dividend_funding "
            "FROM share_issuances s JOIN company c ON c.share_issuance_id=s.issuance_id "
            "WHERE s.organization_code=? ORDER BY s.legacy_product_code", (row["organization_code"],)
        ).fetchall()
        item["issuances"] = [dict(issue) for issue in issues]
        organizations.append(item)
    groups = conn.execute(
        "SELECT g.group_id,g.name,g.representative_organization_code,g.description,g.status,"
        "m.organization_code,m.joined_epoch FROM organization_groups g "
        "LEFT JOIN organization_group_members m ON m.group_id=g.group_id AND m.left_epoch IS NULL "
        "WHERE g.status='active' ORDER BY g.name,m.organization_code"
    ).fetchall()
    group_map = {}
    for row in groups:
        group = group_map.setdefault(int(row[0]), {
            "group_id": int(row[0]), "name": row[1], "representative_organization_code": row[2],
            "description": row[3], "status": row[4], "members": [],
        })
        if row[5]:
            group["members"].append(str(row[5]))
    result = {"organizations": organizations, "groups": list(group_map.values()), "sizeChanges": size_changes,
              "sizeSettings": {"announcement_seconds": int(settings[0]) if settings else 0,
                               "rules": json.loads(settings[1] or "{}") if settings else {}}}
    if organization_code:
        result["graph"] = OrganizationManager.ownership_graph(conn, organization_code, min_percent=min_percent)
    return result


def action(conn, payload, actor, now=None):
    from managers.organization_manager import OrganizationManager
    now = int(time.time() if now is None else now)
    kind = str(payload.get("action") or "")
    if kind == "group_create":
        group_id = OrganizationManager.create_group(
            conn, payload.get("name"), payload.get("representative"), payload.get("description", "")
        )
        return {"action": kind, "group_id": group_id}
    if kind == "group_member":
        OrganizationManager.set_group_member(
            conn, payload.get("group_id"), payload.get("organization_code"),
            payload.get("active") is not False, now,
        )
        return {"action": kind, "organization_code": str(payload.get("organization_code"))}
    if kind == "size_override":
        code, size = str(payload.get("organization_code") or ""), payload.get("size")
        exists = conn.execute("SELECT 1 FROM organizations WHERE organization_code=?", (code,)).fetchone()
        if not exists or size not in {None, "sme", "mid_market", "large"}:
            raise ValueError("기업 또는 규모를 확인해주세요.")
        conn.execute(
            "UPDATE organizations SET size_override=?,size_override_reason=?,updated_epoch=? WHERE organization_code=?",
            (size, str(payload.get("reason", ""))[:1000] or None, now, code),
        )
        return {"action": kind, "organization_code": code, "size": size}
    if kind == "classification":
        code = str(payload.get("organization_code") or "")
        ownership = payload.get("ownership_type")
        public_type = payload.get("public_enterprise_type")
        if ownership not in {"private", "national", "local", "mixed", "other"}:
            raise ValueError("기업 소유 유형을 확인해주세요.")
        if public_type not in {"none", "public_corporation", "market_based", "quasi_market_based", "other_public"}:
            raise ValueError("공공기관 분류를 확인해주세요.")
        if public_type != "none" and ownership not in {"national", "local", "mixed"}:
            raise ValueError("공기업 분류에는 국가·지방자치단체·혼합 소유를 선택해주세요.")
        cur = conn.execute(
            "UPDATE organizations SET ownership_type=?,public_enterprise_type=?,updated_epoch=? "
            "WHERE organization_code=?", (ownership, public_type, now, code)
        )
        if cur.rowcount != 1:
            raise ValueError("기업을 찾을 수 없습니다.")
        return {"action": kind, "organization_code": code,
                "ownership_type": ownership, "public_enterprise_type": public_type}
    if kind == "size_settings":
        seconds = int(payload.get("announcement_seconds", -1))
        rules = payload.get("rules")
        if seconds < 0 or not isinstance(rules, dict):
            raise ValueError("규모 공시기간과 판정 기준을 확인해주세요.")
        if rules.get("mode", "any") not in {"any", "all"}:
            raise ValueError("규모 판정 방식은 any 또는 all이어야 합니다.")
        for metric, limits in rules.get("thresholds", {}).items():
            if metric not in {"revenue", "assets", "employees"} or not isinstance(limits, list) or len(limits) != 2:
                raise ValueError("기업 규모 기준 항목을 확인해주세요.")
            try:
                low, high = map(Decimal, map(str, limits))
            except (InvalidOperation, TypeError, ValueError):
                raise ValueError("기업 규모 기준은 유한한 숫자여야 합니다.") from None
            if not low.is_finite() or not high.is_finite() or low < 0 or low > high or high > Decimal("1e24"):
                raise ValueError("기업 규모 경계값 순서를 확인해주세요.")
        encoded = json.dumps(rules, ensure_ascii=False, sort_keys=True)
        conn.execute(
            "UPDATE organization_size_settings SET announcement_seconds=?,rule_json=?,updated_epoch=? WHERE singleton=1",
            (seconds, encoded, now),
        )
        return {"action": kind, "announcement_seconds": seconds}
    if kind == "issuance_create":
        code = str(payload.get("legacy_product_code") or "").strip().upper()
        class_name = str(payload.get("class_name") or "").strip()
        organization_code = str(payload.get("organization_code") or "").strip()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,31}", code):
            raise ValueError("종목 코드는 영문·숫자와 . _ - 조합으로 1~32자 입력해주세요.")
        if not class_name or len(class_name) > 120:
            raise ValueError("주식 종류 이름은 1~120자로 입력해주세요.")
        try:
            shares = int(payload.get("total_shares"))
            price = int(payload.get("share_price"))
            votes = Decimal(str(payload.get("voting_rights_per_share", "0")))
            equity = Decimal(str(payload.get("economic_interest_per_share", "1")))
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            raise ValueError("주식 수량, 초기 가격과 주당 권리 수치를 확인해주세요.") from None
        if (shares <= 0 or shares > 9_007_199_254_740_991 or price <= 0
                or not votes.is_finite()
                or not equity.is_finite() or votes < 0 or votes > Decimal("1e12")
                or equity <= 0 or equity > Decimal("1e12")
                or isinstance(payload.get("total_shares"), bool)
                or isinstance(payload.get("share_price"), bool)
                or str(shares) != str(payload.get("total_shares")).strip()
                or str(price) != str(payload.get("share_price")).strip()):
            raise ValueError("주식 수량은 양의 정수, 가격은 양수여야 하며 주당 의결권은 0 이상·경제 지분은 0보다 커야 합니다.")
        from functions.amount_limits import MAX_NATIVE
        if price > MAX_NATIVE:
            raise ValueError("초기 가격이 통화 금액 한도를 초과했습니다.")
        parent = conn.execute(
            "SELECT name,industry,exchange_id,quote_currency FROM company "
            "WHERE company_code=? AND share_class='common' AND status='active'", (organization_code,)
        ).fetchone()
        if not parent:
            raise ValueError("활성 기업을 찾을 수 없습니다.")
        from database import DatabaseManager
        if DatabaseManager.code_locations(conn, code):
            raise ValueError("이미 사용 중인 종목 코드입니다.")
        from database.user_db import TREASURY_USER_ID
        from managers.asset_cost_manager import AssetCostManager
        from managers.currency_manager import CurrencyManager
        conn.execute("SAVEPOINT organization_share_issuance_create")
        try:
            conn.execute(
                "INSERT INTO company(company_code,name,industry,capital,total_shares,share_price,"
                "max_shareholder_id,exchange_id,quote_currency,share_class,parent_company_code) "
                "VALUES (?,?,?,0,?,?,?,?,?,'preferred',?)",
                (code, f"{parent[0] or organization_code} · {class_name}", parent[1], shares, price,
                 str(TREASURY_USER_ID), parent[2], parent[3], organization_code),
            )
            conn.execute(
                "INSERT INTO share_issuances "
                "(legacy_product_code,organization_code,class_name,share_class,voting_rights_per_share,"
                "economic_interest_per_share,updated_epoch) VALUES (?,?,?,'preferred',?,?,?) "
                "ON CONFLICT(legacy_product_code) DO UPDATE SET class_name=excluded.class_name,"
                "voting_rights_per_share=excluded.voting_rights_per_share,"
                "economic_interest_per_share=excluded.economic_interest_per_share,updated_epoch=excluded.updated_epoch",
                (code, organization_code, class_name, str(votes), str(equity), now),
            )
            seller = CurrencyManager.exchange_treasury(conn, code)
            conn.execute("UPDATE company SET max_shareholder_id=? WHERE company_code=?", (seller, code))
            AssetCostManager.upsert(conn, seller, code, None, shares, price)
            order_id = conn.execute(
                "INSERT INTO market_orders(user_id,asset_code,side,limit_price,quantity,remaining_quantity,"
                "order_kind,status,created_epoch,updated_epoch,quote_currency) "
                "VALUES (?,?,'sell',?,?,?,'asset','open',?,?,?)",
                (seller, code, price, shares, shares, now, now, parent[3]),
            ).lastrowid
            conn.execute(
                "INSERT INTO corporate_equity_offerings(asset_code,order_id,quantity,issue_price,recorded_epoch) "
                "VALUES (?,?,?,?,?)", (code, order_id, shares, price, now),
            )
            conn.execute("RELEASE SAVEPOINT organization_share_issuance_create")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT organization_share_issuance_create")
            conn.execute("RELEASE SAVEPOINT organization_share_issuance_create")
            raise
        return {"action": kind, "organization_code": organization_code, "legacy_product_code": code,
                "class_name": class_name, "total_shares": shares, "share_price": price,
                "initial_offering_order": order_id}
    if kind == "issuance_rights":
        code = str(payload.get("legacy_product_code") or "")
        class_name = payload.get("class_name")
        if class_name is not None:
            class_name = str(class_name).strip()
            if not class_name or len(class_name) > 120:
                raise ValueError("주식 종류 이름은 1~120자로 입력해주세요.")
        try:
            votes = Decimal(str(payload.get("voting_rights_per_share")))
            equity = Decimal(str(payload.get("economic_interest_per_share")))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("주당 권리 수치를 확인해주세요.") from None
        if (not votes.is_finite() or not equity.is_finite() or votes < 0
                or votes > Decimal("1e12") or equity <= 0 or equity > Decimal("1e12")):
            raise ValueError("의결권은 0~1조, 경제적 지분계수는 0 초과~1조 범위여야 합니다.")
        cur = conn.execute(
            "UPDATE share_issuances SET voting_rights_per_share=?,economic_interest_per_share=?,"
            "updated_epoch=? WHERE issuance_id=COALESCE(?,(SELECT issuance_id FROM share_issuances WHERE legacy_product_code=?))",
            (str(votes), str(equity), now, payload.get("issuance_id"), code),
        )
        if cur.rowcount != 1:
            raise ValueError("주식 발행 종류를 찾을 수 없습니다.")
        if class_name is not None:
            conn.execute(
                "UPDATE share_issuances SET class_name=?,updated_epoch=? WHERE issuance_id=COALESCE(?,(SELECT issuance_id FROM share_issuances WHERE legacy_product_code=?))",
                (class_name, now, payload.get("issuance_id"), code),
            )
        company = conn.execute(
            "SELECT share_class,dividend_priority,dividend_mode,dividend_per_share,dividend_percent,"
            "dividend_cycle_seconds,dividend_is_active,cumulative_dividend,liquidation_value_per_share,"
            "dividend_funding,quote_currency FROM company WHERE company_code=?", (code,)
        ).fetchone()
        if not company:
            raise ValueError("기존 주식 발행 자료를 찾을 수 없습니다.")
        rights = payload.get("rights", {})
        if not isinstance(rights, dict) or set(rights) - {
            "dividend_mode", "dividend_per_share", "dividend_percent", "dividend_priority",
            "dividend_cycle_seconds", "dividend_is_active", "cumulative_dividend",
            "liquidation_value_per_share", "dividend_funding",
        }:
            raise ValueError("주식 권리 설정 항목을 확인해주세요.")
        current = {
            "dividend_mode": company[2], "dividend_per_share": company[3],
            "dividend_percent": company[4], "dividend_cycle_seconds": company[5],
            "dividend_is_active": company[6], "cumulative_dividend": company[7],
            "liquidation_value_per_share": company[8], "dividend_funding": company[9],
            "dividend_priority": company[1],
        }
        current.update(rights)
        if current["dividend_mode"] not in {"fixed", "percent"}:
            raise ValueError("배당 방식은 fixed 또는 percent입니다.")
        if current["dividend_funding"] not in {"treasury", "issuer"}:
            raise ValueError("배당 재원은 treasury 또는 issuer입니다.")
        try:
            dividend = Decimal(str(current["dividend_per_share"]))
            dividend_percent = Decimal(str(current["dividend_percent"]))
            liquidation = Decimal(str(current["liquidation_value_per_share"]))
            priority = int(current["dividend_priority"])
            cycle = int(current["dividend_cycle_seconds"])
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            raise ValueError("배당·청산 권리 수치를 확인해주세요.") from None
        if (not all(value.is_finite() and value >= 0 for value in (dividend, dividend_percent, liquidation))
                or any(value > Decimal("1e24") for value in (dividend, liquidation))
                or dividend_percent > 100 or priority < 0 or priority > 1_000_000
                or cycle < 0 or cycle > 315_360_000
                or isinstance(current["dividend_priority"], bool) or str(priority) != str(current["dividend_priority"]).strip()
                or isinstance(current["dividend_cycle_seconds"], bool) or str(cycle) != str(current["dividend_cycle_seconds"]).strip()):
            raise ValueError("배당·청산 조건의 범위를 확인해주세요.")
        if current["dividend_funding"] == "issuer":
            from functions.money import base_currency_code
            if str(company[10] or "").upper() != base_currency_code(conn):
                raise ValueError("법인 현금 재원 배당은 기준 통화 주식만 지원합니다.")
        if priority != int(company[1] or 0):
            unpaid = conn.execute(
                "SELECT 1 FROM preferred_dividend_claims WHERE asset_code=? AND status IN ('unpaid','partial') LIMIT 1",
                (code,),
            ).fetchone()
            if unpaid:
                raise ValueError("미지급 우선배당 청구가 있어 배당 순위를 바꿀 수 없습니다.")
        for key in ("dividend_is_active", "cumulative_dividend"):
            value = current[key]
            if isinstance(value, bool):
                current[key] = int(value)
            elif value not in (0, 1, "0", "1"):
                raise ValueError("배당 활성·누적 설정은 참 또는 거짓이어야 합니다.")
            else:
                current[key] = int(value)
        conn.execute(
            "UPDATE company SET dividend_mode=?,dividend_per_share=?,dividend_percent=?,"
            "dividend_priority=?,dividend_cycle_seconds=?,dividend_is_active=?,cumulative_dividend=?,"
            "liquidation_value_per_share=?,dividend_funding=?,dividend_next_epoch=? WHERE company_code=?",
            (current["dividend_mode"], str(dividend), str(dividend_percent), priority, cycle,
             current["dividend_is_active"], current["cumulative_dividend"], str(liquidation),
             current["dividend_funding"],
             int(time.time()) + cycle if current["dividend_is_active"] and cycle > 0
             and ((current["dividend_mode"] == "fixed" and dividend > 0)
                  or (current["dividend_mode"] == "percent" and dividend_percent > 0)) else None,
             code),
        )
        return {"action": kind, "legacy_product_code": code,
                "voting_rights_per_share": str(votes), "economic_interest_per_share": str(equity),
                "rights": {key: str(value) for key, value in current.items()}}
    raise ValueError("조직 관리 작업을 확인해주세요.")
