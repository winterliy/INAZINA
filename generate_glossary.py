from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output" / "pdf" / "DB_Glossary.pdf"
FONT_PATH = Path("/System/Library/Fonts/Supplemental/AppleGothic.ttf")
FONT_NAME = "AppleGothic"
if FONT_PATH.exists():
    pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT_PATH)))
else:
    FONT_NAME = "Helvetica"


def _table(rows, widths, body):
    data = [[Paragraph(str(cell), body) for cell in row] for row in rows]
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#24476b")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), FONT_NAME),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#aab7c4")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f8")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont(FONT_NAME, 8)
    canvas.setFillColor(colors.HexColor("#607080"))
    canvas.drawString(38, 22, "Inazina DB Glossary · NWX")
    canvas.drawRightString(A4[0] - 38, 22, str(doc.page))
    canvas.restoreState()


def create_pdf():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("GlossTitle", parent=styles["Title"], fontName=FONT_NAME,
                           fontSize=19, leading=25, textColor=colors.HexColor("#183653"),
                           alignment=1, spaceAfter=8)
    section = ParagraphStyle("GlossSection", parent=styles["Heading2"], fontName=FONT_NAME,
                             fontSize=12, leading=16, textColor=colors.HexColor("#24476b"),
                             spaceBefore=8, spaceAfter=6)
    body = ParagraphStyle("GlossBody", parent=styles["BodyText"], fontName=FONT_NAME,
                          fontSize=8.5, leading=12)
    story = [
        Paragraph("Inazina 데이터베이스 용어 사전", title),
        Paragraph("SQLite 파일은 프로젝트의 database/ 폴더에 저장됩니다. 아래 설명은 현재 코드의 스키마와 동작 기준입니다.", body),
        Spacer(1, 12),
        Paragraph("1. inazina.db / 사용자", section),
        _table([
            ["테이블.컬럼", "설명"],
            ["users.user_id", "Discord 사용자 고유 ID. 기본키."],
            ["users.nickname", "시민 표시 이름."],
            ["users.nationality", "소속 지역. 현재 기본값은 낙원."],
            ["users.cash", "현금 지갑 잔액 (NWX)."],
            ["users.credit_score", "대출 심사 신용 점수."],
            ["users.status", "active 상태만 거래 가능."],
            ["users.role", "admin 또는 user. 관리자 권한은 SQL Browser에서 관리."],
        ], [170, 349], body),
        Paragraph("2. inazina.db / 시장과 보유량", section),
        _table([
            ["테이블.컬럼", "설명"],
            ["market_assets.code/name", "시장 자산 코드와 표시 이름."],
            ["market_assets.exchange_id", "상장 거래소 식별자."],
            ["market_assets.price", "현재 단가 (NWX). 변동 루프는 매 회차 최신 DB 값을 읽습니다."],
            ["market_assets.market_volatility", "해당 시장 자산의 개별 변동률 (0~1). NULL이면 system.db settings.market_volatility를 따릅니다."],
            ["market_assets.type/status", "자산 종류와 거래 상태. active 상태만 매수/매도가 가능합니다."],
            ["market_assets.is_fixed", "1이면 자동 가격 변동에서 제외, 0이면 변동 대상."],
            ["user_assets.user_id/asset_code", "보유 시민과 자산 코드."],
            ["user_assets.account_number", "매수 자금이 나온 통장 번호. NULL은 현금 지갑 귀속."],
            ["user_assets.quantity", "보유 수량. 양의 정수."],
            ["user_assets.average_cost", "평균 매입 단가. 주식 매도 시 양의 실현이익과 세금 계산에 사용."],
        ], [170, 349], body),
        Paragraph("3. inazina.db / 회사 주식", section),
        _table([
            ["컬럼", "설명"],
            ["company_code", "회사 주식 코드. 다른 자산/상품 코드와 중복되지 않아야 합니다."],
            ["name/industry", "회사명과 업종."],
            ["capital", "자본금 (NWX)."],
            ["total_shares", "발행 주식 수. 매수 가능 수량의 상한."],
            ["share_price", "현재 주당 가격 (NWX)."],
            ["company.market_condition", "해당 회사 주식의 개별 흐름. bull/bear/flat 또는 상승장/하락장/평장. NULL이면 system.db settings.market_condition을 따릅니다."],
            ["company.market_volatility", "해당 회사 주식의 개별 변동률 (0~1). NULL이면 system.db settings.market_volatility를 따릅니다."],
            ["max_shareholder_id", "현재 보유 주식이 가장 많은 시민의 Discord ID. 매수/매도 후 자동 갱신."],
            ["exchange_id/status/is_fixed", "거래소, 상태, 가격 고정 여부."],
        ], [170, 349], body),
        PageBreak(),
        Paragraph("4. inazina.db / 금융상품", section),
        _table([
            ["테이블.컬럼", "설명"],
            ["saving.product_code/name", "저축 상품 코드와 이름."],
            ["saving.interest_rate", "연 이자율 (%). 숫자 또는 숫자형 문자열."],
            ["saving.deposit_amount", "가입 시 최소 초기 예치금 (NWX)."],
            ["saving.maturity_days", "만기 일수. 0은 무기한."],
            ["saving.interest_cycle", "이자 지급 주기 (초). 예: 3600은 한 시간."],
            ["saving.compound_type", "0은 단리, 1은 복리."],
            ["saving.issuer/status", "발급 은행/기관과 가입 가능 상태."],
            ["liability.interest_rate/loan_limit", "연 대출 이자율 (%)과 대출 한도 (NWX). loan_limit=0이면 금액 한도가 없습니다."],
            ["liability.maturity_days/interest_cycle", "만기 일수와 복리 반영 주기 (초). 만기 0은 무기한."],
            ["liability.min_credit_score", "대출 신청에 필요한 최소 신용 점수."],
            ["liability.issuer/status", "대출 발급 기관과 대출 가능 상태."],
            ["investment.product_code/name", "ETF·선물·옵션 등 투자상품 코드와 이름."],
            ["investment.total_supply/unit_price", "총 공급량과 단가 (NWX). 보유량 합산으로 공급량을 제한."],
            ["investment.market_condition", "해당 투자상품의 개별 흐름. bull/bear/flat 또는 상승장/하락장/평장. NULL이면 system.db settings.market_condition을 따릅니다."],
            ["investment.market_volatility", "해당 투자상품의 개별 변동률 (0~1). NULL이면 system.db settings.market_volatility를 따릅니다."],
            ["investment.product_type/exchange_id", "상품 종류와 상장 거래소."],
            ["investment.status/is_fixed", "거래 상태와 가격 자동 변동 여부."],
        ], [170, 349], body),
        Paragraph("5. inazina.db / 계좌와 대출", section),
        _table([
            ["테이블.컬럼", "설명"],
            ["accounts.account_number", "고유 계좌번호. 전역 코드 중복 검사를 수행."],
            ["accounts.user_id/product_code", "계좌 소유자와 연결된 금융 상품 코드."],
            ["accounts.balance", "현재 잔액 (NWX)."],
            ["accounts.password", "비밀번호 원문이 아닌 PBKDF2-SHA256 해시."],
            ["accounts.principal", "단리 계산에 쓰는 원금."],
            ["accounts.last_payment_epoch", "마지막 이자 지급 시각 (Unix seconds)."],
            ["accounts.account_type", "saving 또는 대출금 자동 지급용 loan_payout."],
            ["loans.principal", "대출 최초 원금."],
            ["loans.outstanding_balance", "이자 누적 후 현재 상환해야 할 원리금."],
            ["loans.last_payment_epoch/status", "최근 이자 반영 시각 및 active/paid 상태."],
        ], [170, 349], body),
        Paragraph("6. inazina.db / 도박 확률", section),
        _table([
            ["테이블.컬럼", "설명"],
            ["probability.id", "설정 행 고정값 1."],
            ["probability.chance", "당첨 확률 퍼센트. 허용 범위 0~100, 기본값 50."],
        ], [170, 349], body),
        Paragraph("7. inazina.db / 세금", section),
        _table([
            ["테이블.컬럼", "설명"],
            ["tax_settings.rate_percent", "원천징수율 퍼센트. 기본값 3."],
            ["tax_transactions.source", "saving_interest, gambling_winnings, stock_sale_gain 등 소득 종류."],
            ["gross_amount/tax_amount/net_amount", "세전 소득, 세금, 세후 소득."],
            ["created_at", "세금 처리 일시."],
        ], [170, 349], body),
        Spacer(1, 10),
        Paragraph("현재 모든 테이블은 inazina.db에 저장됩니다. 기존 DB 파일은 이전 원본입니다. 종목 코드는 관리자 모달로 관리하며 다른 상품과 중복할 수 없습니다. 최신 외환·관리자 명령어는 docs/inazina_operations.md를 확인하세요.", body),
    ]
    doc = SimpleDocTemplate(str(OUTPUT), pagesize=A4, rightMargin=38, leftMargin=38,
                            topMargin=38, bottomMargin=38, title="Inazina DB Glossary")
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return OUTPUT


if __name__ == "__main__":
    print(create_pdf())
