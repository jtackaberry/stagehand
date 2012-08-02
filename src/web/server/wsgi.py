from __future__ import absolute_import
import logging
import sys
import os
import socket
import time
import select
import threading
from hashlib import md5, sha1
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, SimpleHandler, make_server

import kaa
from . import bottle
from .bottle import route

# Bottle assumes it's in sys.path for error pages.  Add it to sys.modules so
# import inside the bottle error template can find it.
sys.modules['bottle'] = bottle

log = logging.getLogger('stagehand.web')

class LoggingMiddleware(object):
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

            self._httplog.log(bottle.response.loglevel, '%s "%s %s %s" %s %s %.03fs%s', 
                              environ['REMOTE_ADDR'], environ['REQUEST_METHOD'],
                              uri, environ['SERVER_PROTOCOL'], code, size, time.time()-t0,
                              ' ' + bottle.response.logextra if bottle.response.logextra else '')
            return start_response(status, headers, *args)

        try:
            return self._application(environ, _start_response)
        except Exception, r:
            log.exception('uncaught WSGI exception')



class AuthMiddleware(object):
    def __init__(self, application, user, passwd):
        self._application = application
        self._nonce_passwd = os.urandom(64)
        self._user = user
        self._passwd = passwd


    def __call__(self, environ, start_response):
        try:
            nexthop = self._application if self._check_auth(environ) else self._auth_failed
            return nexthop(environ, start_response)
        except Exception, e:
            log.exception('error handling authentication')
            return []


    def _check_auth(self, environ):
        response = environ.get('HTTP_AUTHORIZATION')
        if not response or not response.lower().startswith('digest '):
            return False
        # Parse response into a list of 2-tuples (key, value)
        parts = (f.strip().split('=', 1) for f in response[7:].split(',') if '=' in f)
        # Remove enclosing quotes and toss into a dict.
        rdict = dict((k.lower(), v.strip('"')) for k, v in parts)
        HA1 = md5('%s:%s:%s' % (self._user, rdict.get('realm'), self._passwd)).hexdigest()
        HA2 = md5('%s:%s' % (environ.get('REQUEST_METHOD'), rdict.get('uri'))).hexdigest()
        expected = md5('%s:%s:%s' % (HA1, rdict.get('nonce'), HA2)).hexdigest()
        return rdict.get('response') == expected


    def _auth_failed(self, environ, start_response):
        now = str(time.time())
        nonce = '%s/%s' % (now, md5(str(now) + self._nonce_passwd).hexdigest())
        # TODO: advertise qop and support cnonce
        response_headers = [
            ('WWW-Authenticate', 'Digest realm="Secure Area", nonce="%s", algorithm=MD5' % nonce),
            ('Content-Type', 'text/html')
        ]
        start_response('401 Authorization Required', response_headers)
        return ["<html><body>Authorization Required</body></html>"]



class UserDataMiddleware(object):
    def __init__(self, application, userdict):
        self._application = application
        self._userdict = userdict


    def __call__(self, environ, start_response):
        environ.update(self._userdict)
        return self._application(environ, start_response)


