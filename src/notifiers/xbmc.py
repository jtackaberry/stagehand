from __future__ import absolute_import
import json
import socket
import urllib
import logging
import re
import os
import time
import kaa

from ..config import config
from ..utils import download
from .base import NotifierBase, NotifierError
from .xbmc_config import config as modconfig

__all__ = ['Notifier']

log = logging.getLogger('stagehand.notifiers.xbmc')

class Notifier(NotifierBase):
    def __init__(self):
        self._rpcsock = kaa.Socket()
        self._rpcver = 0

    @kaa.coroutine()
    def _jsonrpc(self, method, params):
        request = {
            'jsonrpc': '2.0',
            'method': method,
            'params': params,
            'id': 1
        }
        log.debug2('issuing JSON-RPC method=%s params=%s', method, params)
        self._rpcsock.write(json.dumps(request))
        data = yield self._rpcsock.read().timeout(5)
        try:
            response = json.loads(data)
            yield response['result']
        except (ValueError, KeyError):
            log.error('unexpected response from JSON-RPC: %s...', data[:1000])


    @kaa.coroutine()
    def _httpapi(self, command, param):
        url = 'http://%s:%d/xbmcCmds/xbmcHttp?command=%s&parameter=%s'
        url %= (modconfig.hostname, modconfig.http_port, command, param)
        log.debug2('poking %s', url)
        # Assume XBMC is local and so override the configured bind address
        # (which is supposed to apply just for outbound Internet connections).
        status, data = yield download(url, bind_address=None, timeout=5)
        yield status, data


    @kaa.coroutine()
    def _httpapi_builtin(self, method, *args):
        param = method + '(%s)' % ','.join(urllib.quote_plus(arg) for arg in args if arg is not None)
        status, data = yield self._httpapi('ExecBuiltIn', param)
        yield status, data


    @kaa.coroutine()
    def _wait_for_idle(self, timeout=120):
        """
        Waits until XBMC is idle (i.e. not scanning library).

        :returns: True if XBMC is idle, False if it isn't (due to timeout), or
                  None if JSON-RPC isn't working.
        """
        if not self._rpcsock.connected:
            # No RPC support
            yield None

        if self._rpcver >= 3:
            check_scanning_args = 'XBMC.GetInfoBooleans', {'booleans': ['library.isscanning']}
        else:
            check_scanning_args = 'System.GetInfoBooleans', ['library.isscanning']

        t0 = time.time()
        while time.time() - t0 < timeout:
            result = yield self._jsonrpc(*check_scanning_args)
            if not result or result.get('library.isscanning') != True:
                yield True
            log.debug2('XBMC busy scanning, waiting')
            yield kaa.delay(5)
        yield False


    @kaa.coroutine()
    def _send_notification(self, header, msg):
        yield self._httpapi_builtin('XBMC.Notification', header, msg)

    @kaa.coroutine()
    def _update_library(self, path=None):
        path = path + '/' if path and not path.endswith('/') else path
        yield self._httpapi_builtin('XBMC.updatelibrary', 'video', path)


    @kaa.coroutine()
    def _do_notify(self, episodes):
        try:
            yield self._rpcsock.connect((modconfig.hostname, modconfig.tcp_port))
        except socket.error:
            log.warning('JSON-RPC connection to XBMC host failed')
        else:
            # Determine JSON-RPC API version used by XBMC.
            result = yield self._jsonrpc('JSONRPC.Version', [])
            self._rpcver = result.get('version', 0)

            if self._rpcver >= 3:
                # XBMC 11+.  We need to disable notifications as they can precede
                # responses to methods we invoke and we don't handle that (nor do
                # we need to).
                config = {
                    'GUI': False,
                    'System': False,
                    'Player': False,
                    'AudioLibrary': False,
                    'VideoLibrary': False,
                    'Other': False
                }
                yield self._jsonrpc('JSONRPC.SetConfiguration', {'notifications': config})

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

        use_rpc = yield self._wait_for_idle()
        if modconfig.individual:
            # If _wait_for_idle() returns non-True, it means the JSON-RPC request
            # failed or timed out.  In this case, we can't rely on it, and
            # can't be quite as clever in terms of selective library updating
            # if we have multiple directories.
            if use_rpc:
                # Issue an update for each path.
                for dir in dirs:
                    yield self._update_library(dir)
                    yield self._wait_for_idle()
            elif len(dirs) == 1:
                # We may not be able to check if XBMC is scanning, but we only have
                # one directory that's changed, so we can update that.
                yield self._update_library(tuple(dirs)[0])
            else:
                # individual is True, multiple directories, but no JSON-RPC.  No
                # choice but to do a full update.
                yield self._update_library()
        else:
            yield self._update_library()

        if modconfig.notify:
            msg = '%d new episode%s added to library.' % (len(episodes), '' if len(episodes) == 1 else 's')
            yield self._send_notification('New TV Episodes', msg)
        self._rpcsock.close()
        log.debug('updated library with %d episodes', len(episodes))


    @kaa.coroutine()
    def _notify(self, episodes):
        try:
            yield self._do_notify(episodes)
        except kaa.TimeoutException:
            log.error('timed out waiting for XBMC server')
