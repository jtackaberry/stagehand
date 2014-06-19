import sys
import os
import time
import asyncio
import inspect
import traceback
import json
import logging
import socket
from hashlib import md5

from aiohttp.wsgi import WSGIServerHttpProtocol
from ...toolbox import tobytes, tostr
from . import bottle
from .bottle import Bottle, HTTPResponse, HTTPError, tob, _e, html_escape, DEBUG, RouteReset

log = logging.getLogger('stagehand.web')

class LoggingMiddleware:
    def __init__(self, application, logger):
        self._application = application
        if isinstance(logger, logging.Logger):
            self._httplog = logger
        else:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter('%(asctime)s [HTTP] %(message)s'))
            self._httplog = logging.getLogger(logger)
            self._httplog.addHandler(handler)
            self._httplog.propagate = False


    def __call__(self, environ, start_response):
        # Default log level for this call.  The handler can modify it.
        bottle.response.loglevel = logging.INFO
        bottle.response.logextra = None
        t0 = time.time()

        def _start_response(status, headers, *args):
            uri = environ['PATH_INFO'] + ('?%s' % environ['QUERY_STRING'] if environ['QUERY_STRING'] else '')
            code = status.split()[0]
            size = dict(headers).get('Content-Length', '-')

            self._httplog.log(bottle.response.loglevel, '%s "%s %s %s" %s %s %.02fms%s',
                              environ['REMOTE_ADDR'], environ['REQUEST_METHOD'],
                              uri, environ['SERVER_PROTOCOL'], code, size, (time.time()-t0) * 1000.0,
                              ' ' + bottle.response.logextra if bottle.response.logextra else '')
            return start_response(status, headers, *args)

        return self._application(environ, _start_response)



class AuthMiddleware:
    def __init__(self, application, user, passwd):
        self._application = application
        self._nonce_passwd = os.urandom(64)
        self._user = user.encode()
        self._passwd = passwd.encode()


    def __call__(self, environ, start_response):
        try:
            nexthop = self._application if self._check_auth(environ) else self._auth_failed
            return nexthop(environ, start_response)
        except Exception as e:
            log.exception('error handling authentication')
            return []


    def _check_auth(self, environ):
        response = environ.get('HTTP_AUTHORIZATION')
        if not response or not response.lower().startswith('digest '):
            return False
        # Parse response into a list of 2-tuples (key, value)
        parts = (f.strip().split('=', 1) for f in response[7:].split(',') if '=' in f)
        # Remove enclosing quotes and toss into a dict.
        rdict = dict((k.lower(), tobytes(v.strip('"'))) for k, v in parts)
        HA1 = md5(b':'.join([self._user, rdict.get('realm'), self._passwd])).hexdigest()
        HA2 = md5(b':'.join([environ.get('REQUEST_METHOD').encode(), rdict.get('uri')])).hexdigest()
        expected = md5(b':'.join([HA1.encode(), rdict.get('nonce'), HA2.encode()])).hexdigest()
        return rdict.get('response') == expected.encode()


    def _auth_failed(self, environ, start_response):
        now = tobytes(int(time.time()), coerce=True)
        nonce = now + b'/' + tobytes(md5(now + self._nonce_passwd).hexdigest())
        # TODO: advertise qop and support cnonce
        response_headers = [
            ('WWW-Authenticate', 'Digest realm="Secure Area", nonce="%s", algorithm=MD5' % tostr(nonce)),
            ('Content-Type', 'text/html')
        ]
        start_response('401 Authorization Required', response_headers)
        return [b"<html><body>Authorization Required</body></html>"]



class UserDataMiddleware:
    def __init__(self, application, userdict):
        self._application = application
        self._userdict = userdict


    def __call__(self, environ, start_response):
        environ.update(self._userdict)
        return self._application(environ, start_response)