class NonBlockingWSGIServer(WSGIServer):
    """
    Implements a non-blocking variant of the batteries-included simple WSGI
    server.  This one supports HTTP keep-alive and pipelining, as well as
    sendfile() so that transfers of large files are feasible.

    However it doesn't support POSTing of binary data yet.
    """
    def __init__(self, *args, **kwargs):
        WSGIServer.__init__(self, *args, **kwargs)

        # NonBlockingClient -> timestamp
        self.clients = {}
        # Number of simultaneous clients.
        self.limit = 1000
        # Number of seconds before we bounce idle clients.
        self.timeout = 60


    def shutdown(self):
        if not self._running:
            return
        self._running = False
        # Dump all clients.
        for client in self.clients.keys():
            # Calling close() on client will remove client from self.clients
            client.close(force=True)
        # Poke the socket to wake up the socket server loop.  FIXME: IPv6
        socket.socket().connect(self.socket.getsockname())


    def serve_forever(self, poll_interval=30):
        host, port = self.server_address
        self._running = True
        while self._running:
            self.kill_idle_clients()
            rfds = [self] + self.clients.keys()
            wfds = [client for client in self.clients if client.writes_pending]
            # TODO: epoll instead
            r, w, e = select.select(rfds, wfds, [], poll_interval)
            for fd in r:
                if fd == self:
                    self.get_request()
                else:
                    try:
                        fd.handle_read()
                        if fd.requests_pending > 1:
                            log.debug2('pipelined [%d] %d', fd.fileno(), fd.requests_pending)
                            print '******** pipelined [%d] %d' % (fd.fileno(), fd.requests_pending)
                        # Process all fully buffered requests (if more than 1,
                        # client is using pipelining)
                        while fd.requests_pending:
                            self._handle_request(fd)
                    except:
                        log.exception('unhandled error during client request')
                        self.handle_error(client, client.addr)
                        fd.close()
            
            for fd in w:
                try:
                    if fd.handle_write():
                        # Write successful, update timestamp for this client.
                        self.clients[fd] = time.time()
                except:
                    log.exception('unhandled error writing to client')
                    fd.close()


    def kill_idle_clients(self):
        now = time.time()
        for client, timestamp in self.clients.items():
            if now - timestamp > self.timeout:
                log.debug2('timeout on [%d]: %s:%d', client.fileno(), *client.addr)
                client.close()


    def _handle_request(self, client):
        self.clients[client] = time.time()
        if self.verify_request(client, client.addr):
            self.process_request(client, client.addr)


    def get_request(self):
        sock, addr = WSGIServer.get_request(self)
        if len(self.clients) >= self.limit:
            # Too many clients, disconnect idle ones.
            self.kill_idle_clients()
            if len(self.clients) >= self.limit:
                # We _still_ have too many connected non-idle clients.  We
                # can't accept this request.
                return sock.close()

        client = NonBlockingClient(self, sock, addr)
        self.clients[client] = time.time()
        log.debug2('new client [%d]: %s:%d', client.fileno(), *addr)
        return client, addr


    def close_request(self, client):
        # no-op to ensure socket is kept alive
        pass




