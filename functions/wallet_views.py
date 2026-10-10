"""Private wallet summary and paginated categories from a single asset snapshot."""
import copy
import discord
from functions.interaction_checks import owner_interaction_check


ELS_STATUS_LABELS = {
    'subscription': '모집 중', 'active': '운용 중',
    'observation_pending': '평가 대기', 'redemption_pending': '상환 대기',
    'redeemed': '상환 완료', 'cancelled': '발행 취소',
}


def load_active_wallet_loans(db, user_id, *, require_product=False, ordered=False):
    join = "JOIN" if require_product else "LEFT JOIN"
    order = " ORDER BY l.loan_id" if ordered else ""
    return db.accounts.execute(
        "SELECT l.loan_id,l.product_code,l.principal,l.outstanding_balance,"
        "l.maturity_epoch,p.name FROM loans l "
        f"{join} liability p ON p.product_code=l.product_code "
        "WHERE l.user_id=? AND l.status='active'" + order,
        (str(user_id),),
    )


def els_status_text(details):
    status = str(details.get('status') or details.get('state') or '')
    return ELS_STATUS_LABELS.get(status, status or '확인 중')


def els_position_lines(positions):
    """Format contractual ELS holdings without presenting a fictitious NAV."""
    lines = []
    for item in positions:
        quantity = int(item.get('quantity') or 0)
        code = str(item.get('product_code') or item.get('code') or '')
        next_epoch = item.get('next_observation_epoch')
        next_text = f"<t:{int(next_epoch)}:f>" if next_epoch else '없음'
        barrier = '발생' if item.get('knocked_in') else '미발생'
        principal = item.get('principal', quantity * float(item.get('face_value') or 0))
        lines.append(
            f"• **{str(item.get('name') or code)[:70]}** (`{code}`) · {quantity:,}좌 · "
            f"인수 원금 **{float(principal or 0):,.0f} NWX**\n"
            f"  {els_status_text(item)} · 연 쿠폰 {float(item.get('annual_coupon') or 0):g}% · "
            f"낙인 **{barrier}** · 다음 평가 {next_text}"
        )
    return lines


def preferred_claim_lines(claims):
    """Dividend claims belong to the fixed record holder, even after a sale."""
    lines = []
    for item in claims:
        remaining = item.get('remaining_amount')
        if remaining is None:
            remaining = float(item.get('gross_amount') or 0) - float(item.get('paid_amount') or 0)
        due = item.get('due_epoch')
        due_text = f"<t:{int(due)}:D>" if due else '확정 회차'
        issuer = str(item.get('issuer_code') or item.get('company_code') or '')
        lines.append(
            f"• **`{str(item.get('asset_code') or '')}`** · 발행법인 `{issuer}`\n"
            f"  확정수량 {int(item.get('quantity') or 0):,}주 · 미지급 배당 "
            f"**{float(remaining):,.0f} {item.get('quote_currency') or 'NWX'}** · {due_text}"
        )
    return lines


