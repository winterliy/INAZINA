"""Administrator help, grouped into private category pages."""
import discord
from functions.admin_embeds import admin_help_embed,admin_response,creation_argument_lines


class AdminHelpView(discord.ui.View):
    PAGES=(('main','📖 메인'),('products','📦 상품 관리'),('citizens','👥 시민 · 잔고'),('market','📈 시장 · 환율 · 사건'),
           ('gamble','🎲 도박 · 세금'),('rates','🏦 금리 · 법정이자'),('lottery','🎟️ 복권'),('national','🏛️ 국가 명의 기능'))
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
                'gamble':('🎲 도박 · 세금',),'rates':('🏦 금리 · 법정이자',),
                'lottery':('🎟️ 복권 관리',),'national':('🏛️ 국가 명의 일반 기능',)}[key]
        for field in self.source.fields:
            if any(field.name.startswith(prefix) for prefix in titles):
                embed.add_field(name=field.name,value=field.value,inline=False)
        if key in {'products','citizens','rates'}:
            embed.add_field(name='🔢 add / del / modify',value='**`add`** → 현재값 + 입력값\n**`del`** → 현재값 − 입력값\n**`modify`** → 입력값으로 설정(set)\n예: 현재 100에서 `add 20` → 120, `del 20` → 80, `modify 20` → 20',inline=False)
        if key=='products':
            embed.description+='\n\n**🧩 상품생성 인자 순서**\n'+creation_argument_lines()
            embed.add_field(name='💡 구성 종목 입력',value='펀드 `components`는 `INZST001:0.5,INZST002:0.5`처럼 입력합니다. 비중 합계는 1입니다.\n추가 JSON은 마지막에 선택적으로 입력하며 기본 인자를 중복 지정하지 않습니다.',inline=False)
            embed.add_field(name='💰 배당 · 우선주 · 상품수정',value='배당 지원 유형은 끝에 `dividend_cycle_seconds dividend_per_share`를 입력합니다. 주기는 `1일`, `1주`, `1달`도 가능하며 두 값을 생략하면 기존값을 유지합니다.\n우선주 유형은 `preferred`이며 `parent_company_code`에 보통주 법인 id를 입력합니다.\n`$상품수정`은 상품생성과 같은 인자 순서입니다. 가격만 변경할 때는 `$상품가격수정`을 사용하세요.',inline=False)
            embed.add_field(name='🏦 대출연장 설정',value='`loan` 유형 끝의 선택 인자는 `extension_min_credit_score extension_days`입니다. 연장일수 0이면 연장이 허용되지 않습니다.',inline=False)
        if key=='lottery':
            embed.add_field(name='📌 적용 시점',value='복권 가격은 **다음 개설 회차**부터 적용됩니다. 번호 변경은 **현재 미추첨 회차**에 적용되며 추첨 후 관리자 지정 여부를 표시합니다.',inline=False)
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