class AsyncBottle(Bottle):
    """
    This class is lifted from https://github.com/Lupino/aiobottle with
    modifications.
    """
    def _handle(self, environ):
        path = environ['bottle.raw_path'] = environ['PATH_INFO']
        try:
            environ['PATH_INFO'] = tostr(path)
        except UnicodeError:
            return HTTPError(400, 'Invalid path string. Expected UTF-8')

        try:
            environ['bottle.app'] = self
            try:
                self.trigger_hook("before_request")
                route, args = self.router.match(environ)
                environ['route.handle'] = route
                environ['bottle.route'] = route
                environ['route.url_args'] = args
                out = route.call(**args)
                if isinstance(out, asyncio.Future) or inspect.isgenerator(out):
                    out = yield from out
                return out
            finally:
                self.trigger_hook("after_request")
        except HTTPResponse:
            return _e()
        except RouteReset:
            route.reset()
            return (yield from self._handle(environ))
        except (KeyboardInterrupt, SystemExit, MemoryError):
            raise
        except Exception:
            if not self.catchall: raise
            stacktrace = traceback.format_exc()
            if self.log:
                self.log.exception('error from handler')
            else:
                environ['wsgi.errors'].write(stacktrace)
            return HTTPError(500, "Internal Server Error", _e(), stacktrace)


    def _cast(self, out, peek=None):
        if isinstance(out, dict):
            bottle.response.content_type = 'application/json'
            out = json.dumps(out)
        return super()._cast(out, peek)


    def wsgi(self, environ, start_response):
        """ The bottle WSGI-interface. """
        response = bottle.response
        try:
            out = self._cast((yield from self._handle(environ)))
            # rfc2616 section 4.3
            if response._status_code in (100, 101, 204, 304)\
            or environ['REQUEST_METHOD'] == 'HEAD':
                if hasattr(out, 'close'): out.close()
                out = []
            start_response(response._status_line, response.headerlist)
            return out
        except (KeyboardInterrupt, SystemExit, MemoryError):
            raise
        except Exception:
            if not self.catchall: raise
            err = '<h1>Critical error while processing request: %s</h1>' \
                  % html_escape(environ.get('PATH_INFO', '/'))
            if bottle.DEBUG:
                err += '<h2>Error:</h2>\n<pre>\n%s\n</pre>\n' \
                       '<h2>Traceback:</h2>\n<pre>\n%s\n</pre>\n' \
                       % (html_escape(repr(_e())), html_escape(traceback.format_exc()))
            environ['wsgi.errors'].write(err)
            headers = [('Content-Type', 'text/html; charset=UTF-8')]
            start_response('500 INTERNAL SERVER ERROR', headers, sys.exc_info())
            return [tob(err)]


    def _copy_response(self, src, dst):
            dst._status_line = src._status_line
            dst._status_code = src._status_code
            dst._cookies = src._cookies
            dst._headers = src._headers
            dst.body = src.body


    def __call__(self, environ, start_response):
        ''' Each instance of :class:'Bottle' is a WSGI application. '''
        # Bottle uses thread-local variables for request and response.  This
        # is fine for the standard WSGI model in which the entire request is
        # completed by a single invocation of the handler, but in a world of
        # coroutines it's rather broken because request handlers can be
        # interleaved even within a single thread.
        #
        # So we must explicitly context switch: restore the request environment
        # and response attributes each time we enter the generator, and save
        # it after the generator yields.
        response = bottle.BaseResponse()
        coro = self.wsgi(environ, start_response)
        while True:
            bottle.request.bind(environ)
            self._copy_response(response, bottle.response)
            try:
                result = next(coro)
            except StopIteration as e:
                return e.args[0]
            else:
                # Not done, store response.
                self._copy_response(bottle.response, response)
                yield result




class Server(bottle.ServerAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.app = AsyncBottle()
        self._last_args = {}
        self._server = None
        self._task = None


    def _create_server_done(self, f):
        self._task = None
        try:
            self._server = f.result()
            log.info('started webserver at http://%s:%s/', self.host or socket.gethostname(), self.port)
        except:
            pass


    def start(self, **kwargs):
        self.loop = kwargs.pop('loop', None)
        if not self.loop:
            self.loop = asyncio.get_event_loop()
        args = self._last_args.copy()
        args.update(kwargs)
        self._last_args = args
        bottle.debug(kwargs.get('debug', False))

        app = self.app
        app.config.update(args)
        app.log = kwargs.get('log')

        # Add middleware.
        if 'userdata' in kwargs:
            app = UserDataMiddleware(app, kwargs['userdata'])
        if kwargs.get('user'):
            app = AuthMiddleware(app, kwargs['user'], kwargs.get('password', ''))
        if kwargs.get('log'):
            app = LoggingMiddleware(app, kwargs['log'])

        self.host = kwargs.get('host', '')
        self.port = kwargs.get('port', 8080)
        bottle.run(app, server=self, quiet=True)
        return self._task


    def run(self, handler):
        def wsgi_app(env, start):
            return handler(env, start)

        f = self.loop.create_server(
                lambda: WSGIServerHttpProtocol(wsgi_app, loop=self.loop, keep_alive=60, readpayload=True, access_log=None), self.host, self.port)
        self._task = asyncio.Task(f, loop=self.loop)
        self._task.add_done_callback(self._create_server_done)


    def stop(self):
        if self._server:
            log.warning('stopping web server')
            self._server.close()
            self._server = None