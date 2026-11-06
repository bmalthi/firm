class FirmError(ValueError):
    """An expected, caller-visible contract error."""


class ConfigurationError(FirmError):
    """The owner's configuration is wrong, which no role can correct."""
