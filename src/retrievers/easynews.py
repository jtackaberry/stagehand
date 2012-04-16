from __future__ import absolute_import
import os
import logging
import kaa

from ..utils import Curl
from ..curl import CurlError
from ..config import config
from .base import RetrieverBase, RetrieverError
from ..searchers import get_search_entity

__all__ = ['Retriever']

log = logging.getLogger('stagehand.retrievers.easynews')

def progress(curl, state, position, total, speed):
    log.debug('[%s] %d KB/s, %d KB / %d KB', state, speed/1024, position/1024, total/1024)


class Retriever(RetrieverBase):
    SUPPORTED_TYPES = ('easynews',)

    @kaa.coroutine()
    def retrieve(self, result, outfile, episode):
        """
        Retrieve the given SearchResult object.
        """
        url = yield get_search_entity(result)
        if not url:
            raise RetrieverError('Searcher did not provide a URL')

        user, pwd = config.searchers.easynews.username, config.searchers.easynews.password
        if not user or not pwd:
            raise RetrieverError('Searcher configuration lacks username and/or password')

        c = Curl(userpwd='%s:%s' % (user, pwd))
        # TODO: once we've fetched enough, get metadata and confirm if HD, abort if
        # not and HD only is required for this search result.
        c.signals['progress'].connect(progress)
        c.progress_interval = 5
        log.debug('fetching %s', url)
        for i in range(config.searchers.easynews.retries or 1):
            try:
                status = yield c.get(url, outfile)
                break
            except CurlError, e:
                # TODO: don't retry on permanent errors
                log.warning('download failed (%s), retrying %d of %d', e.args[0], i + 1,
                            config.searchers.easynews.retries)
        else:
            raise RetrieverError('download failed too many times')
        
        if status == 416 and c.content_length_download == 0:
            log.info('file already fully retrieved')
        elif status not in (200, 206):
            raise RetrieverError('Status %d != 200' % status)
