"""Structured private administrator responses within Discord embed limits."""
import io
import re
import discord


def _chunks(text,limit=1000):
    while text:
        end=min(len(text),limit)
        if end<len(text):
            boundary=text.rfind('\n',0,end)
            if boundary>0:end=boundary
        yield text[:end]
        text=text[end:].lstrip('\n')


def admin_embed(text, title='Inazina · 관리자', *, pending=False):
    text=str(text or '조회 결과가 없습니다.')[:3900]
    error=text.lstrip().startswith('❌')
    success=text.lstrip().startswith('✅')
    icon,status,color=('❌','오류',discord.Color.red()) if error else ('⏳','처리 중',discord.Color.gold()) if pending else ('✅','완료',discord.Color.green()) if success else ('📋','안내',discord.Color.blue())
    first,_,rest=text.partition('\n')
    if text.lstrip().startswith(('{','[')) or len(first)>300:
        summary='조회 결과를 확인해주세요.'
        details=text
    else:
        summary=first
        details=rest.strip()
    embed=discord.Embed(title=f'{icon} {title}'[:256],description=f'**{summary}**',color=color)
    embed.add_field(name='📌 처리 상태',value=f'**{status}**',inline=True)
    if '$' in title:
        embed.add_field(name='⌨️ 명령어',value=f"`{title[title.index('$'):][:80]}`",inline=True)
    change=re.fullmatch(r'✅\s+(.+?)\s+(가격|잔고)\s+(.+?)\s+→\s+(.+?)\s+([A-Z]+)',text)
    if change:
        target,kind,before,after,currency=change.groups()
        embed.description=f'**{kind} 변경을 완료했습니다.**'
        embed.add_field(name='🏷️ 대상',value=f'`{target[:100]}`',inline=False)
        embed.add_field(name='📤 변경 전',value=f'```{before} {currency}```',inline=True)
        embed.add_field(name='📥 변경 후',value=f'```{after} {currency}```',inline=True)
    for index,chunk in enumerate(_chunks(details)):
        embed.add_field(name='📝 상세 내용' if index==0 else f'📝 상세 내용 · {index+1}',value=chunk,inline=False)
    embed.set_footer(text='🔒 Inazina · 관리자 전용 DM')
    return embed


def admin_response(text, title='Inazina · 관리자'):
    text=str(text or '조회 결과가 없습니다.')
    preview=text if len(text)<=3900 else text[:3700]+'\n\n📎 **전체 내용은 첨부 파일에서 확인하세요.**'
    options={'embed':admin_embed(preview,title)}
    if len(text)>3900:
        options['file']=discord.File(io.BytesIO(text.encode('utf-8')),filename='admin_result.txt')
    return options


# Korean labels for product creation arguments, shared with the web dashboard.
ARG_LABELS = {
    'name':'이름', 'industry':'업종', 'capital':'자본금', 'total_shares':'발행주식수',
    'share_price':'주가', 'exchange_id':'거래소ID', 'type':'자산유형', 'price':'가격',
    'total_supply':'발행수량', 'interest_rate':'연이율%', 'deposit_amount':'최소예치금',
    'maturity_days':'만기일수', 'interest_cycle':'이자주기(초)', 'compound_type':'단리0/복리1',
    'issuer':'발행기관', 'loan_limit':'대출한도', 'min_credit_score':'최소신용점수',
    'unit_price':'단위가격', 'underlying_exchange_id':'기초거래소ID',
    'leverage_multiplier':'배율', 'rebalance_cycle_seconds':'재조정주기(초)',
    'underlying_code':'기초종목ID', 'contract_size':'계약당수량',
    'initial_margin':'최초증거금비율', 'maintenance_margin':'유지증거금비율',
    'issuer_code':'발행회사ID', 'face_value':'액면가', 'annual_rate':'연이율%',
    'components':'구성종목:비중', 'market_price':'시장가격', 'option_type':'call/put',
    'strike_price':'행사가', 'quantity':'계약수', 'limit_price':'매도지정가',
    'volatility_percent':'변동성%', 'asset_code':'환율종목ID', 'nationality':'국적',
    'currency_code':'통화코드', 'parent_company_code':'보통주회사ID',
    'dividend_cycle_seconds':'배당주기', 'dividend_per_share':'주·좌당금액/비율%',
    'extension_min_credit_score':'연장최소신용', 'extension_days':'연장일수',
    'max_dti_percent':'DTI한도%', 'max_dsr_percent':'DSR한도%',
    'borrower_history_spread_max':'연체이력가산최대%p',
    'affordability_spread_max':'상환부담가산최대%p',
    'mandatory_cost_percent':'법정산입의무비용%',
    'liquidation_cost_percent':'담보청산비용%',
    'underlyings':'기초코드1,코드2,코드3', 'annual_coupon':'연쿠폰%',
    'observation_days':'평가일수목록', 'redemption_levels':'조기상환기준%목록',
    'maturity_level':'만기상환기준%', 'knock_in_level':'낙인기준%',
    'subscription_days':'모집일수',
    'rate_type':'금리유형', 'rate_nationality':'기준금리국가',
    'base_rate_spread':'가산금리%p', 'rate_floor':'금리하한%',
    'rate_cap':'상품금리상한%', 'repricing_cycle_seconds':'금리재산정주기(초)',
    'negative_rate_allowed':'마이너스금리허용0/1',
    'day_count_convention':'일수계산ACT/365·ACT/360·30/360',
    'amortization_type':'상환방식legacy·bullet·equal_principal·annuity',
    'payment_frequency_days':'상환간격일',
    'coupon_frequency_days':'쿠폰지급간격일',
    'collateral_haircut':'담보헤어컷0~1',
    'margin_call_ltv':'마진콜LTV', 'liquidation_ltv':'청산LTV',
    'early_withdrawal_rate':'중도해지금리%',
    'early_withdrawal_fee_percent':'중도해지수수료%',
    'deposit_insured':'예금보호0/1',
    'business_day_convention':'영업일조정규칙',
    'underlying_per_receipt':'DR당 원주 수량', 'receipt_type':'DR 유형',
    'depositary_bank':'예탁기관', 'custodian':'수탁기관',
    'home_exchange_id':'원주 상장거래소', 'quote_currency':'결제 통화',
}


