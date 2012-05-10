from __future__ import absolute_import
import logging
import kaa

from ..utils import load_plugins
from ..config import config
from .base import RetrieverError

log = logging.getLogger('stagehand.retrievers')
plugins = load_plugins('retrievers', globals())

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
            yield retriever.retrieve(progress, result, outfile, episode)
        except RetrieverError, e:
            log.warning('retriever %s failed to retrieve %s: %s', name, result.filename, e.args[0])
        except Exception:
            log.exception('retriever failed with unhandled error')
        else:
            return

    if not tried:
        raise RetrieverError('No enabled retriever found for the given result')
    else:
        raise RetrieverError('No retriever plugins were able to fetch the file')
