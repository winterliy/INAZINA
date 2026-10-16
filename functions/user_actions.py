"""Discord user commands as plain actions, so the HTS can offer every one of them.

Each entry mirrors the slash command of the same name and calls the same
managers. Commands that must send Discord DMs (/파산, /주총소집) are returned as
bot jobs and processed by the running bot (functions/web_jobs.py).
"""
import json
import uuid
from decimal import Decimal

from functions.shared_actions import execute_shared_action

# (key, label, kind, optional, hint) — kind: text, number, select:<a,b>, password
USER_ACTIONS = {
    "매수": ("거래", "지정가로 사거나 펀드를 NAV로 가입합니다", [("asset", "종목", "text", False, ""), ("quantity", "수량", "number", False, "소수 가능 종목은 0.001 단위"), ("price", "지정가", "number", True, "펀드는 비우기")]),
    "매도": ("거래", "지정가로 팔거나 펀드를 NAV로 환매합니다", [("asset", "종목", "text", False, ""), ("quantity", "수량", "number", False, ""), ("price", "지정가", "number", True, "펀드는 비우기")]),
    "주문취소": ("거래", "미체결 주문을 취소하고 예치금·수량을 돌려받습니다", [("order", "주문 번호", "number", False, "내주문의 # 번호")]),
    "내주문": ("거래", "최근 주문과 미체결 주문을 봅니다", []),
    "호가": ("거래", "종목의 매수·매도 대기 주문을 봅니다", [("asset", "종목", "text", False, "")]),
    "송금": ("돈", "다른 사용자·계좌로 보내거나 내 외환통장으로 보내 환전합니다", [("recipient", "받는 사람 ID 또는 계좌", "text", False, ""), ("amount", "금액", "number", False, "출금 통화 기준"), ("source", "출금 계좌", "text", True, "비우면 지갑")]),
    "도박": ("돈", "금액을 걸고 도박합니다", [("amount", "금액", "number", False, "")]),
    "계좌개설": ("은행", "저축·외환통장 상품으로 계좌를 엽니다", [("product", "상품 코드", "text", False, ""), ("password", "계좌 비밀번호", "password", True, "외환통장은 비우기"), ("deposit", "예치금", "number", True, "외환통장은 0")]),
    "계좌해지": ("은행", "통장을 해지합니다", [("account", "계좌 번호", "text", False, ""), ("password", "계좌 비밀번호", "password", True, "외환통장은 비우기"), ("destination", "입금 계좌", "text", True, "비우면 지갑")]),
    "대출신청": ("은행", "대출 상품으로 대출을 받습니다", [("product", "대출 상품 코드", "text", False, ""), ("amount", "금액", "number", False, "")]),
    "대출상환": ("은행", "대출을 상환합니다", [("product", "대출 상품 코드", "text", False, ""), ("amount", "상환액", "number", False, ""), ("loan", "대출 번호", "number", True, "여러 건이면 지정")]),
    "대출연장": ("은행", "대출 만기를 연장합니다", [("loan", "대출 번호 또는 상품 코드", "text", False, "")]),
    "선물증거금": ("파생", "보유 선물 포지션에 증거금을 더합니다", [("future", "선물 코드", "text", False, ""), ("amount", "금액", "number", False, "")]),
    "옵션체인": ("파생", "거래 가능한 콜·풋 옵션을 봅니다", [("underlying", "기초 주식", "text", True, "비우면 전체")]),
    "옵션작성": ("파생", "담보를 맡기고 옵션 매도 호가를 냅니다", [("underlying", "기초 주식", "text", False, ""), ("type", "유형", "select:call,put", False, ""), ("strike", "행사가", "number", False, ""), ("days", "만기일수", "number", False, "1~365"), ("contracts", "계약수", "number", False, ""), ("premium", "지정 프리미엄", "number", False, ""), ("volatility", "변동성 %", "number", True, "기본 50")]),
    "복권구매": ("복권", "주간 6/45 복권을 삽니다", [("quantity", "장 수", "number", False, "1~1000"), ("numbers", "번호", "text", True, "비우면 자동 · 예: 1,2,3,4,5,6")]),
    "내복권": ("복권", "내 복권 번호와 당첨 결과를 봅니다", [("round", "회차", "number", True, "비우면 현재")]),
    "복권결과": ("복권", "회차별 당첨번호와 상금을 봅니다", [("round", "회차", "number", True, "비우면 최근")]),
    "복권규칙": ("복권", "현재 회차의 가격·마감·상금 배분을 봅니다", []),
    "법정최고이자": ("정보", "국가별 법정 최고 이율을 봅니다", [("country", "국가", "text", False, "")]),
    "재무": ("정보", "회사·우선주의 재무제표를 봅니다", [("product", "회사 코드", "text", False, "")]),
    "파산": ("요청", "자산 현황과 함께 관리자에게 파산을 신청합니다", []),
    "주총소집": ("요청", "최대주주가 주주총회를 소집해 주주에게 알립니다", [("code", "회사 코드", "text", False, ""), ("place", "장소", "text", False, "")]),
}
JOB_ACTIONS = {"파산", "주총소집"}


def catalog():
    return [{"name": name, "group": group, "description": description,
             "fields": [{"key": key, "label": label, "kind": kind, "optional": optional, "hint": hint}
                        for key, label, kind, optional, hint in fields],
             "job": name in JOB_ACTIONS}
            for name, (group, description, fields) in USER_ACTIONS.items()]


