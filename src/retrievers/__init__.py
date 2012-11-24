from __future__ import absolute_import
import logging
import kaa

from ..utils import load_plugins, invoke_plugins
from ..config import config
from .base import RetrieverError, RetrieverSoftError, RetrieverHardError, RetrieverAborted, RetrieverAbortedSoft, RetrieverAbortedHard

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
    """
    Given a SearchResult object, retrieve the file using retriever plugins that
    support the result type.
    """
    tried = set()
    always = [name for name in plugins if plugins[name].Retriever.ALWAYS_ENABLED]
    for name in config.retrievers.enabled + always:
        if name not in plugins or name in skip or result.type not in plugins[name].Retriever.SUPPORTED_TYPES or name in tried:
            continue

        tried.add(name)
        retriever = plugins[name].Retriever()
        try:
            yield retriever.retrieve(progress, episode, result, outfile)
        except RetrieverAbortedSoft as e:
            # Happens when the retriever itself aborts the download, e.g. because
            # the file failed to meet the resolution requirements.
            log.info('retriever %s aborted transfer of %s: %s', name, result.filename, e.args[0])
            raise
        except RetrieverAbortedHard as e:
            # Happens when something outside (like the manager) wants to abort
            # retrieval of this episode altogether.
            log.info('transfer of %s was aborted: %s', result.filename, e.args[0])
            raise
        except RetrieverError as e:
            log.error('retriever %s failed to retrieve %s: %s', name, result.filename, e.args[0])
        except Exception:
            log.exception('retriever failed with unhandled error')
        else:
            return

    # TODO: reraise RetrieverHardError if all searchers for this result raised
    # it.
    if not tried:
        raise RetrieverSoftError('No enabled retriever found for the given result (%s)' % result.type)
    else:
        raise RetrieverSoftError('No retriever plugins were able to fetch the file')