def creation_argument_lines():
    from managers.admin_commands_manager import CREATE_ARGS, OPTIONAL_ARGS
    labels = ARG_LABELS
    return '\n'.join(
        f"**`{'company' if kind=='stock' else kind}`** → `"
        + ' → '.join(('발행총액' if kind == 'bond' and field == 'total_supply' else labels.get(field, field))
                     + ('(선택)' if field in OPTIONAL_ARGS.get(kind, ()) else '') for field in fields)
        + '`' for kind, fields in CREATE_ARGS.items()
    )


def admin_help_embed(national_commands):
    from managers.admin_commands_manager import USAGE
    embed=discord.Embed(title='🛠️ Inazina · 관리자 도움말',
        description='**관리자 설정과 국가 명의 기능을 한곳에서 확인하세요.**\n명령어의 `[ ]` 부분에 값을 입력합니다.',color=discord.Color.blue())
    groups=(('📦 상품 관리',('상품생성','상품수정','상품가격수정','상품삭제')),
            ('🏢 기업 · 배당',('자본변경','액면분할병합','정기배당수정','정기배당활성','특별배당실시','합병','법인잔고수정','법인청산')),
            ('🧾 ELS 관리',('ELS취소',)),
            ('👥 시민 · 잔고',('서버설치','등록','기본소득','잔고수정')),
            ('📈 시장 · 환율',('시장분위기','상품분위기','환율수정','휴장','개장','장일시정지','장재개','서킷브레이커','사이드카')),
            ('🎲 도박 · 세금',('도박확률','도박리턴값','세율')),
            ('🏦 금리 · 법정최고이자',('금리수정','법정최고이자수정')),
            ('🎟️ 복권 관리',('복권가격설정','복권당첨번호보기','복권당첨번호변경')))
    for title,commands in groups:
        embed.add_field(name=title,value='\n'.join(f'**`${command}`**\n{USAGE[command] or "인자 없이 실행"}' for command in commands if command in USAGE) or '상품생성에서 els 유형을 사용합니다.',inline=False)
    embed.add_field(name='📰 사건 설정',value='**`$사건`** → DM 버튼 → 모달\n상품코드 · 호재/악재 · 기간 · 변동률 · 선택 목표가격',inline=False)
    embed.add_field(name='🏛️ 국가 명의 일반 기능',value=' · '.join(f'`$'+command+'`' for command in sorted(national_commands))+'\n**국고의 잔액·보유자산·담보로 실행합니다.**\n`$login [국가 ID]`로 그 나라 국고를 주체로 정하고 `$logout`으로 해제합니다 (기본: 낙원 국고).',inline=False)
    embed.add_field(name='💡 입력 예시',value='```\n$상품가격수정 INZST001 add 100\n$잔고수정 사용자ID modify 1000000\n$환율수정 USD EUR 0.9\n$금리수정 낙원 modify 3\n```',inline=False)
    embed.description+='\n\n상품별 인자 순서는 `$도움말`의 **상품 관리**, 우선주·ELS 규칙은 **우선주 · ELS** 페이지에서 확인하세요.'
    embed.add_field(name='📦 상품생성 예시',value='```\n$상품생성 SV saving "새 적금" 3 1000 30 86400 1 Inazina\n$상품생성 ST company "새 회사" 엔터테인먼트 1000000 10000 100 INZ\n```\n**추가 JSON은 마지막 선택 인자**입니다. 공백이 있는 이름은 따옴표로 묶으세요.',inline=False)
    embed.add_field(name='🔒 사용 안내',value='**DB role=admin 전용** · 결과는 개인 DM\n응답 후 입력 채팅 삭제 · DM에서 추가 인증 입력 없음\n국가 기능은 인자 없이 실행하면 입력 창을 엽니다.',inline=False)
    embed.set_footer(text='Inazina · $도움말 / $help')
    return embed
