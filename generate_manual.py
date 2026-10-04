from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output" / "pdf" / "Inazina_Manual.pdf"
FONT_PATH = Path("/System/Library/Fonts/Supplemental/AppleGothic.ttf")
FONT_NAME = "AppleGothic"

if FONT_PATH.exists():
    pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT_PATH)))
else:
    FONT_NAME = "Helvetica"


def _table(rows, widths, body_style):
    header_style = ParagraphStyle(
        "ManualTableHeader",
        parent=body_style,
        textColor=colors.white,
        fontName=FONT_NAME,
    )
    data = [
        [Paragraph(str(cell), header_style if row_index == 0 else body_style) for cell in row]
        for row_index, row in enumerate(rows)
    ]
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#24476b")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), FONT_NAME),
        ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#aab7c4")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f8")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return table


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont(FONT_NAME, 8)
    canvas.setFillColor(colors.HexColor("#607080"))
    canvas.drawString(38, 22, "Inazina · 낙원자화 (NWX)")
    canvas.drawRightString(A4[0] - 38, 22, f"{doc.page}")
    canvas.restoreState()


def create_manual():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("ManualTitle", parent=styles["Title"], fontName=FONT_NAME,
                           fontSize=21, leading=28, textColor=colors.HexColor("#183653"),
                           alignment=1, spaceAfter=8)
    h1 = ParagraphStyle("ManualH1", parent=styles["Heading1"], fontName=FONT_NAME,
                        fontSize=15, leading=20, textColor=colors.HexColor("#24476b"),
                        spaceBefore=10, spaceAfter=8)
    h2 = ParagraphStyle("ManualH2", parent=styles["Heading2"], fontName=FONT_NAME,
                        fontSize=11.5, leading=15, textColor=colors.HexColor("#315b7d"),
                        spaceBefore=8, spaceAfter=5)
    body = ParagraphStyle("ManualBody", parent=styles["BodyText"], fontName=FONT_NAME,
                          fontSize=9.5, leading=14, spaceAfter=6)
    small = ParagraphStyle("ManualSmall", parent=body, fontSize=8.2, leading=11.2)

    doc = SimpleDocTemplate(str(OUTPUT), pagesize=A4, rightMargin=38, leftMargin=38,
                            topMargin=38, bottomMargin=38, title="Inazina Manual")
    story = [
        Spacer(1, 8),
        Paragraph("Inazina 통합 운영 설명서", title),
        Paragraph(
            f"시민·운영자 안내 · 작성일 {date.today().isoformat()} · 화폐 단위 낙원 자이옌 (NWX)",
            body,
        ),
        Paragraph("1. 처음 이용하기", h1),
        Paragraph(
            "Discord에서 슬래시 명령어를 입력합니다. 처음에는 <b>/등록</b>을 실행하고 안내문과 등록 양식을 완료하세요. "
            "시민 명령어는 등록된 계정으로 사용할 수 있습니다. <b>/도움말</b>은 명령어 안내 버튼을 엽니다.",
            body,
        ),
        Paragraph("2. 자산과 금융", h1),
        _table([
            ["명령어", "기능"],
            ["/내자산", "현금·통장·주식·투자상품·국채·옵션·미체결 매수 예치금, 총자산·대출·순자산·신용점수를 확인합니다."],
            ["/전체자산확인", "등록 시민의 자산 현황을 조회하는 화면을 엽니다."],
            ["/송금 수신자 금액 [출금계좌]", "시민 ID나 멘션, 계좌번호 또는 국고를 수신자로 지정합니다. 출금 계좌를 생략하면 잔액이 가장 큰 현금원에서 보냅니다."],
            ["/도박 금액", "잔액이 가장 큰 현금원에서 베팅합니다. 당첨 확률과 지급률은 운영자 설정에 따르며, 당첨금에는 세금이 원천징수됩니다."],
            ["/계좌개설", "저축 상품을 고른 뒤 비밀번호와 초기 예치금을 입력합니다."],
            ["/계좌해지", "본인 통장을 고르고 비밀번호를 확인합니다. 잔액과 통장에 귀속된 자산은 다른 본인 통장, 없으면 지갑으로 옮깁니다."],
            ["/대출", "대출 상품과 신청액을 선택합니다. 신용점수와 상품별 한도·조건을 확인합니다."],
            ["/대출상환", "활성 대출을 선택해 상환액을 입력합니다. 정기 상환이 설정된 상품은 약정 주기마다 자동 납부를 시도합니다."],
            ["/회생신청 · /파산신청", "현재 자산 요약과 함께 관리자에게 신청을 DM으로 전달합니다."],
        ], [176, 343], small),
        PageBreak(),
        Paragraph("3. 시장과 투자", h1),
        _table([
            ["명령어", "기능"],
            ["/검색", "주식·투자·저축·대출 상품과 본인 계좌를 검색하는 정보 패널을 엽니다."],
            ["/기준금리 · /법정이자", "현재 게임 연이율을 조회합니다. 금리 설정은 system.db의 settings에서 조정합니다."],
            ["/국채", "판매 중인 국채의 거래소, 액면가, 유통 기준가, 연이율, 만기와 발행 잔량을 확인합니다."],
            ["/옵션체인 [기초주식]", "거래 중인 콜·풋 계약, 만기·행사가·참고 프리미엄과 현재 매수·매도 호가를 확인합니다."],
            ["/옵션작성 기초주식 유형 행사가 만기일수 계약수 지정프리미엄 [변동성]", "주식 옵션 매도 호가를 만들고 담보를 잠급니다. 콜은 보유 주식, 풋은 행사가×계약수만큼 현금이 필요합니다."],
            ["/내옵션", "보유 옵션, 작성한 계약 수와 남은 담보를 확인합니다."],
            ["/매수 자산 수량 지정가", "자동완성에서 주식·투자상품·국채·옵션을 고르고 지정가 매수 주문을 등록합니다. 국채 신규 청약도 여기서 신청합니다."],
            ["/매도 자산 수량 지정가", "보유 주식·투자상품·국채·옵션의 지정가 매도 주문을 등록합니다."],
            ["/호가 자산", "자산의 매수·매도 대기 주문, 최우선 가격과 스프레드를 확인합니다."],
            ["/내주문", "최근 주문과 미체결 주문을 확인하고 남은 주문을 취소합니다."],
            ["/그래프 [자산]", "자산·거래소 지수 또는 기준금리·법정이자 그래프를 엽니다. 화면에서 조회 기간을 바꿀 수 있습니다."],
        ], [176, 343], small),
        Paragraph("지정가 주문 처리", h2),
        Paragraph(
            "주문은 가격 우선, 같은 가격에서는 먼저 등록한 순서로 체결됩니다. 체결 가격은 먼저 호가에 올라와 있던 주문의 지정가입니다. "
            "일부만 체결되면 나머지는 호가에 남습니다. 매수 주문의 예치금과 매도 주문의 수량은 체결 또는 취소 전까지 잠깁니다. "
            "취소는 <b>/내주문</b> 화면에서 할 수 있습니다.",
            body,
        ),
        Paragraph("주식 옵션 MVP", h2),
        Paragraph(
            "옵션은 만기 때만 행사하는 유럽형 실물결제 계약이며 1계약은 기초주식 1주입니다. "
            "콜 작성은 주식을 맡기고, 풋 작성은 행사가 전액을 현금으로 맡깁니다. 무담보 작성은 허용하지 않습니다. "
            "만기 때 콜은 주가가 행사가보다 높고 행사대금이 있을 때, 풋은 주가가 행사가보다 낮고 인도할 주식이 있을 때 자동 행사됩니다. "
            "조건이나 결제 자산이 부족한 계약은 행사되지 않고 만료됩니다. 옵션 호가는 주문장 가격으로 체결되며, 참고 프리미엄은 연 IV 기본 50%·무위험이자율 0%인 블랙-숄즈 값입니다. "
            "IV는 옵션 작성 때 1~500% 범위에서 입력할 수 있습니다. 보유 옵션과 작성 의무 평가는 <b>/내자산</b>에도 반영됩니다.",
            body,
        ),
        Paragraph("4. 관리자 명령어", h1),
        _table([
            ["명령어", "기능 및 권한"],
            ["$기본소득", "관리자 모달에서 국고의 기본소득 지급액을 설정합니다."],
            ["$setup", "관리자 모달에서 지정한 한 명을 등록합니다."],
            ["$register", "관리자 모달에서 서버 구성원을 일괄 등록합니다. Server Members Intent가 필요합니다."],
            ["/주총 주식코드 장소", "관리자 또는 해당 회사의 최대주주가 소집합니다. 주주에게 참석 여부 버튼이 있는 DM을 보냅니다."],
        ], [176, 343], small),
        Paragraph(
            "관리자 권한은 <b>inazina.db</b>의 <b>users.role=admin</b>으로 관리합니다. <b>$help</b>에서 상품·시민·경제 설정 창을 열며, <b>$매수/$매도</b> 등은 국고 명의로 거래합니다. 관리자 슬래시 명령어는 등록하지 않습니다.",
            body,
        ),
        PageBreak(),
        Paragraph("5. 자동 처리와 계산 기준", h1),
        _table([
            ["항목", "기준"],
            ["시장 가격", "활성 가격과 거래소 지수는 1초 간격으로 갱신됩니다. is_fixed=1인 항목은 고정됩니다. 전체 변동폭·시장 흐름은 system.db에서 설정하고, 자산별 값으로 재정의할 수 있습니다."],
            ["거래소 지수·레버리지 상품", "회사 주식의 시가총액 비중을 반영해 거래소 지수를 계산합니다. 기초 거래소와 배율이 설정된 상품은 지수 변동을 반영하고 재조정 주기마다 기준값을 갱신합니다."],
            ["저축 이자", "계좌 조회나 사용 시 지난 완전 이자 주기를 정산합니다. 상품의 단리·복리 방식과 만기일을 따릅니다. 만기일수 0은 무기한입니다."],
            ["대출 이자·정기 상환", "대출 이자는 설정된 주기마다 원리금에 복리로 반영됩니다. 정기 상환 주기와 금액이 설정된 경우 5초마다 납부 시점을 확인해 가장 잔액이 큰 현금원에서 출금합니다. 미납 회당 신용점수 1점이 차감됩니다."],
            ["국채 만기", "5초마다 만기 국채를 확인해 원금과 기간에 따른 이자를 지갑에 지급합니다. 이자에는 세금이 적용되고 상환 결과를 DM으로 알립니다."],
            ["세금", "tax.db의 세율 기본값은 3%입니다. 저축 이자·도박 당첨금·국채 이자·양의 실현 투자이익에 원천징수합니다. 원금 반환과 대출금에는 세금을 매기지 않습니다."],
            ["그래프 기록", "/그래프의 가격·체결·지수 기록은 한국시간 날짜가 바뀌면 전날까지 삭제합니다. 금리 변경 이력은 유지하며 연이율(%)과 변동(%p)을 계단 그래프로 표시합니다."],
        ], [176, 343], small),
        Spacer(1, 8),
        Paragraph("6. 운영과 보안", h1),
        Paragraph(
            "DB 작업은 여러 파일을 함께 갱신하는 거래 단위로 처리합니다. SQL Browser에서 값을 바꾼 뒤 저장/Write Changes를 완료하세요. "
            "봇 토큰은 <b>run.py</b>가 읽는 <b>INAZINA_TOKEN</b> 환경 변수에 보관합니다. 관리자 채팅은 Message Content Intent와 메시지 관리 권한이 필요합니다. 현재 DB는 <b>database/inazina.db</b> 하나이며 최신 운영 안내는 docs/inazina_operations.md를 참고하세요.",
            body,
        ),
        PageBreak(),
        Paragraph("7. SQL Browser 데이터베이스 안내", h1),
        Paragraph(
            "파일은 프로젝트의 <b>database/</b> 폴더에 있습니다. 직접 수정할 때 아래 테이블과 단위를 확인하고, 편집 후 Write Changes를 누르세요.",
            body,
        ),
        _table([
            ["DB / 테이블", "주요 관리 값"],
            ["users.db · users", "user_id, nickname, cash, credit_score, status, role. role은 admin 또는 user입니다."],
            ["assets.db · market_assets", "시장 자산 코드·이름·거래소·가격·종류·상태·is_fixed·market_volatility·total_supply."],
            ["assets.db · user_assets", "보유 자산·수량·원가·귀속 계좌. account_number가 NULL이면 지갑에 귀속됩니다."],
            ["assets.db · market_orders", "지정가 주문의 방향·가격·수량·상태·매수 예치액."],
            ["assets.db · option_contracts / option_positions", "옵션 기초주식·콜/풋·행사가·만기·IV·작성자·보유 계약 수."],
            ["assets.db · option_collateral_lots / option_collateral_cash_lots", "콜 작성 주식담보와 풋 작성 현금담보. 옵션 주문은 market_orders의 order_kind로 구분됩니다."],
            ["assets.db · market_price_history / market_trade_history", "가격 이력과 실제 체결 수량·가격. 그래프와 거래량에 사용됩니다."],
            ["assets.db · market_exchange_indices", "거래소 지수와 구성 종목별 이력. leveraged_etf_state는 레버리지 상품의 재조정 기준을 보관합니다."],
            ["company.db · company", "회사 코드·업종·자본·발행주식수·주가·거래소·상태·시장 흐름·변동폭."],
            ["product.db · saving / liability", "저축·대출 상품 조건, 연이율(%), 한도, 만기·이자 주기, 신용 기준. liability에는 정기 상환 주기와 납부액이 있습니다."],
            ["product.db · investment", "ETF·투자상품 공급량·기준가·만기·거래소·시장 설정. 레버리지 상품은 기초 거래소·배율·재조정 주기를 설정합니다."],
            ["product.db · treasury_bond_products", "국채 코드·연이율·만기·액면가·거래소·유통 기준가·총 발행량·판매량·상태."],
            ["accounts.db · accounts / loans", "통장 잔액·원금·이자 시각·PBKDF2 비밀번호 해시, 대출 원금·현재 원리금·상태."],
            ["accounts.db · treasury_bond_holdings", "보유 국채 원금·매입 원가·이율·만기·상환 상태."],
            ["system.db · settings", "market_volatility는 전체 변동폭(0~1), market_condition은 bull/bear/flat 시장 흐름입니다."],
            ["system.db · rate_history", "기준금리·법정이자의 적용 시각과 연이율 변경 기록. settings의 base_rate/statutory_rate를 수정하면 자동 기록하며 금리 그래프와 구간별 이자 계산에 사용합니다."],
            ["pot.db · probability / return", "probability.chance는 당첨 확률(0~100%), return.return_percent는 당첨 지급률입니다."],
            ["tax.db · tax_settings / tax_transactions", "rate_percent는 세율(%), 거래 내역에는 세전액·세금·세후액이 기록됩니다. 기본 세율은 3입니다."],
        ], [190, 329], small),
        Spacer(1, 8),
        Paragraph("코드와 금액 입력", h2),
        Paragraph(
            "시장 자산·회사 주식·저축·대출·투자·국채 상품 코드는 서로 중복될 수 없습니다. 계좌번호도 상품 코드와 겹치지 않게 유지하세요. "
            "금액 필드는 정수 NWX로 저장됩니다. 외화는 정수 마이크로 단위로 저장합니다. inazina.db의 probability와 return 테이블이 도박 설정의 기준입니다.",
            body,
        ),
    ]
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return OUTPUT


if __name__ == "__main__":
    print(create_manual())