def parse_values(command, values):
    if command not in USER_ACTIONS:
        raise ValueError("알 수 없는 명령어입니다.")
    fields = USER_ACTIONS[command][2]
    if not isinstance(values, dict) or set(values) - {key for key, *_ in fields}:
        raise ValueError("입력값을 확인해주세요.")
    parsed = {}
    for key, label, kind, optional, _hint in fields:
        value = values.get(key, "")
        if not isinstance(value, str) or len(value) > 2000:
            raise ValueError("입력값을 확인해주세요.")
        value = value.strip()
        if not value and not optional:
            raise ValueError(f"{label}을(를) 입력해주세요.")
        if kind.startswith("select:") and value and value not in kind[7:].split(","):
            raise ValueError(f"{label}을(를) 확인해주세요.")
        parsed[key] = value
    return parsed


def _integer(value, label):
    try:
        number = Decimal(value)
    except Exception:
        raise ValueError(f"{label}은(는) 숫자로 입력해주세요.") from None
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError(f"{label}은(는) 정수로 입력해주세요.")
    return int(number)


def _text(result):
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool):
        return result[0], str(result[1])
    if hasattr(result, "keys") and not isinstance(result, dict):
        result = dict(result)
    if isinstance(result, (dict, list)):
        return True, json.dumps(result, ensure_ascii=False, indent=2, default=lambda row: dict(row))
    return not str(result).startswith("❌"), str(result)


def execute(bot, user_id, command, values):
    """Run a user command for ``user_id`` (the trading identity); returns (success, message)."""
    from managers.investment_manager import FUND_TYPES
    v = parse_values(command, values)
    if command in {"매수", "매도"}:
        asset = bot.market_mgr.get_asset_info(v["asset"])
        if asset and asset.get("type") in FUND_TYPES:
            action = "fund_subscribe" if command == "매수" else "fund_redeem"
            return _text(execute_shared_action(bot, user_id, action, v["asset"], v["quantity"]))
        if not v["price"]:
            raise ValueError("펀드가 아닌 자산은 지정가를 입력해주세요.")
        return _text(execute_shared_action(bot, user_id, "buy" if command == "매수" else "sell",
                                           v["asset"], v["quantity"], v["price"]))
    if command == "주문취소":
        return _text(bot.order_book.cancel_order(user_id, _integer(v["order"], "주문 번호")))
    if command == "내주문":
        return _text([dict(row) for row in bot.order_book.get_user_orders(user_id)])
    if command == "호가":
        return _text(bot.order_book.get_order_book(v["asset"]))
    if command == "송금":
        return _text(execute_shared_action(bot, user_id, "transfer", v["recipient"], v["amount"], v["source"]))
    if command == "도박":
        return _text(bot.user_mgr.gamble(user_id, _integer(v["amount"], "금액")))
    if command == "계좌개설":
        is_fx = bot.db.products.execute_one(
            "SELECT 1 FROM saving WHERE product_code=? AND product_kind='currency_saving' AND status='active'", (v["product"],))
        if is_fx:
            if v["deposit"] and _integer(v["deposit"], "예치금") != 0:
                raise ValueError("외환통장 초기 예치금은 0으로 두고 송금으로 입금해주세요.")
            return True, "✅ 외환통장: " + str(bot.currency_mgr.open_account(user_id, v["product"]))
        return _text(bot.bank_mgr.open_account(user_id, v["product"], v["password"], _integer(v["deposit"] or "0", "예치금")))
    if command == "계좌해지":
        return _text(bot.bank_mgr.close_account(user_id, v["account"], v["destination"] or None, v["password"]))
    if command == "대출신청":
        return _text(bot.bank_mgr.apply_loan(user_id, v["product"], _integer(v["amount"], "금액")))
    if command == "대출상환":
        return _text(bot.bank_mgr.repay_loan(user_id, v["product"], _integer(v["amount"], "상환액"),
                                             _integer(v["loan"], "대출 번호") if v["loan"] else None))
    if command == "대출연장":
        return _text(bot.bank_mgr.extend_loan(user_id, v["loan"]))
    if command == "선물증거금":
        return _text(bot.futures_mgr.add_margin(user_id, v["future"], _integer(v["amount"], "금액")))
    if command == "옵션체인":
        return _text([dict(row) for row in bot.option_mgr.get_active_contracts(v["underlying"] or None, limit=25)])
    if command == "옵션작성":
        return _text(execute_shared_action(bot, user_id, "option_write", (
            v["underlying"], v["type"], _integer(v["strike"], "행사가"), _integer(v["days"], "만기일수"),
            _integer(v["contracts"], "계약수"), _integer(v["premium"], "지정 프리미엄"),
            _integer(v["volatility"] or "50", "변동성"))))
    if command == "복권구매":
        current = bot.lottery_mgr.current()
        numbers = v["numbers"] or "자동"
        bot.lottery_mgr.numbers(numbers)
        return _text(bot.lottery_mgr.purchase(user_id, _integer(v["quantity"], "장 수"), numbers,
                                              uuid.uuid4().hex, expected_round=current["round_id"]))
    if command == "내복권":
        return _text([dict(row) for row in bot.lottery_mgr.tickets(user_id, _integer(v["round"], "회차") if v["round"] else None)])
    if command == "복권결과":
        return _text(bot.lottery_mgr.result(_integer(v["round"], "회차") if v["round"] else None))
    if command == "복권규칙":
        return _text(bot.lottery_mgr.current())
    if command == "법정최고이자":
        return True, f"{v['country']} 법정최고이자: 연 {bot.rate_mgr.get_rate('legal_max_rate', v['country']):g}%"
    if command == "재무":
        from managers.financial_manager import FinancialManager
        return _text(FinancialManager(bot.db).report(v["product"]))
    raise ValueError("이 명령어는 Discord 봇이 처리합니다.")
