"""Discord user commands as plain actions, so the HTS can offer every one of them.

Each entry mirrors the slash command of the same name and calls the same
managers. Commands that must send Discord DMs (/파산, /주총소집) are returned as
bot jobs and processed by the running bot (functions/web_jobs.py).
"""
import json
import uuid
from decimal import Decimal

from functions.shared_actions import execute_shared_action

# Field: (key, label, kind, optional, hint, source)
#   kind   — text, number, password, select:<a,b> (shown as buttons)
#   source — list the HTS offers to pick from: instruments, funds, stocks, futures,
#            orders, accounts, fx_products, saving_products, loan_products, loans, countries
USER_ACTIONS = {
    "매수": ("거래", "지정가로 사거나 펀드를 기준가(NAV)로 가입합니다.",
            "종목을 고르고 수량과 지정가를 넣으세요. 펀드는 지정가 없이 좌수만 넣으면 기준가로 가입합니다. 소수점 거래를 허용한 종목은 0.001 단위까지 살 수 있어요.",
            [("asset", "종목", "text", False, "", "instruments"), ("quantity", "수량", "number", False, "소수 가능 종목은 0.001 단위", None), ("price", "지정가", "number", True, "펀드는 비워 두세요", None)]),
    "매도": ("거래", "지정가로 팔거나 펀드를 기준가로 환매합니다.",
            "보유 종목 중에서 고르세요. 이미 매도 주문에 걸린 수량과 담보로 잡힌 주식은 팔 수 없어요.",
            [("asset", "종목", "text", False, "", "holdings"), ("quantity", "수량", "number", False, "", None), ("price", "지정가", "number", True, "펀드는 비워 두세요", None)]),
    "주문취소": ("거래", "미체결 주문을 취소합니다.",
             "취소하면 매수 예치금이나 매도 대기 수량을 바로 돌려받아요. 주문·체결 표의 취소 버튼과 같습니다.",
             [("order", "취소할 주문", "number", False, "", "orders")]),
    "내주문": ("거래", "최근 주문과 미체결 주문을 봅니다.", "최근 주문 25건을 보여 줍니다.", []),
    "호가": ("거래", "종목의 매수·매도 대기 주문을 봅니다.", "가격별로 쌓인 매수·매도 수량을 보여 줍니다.",
           [("asset", "종목", "text", False, "", "instruments")]),
    "송금": ("돈", "다른 사람이나 계좌로 돈을 보냅니다.",
           "받는 사람의 아이디 또는 계좌번호를 넣으세요. 내 외환통장 번호를 넣으면 그 통화로 환전돼요. 금액은 출금하는 통화 기준입니다.",
           [("recipient", "받는 사람 ID 또는 계좌", "text", False, "내 외환통장이면 환전", "accounts"), ("amount", "금액", "number", False, "출금 통화 기준", None), ("source", "출금 계좌", "text", True, "비우면 지갑에서", "accounts")]),
    "도박": ("돈", "금액을 걸고 도박합니다.", "",
           [("amount", "건 금액", "number", False, "", None), ("source", "출금 계좌", "text", True, "비우면 자동 · 현금은 __wallet__", "accounts")]),
    "계좌개설": ("은행", "저축 상품이나 외환통장으로 계좌를 엽니다.",
             "저축과 외환통장 모두 비밀번호와 상품의 최소 예치금이 필요해요. 예치금은 상품 통화 기준이고, 외환통장은 현재 환율로 내 현금에서 빠져나가요.",
             [("product", "상품", "text", False, "", "saving_products"), ("password", "계좌 비밀번호", "password", False, "4~16자", None), ("deposit", "예치금", "number", True, "상품 통화 기준 · 비우면 0", None)]),
    "계좌해지": ("은행", "통장을 해지하고 잔액을 돌려받습니다.",
             "만기 전에 해지하면 상품에 따라 중도해지 이율이나 수수료가 적용될 수 있어요. 외환통장 잔액은 받을 계좌가 없으면 현재 환율로 내 현금에 들어와요.",
             [("account", "해지할 계좌", "text", False, "", "accounts"), ("password", "계좌 비밀번호", "password", False, "", None), ("destination", "받을 계좌", "text", True, "비우면 지갑으로", "accounts")]),
    "대출신청": ("은행", "대출 상품으로 돈을 빌립니다.",
             "상품마다 한도, 최소 신용점수, 이율, 만기가 달라요. 소득 대비 상환 부담(DTI·DSR)도 심사합니다.",
             [("product", "대출 상품", "text", False, "", "loan_products"), ("amount", "금액", "number", False, "", None)]),
    "대출상환": ("은행", "대출을 갚습니다.", "같은 상품 대출이 여러 건이면 대출 번호를 고르세요.",
             [("product", "대출 상품", "text", False, "", "loan_products"), ("amount", "상환액", "number", False, "", None), ("loan", "대출 번호", "number", True, "여러 건일 때만", "loans")]),
    "대출연장": ("은행", "대출 만기를 연장합니다.", "상품의 연장 신용 기준을 넘어야 하고, 이전 만기가 지난 뒤 다시 연장할 수 있어요.",
             [("loan", "대출", "text", False, "", "loans")]),
    "선물증거금": ("파생", "보유 선물 포지션에 증거금을 더합니다.", "유지 증거금 아래로 떨어지면 청산될 수 있어요. 여유 있게 넣어 두세요.",
              [("future", "선물", "text", False, "", "futures"), ("amount", "추가할 금액", "number", False, "", None)]),
    "옵션체인": ("파생", "거래할 수 있는 콜·풋 옵션을 봅니다.", "기초 주식을 고르면 그 종목 옵션만 보여 줍니다.",
             [("underlying", "기초 주식", "text", True, "비우면 전체", "stocks")]),
    "옵션작성": ("파생", "담보를 맡기고 옵션 매도 호가를 냅니다.",
             "콜은 기초 주식을, 풋은 행사가만큼 현금을 담보로 맡겨요. 만기까지 의무가 남고 프리미엄은 체결될 때 받습니다.",
             [("underlying", "기초 주식", "text", False, "", "stocks"), ("type", "유형", "select:call,put", False, "call = 살 권리 · put = 팔 권리", None), ("strike", "행사가", "number", False, "", None), ("days", "만기일수", "number", False, "1~365일", None), ("contracts", "계약수", "number", False, "", None), ("premium", "지정 프리미엄", "number", False, "계약당 받을 가격", None), ("volatility", "변동성 %", "number", True, "기본 50", None)]),
    "전환청구": ("채권", "전환사채(CB)를 발행회사 주식으로 바꿉니다.",
             "액면 ÷ 전환가격만큼 신주를 받고 사채는 소멸해요. 1주가 안 되는 단주는 회사가 현금으로 줍니다.",
             [("bond", "전환사채", "text", False, "", "holdings"), ("quantity", "사채 수량", "number", False, "", None)]),
    "신주인수권행사": ("채권", "신주인수권부사채(BW)의 신주인수권을 행사합니다.",
                "사채 1매당 액면 ÷ 행사가격만큼 신주를 행사가로 삽니다. 대금은 지갑·통장에서 나가고 사채는 그대로 남아요. 1매당 한 번만 행사할 수 있어요.",
                [("bond", "신주인수권부사채", "text", False, "", "holdings"), ("quantity", "사채 수량", "number", False, "", None)]),
    "스왑": ("파생", "국고와 금리스왑(고정↔변동)을 체결·해지하거나 내 스왑을 봅니다.",
           "고정지급은 고정금리를 내고 기준금리를 받아 금리 상승에 대비해요. 고정수취는 그 반대입니다. 명목금액의 5%를 증거금으로 맡기고 매일 차액을 정산해요. 해지하면 남은 기간의 금리 차이를 현재 시세로 정산합니다.",
           [("action", "작업", "select:체결,해지,조회", False, "", None), ("side", "방향", "select:고정지급,고정수취", True, "체결 시 · 고정지급 = 금리 상승 때 이익", None), ("notional", "명목금액", "number", True, "체결 시", None), ("days", "만기일수", "number", True, "체결 시 1~3650일", None), ("swap", "스왑 번호", "number", True, "해지 시", None)]),
    "랩주문": ("운용", "내가 운용하는 랩어카운트로 주문합니다.",
            "고객 랩 계좌의 현금과 보유자산으로만 사고팔아요. 계좌 통화로 결제하는 종목만 가능합니다.",
            [("account", "랩 계좌번호", "text", False, "", None), ("asset", "종목", "text", False, "", "instruments"), ("side", "방향", "select:buy,sell", False, "buy = 매수 · sell = 매도", None), ("quantity", "수량", "number", False, "", None), ("price", "지정가", "number", False, "", None)]),
    "운용계좌": ("운용", "내가 운용하는 랩어카운트 목록을 봅니다.", "", []),
    "복권구매": ("복권", "주간 6/45 복권을 삽니다.", "번호를 비우면 자동으로 뽑아요. 직접 고를 때는 1~45 중 서로 다른 6개를 쉼표로 적으세요.",
             [("quantity", "장 수", "number", False, "1~1000", None), ("numbers", "번호", "text", True, "비우면 자동 · 예: 3,11,19,27,35,42", None), ("source", "출금 계좌", "text", True, "비우면 자동 · 현금은 __wallet__", "accounts")]),
    "내복권": ("복권", "내 복권 번호와 당첨 결과를 봅니다.", "회차를 비우면 현재 회차를 보여 줍니다.",
            [("round", "회차", "number", True, "비우면 현재", None)]),
    "복권결과": ("복권", "회차별 당첨번호와 상금을 봅니다.", "회차를 비우면 가장 최근 추첨 결과를 보여 줍니다.",
             [("round", "회차", "number", True, "비우면 최근", None)]),
    "복권규칙": ("복권", "현재 회차의 가격·마감·상금 배분을 봅니다.", "", []),
    "법정최고이자": ("정보", "국가별 법정 최고 이율을 봅니다.", "대출 이율은 이 상한을 넘을 수 없어요.",
               [("country", "국가", "text", False, "", "countries")]),
    "재무": ("정보", "회사·우선주의 재무제표를 봅니다.", "자산·부채·자본과 손익, 주요 비율을 보여 줍니다.",
           [("product", "회사", "text", False, "", "stocks")]),
    "파산": ("요청", "관리자에게 파산을 신청합니다.", "현재 자산 현황이 관리자에게 Discord DM으로 전달돼요. 실행 중인 봇이 처리합니다.", []),
    "주총소집": ("요청", "주주총회를 소집해 주주에게 알립니다.", "최대주주만 소집할 수 있어요. 주주들에게 Discord DM으로 참석 여부를 묻습니다.",
             [("code", "회사", "text", False, "", "holdings"), ("place", "장소", "text", False, "예: 국가 회의실", None)]),
}
JOB_ACTIONS = {"파산", "주총소집"}