class NonBlockingClient(object):
    """
    Provides non-blocking socket behaviour with a blocking-style interface.  Implements
    enough of the socket interface to make WSGIRequestHandler happy.
    """
    def __init__(self, server, sock, addr, buffer_size=2*1024*1024):
        sock.setblocking(False)
        self._server = server
        self._sock = sock
        self.addr = addr
        self.closed = False

        self._lines = []
        self._file = None
        self._continue_last = False
        self._write_queue = []
        self._close_after_flush = False
        self._processing_post = False
        # [n bytes accumulated, Content-Length for POST body]
        self._processing_post_lengths = [-1, 0]
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, buffer_size)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, buffer_size)


    @property
    def writes_pending(self):
        return len(self._write_queue) > 0 or self._file


    @property
    def requests_pending(self):
        return self._lines.count(b'\r\n') if not self._processing_post else 0

    def handle_read(self):
        try:
            data = self._sock.recv(65536)
        except socket.error:
            data = None

        if not data:
            return self.close()

        current_lines_idx = max(len(self._lines) - 1, 0)
        newlines = data.splitlines(True)
        if self._continue_last:
            self._lines[-1] += newlines.pop(0)
        self._continue_last = not data.endswith(b'\n')
        self._lines.extend(newlines)

        # Need to peek into the data to determine if it's a POST and to
        # fetch the content length.  The requests_pending property can't
        # report the POST request is ready to the wsgi handler until we
        # have the entire body buffered.
        idx = 0
        if self._processing_post_lengths[0] == -1:
            for idx, line in enumerate(self._lines[current_lines_idx:]):
                if not self._processing_post and line[:5].lower() == b'post ':
                    self._processing_post = True
                    self._processing_post_lengths = [-1, 0]
                elif self._processing_post:
                    if self._processing_post_lengths[1] == 0 and line[:14].lower() == b'content-length':
                        length = line[15:].strip()
                        if length == '0':
                            # Zero-length POST body, so we're done.
                            self._processing_post = False
                            break
                        self._processing_post_lengths[1] = int(length)
                    elif self._processing_post_lengths[0] == -1 and line.strip() == b'':
                        self._processing_post_lengths[0] = 0
                        idx += 1
                        break

        if self._processing_post_lengths[0] != -1:
            if current_lines_idx + idx < len(self._lines):
                self._processing_post_lengths[0] += sum(len(x) for x in self._lines[current_lines_idx + idx:])
            if self._processing_post_lengths[0] >= self._processing_post_lengths[1]:
                self._processing_post = False

        while self._lines and not self._lines[0].strip():
            self._lines.pop(0)


    def read(self, nbytes):
        # read is called for POST methods to retrieve the body of the HTTP
        # request.  FIXME: This retrieves _all_ queued data, which won't
        # work for pipelined POST requests.  POSTs aren't supposed to be
        # pipelined because they aren't idempotent but if the browser
        # does, this will break.  So we should look at _processing_post_lengths
        # and return only the amount of data declared in the Content-Length.
        data = b''.join(self._lines)
        log.debug2('read [%s]: %d bytes', self.fileno(), len(data))
        del self._lines[:]
        self._continue_last = False
        self._processing_post_lengths = [-1, 0]
        return data

    def readline(self):
        log.debug2('readline [%s]: %s', self.fileno(), repr(self._lines[0]).rstrip())
        return self._lines.pop(0)

    def sendfile(self, file):
        self._write_queue.append(iter(file))


    def handle_write(self):
        """
        Writes as much queued data to the socket as possible.

        :returns: True if the socket is available for further writes, or False
                  if it's closed.
        """
        startlen = len(self._write_queue)
        while self._write_queue:
            # Peek at the front of the write queue and see if we have an
            # iterator queued.
            if hasattr(self._write_queue[0], 'next'):
                # Yes, looks like one.  Grab the next chunk.
                try:
                    data = self._write_queue[0].next()
                except StopIteration:
                    # We're done.  Move to the next queued item.
                    self._write_queue.pop(0).close()
                    continue
            else:
                # It's a string, dequeue.
                data = self._write_queue.pop(0)

            try:
                sent = self._sock.send(data)
            except Exception, e:
                if isinstance(e, (OSError, IOError, socket.error)) and e.args[0] == 11:
                    sent = 0
                else:
                    self.close(force=True)
                    break

            log.debug2('write [%s] %d/%d (q %d/%d): %s', self.fileno(), sent, len(data), len(self._write_queue),
                       startlen, repr(data[:sent] if len(data) < 200 else ''))
            if sent < len(data):
                # Push unsent data from this chunk back to the head of the
                # queue for the next pass.
                self._write_queue.insert(0, data[(sent if sent >= 0 else 0):])
                break

        if not self._write_queue and self._close_after_flush:
            # We're done writing and have a pending close, so do that now.
            self.close()

        return not self.closed


    def write(self, data):
        self._write_queue.append(data)

    
    def fileno(self):
        try:
            return self._sock.fileno()
        except socket.error:
            return None


    def close(self, force=False):
        log.debug2('close %s force=%s (already? %s)', self.fileno(), force, self.closed)
        if not self.closed:
            if not force and self._write_queue:
                self._close_after_flush = True
                return

            self._sock.close()
            del self._server.clients[self]
            del self._write_queue[:]
            del self._lines[:]
            self.closed = True


    def flush(self):
        pass

    def shutdown(self, flag):
        # make shutdown a no-op; rely on close() instead.
        pass
        

class NonBlockingServerHandler(SimpleHandler):
    http_version = '1.1'

    def log_exception(self, *args):
        pass

    def cleanup_headers(self):
        if self.request_handler.headers.get('Connection', '').lower() == 'keep-alive':
            self.headers['Connection'] = 'Keep-Alive'

    def sendfile(self):
        if not self.headers_sent:
            self.send_headers()
        self.result.buffer_size = 512*1024
        self.stdout.sendfile(self.result)
        return True

    def close(self):
        self.last_status = self.status
        if not self.result_is_file():
            return SimpleHandler.close(self)


