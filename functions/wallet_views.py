"""Private wallet summary and paginated categories from a single asset snapshot."""
import copy
import discord


class WalletCategoryView(discord.ui.View):
    PAGE_SIZE=10
    CATEGORIES=(('main','메인'),('accounts','통장'),('fx_accounts','외환통장'),('stocks','주식'),
                ('etf','ETF'),('treasury_bond','국채'),('corporate_bond','회사채'),
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
        if str(interaction.user.id)==self.owner:return True
        await interaction.response.send_message('❌ 본인 지갑만 조회할 수 있습니다.',ephemeral=True)
        return False

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
        embed=discord.Embed(title=f'💰 내지갑 · {labels[self.category]}',description=text,color=discord.Color.gold())
        if self.category=='written':self.add_obligation_field(embed)
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
