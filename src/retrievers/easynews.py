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

log = logging.getLogger('stagehand.retrievers.easynews')

def download_progress_cb(curl, state, position, total, speed, progress):
    log.debug('[%s] %d KB/s, %d KB / %d KB', state, speed/1024, position/1024, total/1024)
    progress.set(position/1024, total/1024, speed/1024)


class Retriever(RetrieverBase):
    SUPPORTED_TYPES = ('easynews',)

    @kaa.coroutine()
    def retrieve(self, progress, result, outfile, episode):
        """
        Retrieve the given SearchResult object.
        """
        url = yield get_search_entity(result)
        if not url:
            raise RetrieverError('Searcher did not provide a URL')

        user, pwd = config.searchers.easynews.username, config.searchers.easynews.password
        if not user or not pwd:
            raise RetrieverError('Searcher configuration lacks username and/or password')

        # Before we start fetching, initialize progress.
        progress.set(0, result.size / 1024.0, 0)
        log.debug('fetching %s', url)
        # TODO: once we've fetched enough, get metadata and confirm if HD, abort if
        # not and HD only is required for this search result.
        status, pos = yield download(url, outfile, retry=config.searchers.easynews.retries, userpwd='%s:%s' % (user, pwd),
                                     progress=kaa.Callable(download_progress_cb, progress), progress_interval=5)
        if status == 416 and c.content_length_download == 0:
            log.info('file already fully retrieved')
        elif status not in (200, 206):
            raise RetrieverError('Status %d != 200' % status)
