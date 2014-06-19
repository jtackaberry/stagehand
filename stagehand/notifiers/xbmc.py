import json
import socket
import urllib
import logging
import re
import os
import time
import asyncio

from ..config import config
from ..toolbox import tobytes, tostr
from ..toolbox.net import download
from .base import NotifierBase, NotifierError
from .xbmc_config import config as modconfig

__all__ = ['Notifier']

log = logging.getLogger('stagehand.notifiers.xbmc')

class Notifier(NotifierBase):
    def __init__(self, loop=None):
        super().__init__(loop)
        self._rpcver = 0

    @asyncio.coroutine
    def _jsonrpc(self, method, params):
        request = {
            'jsonrpc': '2.0',
            'method': method,
            'params': params,
            'id': 1
        }
        log.debug2('issuing JSON-RPC method=%s params=%s', method, params)
        buf = tobytes(json.dumps(request))
        self._rpcwriter.write(buf)
        data = yield from asyncio.wait_for(self._rpcreader.read(2048), timeout=5)
        try:
            response = json.loads(tostr(data))
            return response['result']
        except (ValueError, KeyError):
            log.error('unexpected response from JSON-RPC: %s...', data[:1000])


    @asyncio.coroutine
    def _wait_for_idle(self, timeout=120):
        """
        Waits until XBMC is idle (i.e. not scanning library).

        :returns: True if XBMC is idle, False if it isn't (due to timeout), or
                  None if JSON-RPC isn't working.
        """
        t0 = time.time()
        while time.time() - t0 < timeout:
            result = yield from self._jsonrpc('XBMC.GetInfoBooleans', {'booleans': ['library.isscanning']})
            if not result or result.get('library.isscanning') != True:
                return True
            log.debug2('XBMC busy scanning, waiting')
            yield from asyncio.sleep(1)
        return False


    @asyncio.coroutine
    def _send_notification(self, title, msg):
        result = yield from self._jsonrpc('GUI.ShowNotification', [title, msg])
        return result


    @asyncio.coroutine
    def _update_library(self, path=""):
        path = path + '/' if path and not path.endswith('/') else path
        result = yield from self._jsonrpc('VideoLibrary.scan', [path])
        return result


    @asyncio.coroutine
    def _do_notify(self, episodes):
        self._rpcreader, self._rpcwriter = yield from asyncio.open_connection(str(modconfig.hostname), int(modconfig.tcp_port), loop=self._loop)
        # Determine JSON-RPC API version used by XBMC.
        result = yield from self._jsonrpc('JSONRPC.Version', [])
        self._rpcver = result.get('version', {'major': 0})['major']
        log.warning('rpc version %d', self._rpcver)

        # We need to disable notifications as they can precede
        # responses to methods we invoke and we don't handle that (nor do
        # we need to).
        config = {
            'Application': False,
            'GUI': False,
            'System': False,
            'Player': False,
            'AudioLibrary': False,
            'VideoLibrary': False,
            'Other': False
        }
        yield from self._jsonrpc('JSONRPC.SetConfiguration', {'notifications': config})

        # Get a list of all series directories for new episodes.  We
        # _could_ add the season directory, except that if the series isn't
        # one XBMC yet knows about, it won't discover new series metadata.
        # If it does already know about it, it will pick up the episode in the
        # season directory properly.  But there doesn't seem to be a way to
        # distinguish this.  VideoLibrary.GetTVShows isn't foolproof because
        # XBMC doesn't provide the path to the series.
        dirs = set(ep.series.path for ep in episodes)

        # Translate local path to XBMC path.
        if modconfig.tvdir:
            # Normalize local and remote paths so they have trailing slash
            frm = os.path.normpath(config.misc.tvdir) + '/'
            to = os.path.normpath(modconfig.tvdir) + '/'
            dirs = [re.sub(r'^' + frm, to, dir) for dir in dirs]

        yield from self._wait_for_idle()
        if modconfig.individual:
                # Issue an update for each path.
            for dir in dirs:
                yield from self._update_library(dir)
                yield from self._wait_for_idle()
        else:
            yield from self._update_library()

        if modconfig.notify:
            if len(episodes) == 1:
                msg = 'New episode for {} available.'.format(episodes[0].series.name)
            else:
                msg = '{} new episodes added to library.'.format(len(episodes))
            yield from self._send_notification('New TV Episodes', msg)

        self._rpcwriter.close()
        log.debug('updated library with %d episodes', len(episodes))


    @asyncio.coroutine
    def _notify(self, episodes):
        try:
            yield from self._do_notify(episodes)
        except asyncio.TimeoutError:
            log.error('timed out waiting for XBMC server')
        else:
            log.info('send xbmc notification')
