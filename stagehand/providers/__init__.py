import asyncio
import logging

from ..utils import load_plugins, invoke_plugins
from .base import ProviderError

log = logging.getLogger('stagehand.providers')

plugins, broken_plugins = load_plugins('providers', ['thetvdb', 'tvmaze'])

@asyncio.coroutine
def start(manager):
    """
    Called when the manager is starting.
    """
    yield from invoke_plugins(plugins, 'start', manager)
    for name, error in broken_plugins.items():
        log.warning('failed to load provider plugin %s: %s', name, error)
