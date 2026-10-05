from functions.currency_display import relative_embed
import asyncio
import io
import json
import discord


def rules_embed(round):
    rules = round['rules']
    reserve = 100-rules['prize_percent']-rules['treasury_percent']
    embed = discord.Embed(title=f"🎟️ 복권 규칙 · {round['round_id']}회",description=(
        f"1~45에서 중복 없는 번호 6개 · 자동 선택 가능\n"
        f"한 장 **{rules['ticket_price']:,} NWX** · 회차당 최대 **{rules['max_tickets']}장**\n"
        f"판매 마감 <t:{round['close_epoch']}:F> · 추첨 <t:{round['draw_epoch']}:F>\n"
        f"판매액 배분: 상금 {rules['prize_percent']}% · 국고 {rules['treasury_percent']}% · 준비금 {reserve}%\n"
        '1등 6개 / 2등 5개+보너스 / 3등 5개 / 4등 4개 / 5등 3개\n'
        '등수별 풀 배분 50/10/15/15/10%, 당첨 장수로 균등 분배합니다.\n'
        '무당첨·정수 나눗셈 잔액은 등수별로 이월합니다. 상금은 세금 공제 후 지갑에 자동 입금합니다.\n'
        '1등 확률 1 / 8,145,060 · 게임 NWX 복권입니다.'),color=discord.Color.gold())
    embed.add_field(name='추첨 사전 약속 SHA-256',value=f"`{round['commitment']}`",inline=False)
    embed.set_footer(text='OS 난수와 사전 약속 해시를 사용합니다. 운영자가 서버·비밀값을 관리하는 신뢰 한계가 있습니다.')
    return embed


class LotteryPurchaseView(discord.ui.View):
    def __init__(self,bot,owner,round,quantity,numbers,request_id):
        super().__init__(timeout=300)
        self.bot,self.owner,self.round = bot,str(owner),round
        self.quantity,self.numbers,self.request_id = quantity,numbers,str(request_id)

    async def interaction_check(self,interaction):
        if str(interaction.user.id)==self.owner:
            return True
        await interaction.response.send_message('❌ 본인의 구매 요청만 확인할 수 있습니다.',ephemeral=True)
        return False

    @discord.ui.button(label='구매 확정',style=discord.ButtonStyle.success)
    async def confirm(self,interaction,button):
        await interaction.response.defer(ephemeral=True)
        try:
            result = await asyncio.to_thread(self.bot.lottery_mgr.purchase,self.owner,self.quantity,self.numbers,self.request_id,expected_round=self.round['round_id'])
        except ValueError as exc:
            await interaction.followup.send(f'❌ {exc}',ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        embed = discord.Embed(title=f"✅ 복권 구매 · {result['round_id']}회",description=f"{len(result['tickets'])}장 · 합계 **{result['price']*len(result['tickets']):,} NWX**\n/내복권에서 번호와 지급 결과를 확인하세요.",color=discord.Color.green())
        display=getattr(self,'money_display',None)
        if display:embed=display.embed(embed)
        lines = [f"#{t['ticket_id']} · {' '.join(map(str,t['numbers']))}" for t in result['tickets']]
        if len(lines)<=10:
            embed.add_field(name='발권 번호',value='\n'.join(lines),inline=False)
            await interaction.followup.send(embed=embed,ephemeral=True)
        else:
            file = discord.File(io.BytesIO('\n'.join(lines).encode('utf-8')),filename='lottery_tickets.txt')
            await interaction.followup.send(embed=embed,file=file,ephemeral=True)
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(label='취소',style=discord.ButtonStyle.secondary)
    async def cancel(self,interaction,button):
        for item in self.children:
            item.disabled = True
        self.stop()
        await interaction.response.edit_message(content='복권 구매를 취소했습니다.',embed=None,view=self)


class LotteryTicketsView(discord.ui.View):
    PAGE_SIZE = 10

    def __init__(self,owner,rows):
        super().__init__(timeout=300)
        self.owner,self.rows,self.page = str(owner),rows,0

    async def interaction_check(self,interaction):
        if str(interaction.user.id)==self.owner:
            return True
        await interaction.response.send_message('❌ 본인의 복권만 조회할 수 있습니다.',ephemeral=True)
        return False

    @relative_embed
    def build_embed(self):
        pages = max(1,(len(self.rows)+self.PAGE_SIZE-1)//self.PAGE_SIZE)
        self.page = min(max(0,self.page),pages-1)
        embed = discord.Embed(title='🎟️ 내 복권',color=discord.Color.gold())
        for row in self.rows[self.page*self.PAGE_SIZE:(self.page+1)*self.PAGE_SIZE]:
            numbers = ' '.join(map(str,json.loads(row['numbers_json'])))
            status = '추첨·지급 대기' if row['rank'] is None else '미당첨' if row['rank']==0 else f"{row['rank']}등 · 세전 {row['gross'] or 0:,} NWX / 세금 {row['tax'] or 0:,} NWX / 입금 {row['net'] or 0:,} NWX"
            embed.add_field(name=f"{row['round_id']}회 · 티켓 #{row['ticket_id']}",value=f'{numbers}\n{status}',inline=False)
        if not self.rows:
            embed.description='구매한 복권이 없습니다. /복권구매에서 발권할 수 있습니다.'
        embed.set_footer(text=f'{len(self.rows)}장 · {self.page+1}/{pages} 페이지')
        self.previous.disabled = self.page==0
        self.next.disabled = self.page==pages-1
        return embed

    @discord.ui.button(label='◀ 이전',style=discord.ButtonStyle.secondary)
    async def previous(self,interaction,button):
        self.page -= 1
        await interaction.response.edit_message(embed=self.build_embed(),view=self)

    @discord.ui.button(label='다음 ▶',style=discord.ButtonStyle.secondary)
    async def next(self,interaction,button):
        self.page += 1
        await interaction.response.edit_message(embed=self.build_embed(),view=self)


def result_embed(round):
    embed = discord.Embed(title=f"🎟️ 복권 결과 · {round['round_id']}회",color=discord.Color.gold())
    if not round['winning']:
        embed.description=f"아직 추첨하지 않았습니다. 추첨 <t:{round['draw_epoch']}:F>"
        embed.add_field(name='사전 약속',value=round['commitment'],inline=False)
        return embed
    embed.description=f"당첨번호 **{' '.join(map(str,round['winning']))}** · 보너스 **{round['bonus']}**\n상태: {round['status']} · 판매액 {round['sales_total']:,} NWX"
    for rank,row in round['result'].items():
        embed.add_field(name=f'{rank}등 · {row["winners"]}장',value=f"장당 세전 {row['gross_per_ticket']:,} NWX · 이월 {row['carry']:,} NWX",inline=False)
    for name,key in [('사전 약속 SHA-256','commitment'),('공개 비밀값','secret'),('추첨 시 OS 난수','entropy')]:
        embed.add_field(name=name,value=f"`{round[key]}`",inline=False)
    embed.set_footer(text='관리자가 당첨번호를 지정한 회차입니다. 난수로 번호를 재현하는 검증은 적용되지 않습니다.' if round.get('admin_override') else '검증: SHA-256(비밀값 bytes)=사전 약속. LotteryManager.draw_numbers(비밀값, OS 난수)로 번호 재현 가능.')
    return embed
