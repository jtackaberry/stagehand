import logging
import asyncio

from ..utils import load_plugins, invoke_plugins
from ..config import config
from .base import NotifierError

log = logging.getLogger('stagehand.notifiers')
plugins, broken_plugins = load_plugins('notifiers', ['email', 'xbmc'])

@asyncio.coroutine
def start(manager):
    """
    Called when the manager is starting.
    """
    yield from invoke_plugins(plugins, 'start', manager)
    for name, error in broken_plugins.items():
        log.warning('failed to load notifier plugin %s: %s', name, error)


@asyncio.coroutine
def notify(episodes, skip=[], loop=None):
    for name in config.notifiers.enabled:
        if name not in plugins or name in skip:
            continue
        notifier = plugins[name].Notifier(loop=loop)
        try:
            yield from notifier.notify(episodes)
        except NotifierError as e:
            log.error('notifier %s failed: %s', name, e.args[0])
        except Exception:
            log.exception('notifier failed with unhandled error')