def catalog():
    return [{"name": name, "group": group, "description": description, "guide": guide,
             "fields": [{"key": key, "label": label, "kind": kind, "optional": optional, "hint": hint, "source": source}
                        for key, label, kind, optional, hint, source in fields],
             "job": name in JOB_ACTIONS}
            for name, (group, description, guide, fields) in USER_ACTIONS.items()]


def parse_values(command, values):
    if command not in USER_ACTIONS:
        raise ValueError("알 수 없는 명령어입니다.")
    fields = USER_ACTIONS[command][3]
    if not isinstance(values, dict) or set(values) - {key for key, *_ in fields}:
        raise ValueError("입력값을 확인해주세요.")
    parsed = {}
    for key, label, kind, optional, _hint, _source in fields:
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


def _plain(value):
    if hasattr(value, "keys") and not isinstance(value, dict):
        value = dict(value)
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, memoryview)):
        return None
    return value


def _text(result):
    """(success, message, data): data carries lists/records for the HTS to lay out."""
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool):
        return result[0], str(result[1]), None
    if hasattr(result, "keys") or isinstance(result, (dict, list)):
        data = _plain(result)
        return True, json.dumps(data, ensure_ascii=False, indent=2, default=str), data
    return not str(result).startswith("❌"), str(result), None


