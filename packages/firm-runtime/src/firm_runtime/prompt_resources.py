"""Load firm-authored prose; substitute trusted templates once, keeping values literal."""

import json
from functools import cache
from importlib.resources import files
from string import Template


@cache
def prompt(name: str) -> str:
    return files("firm_runtime").joinpath("prompts", name).read_text(encoding="utf-8").strip()


@cache
def _group(name: str) -> dict[str, str]:
    return json.loads(prompt(name + ".json"))


def render(group: str, key: str, **values: object) -> str:
    """Only resource text is a template; substituted mandates, notes and mail are data."""
    return Template(_group(group)[key]).substitute(values)
