from __future__ import absolute_import
import logging
import kaa

from ..utils import load_plugins, invoke_plugins
from ..config import config
from .base import RetrieverError, RetrieverAborted

log = logging.getLogger('stagehand.retrievers')
plugins, plugins_broken = load_plugins('retrievers', globals())


@kaa.coroutine()
def start(manager):
    """
    Called when the manager is starting.
    """
    yield invoke_plugins(plugins, 'start', manager)


@kaa.coroutine(progress=True)
def retrieve(progress, result, outfile, episode, skip=[]):
    tried = set()
    always = [name for name in plugins if plugins[name].Retriever.ALWAYS_ENABLED]
    for name in config.retrievers.enabled + always:
        if name not in plugins or name in skip or result.type not in plugins[name].Retriever.SUPPORTED_TYPES or name in tried:
            continue

        tried.add(name)
        retriever = plugins[name].Retriever()
        try:
            yield retriever.retrieve(progress, episode, result, outfile)
        except RetrieverAborted as e:
            log.info('retriever %s aborted transfer of %s: %s', name, result.filename, e.args[0])
        except RetrieverError as e:
            log.error('retriever %s failed to retrieve %s: %s', name, result.filename, e.args[0])
        except Exception:
            log.exception('retriever failed with unhandled error')
        else:
            return

    if not tried:
        raise RetrieverError('No enabled retriever found for the given result (%s)' % result.type)
    else:
        raise RetrieverError('No retriever plugins were able to fetch the file')
