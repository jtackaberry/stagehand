import os
import logging
import time
import asyncio

from ..config import config
from .base import RetrieverBase, RetrieverError, RetrieverAbortedSoft
from ..toolbox.net import download

__all__ = ['Retriever']

log = logging.getLogger('stagehand.retrievers.http')

class Retriever(RetrieverBase):
    NAME = 'http'
    PRINTABLE_NAME = 'HTTP'
    SUPPORTED_TYPES = ('http',)
    ALWAYS_ENABLED = True

    def _verify_timer(self, progress, episode, result, outfile, task):
        # Wait until we have 512KB before checking file.
        if progress.pos >= 512*1024:
            try:
                r = self.verify_result_file(episode, result, outfile)
            except RetrieverError as e:
                # Verify failed, abort download.
                log.info('cancelling download')
                task.cancelmsg = e.args
                task.cancel()
                return
            else:
                if r is not False or progress.percentage >= 100:
                    # verify function returned either True (verified ok) or None
                    # (no ability to get metadata).  Either way, stop the timer.
                    return
        return self._loop.call_later(1, self._verify_timer, progress, episode, result, outfile, task)


    def _download_progress_cb(self, ep, progress):
        log.debug('[%s %s] %d KB/s, %d KB / %d KB', ep.series.name, ep.code, progress.speed/1024, progress.pos/1024, progress.max/1024)


    @asyncio.coroutine
    def _retrieve(self, progress, episode, result, outfile):
        """
        Retrieve the given SearchResult object.
        """
        rdata = yield from result.get_retriever_data()
        if not rdata.get('url'):
            raise RetrieverError('Searcher did not provide a URL')

        opts = {}
        if 'username' in rdata:
            opts['auth'] = rdata['username'], rdata.get('password', '')
        if 'retry' in rdata:
            opts['retry'] = rdata['retry']

        # Before we start fetching, initialize progress.
        if result.size:
            progress.set(0, result.size / 1024.0, 0)

        log.debug('fetching %s', rdata['url'])
        progress.connect(self._download_progress_cb, episode)
        task = asyncio.Task(download(rdata['url'], outfile, progress=progress, **opts))
        self._loop.call_later(1, self._verify_timer, progress, episode, result, outfile, task)

        try:
            task.cancelmsg = None
            status, c = yield from task
        except asyncio.CancelledError:
            if task.cancelmsg:
                raise RetrieverAbortedSoft(*task.cancelmsg)
            else:
                raise
        finally:
            progress.disconnect(self._download_progress_cb)

        if status == 416 and c.content_length_download == 0:
            log.info('file already fully retrieved')
        elif status not in (200, 206):
            raise RetrieverError('Status %d != 200' % status)
