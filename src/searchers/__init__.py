from __future__ import absolute_import
import logging
import re
import kaa

from ..utils import load_plugins
from ..config import config
from .base import SearcherError

log = logging.getLogger('stagehand.searchers')
plugins = load_plugins('searchers', globals())

@kaa.coroutine(progress=True)
def search(progress, series, episodes, date=None, min_size=None, ideal_size=None, quality='HD', skip=[]):
    tried = set()
    always = [name for name in plugins if plugins[name].Searcher.ALWAYS_ENABLED]
    for name in config.searchers.enabled + always:
        if name not in plugins or name in skip or name in tried:
            continue
        tried.add(name)
        searcher = plugins[name].Searcher()
        try:
            results = yield searcher.search(series, episodes, date, min_size, ideal_size, quality)
        except SearcherError, e:
            log.warning('%s failed: %s', name, e.args[0])
        except Exception:
            log.exception('%s failed with unhandled error', name)
        else:
            # FIXME: if some episodes don't have results, need to try other searchers.
            if results:
                yield results
                return
            else:
                log.debug2('%s found no results', name)
    yield {}


@kaa.coroutine()
def get_search_entity(search_result):
    if search_result.searcher not in plugins:
        raise SearcherError('Searcher plugin %s not present' % search_result.searcher)
    entity = yield plugins[search_result.searcher].Searcher().get_search_entity(search_result)
    yield entity