class WalletCategoryView(discord.ui.View):
    PAGE_SIZE=10
    CATEGORIES=(('main','메인'),('accounts','통장'),('currency_saving_accounts','외환통장'),('stocks','주식'),
                ('etf','ETF'),('els','ELS'),('preferred_claims','우선주 미지급 배당'),('treasury_bond','국채'),('corporate_bond','회사채'),
                ('stock_future','주식 선물'),('commodity_futures','원자재 선물'),
                ('mutual_fund','일반 펀드'),('index_fund','인덱스 펀드'),('commodity_spot','현물 원자재'),
                ('option','보유 옵션'),('written','작성 옵션'),('collateral','옵션 담보'),('loans','대출'),('other','기타'))

    def __init__(self,owner,summary,sections,option_liability=0):
        super().__init__(timeout=300)
        self.owner,self.summary,self.sections=str(owner),summary,sections
        self.option_liability=option_liability
        self.category,self.page='main',0
        self.category_select=discord.ui.Select(placeholder='확인할 지갑 카테고리를 선택하세요',
            options=[discord.SelectOption(label=label,value=key,default=key=='main') for key,label in self.CATEGORIES],row=0)
        self.category_select.callback=self.select_category
        self.add_item(self.category_select)

    async def interaction_check(self,interaction):
        return await owner_interaction_check(
            interaction, self.owner, '❌ 본인 지갑만 조회할 수 있습니다.'
        )

    def build_embed(self):
        labels=dict(self.CATEGORIES)
        if self.category=='main':
            embed=discord.Embed.from_dict(copy.deepcopy(self.summary.to_dict()))
            embed.title='💰 내지갑 · 메인'
            self.add_obligation_field(embed)
            embed.set_footer(text='통장·보유자산·대출 상세 내역은 아래 카테고리 메뉴에서 확인하세요.')
            self.previous.disabled=self.next.disabled=True
            return embed
        lines=self.sections.get(self.category,[])
        pages=max(1,(len(lines)+self.PAGE_SIZE-1)//self.PAGE_SIZE)
        self.page=min(max(0,self.page),pages-1)
        visible=lines[self.page*self.PAGE_SIZE:(self.page+1)*self.PAGE_SIZE]
        text='\n'.join(line[:340] for line in visible) or '보유 내역이 없습니다.'
        if self.category=='written':
            text='작성해 판매한 옵션은 만기까지 의무가 남습니다. 콜은 담보 주식 인도, 풋은 담보금으로 주식 매수 의무가 있습니다. 행사되지 않은 계약의 남은 담보는 만기에 반환됩니다.\n\n'+text
        elif self.category=='els':
            text='표시 원금은 인수액이며 현재 매도 가능한 평가가격이 아닙니다. 모집 종료 후 조기·만기 평가에 따라 자동 상환합니다. 중도 매도·환매는 지원하지 않습니다.\n\n'+text
        elif self.category=='preferred_claims':
            text='해당 배당회차의 확정 보유자에게 귀속된 세전 미지급 배당입니다. 주식을 매도해도 이 채권은 유지하며 발행법인의 재원 확보 후 지급합니다.\n\n'+text
        embed=discord.Embed(title=f'💰 내지갑 · {labels[self.category]}',description=text,color=discord.Color.gold())
        embed.set_footer(text=f'{len(lines)}개 · {self.page+1}/{pages} 페이지 · 조회 시점 기준 · 최신 정보는 /내지갑 재실행')
        self.previous.disabled=self.page==0
        self.next.disabled=self.page==pages-1
        return embed

    def add_obligation_field(self,embed):
        name='📄 옵션 작성 의무 평가'
        value=f"```{'−' if self.option_liability else ''}{self.option_liability:,} NWX```\n미만기 작성 옵션의 현재 의무 가치 · 순자산 차감\n만기에 이 금액을 그대로 납부하는 것은 아닙니다."
        index=next((i for i,f in enumerate(embed.fields) if f.name==name),None)
        if index is None:embed.add_field(name=name,value=value,inline=True)
        else:embed.set_field_at(index,name=name,value=value,inline=True)

    async def select_category(self,interaction):
        self.category=self.category_select.values[0]
        self.page=0
        for option in self.category_select.options:option.default=option.value==self.category
        await interaction.response.edit_message(embed=self.build_embed(),view=self)

    @discord.ui.button(label='◀ 이전',style=discord.ButtonStyle.secondary,row=1)
    async def previous(self,interaction,button):
        self.page-=1
        await interaction.response.edit_message(embed=self.build_embed(),view=self)

    @discord.ui.button(label='다음 ▶',style=discord.ButtonStyle.secondary,row=1)
    async def next(self,interaction,button):
        self.page+=1
        await interaction.response.edit_message(embed=self.build_embed(),view=self)
