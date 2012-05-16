from __future__ import absolute_import
import logging
import kaa

from ..utils import load_plugins
from ..config import config
from .base import NotifierError

log = logging.getLogger('stagehand.notifiers')
plugins, plugins_broken = load_plugins('notifiers', globals())

@kaa.coroutine()
def notify(episodes, skip=[]):
    for name in config.notifiers.enabled:
        if name not in plugins or name in skip:
            continue
        notifier = plugins[name].Notifier()
        try:
            yield notifier.notify(episodes)
        except NotifierError, e:
            log.warning('notifier %s failed: %s', name, e.args[0])
        except Exception:
            log.exception('notifier failed with unhandled error')
