"""Test helper: the text of what the bot sent, whether it went out as plain content or as a decorated embed."""


def text_of(kwargs):
    embed = kwargs.get("embed")
    parts = [kwargs.get("content") or ""]
    if embed is not None:
        parts += [embed.title or "", embed.description or ""] + [f"{field.name} {field.value}" for field in embed.fields]
    return "\n".join(part for part in parts if part)
