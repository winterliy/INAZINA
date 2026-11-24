"""Optional mixed/stepped rate schedules on loan and deposit contracts (M7). Off unless an administrator enables one on a product.

A product with an enabled schedule gives each NEW contract the schedule's day-0 rate and a private snapshot of the schedule.
Existing contracts and products without a schedule are never touched. A snapshot contract is stored as ``floating`` with its
own reset epochs (segment boundaries, plus the reset cycle inside floating segments); InterestManager settles interest at the
old rate up to each reset, recasts annuity/equal-principal repayment tables and records the change in the reset history.
"""
from functions.rate_schedule import RateSchedule
from managers.rate_manager import RateManager

TABLE = {"loan": "liability", "deposit": "saving"}


def set_product_schedule(conn, *, product_type, product_code, schedule, enabled, actor, now):
    if product_type not in TABLE or type(enabled) is not bool or not isinstance(schedule, dict):
        raise ValueError("invalid rate schedule")
    if not conn.execute(f"SELECT 1 FROM {TABLE[product_type]} WHERE product_code=?", (str(product_code),)).fetchone():
        raise ValueError("product not found")
    parsed = RateSchedule.parse(schedule.get("segments"), floor=schedule.get("floor"), cap=schedule.get("cap"),
                                max_change_per_reset=schedule.get("max_change_per_reset"), rounding_step=schedule.get("rounding_step"))
    if len(parsed.segment_boundaries()) > 20:
        raise ValueError("too many rate segments")
    conn.execute("INSERT INTO product_rate_schedules(product_type,product_code,schedule_json,enabled,updated_by,updated_epoch) VALUES (?,?,?,?,?,?) "
                 "ON CONFLICT(product_type,product_code) DO UPDATE SET schedule_json=excluded.schedule_json,enabled=excluded.enabled,"
                 "updated_by=excluded.updated_by,updated_epoch=excluded.updated_epoch",
                 (product_type, str(product_code), parsed.to_json(), int(enabled), str(actor), int(now)))
    return parsed.to_json()


def plan_new_contract(conn, product_type, product_code, nationality, epoch, *, legal_cap=None):
    """The day-0 terms for a new contract under an enabled schedule, or None (the product has no active schedule)."""
    row = conn.execute("SELECT schedule_json FROM product_rate_schedules WHERE product_type=? AND product_code=? AND enabled=1",
                       (product_type, str(product_code))).fetchone()
    if not row:
        return None
    if not nationality:
        raise ValueError("금리 일정 상품에는 기준금리 적용 국적이 필요합니다.")
    schedule = RateSchedule.from_json(row["schedule_json"])
    product = conn.execute(f"SELECT * FROM {TABLE[product_type]} WHERE product_code=?", (str(product_code),)).fetchone()
    tenor = int(product["benchmark_tenor_days"] or product["maturity_days"] or 0)
    benchmark = product["benchmark_key"] or "policy"
    reference = (RateManager.market_rate(conn, nationality, benchmark, tenor, epoch=epoch)
                 if schedule.kind_at(0) == "floating" else None)
    rate = float(schedule.rate_at(0, reference_rate=reference))
    if legal_cap is not None:
        rate = min(rate, float(legal_cap))
    cycle = int(product["repricing_cycle_seconds"] or product["interest_cycle"] or 86400)
    return {"json": schedule.to_json(), "rate": rate, "start_epoch": int(epoch), "benchmark_key": benchmark,
            "tenor_days": tenor, "cycle": max(1, cycle), "schedule": schedule,
            "interest_cycle": float(product["interest_cycle"] or 31536000)}


def first_reset_epoch(plan):
    """The first epoch after signing at which the rate may change, or None."""
    boundaries = [d for d in plan["schedule"].segment_boundaries() if d > 0]
    candidates = [plan["start_epoch"] + d * 86400 for d in boundaries]
    if plan["schedule"].kind_at(0) == "floating":
        candidates.append(plan["start_epoch"] + plan["cycle"])
    return min(candidates) if candidates else None


def attach(conn, contract_type, contract_id, plan, nationality, now):
    """Snapshot the schedule on the contract and make it follow its own reset epochs."""
    conn.execute("INSERT INTO contract_rate_schedules(contract_type,contract_id,schedule_json,start_epoch,created_epoch) VALUES (?,?,?,?,?)",
                 (contract_type, str(contract_id), plan["json"], plan["start_epoch"], int(now)))
    table, key = (("loans", "loan_id") if contract_type == "loan" else ("accounts", "account_number"))
    conn.execute(f"UPDATE {table} SET rate_type='floating',rate_nationality=?,next_repricing_epoch=?,repricing_cycle_seconds=?,"
                 f"benchmark_key=?,benchmark_tenor_days=?,base_rate_spread=0,rate_floor=-100,rate_cap=NULL WHERE {key}=?",
                 (nationality, first_reset_epoch(plan), plan["cycle"], plan["benchmark_key"], plan["tenor_days"], contract_id))
    if contract_type == "account":
        conn.execute("UPDATE accounts SET contract_rate=?,contract_effective_rate=? WHERE account_number=?",
                     (plan["rate"], RateManager.effective_annual_rate(plan["rate"], plan["interest_cycle"]), contract_id))


def snapshot(conn, contract_type, contract_id):
    """``(RateSchedule, start_epoch)`` of a contract signed under a schedule, else None."""
    row = conn.execute("SELECT schedule_json,start_epoch FROM contract_rate_schedules WHERE contract_type=? AND contract_id=?",
                       (contract_type, str(contract_id))).fetchone()
    return (RateSchedule.from_json(row["schedule_json"]), int(row["start_epoch"])) if row else None


def notify_resets(conn, contract_type, contract_id, user_id, resets, kind_label):
    """Tell the contract holder (bell inbox, once per reset) that a rate schedule changed their rate; no-op without a snapshot."""
    if not resets or snapshot(conn, contract_type, contract_id) is None:
        return 0
    from managers.notification_manager import NotificationManager
    sent = 0
    for old_rate, new_rate, scheduled_epoch, _observation in resets:
        if abs(float(new_rate) - float(old_rate)) < 1e-12:
            continue
        added = NotificationManager.add(
            conn, user_id, "rate_schedule", f"{kind_label} 금리가 바뀌었습니다",
            f"계약 금리 일정에 따라 연 {float(old_rate):g}% → 연 {float(new_rate):g}%로 변경되었습니다.",
            payload={"contract_type": contract_type, "contract_id": str(contract_id), "old_rate": float(old_rate),
                     "new_rate": float(new_rate), "scheduled_epoch": int(scheduled_epoch)},
            dedupe_key=f"rate-reset:{contract_type}:{contract_id}:{int(scheduled_epoch)}", now=int(scheduled_epoch))
        sent += 1 if added else 0
    return sent