def execute(bot, user_id, command, values):
    """Run a user command for ``user_id`` (the trading identity); returns (success, message, data)."""
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
        return _text(bot.user_mgr.gamble(user_id, _integer(v["amount"], "금액"), source=v.get("source") or None))
    if command == "계좌개설":
        return _text(bot.bank_mgr.open_account(user_id, v["product"], v["password"], v["deposit"] or "0"))
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
    if command == "전환청구":
        return _text(bot.capital_markets.convert_bond(user_id, v["bond"], _integer(v["quantity"], "사채 수량")))
    if command == "신주인수권행사":
        return _text(bot.capital_markets.exercise_warrant(user_id, v["bond"], _integer(v["quantity"], "사채 수량")))
    if command == "스왑":
        if v["action"] == "체결":
            if not (v["side"] and v["notional"] and v["days"]):
                raise ValueError("체결에는 방향·명목금액·만기일수가 필요합니다.")
            return _text(bot.capital_markets.open_swap(user_id, _integer(v["notional"], "명목금액"), v["side"], _integer(v["days"], "만기일수")))
        if v["action"] == "해지":
            if not v["swap"]:
                raise ValueError("해지할 스왑 번호를 입력해주세요.")
            return _text(bot.capital_markets.close_swap(user_id, _integer(v["swap"], "스왑 번호")))
        return _text(bot.capital_markets.swaps(user_id))
    if command == "랩주문":
        return _text(bot.capital_markets.wrap_order(user_id, v["account"], v["asset"], v["side"],
                                                    _integer(v["quantity"], "수량"), _integer(v["price"], "지정가")))
    if command == "운용계좌":
        return _text(bot.capital_markets.managed_accounts(user_id))
    if command == "복권구매":
        current = bot.lottery_mgr.current()
        numbers = v["numbers"] or "자동"
        bot.lottery_mgr.numbers(numbers)
        return _text(bot.lottery_mgr.purchase(user_id, _integer(v["quantity"], "장 수"), numbers,
                                              uuid.uuid4().hex, expected_round=current["round_id"], source=v.get("source") or None))
    if command == "내복권":
        return _text([dict(row) for row in bot.lottery_mgr.tickets(user_id, _integer(v["round"], "회차") if v["round"] else None)])
    if command == "복권결과":
        return _text(bot.lottery_mgr.result(_integer(v["round"], "회차") if v["round"] else None))
    if command == "복권규칙":
        return _text(bot.lottery_mgr.current())
    if command == "법정최고이자":
        rate = bot.rate_mgr.get_rate('legal_max_rate', v['country'])
        return True, f"{v['country']} 법정최고이자: 연 {rate:g}%", {"국가": v["country"], "법정최고이자": f"연 {rate:g}%"}
    if command == "재무":
        from managers.company_account_manager import CompanyAccounts
        return _text(CompanyAccounts.financial_report(bot.db, v["product"]))
    raise ValueError("이 명령어는 Discord 봇이 처리합니다.")
