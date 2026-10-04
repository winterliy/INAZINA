import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from functions.admin_prefix import AdminPrefixRegistry


class GracefulShutdownTests(unittest.TestCase):
    def test_cancelled_admin_command_does_not_use_closed_http_session(self):
        async def run():
            bot=SimpleNamespace(is_closed=lambda:True)
            registry=AdminPrefixRegistry(bot)
            author=SimpleNamespace(id='admin',bot=False,send=AsyncMock(
                return_value=SimpleNamespace(edit=AsyncMock())))
            message=SimpleNamespace(content='$내지갑',author=author,guild=None,delete=AsyncMock())
            with patch('functions.admin_prefix.admin_allowed',return_value=True), \
                 patch('asyncio.to_thread',new=AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await registry.handle(message)
            message.delete.assert_not_awaited()
        asyncio.run(run())


if __name__=='__main__':
    unittest.main()
