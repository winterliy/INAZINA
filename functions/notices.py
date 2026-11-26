"""Everything the bot says on Discord is a decorated embed, not bare text.

A message that starts with an emoji (✅ ❌ ⛔ ⚠️ ⏰) keeps its meaning: the emoji picks the colour and the title, and is not repeated in the
text. Text without one is a plain information notice. Messages that already carry their own embed (wallets, charts, panels) are left as they are.
"""
import functools

import discord

FOOTER = "INAZINA"
LIMIT = 4096  # an embed description holds this much

# leading emoji -> (title, colour)
TONES = {
    "✅": ("✅ 완료", discord.Color.green),
    "❌": ("❌ 실패", discord.Color.red),
    "⛔": ("⛔ 제한", discord.Color.red),
    "⚠️": ("⚠️ 주의", discord.Color.orange),
    "⚠": ("⚠️ 주의", discord.Color.orange),
    "⏰": ("⏰ 시간 초과", discord.Color.orange),
}
INFO = ("ℹ️ 안내", discord.Color.blurple)


def notice_embed(text):
    """The text as a decorated embed (colour and title from a leading emoji)."""
    text = str(text).strip()
    title, colour = INFO
    for emoji, tone in TONES.items():
        if text.startswith(emoji):
            (title, colour), text = tone, text[len(emoji):].strip()
            break
    if len(text) > LIMIT:
        text = text[:LIMIT - 1] + "…"
    embed = discord.Embed(title=title, description=text, color=colour())
    embed.set_footer(text=FOOTER)
    return embed


def notice_kwargs(kwargs):
    """Message options with plain text turned into an embed; anything that already has an embed is left alone."""
    content = kwargs.get("content")
    if not isinstance(content, str) or not content.strip() or kwargs.get("embed") is not None or kwargs.get("embeds"):
        return kwargs
    if "<@" in content:
        return kwargs  # a mention only pings in plain content
    return {**kwargs, "content": None, "embed": notice_embed(content)}


def wrap_sender(original, edit=False):
    """A discord.py sender that turns bare text into a decorated embed.

    The text may be the first positional argument or ``content=``. For an edit (``edit=True``) only a call that clears the old embed
    (``embed=None``) is converted: an edit that leaves the message's embed in place keeps its text line as it is.
    """
    @functools.wraps(original)
    async def sender(self, *args, **kwargs):
        if args and "content" not in kwargs:
            kwargs, args = {"content": args[0], **kwargs}, args[1:]
        if not edit or ("embed" in kwargs and kwargs["embed"] is None):
            kwargs = notice_kwargs({**kwargs, "embed": kwargs.get("embed")}) if edit else notice_kwargs(kwargs)
        return await original(self, *args, **kwargs)
    sender.__notice_wrapped__ = True
    return sender


def install():
    """Wrap the senders once (idempotent): replies, follow-ups, DMs and channel messages, and edits that replace the embed."""
    targets = (
        (discord.InteractionResponse, "send_message", False),
        (discord.InteractionResponse, "edit_message", True),
        (discord.Webhook, "send", False),
        (discord.abc.Messageable, "send", False),
    )
    for owner, name, edit in targets:
        current = getattr(owner, name)
        if not getattr(current, "__notice_wrapped__", False):
            setattr(owner, name, wrap_sender(current, edit=edit))
