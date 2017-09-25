import asyncio

from ..utils import load_plugins, invoke_plugins
from .base import ProviderError

plugins, broken_plugins = load_plugins('providers', ['thetvdb'])

@asyncio.coroutine
def start(manager):
    """
    Called when the manager is starting.
    """
    yield from invoke_plugins(plugins, 'start', manager)
    for name, error in broken_plugins.items():
        log.warning('failed to load provider plugin %s: %s', name, error)
