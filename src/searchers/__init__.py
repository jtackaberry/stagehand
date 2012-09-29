from __future__ import absolute_import
import logging
import re
from datetime import timedelta
import kaa

from ..utils import load_plugins, invoke_plugins
from ..config import config
from .base import SearcherError

log = logging.getLogger('stagehand.searchers')
plugins, plugins_broken  = load_plugins('searchers', globals())


@kaa.coroutine()
def start(manager):
    """
    Called when the manager is starting.
    """
    yield invoke_plugins(plugins, 'start', manager)


@kaa.coroutine(progress=True)
def search(progress, series, episodes, skip=[]):
    try:
        earliest = min(ep.airdate for ep in episodes if ep.airdate)
    except ValueError:
        # Empty sequence: no eps had an airdate.
        earliest = None
    if earliest:
        # Allow for episodes to be posted 10 days before the supposed
        # air date.
        earliest = (earliest - timedelta(days=10)).strftime('%Y-%m-%d')

    # XXX: should probably review these wild-ass min size guesses
    mb_per_min = 5.5 if series.cfg.quality == 'HD' else 3
    min_size = (series.runtime or 30) * mb_per_min * 1024 * 1024
    # FIXME: magic factor
    ideal_size = min_size * (10 if series.cfg.quality == 'Any' else 5)

    tried = set()
    always = [name for name in plugins if plugins[name].Searcher.ALWAYS_ENABLED]
    for name in config.searchers.enabled + always:
        if name not in plugins or name in skip or name in tried:
            continue
        tried.add(name)
        searcher = plugins[name].Searcher()
        try:
            results = yield searcher.search(series, episodes, earliest, min_size, ideal_size, series.cfg.quality)
        except SearcherError as e:
            log.error('%s failed: %s', name, e.args[0])
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
