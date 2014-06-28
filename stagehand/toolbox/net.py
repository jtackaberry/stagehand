# -----------------------------------------------------------------------------
# net.py - Miscellaneous network helper functions
# -----------------------------------------------------------------------------
# Copyright 2014 Jason Tackaberry
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
# -----------------------------------------------------------------------------

import sys
import os
import io
import re
import logging
import asyncio
import aiohttp

log = logging.getLogger('http')

@asyncio.coroutine
def _download(url, target=None, resume=True, progress=None, **kwargs):
    if not target:
        target = io.BytesIO()
    elif not hasattr(target, 'write'):
        target = open(target, 'ab+' if resume else 'wb')

    try:
        kwargs['headers'] = headers = {}
        if resume:
            pos = target.seek(0, io.SEEK_END)
            if pos > 0:
                headers['Range'] = 'bytes={}-'.format(pos)
            expected_pos = pos
        else:
            target.seek(0, io.SEEK_SET)
            target.truncate()
            expected_pos = 0

        log.info('fetching %s', url)
        response = yield from aiohttp.request('GET', url, **kwargs)
        if response.status >= 300:
            raise aiohttp.HttpErrorException(response.status)

        # See if server responded with Content-Range header.
        m = re.search(r'bytes +(\d+).*/(\d+|\*)',  response.headers.get('content-range', ''))
        if m:
            pos, size = m.groups()
            pos = int(pos)
            if progress:
                if size != '*':
                    progress.set(pos=pos, max=int(size))
                else:
                    progress.set(pos=pos)

            if pos < expected_pos:
                target.seek(pos, io.SEEK_SET)
                target.truncate()
        else:
            if expected_pos > 0:
                # Server didn't respond with a Content-Range header and we are
                # trying to resume. We have to assume the server doesn't support
                # resume, but we should restart the request without a Range
                # request header.  Simulate a range not satisfiable error.
                raise aiohttp.HttpErrorException(416)
            if progress:
                progress.set(max=int(response.headers.get('content-length', 0)))

        while True:
            try:
                chunk = yield from response.content.read()
                if not chunk:
                    break
                if progress:
                    progress.update(diff=len(chunk))
            except aiohttp.EofStream:
                break
            target.write(chunk)

        if isinstance(target, io.BytesIO):
            return response.status, target.getvalue()
        else:
            return response.status, response
    finally:
        if not isinstance(target, io.BytesIO):
            target.close()



@asyncio.coroutine
def download(url, target=None, resume=True, retry=0, progress=None, noraise=True, **kwargs):
    while retry >= 0:
        status = 0
        try:
            status, response = yield from _download(url, target, resume, progress, **kwargs)
            return status, response
        except (aiohttp.HttpException, aiohttp.ConnectionError, OSError) as e:
            if isinstance(e, aiohttp.HttpException):
                status = e.code
            if status == 416 and resume:
                # Server reported range not satisfiable and we're trying to
                # resume.  Retry again without resumption without counting
                # against the retry counter.
                resume = False
                continue
            elif retry == 0:
                # Done retrying, reraise this exception.
                if noraise:
                    return 0, str(e)
                else:
                    raise
            errmsg = str(e)

        if status != 0:
            errmsg = 'status %d' % status
            if status < 500 or status >= 600:
                # Not a temporary error that we can retry.  We're done.
                return status, None

        log.warning('download failed (%d retries left): %s', retry, errmsg)
        retry -= 1

    # We shouldn't actually ever get here.
    log.warning('BUG: download retry loop did not terminate properly')
    return 0, None
