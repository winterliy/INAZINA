"""Common owner-only check for user-specific Discord views and modals."""


async def owner_interaction_check(interaction, owner_id, message):
    if str(interaction.user.id) == str(owner_id):
        return True
    await interaction.response.send_message(message, ephemeral=True)
    return False
