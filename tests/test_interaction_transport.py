import asyncio
import io
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from functions.discord_views import PriceMovementView
from functions.interaction_reliability import discord_request, prepare_response
from functions.slash_commands import SlashCommandRegistry


def interaction():
    response = SimpleNamespace(type=None)
    response.is_done = lambda: response.type is not None
    async def defer(**kwargs):
        response.type = (discord.InteractionResponseType.deferred_channel_message
                         if kwargs.get('thinking') else discord.InteractionResponseType.deferred_message_update)
    response.defer = AsyncMock(side_effect=defer)
    response.send_message = AsyncMock()
    response.edit_message = AsyncMock()
    return SimpleNamespace(response=response, extras={}, edit_original_response=AsyncMock(),
                           followup=SimpleNamespace(send=AsyncMock()),
                           message=SimpleNamespace(attachments=[SimpleNamespace(filename='movement.png')]))


class InteractionTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_work_uses_one_response_and_slow_work_acknowledges_first(self):
        fast = interaction()
        result, deferred = await prepare_response(fast, AsyncMock(return_value=42))
        self.assertEqual((result,deferred),(42,False))
        await SlashCommandRegistry._reply(fast,content='ready')
        fast.response.defer.assert_not_awaited()
        fast.response.send_message.assert_awaited_once()
        slow = interaction()
        release = asyncio.Event()
        async def work():
            await release.wait()
            return 99
        task = asyncio.create_task(prepare_response(slow,work,thinking=True,delay=0))
        try:
            for _ in range(10):
                await asyncio.sleep(0)
                if slow.response.defer.await_count:
                    break
            slow.response.defer.assert_awaited_once_with(ephemeral=True,thinking=True)
            self.assertFalse(task.done())
        finally:
            release.set()
        self.assertEqual(await task,(99,True))
        await SlashCommandRegistry._reply(slow,content='ready')
        slow.edit_original_response.assert_awaited_once_with(content='ready')
        slow.followup.send.assert_not_awaited()

    async def test_deferred_file_edits_original_and_later_message_is_followup(self):
        target = interaction()
        await target.response.defer(thinking=True)
        view = PriceMovementView(SimpleNamespace(),'owner','기준금리')
        await SlashCommandRegistry._reply(target,view=view,file=discord.File(io.BytesIO(b'png'),filename='movement.png'))
        options = target.edit_original_response.call_args.kwargs
        self.assertNotIn('ephemeral',options)
        self.assertEqual(options['attachments'][0].fp.read(),b'png')
        options['attachments'][0].close()
        self.assertIsNotNone(view._uploaded_chart_digest)
        await SlashCommandRegistry._reply(target,content='second')
        target.followup.send.assert_awaited_once_with(content='second',ephemeral=True)

    async def test_library_exhausted_error_is_not_replayed_and_ack_failure_cancels_work(self):
        error = discord.DiscordServerError(SimpleNamespace(status=503,reason='Unavailable'),'unavailable')
        send = AsyncMock(side_effect=error)
        with self.assertRaises(discord.DiscordServerError):
            await discord_request(send)
        send.assert_awaited_once()
        target = interaction()
        target.response.defer.side_effect = error
        cancelled = asyncio.Event()
        async def work():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with self.assertRaises(discord.DiscordServerError):
            await prepare_response(target,work,delay=0)
        self.assertTrue(cancelled.is_set())

    async def test_identical_png_retains_attachment_and_failure_does_not_cache_new_upload(self):
        view = PriceMovementView(SimpleNamespace(),'owner','기준금리')
        view.remember_uploaded_chart(b'old')
        view.render = Mock(return_value=(discord.Embed(title='updated text'),b'old'))
        same = interaction()
        await view._edit_chart(same)
        self.assertNotIn('attachments',same.edit_original_response.call_args.kwargs)
        view.render.return_value = (discord.Embed(),b'new')
        failed = interaction()
        failed.edit_original_response.side_effect = discord.HTTPException(SimpleNamespace(status=502,reason='bad'),'bad')
        with patch('functions.discord_views.send_interaction_notice',new_callable=AsyncMock):
            await view._edit_chart(failed)
        target = interaction()
        await view._edit_chart(target)
        self.assertEqual(target.edit_original_response.call_args.kwargs['attachments'][0].fp.read(),b'new')
        missing = interaction()
        missing.message.attachments = []
        await view._edit_chart(missing)
        self.assertIn('attachments',missing.edit_original_response.call_args.kwargs)
        for call in (failed,target,missing):
            call.edit_original_response.call_args.kwargs['attachments'][0].close()

    async def test_burst_skips_obsolete_work_and_only_publishes_latest_selection(self):
        view = PriceMovementView(SimpleNamespace(),'owner','기준금리')
        view.period_seconds = 60
        started, release = threading.Event(),threading.Event()
        periods = []
        def render(snapshot):
            periods.append(snapshot.period_seconds)
            if len(periods) == 1:
                started.set()
                if not release.wait(2):
                    raise AssertionError('worker did not resume')
            return discord.Embed(title=str(snapshot.period_seconds)),b'png'
        first, middle, last = interaction(),interaction(),interaction()
        with patch.object(PriceMovementView,'render',render):
            t1 = asyncio.create_task(view._edit_chart(first))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait,2))
                view.period_seconds = 300
                t2 = asyncio.create_task(view._edit_chart(middle))
                await asyncio.sleep(0)
                view.period_seconds = 600
                t3 = asyncio.create_task(view._edit_chart(last))
                await asyncio.sleep(0)
            finally:
                release.set()
            await asyncio.gather(t1,t2,t3)
        self.assertEqual(periods,[60,600])
        first.edit_original_response.assert_not_awaited()
        middle.edit_original_response.assert_not_awaited()
        last.edit_original_response.assert_awaited_once()
        self.assertEqual(last.edit_original_response.call_args.kwargs['embed'].title,'600')
        last.edit_original_response.call_args.kwargs['attachments'][0].close()