class NonBlockingWSGIRequestHandler(WSGIRequestHandler):
    def address_string(self, *args):
        # Disable REMOTE_HOST to avoid DNS lookup
        pass
 
    def setup(self):
        # WSGIRequestHandler calls makefile() on the socket, but instead we
        # want to use the NonBlockingClient object directly to get the, uh,
        # non-blocking behaviour.
        self.rfile = self.wfile = self.request

    def finish(self):
        # Prevent the socket from being closed, except for status 401 (auth needed)
        # TODO: if no Content-Length is specified, should close.
        if not self.handler.last_status.startswith('200') and self.command == 'POST':
            # Request was a POST but it did not succeed, so we must purge the
            # request body so it doesn't run into the next request.
            self.request.read(-1)
        if self.headers.get('Connection', '').lower() != 'keep-alive' or self.handler.last_status.startswith('401'):
            WSGIRequestHandler.finish(self)

    def handle(self):
        self.raw_requestline = self.request.readline()
        if self.parse_request():
            self.handler = NonBlockingServerHandler(self.request, self.request, self.get_stderr(), self.get_environ())
            self.handler.request_handler = self
            self.handler.run(self.server.get_app())
        else:
            self.handler = None


class KaaBottleAdaptor(bottle.ServerAdapter):
    """
    An adaptation of the native bottle WSGIRefServer that uses
    NonBlockingWSGIServer and shuts the web server down when the kaa mainloop
    stops.
    """
    def __init__(self, *args, **kwargs):
        super(KaaBottleAdaptor, self).__init__(*args, **kwargs)
        self.running = False
        self._shutdown_event = threading.Event()


    def run(self, handler):
        self._srv = make_server(self.host, self.port, handler, server_class=NonBlockingWSGIServer, 
                                handler_class=NonBlockingWSGIRequestHandler, **self.options)
        kaa.signals['shutdown'].connect_first(self.shutdown)
        host = self.host if self.host else socket.gethostname()
        log.info('starting webserver at http://%s:%d/', host, self.port)

        self.running = True
        self._shutdown_event.clear()
        self._srv.serve_forever()
        self._srv.server_close()
        kaa.signals['shutdown'].disconnect(self.shutdown)
        self._srv = None
        self.running = False

        log.info('webserver stopped')
        self._shutdown_event.set()


    def shutdown(self):
        if self.running:
            self._srv.shutdown()
            self._shutdown_event.wait()



class Server(object):
    def __init__(self, **kwargs):
        self._default_args = kwargs
        self._last_args = kwargs
        self._adaptor = KaaBottleAdaptor()


    @kaa.threaded(wait=True)
    def start(self, **kwargs):
        args = self._default_args.copy()
        args.update(kwargs)
        self._last_args = args
        bottle.debug(kwargs.get('debug', False))
        app = bottle.app()
        app.config.update(kwargs)
        #app.catchall = False

        # Add middleware.
        if 'userdata' in kwargs:
            app = UserDataMiddleware(app, kwargs['userdata'])
        if kwargs.get('user'):
            app = AuthMiddleware(app, kwargs['user'], kwargs.get('passwd', ''))
        if kwargs.get('log'):
            app = LoggingMiddleware(app, kwargs['log'])

        self._adaptor.host = kwargs.get('host', '')
        self._adaptor.port = kwargs.get('port', 8080)
        bottle.run(app, server=self._adaptor, quiet=True)


    @kaa.threaded(kaa.MAINTHREAD)
    def stop(self):
        if self._adaptor.running:
            self._adaptor.shutdown()


    @kaa.threaded(kaa.MAINTHREAD)
    def restart(self, **kwargs):
        args = self._last_args.copy()
        args.update(kwargs)
        log.info('restarting webserver')
        self.stop()
        return self.start(**args)


    def is_running(self):
        return self._adaptor.running
