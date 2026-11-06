"""The firm's generative image credential: an owner-held key, staged for each run.

The key lives in a file only the owner can read, named by ``[google_cloud]``. Each
role's run receives it as ``GEMINI_API_KEY``, staged into the run's credentials
directory the way a provider token is, so it never enters the firm's
configuration, its database or its input records. What the key may reach is not
this runtime's to enforce: restrict it on the Google Cloud side, to one project
and one API, and give that project a spend cap.
"""

import json
import re
from pathlib import Path

from .errors import FirmError
from .provider_contract import ProviderSetupError
from .provider_support import read_private_credential

# https://cloud.google.com/resource-manager/reference/rest/v1/projects
PROJECT = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
SECRET = "google-cloud-key"
VARIABLE = "GEMINI_API_KEY"
SETTINGS = {"project", "api_key_file"}
MAX_KEY_BYTES = 1024


def key_path(project):
    """Where `[google_cloud]` looks for the key when it names no file."""
    from .accounts import CREDENTIALS_DIR

    return CREDENTIALS_DIR.expanduser() / f"google-cloud-{project}.key"


def validate_google_cloud(config):
    """Check the owner's ``[google_cloud]`` table; the key itself stays on the host."""
    settings = config.get("google_cloud")
    if settings is None:
        return
    if not isinstance(settings, dict) or set(settings) - SETTINGS:
        raise FirmError("google_cloud has unsupported settings")
    project = settings.get("project")
    if not isinstance(project, str) or not PROJECT.fullmatch(project):
        raise FirmError("google_cloud.project must be a Google Cloud project id")
    key_file = settings.setdefault("api_key_file", str(key_path(project)))
    try:
        absolute = isinstance(key_file, str) and Path(key_file).expanduser().is_absolute()
    except RuntimeError:
        absolute = False
    if not absolute:
        raise FirmError("google_cloud requires an absolute api_key_file")


def read_key(path):
    """The one key in ``path``. Faults name the file and never the key."""
    location = Path(path)
    try:
        location = location.expanduser()
        raw = read_private_credential(location, "Google Cloud")
    except (OSError, ProviderSetupError, RuntimeError):
        raise FirmError(
            f"google_cloud api_key_file cannot be read as an owner-private regular file: {location}"
        ) from None
    if len(raw) > MAX_KEY_BYTES:
        raise FirmError(f"google_cloud api_key_file is too large to hold a key: {location}")
    try:
        key = raw.decode().strip()
    except UnicodeDecodeError:
        raise FirmError(f"google_cloud api_key_file is not text: {location}") from None
    # One key on one line: anything else is a file the owner meant differently.
    if not key or any(character.isspace() for character in key):
        raise FirmError(f"google_cloud api_key_file must hold exactly one key: {location}")
    return key


def launch_settings(store):
    """Environment and staged key for a role's run, or None without ``[google_cloud]``."""
    with store.read() as db:
        settings = json.loads(store.firm(db)["config_json"]).get("google_cloud")
    if not settings:
        return None
    return (
        {"GOOGLE_CLOUD_PROJECT": settings["project"]},
        {SECRET: read_key(settings["api_key_file"]).encode()},
        {VARIABLE: SECRET},
    )
