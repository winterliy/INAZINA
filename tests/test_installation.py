import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
import discord
import test_inazina as fixtures
from functions.slash_commands import SlashCommandRegistry
from functions.admin_access import BOOTSTRAP_ADMIN_IDS
from functions.admin_embeds import admin_help_embed
from managers.admin_commands_manager import AdminCommandsManager,COMMANDS

class InstallationTests(unittest.TestCase):
    setUp=fixtures.InazinaTests.setUp
    tearDown=fixtures.InazinaTests.tearDown

    def interaction(self,administrator=True,guild=True):
        response=SimpleNamespace(done=False,send_message=AsyncMock())
        response.is_done=lambda:response.done
        async def defer(**kwargs):response.done=True
        response.defer=AsyncMock(side_effect=defer)
        return SimpleNamespace(guild=SimpleNamespace(id=123456,name='신청 서버',owner_id=900) if guild else None,
            user=SimpleNamespace(id=777,display_name='서버 관리자',guild_permissions=SimpleNamespace(administrator=administrator)),
            response=response,followup=SimpleNamespace(send=AsyncMock()),extras={})

    def registry(self):
        self.bot.get_user=Mock(return_value=SimpleNamespace(send=AsyncMock()))
        self.bot.fetch_user=AsyncMock()
        registry=SlashCommandRegistry(self.bot)
        registry._prepare=AsyncMock(side_effect=AssertionError('설치 신청에는 시민 등록이 필요 없어야 함'))
        return registry

    def test_command_is_server_admin_only_and_old_prefix_is_removed_from_help(self):
        self.bot.tree=SimpleNamespace(add_command=Mock())
        registry=SlashCommandRegistry(self.bot);registry.register()
        install=registry.commands['설치']
        self.assertTrue(install.default_permissions.administrator)
        self.assertEqual([p.name for p in install.parameters],['국명','화폐'])
        self.assertTrue(install.guild_only)
        self.assertIn('서버설치',COMMANDS);self.assertNotIn('서버국적',COMMANDS)
        help_text=str(admin_help_embed(set()).to_dict())
        self.assertIn('$서버설치',help_text);self.assertNotIn('$서버국적',help_text)

    def test_request_delivers_server_id_country_currency_and_approval_without_installing(self):
        registry=self.registry();interaction=self.interaction()
        # Also deliver to a SQL administrator, without a duplicate bootstrap DM.
        self.db.users.create_user('888','INAZINA 관리자','낙원',0,'admin')
        self.db.users.create_user(next(iter(BOOTSTRAP_ADMIN_IDS)),'초기 관리자','낙원',0,'admin')
        asyncio.run(registry.request_installation(interaction,'  A 국가  ',' nwx '))
        self.assertEqual(self.bot.get_user.return_value.send.await_count,2)
        embed=self.bot.get_user.return_value.send.call_args.kwargs['embed']
        fields={field.name:field.value for field in embed.fields}
        self.assertIn('123456',fields['🆔 서버 ID'])
        self.assertEqual(fields['🏳️ 국명'],'A 국가');self.assertEqual(fields['💱 기본화폐'],'NWX')
        self.assertIn("$서버설치 123456 'A 국가' NWX",fields['🛠️ 설치 승인 명령어'])
        self.assertEqual(self.db.fx.execute_one('SELECT COUNT(*) FROM guild_countries')[0],0)
        self.assertIsNone(self.db.users.get_user('777'))
        registry._prepare.assert_not_awaited()
        self.assertTrue(interaction.followup.send.call_args.kwargs['ephemeral'])
        self.assertIn('전달했습니다',interaction.followup.send.call_args.kwargs['content'])

    def test_ordinary_member_dm_and_invalid_input_cannot_send_requests(self):
        registry=self.registry()
        for interaction,country,currency in [(self.interaction(False),'낙원','NWX'),(self.interaction(guild=False),'낙원','NWX'),(self.interaction(),'낙원\n가짜','NWX')]:
            asyncio.run(registry.request_installation(interaction,country,currency))
            self.assertIn('❌',interaction.response.send_message.call_args.kwargs['content'])
            self.assertTrue(interaction.response.send_message.call_args.kwargs['ephemeral'])
        self.bot.get_user.assert_not_called()

    def test_server_owner_can_request_and_uncached_admin_is_fetched(self):
        registry=self.registry();interaction=self.interaction(False)
        interaction.guild.owner_id=777
        self.bot.get_user.return_value=None
        recipient=SimpleNamespace(send=AsyncMock());self.bot.fetch_user.return_value=recipient
        asyncio.run(registry.request_installation(interaction,'낙원','낙원자화'))
        self.bot.fetch_user.assert_awaited()
        fields={f.name:f.value for f in recipient.send.call_args.kwargs['embed'].fields}
        self.assertEqual(fields['💱 기본화폐'],'NWX')

    def test_blocked_admin_dm_reports_failure_and_does_not_install(self):
        registry=self.registry();interaction=self.interaction()
        error=discord.Forbidden(SimpleNamespace(status=403,reason='Forbidden'),{'code':50007,'message':'Cannot send messages to this user'})
        self.bot.get_user.return_value.send.side_effect=error
        asyncio.run(registry.request_installation(interaction,'낙원','NWX'))
        self.assertIn('전달하지 못했습니다',interaction.followup.send.call_args.kwargs['content'])
        self.assertEqual(self.db.fx.execute_one('SELECT COUNT(*) FROM guild_countries')[0],0)

    def test_new_install_prefix_applies_requested_server_and_preserves_citizens(self):
        commands=AdminCommandsManager(self.bot)
        result=commands.execute('admin','서버설치','123456 낙원 NWX')
        self.assertIn('✅',result)
        self.assertEqual(self.db.fx.execute_one("SELECT nationality FROM guild_countries WHERE guild_id='123456'")[0],'낙원')
        self.assertEqual(self.db.users.get_user('citizen')['nationality'],'미국')
        with self.assertRaises(ValueError):commands.execute('citizen','서버설치','123456 낙원 NWX')

if __name__=='__main__':unittest.main()
