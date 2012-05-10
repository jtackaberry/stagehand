from __future__ import absolute_import
import os
import logging
import kaa

from ..utils import Curl, download
from ..curl import CurlError
from ..config import config
from .base import RetrieverBase, RetrieverError
from ..searchers import get_search_entity

__all__ = ['Retriever']

log = logging.getLogger('stagehand.retrievers.http')

def download_progress_cb(curl, state, position, total, speed, progress):
    log.debug('[%s] %d KB/s, %d KB / %d KB', state, speed/1024, position/1024, total/1024)
    progress.set(position/1024, total/1024, speed/1024)


class Retriever(RetrieverBase):
    NAME = 'http'
    PRINTABLE_NAME = 'HTTP'
    SUPPORTED_TYPES = ('http',)
    ALWAYS_ENABLED = True

    @kaa.coroutine()
    def _retrieve(self, progress, episode, result, search_entity, outfile):
        """
        Retrieve the given SearchResult object.
        """
        if not search_entity.get('url'):
            raise RetrieverError('Searcher did not provide a URL')

        opts = {}
        if 'username' in search_entity:
            opts['userpwd'] = '%s:%s' % (search_entity['username'], search_entity.get('password', ''))
        if 'retry' in search_entity:
            opts['retry'] = search_entity['retry']

        # Before we start fetching, initialize progress.
        progress.set(0, result.size / 1024.0, 0)
        log.debug('fetching %s', search_entity['url'])
        # TODO: once we've fetched enough, get metadata and confirm if HD, abort if
        # not and HD only is required for this search result.
        status, c = yield download(search_entity['url'], outfile, progress=kaa.Callable(download_progress_cb, progress),
                                   progress_interval=5, **opts)
        if status == 416 and c.content_length_download == 0:
            log.info('file already fully retrieved')
        elif status not in (200, 206):
            raise RetrieverError('Status %d != 200' % status)
