"""Registration flow errors shared by slash commands and the bot error handler."""


class RegistrationRequired(Exception):
    """Raised after an unregistered user's command is redirected to /등록."""
