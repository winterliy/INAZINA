"""Administrator help, grouped into private category pages."""
import discord
from functions.admin_embeds import admin_help_embed,admin_response,creation_argument_lines


class AdminHelpView(discord.ui.View):
    PAGES=(('main','📖 메인'),('products','📦 상품 관리'),('citizens','👥 시민 · 잔고'),('market','📈 시장 · 환율 · 사건'),
           ('gamble','🎲 도박 · 세금'),('rates','🏦 금리 · 법정최고이자'),('lottery','🎟️ 복권'),
           ('structured','🧾 우선주 · ELS'),('national','🏛️ 국가 명의 기능'))
    def __init__(self,bot,owner,national_commands,guild=None):
        super().__init__(timeout=300)
        self.bot,self.owner,self.page=bot,str(owner),0
        self.guild=guild
        self.source=admin_help_embed(national_commands)
        self.category_select=discord.ui.Select(placeholder='도움말 카테고리를 선택하세요',
            options=[discord.SelectOption(label=label,value=key,default=index==0) for index,(key,label) in enumerate(self.PAGES)],row=0)
        self.category_select.callback=self.select_category
        self.add_item(self.category_select)

    async def interaction_check(self,interaction):
        user=self.bot.db.users.get_user(self.owner)
        if str(interaction.user.id)==self.owner and user and str(user['role']).strip().lower()=='admin':return True
        await interaction.response.send_message(**admin_response('❌ 이 도움말을 연 관리자만 사용할 수 있습니다.'),ephemeral=True)
        return False

    def build_embed(self):
        self.page=min(max(0,self.page),len(self.PAGES)-1)
        key,label=self.PAGES[self.page]
        embed=discord.Embed(title=f'🛠️ Inazina 도움말 · {label}',description='**카테고리별 명령어와 인자 순서**\n`[ ]` 부분에 값을 입력합니다. 공백이 있는 값은 따옴표로 묶으세요.',color=discord.Color.blue())
        if key=='main':
            embed.description='\n\n'.join(f'**{category_label}**' for category_key,category_label in self.PAGES if category_key!='main')
        titles={'main':(),'products':('📦 상품 관리','🏢 기업 · 배당','🧩 상품생성 인자','📦 상품생성 예시'),
                'citizens':('👥 시민 · 잔고',),'market':('📈 시장 · 환율','📰 사건 설정'),
                'gamble':('🎲 도박 · 세금',),'rates':('🏦 금리 · 법정최고이자',),
                'lottery':('🎟️ 복권 관리',),'national':('🏛️ 국가 명의 일반 기능',),
                'structured':('🧾 ELS 관리',)}[key]
        for field in self.source.fields:
            if any(field.name.startswith(prefix) for prefix in titles):
                embed.add_field(name=field.name,value=field.value,inline=False)
        if key in {'products','citizens','rates'}:
            embed.add_field(name='🔢 add / del / modify',value='**`add`** → 현재값 + 입력값\n**`del`** → 현재값 − 입력값\n**`modify`** → 입력값으로 설정(set)\n예: 현재 100에서 `add 20` → 120, `del 20` → 80, `modify 20` → 20',inline=False)
        if key=='products':
            embed.description+='\n\n**🧩 상품생성 인자 순서**\n'+creation_argument_lines()
            embed.add_field(name='🏷️ 공통 인자 · 숫자 단위',value='`id`는 다른 종목과 겹치지 않는 상품 코드입니다. 두 번째 인자인 상품종류가 뒤의 인자 순서를 정합니다.\n가격·자본금·고정배당금은 결제 통화 기준, 수량은 주/좌/계약 단위입니다. 국채 `bond`의 `total_supply`는 수량이 아닌 발행총액입니다. 이율은 연 %, 만기는 일, 이자·재조정 주기는 초입니다. 증거금 비율 `0.2`는 20%입니다.',inline=False)
            embed.add_field(name='💡 구성 종목 입력',value='펀드 `components`는 `INZST001:0.6,INZCB001:0.4`처럼 입력하며 비중 합계는 1입니다. 주식·회사채·국채·원자재·부동산·특별자산을 지원합니다. `fund_category`는 `equity`(주식형), `bond`(채권형), `mixed`(혼합형), `other`(기타형)이며 생략하면 구성 비중으로 자동 판정합니다.\n추가 JSON은 마지막에 선택적으로 입력하며 기본 인자를 중복 지정하지 않습니다.',inline=False)
            embed.add_field(name='💰 정기배당 · 고정금액 / 비율',value='배당 지원 유형은 끝에 `[배당주기] [주·좌당 금액 또는 비율%]`을 함께 선택 입력합니다. `5`는 주·좌당 고정 5, `5%`는 **지급 시점 주가·상품가격의 5%**입니다. 두 방식 중 하나만 적용합니다. 주기는 초 또는 `1일`·`1주`·`1달`(30일)입니다.\n예: `$상품생성 ST company 회사 게임 1000000 10000 100 INZ 1주 5%`\n변경: `$정기배당수정 ST 1주 5` 또는 `$정기배당수정 ST 1주 5%`\n생성 시 생략하면 배당 없음, 수정 시 생략하면 기존값 유지입니다. 특별배당은 고정 주·좌당 금액으로 실행합니다.',inline=False)
            embed.add_field(name='⏯️ 배당 활성 · 우선주 · 상품수정',value='`$정기배당활성 ST off/on`으로 배당 설정을 보존한 채 끄고 켭니다. 재활성 시 한 주기 뒤 지급하며 중단기간은 소급하지 않습니다. 생성 시 마지막 JSON `{"dividend_is_active":0}`으로 OFF를 지정할 수 있습니다.\n우선주는 `preferred`로 생성하고 보통주회사ID를 입력합니다. `$상품수정`은 생성과 같은 순서이며 가격만 변경하려면 `$상품가격수정`을 사용하세요.',inline=False)
            embed.add_field(name='🏦 대출연장 설정',value='`loan` 유형 끝의 선택 인자는 `extension_min_credit_score extension_days`입니다. 연장일수 0이면 연장이 허용되지 않습니다.',inline=False)
        if key=='lottery':
            embed.add_field(name='📌 적용 시점',value='복권 가격은 **다음 개설 회차**부터 적용됩니다. 번호 변경은 **현재 미추첨 회차**에 적용되며 추첨 후 관리자 지정 여부를 표시합니다.',inline=False)
        if key=='structured':
            embed.add_field(name='🏅 우선주 설정',value='`preferred` 유형은 보통주 법인의 현금 계정과 연결되며 의결권이 없습니다. 마지막 선택 JSON에서 `{"dividend_priority":1,"cumulative_dividend":1,"liquidation_value_per_share":1000,"dividend_funding":"issuer"}`로 배당 우선·누적·주당 청산 우선한도·법인 재원을 지정합니다.\n배당주기와 `고정액/비율%`은 기존 배당 인자를 사용합니다. 누적 미지급액은 해당 회차의 확정 보유자에게 귀속되며 `/내지갑`에서 확인합니다.',inline=False)
            embed.add_field(name='💵 법인 배당 · 청산 재원',value='`$법인잔고수정 [보통주회사id] [add/del/modify] [금액]`으로 법인 현금을 조정합니다. 자본금은 현금잔고와 별개입니다.\n`$법인청산 [보통주회사id]`는 회사채 채무 → 확정 미지급 배당 → 우선주 한도 → 보통주 잔여 순으로 배분합니다. 활성 ELS·옵션·선물의 기초/발행 법인은 청산을 먼저 차단합니다.',inline=False)
            embed.add_field(name='📜 ELS 생성 인자',value='`이름 → 액면 → 총좌수 → 기초코드목록 → 연쿠폰% → 만기일수 → 평가일수목록 → 조기상환기준%목록 → 만기상환기준% → 낙인기준% → 모집일수 → 거래소ID`\n목록은 쉼표로 구분합니다. 기초는 주식·거래소지수 1~3개이며 ELS 청약·상환 통화는 NWX입니다. 지수는 `__exchange_index__:INZ`처럼 입력합니다. 기본 발행자는 국가이며 선택 JSON `{"issuer_code":"ST"}`로 법인을 지정합니다.',inline=False)
            embed.add_field(name='📦 ELS 예시',value='```\n$상품생성 E1 els "주가연동 ELS" 1000 100 ST,__exchange_index__:INZ 12 180 30,60,90 90,85,80 75 60 7 INZ\n```\n모집 중 `/매수`로 액면 청약합니다. 모집 종료 후 최초기준가와 보유량을 확정하며 이후 추가 청약·매도·중도환매는 없습니다. `/그래프 종목:E1`은 기초자산 선택과 상환·낙인 기준선을 제공합니다.',inline=False)
            embed.add_field(name='📉 상환 · 낙인',value='가장 낮은 기초가격/최초기준가 비율로 평가합니다. 평가일에 기준 이상이면 원금+예정 경과일 쿠폰으로 조기상환합니다. 만기 기준을 충족하거나 낙인이 없으면 원금+쿠폰, 낙인이 있고 만기 기준 미달이면 원금×평가비율을 지급합니다. 낙인은 기준 **이하**, 상환은 기준 **이상**입니다.\n청약 원금과 발행자의 최대 쿠폰을 예치하며 수익에만 세금을 적용합니다. 모집 중 `$ELS취소`는 청약금과 쿠폰예치금을 환불합니다.',inline=False)
        embed.set_footer(text=f'🔒 관리자 전용 · {self.page+1}/{len(self.PAGES)} 페이지 · $도움말 / $help')
        self.previous.disabled=self.page==0
        self.next.disabled=self.page==len(self.PAGES)-1
        for index,option in enumerate(self.category_select.options):option.default=index==self.page
        return embed

    async def select_category(self,interaction):
        self.page=next(index for index,(key,_) in enumerate(self.PAGES) if key==self.category_select.values[0])
        await interaction.response.edit_message(embed=self.build_embed(),view=self)

    @discord.ui.button(label='◀ 이전',style=discord.ButtonStyle.secondary,row=1)
    async def previous(self,interaction,button):
        self.page-=1
        await interaction.response.edit_message(embed=self.build_embed(),view=self)

    @discord.ui.button(label='다음 ▶',style=discord.ButtonStyle.secondary,row=1)
    async def next(self,interaction,button):
        self.page+=1
        await interaction.response.edit_message(embed=self.build_embed(),view=self)
