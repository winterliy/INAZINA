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


def creation_argument_lines():
    from managers.admin_commands_manager import CREATE_ARGS
    return '\n'.join(f"**`{'company' if kind=='stock' else kind}`** → `{' '.join(fields)}`" for kind,fields in CREATE_ARGS.items())


def admin_help_embed(national_commands):
    from managers.admin_commands_manager import USAGE
    embed=discord.Embed(title='🛠️ Inazina · 관리자 도움말',
        description='**관리자 설정과 국가 명의 기능을 한곳에서 확인하세요.**\n명령어의 `[ ]` 부분에 값을 입력합니다.',color=discord.Color.blue())
    groups=(('📦 상품 관리',('상품생성','상품수정','상품가격수정','상품삭제')),
            ('🏢 기업 · 배당',('증자','정기배당수정','특별배당실시','합병')),
            ('👥 시민 · 잔고',('등록','기본소득','잔고수정')),
            ('📈 시장 · 환율',('시장분위기','상품분위기','환율수정','휴장','개장','장일시정지','장재개','서킷브레이커','사이드카')),
            ('🎲 도박 · 세금',('도박확률','도박리턴값','세율')),
            ('🏦 금리 · 법정이자',('금리수정','법정이자수정')),
            ('🎟️ 복권 관리',('복권가격설정','복권당첨번호보기','복권당첨번호변경')))
    for title,commands in groups:
        embed.add_field(name=title,value='\n'.join(f'**`${command}`**\n{USAGE[command] or "인자 없이 실행"}' for command in commands),inline=False)
    embed.add_field(name='📰 사건 설정',value='**`$사건`** → DM 버튼 → 모달\n상품코드 · 호재/악재 · 기간 · 변동률 · 선택 목표가격',inline=False)
    embed.add_field(name='🏛️ 국가 명의 일반 기능',value=' · '.join(f'`$'+command+'`' for command in sorted(national_commands))+'\n**국고의 잔액·보유자산·담보로 실행합니다.**',inline=False)
    embed.add_field(name='💡 입력 예시',value='```\n$상품가격수정 INZST001 add 100\n$잔고수정 사용자ID modify 1000000\n$환율수정 USD EUR 0.9\n$금리수정 modify 3\n```',inline=False)
    embed.description+='\n\n**🧩 상품생성 인자 순서**\n'+creation_argument_lines()
    embed.add_field(name='📦 상품생성 예시',value='```\n$상품생성 SV saving "새 적금" 3 1000 30 86400 1 Inazina\n$상품생성 ST company "새 회사" 게임 1000000 10000 100 INZ\n```\n**추가 JSON은 마지막 선택 인자**입니다. 공백이 있는 이름은 따옴표로 묶으세요.',inline=False)
    embed.add_field(name='🔒 사용 안내',value='**DB role=admin 전용** · 결과는 개인 DM\n응답 후 입력 채팅 삭제 · DM에서 추가 인증 입력 없음\n국가 기능은 인자 없이 실행하면 입력 창을 엽니다.',inline=False)
    embed.set_footer(text='Inazina · $도움말 / $help')
    return embed
