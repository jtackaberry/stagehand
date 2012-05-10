from __future__ import absolute_import
import logging
import kaa

from ..utils import load_plugins
from ..config import config
from .base import RetrieverError
from ..searchers import get_search_entity

log = logging.getLogger('stagehand.retrievers')
plugins = load_plugins('retrievers', globals())

@kaa.coroutine(progress=True)
def retrieve(progress, result, outfile, episode, skip=[]):
    tried = set()
    always = [name for name in plugins if plugins[name].Retriever.ALWAYS_ENABLED]
    search_entity = None
    for name in config.retrievers.enabled + always:
        if name not in plugins or name in skip or result.type not in plugins[name].Retriever.SUPPORTED_TYPES or name in tried:
            continue

        if search_entity is None:
            # Fetch the search entity (a type-dependent structure/object/whatever)
            # that contains the data needed for this result.  We do this here
            # rather than outside the loop above because there is no sense in doing
            # this if we have no suitable, enabled retriever plugins for the search
            # result.  For some results, this operation could be expensive (e.g.
            # fetching a torrent or nzb file), so it should be done only if truly
            # necessary.
            try:
                search_entity = yield get_search_entity(result)
            except Exception as e:
                raise RetrieverError('search result not valid: %s' % str(e))
            else:
                if not search_entity:
                    # This shouldn't happen.  It's a bug in the searcher, which should
                    # have raised SearcherError instead.
                    raise RetrieverError('searcher plugin did not provide information for retriever')

        tried.add(name)
        retriever = plugins[name].Retriever()
        try:
            yield retriever.retrieve(progress, episode, result, search_entity, outfile)
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
