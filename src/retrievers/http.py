from __future__ import absolute_import
import os
import logging
import time
import kaa

from ..utils import Curl, download
from ..curl import CurlError
from ..config import config
from .base import RetrieverBase, RetrieverError, RetrieverAbortedError

__all__ = ['Retriever']

log = logging.getLogger('stagehand.retrievers.http')

class Retriever(RetrieverBase):
    NAME = 'http'
    PRINTABLE_NAME = 'HTTP'
    SUPPORTED_TYPES = ('http',)
    ALWAYS_ENABLED = True

    def _download_progress_cb(self, curl, state, position, total, speed, progress):
        # We set the InProgressStatus object at curl's progress interval ...
        progress.set(position/1024, total/1024, speed/1024)
        # ... but only log the transfer progress every 5 seconds.
        now = time.time()
        if now - getattr(self, '_last_progress_log_time', 0) > 5:
            log.debug('[%s] %d KB/s, %d KB / %d KB', state, speed/1024, position/1024, total/1024)
            self._last_progress_log_time = now


    @kaa.coroutine()
    def _retrieve(self, progress, episode, result, outfile):
        """
        Retrieve the given SearchResult object.
        """
        rdata = yield result.get_retriever_data()
        if not rdata.get('url'):
            raise RetrieverError('Searcher did not provide a URL')

        opts = {}
        if 'username' in rdata:
            opts['userpwd'] = '%s:%s' % (rdata['username'], rdata.get('password', ''))
        if 'retry' in rdata:
            opts['retry'] = rdata['retry']

        # Before we start fetching, initialize progress.
        progress.set(0, result.size / 1024.0, 0)
        log.debug('fetching %s', rdata['url'])
        ip = download(rdata['url'], outfile, progress=kaa.Callable(self._download_progress_cb, progress),
                      progress_interval=1, **opts)
        status, c = yield ip
        if status == 416 and c.content_length_download == 0:
            log.info('file already fully retrieved')
        elif status not in (200, 206):
            raise RetrieverError('Status %d != 200' % status)
