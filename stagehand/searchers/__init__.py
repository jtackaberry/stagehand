import logging
import re
from datetime import timedelta
import asyncio

from ..utils import load_plugins, invoke_plugins
from ..config import config
from .base import SearcherError, SearchResult

log = logging.getLogger('stagehand.searchers')

plugins, broken_plugins = load_plugins('searchers', ['easynews'])

@asyncio.coroutine
def start(manager):
    """
    Called when the manager is starting.
    """
    yield from invoke_plugins(plugins, 'start', manager)
    for name, error in broken_plugins.items():
        log.warning('failed to load searcher plugin %s: %s', name, error)


@asyncio.coroutine
def search(series, episodes, skip=[], loop=None):
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
    mb_per_min = 2 if series.cfg.quality == 'HD' else 0.5
    min_size = (series.runtime or 30) * mb_per_min * 1024 * 1024
    # FIXME: magic factor
    ideal_size = min_size * (5 if series.cfg.quality == 'Any' else 3)
    log.info('min size=%d ideal size=%d  runtime=%d' , min_size, ideal_size, series.runtime)

    tried = set()
    always = [name for name in plugins if plugins[name].Searcher.ALWAYS_ENABLED]
    for name in config.searchers.enabled + always:
        if name not in plugins or name in skip or name in tried:
            continue
        tried.add(name)
        searcher = plugins[name].Searcher(loop=loop)
        try:
            results = yield from searcher.search(series, episodes, earliest, min_size, ideal_size, series.cfg.quality)
        except SearcherError as e:
            log.error('%s failed: %s', name, e.args[0])
        except Exception:
            log.exception('%s failed with unhandled error', name)
        else:
            # FIXME: if some episodes don't have results, need to try other searchers.
            if results:
                return results
            else:
                log.debug2('%s found no results', name)
    return {}
