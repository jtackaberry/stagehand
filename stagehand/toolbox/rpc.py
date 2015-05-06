# -----------------------------------------------------------------------------
# rpc.py - Simple interprocess communication via remote procedure calls
# -----------------------------------------------------------------------------
# Copyright 2006-2014 Jason Tackaberry, Dirk Meyer
#
# Originally from kaa.base, ported to Python 3 and asyncio by Jason Tackaberry
#
#
# Please see the file AUTHORS for a complete list of authors.
#
# This library is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License version
# 2.1 as published by the Free Software Foundation.
#
# This library is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301 USA
# -----------------------------------------------------------------------------
__all__ = [ 'Server', 'expose', 'connect' ]

import types
import socket
import logging
import pickle
import struct
import sys
import hashlib
import time
import traceback
import os
import asyncio

from .utils import tobytes
from .core import Signals

log = logging.getLogger('rpc')

# Global constants
RPC_PACKET_HEADER_SIZE = struct.calcsize("I4sI")
# Protocol compatible between Python 2 and 3.  (Well, quasi-compatible, there
# are some issues due to the str/unicode changes in 3.)
PICKLE_PROTOCOL = 4


def make_exception_class(name, bases, dict):
    """
    Class generator for AsyncException.  Creates AsyncException class
    which derives the class of a particular Exception instance.
    """
    def create(exc, stack, *args):
        return type(name, bases + (exc.__class__,), dict)(exc, stack, *args)
    return create


class RemoteException(metaclass=make_exception_class):
    """
    Raised when remote RPC calls raise exceptions.  Instances of this class
    inherit the actual remote exception class, so this works:

        try:
            yield client.rpc('write_file')
        except IOError, (errno, msg):
            ...

    When RemoteException instances are printed, they will also include the
    traceback of the remote stack.
    """
    def __init__(self, exc, stack, *args):
        self._remote_exc = exc
        self._remote_exc_stack = stack
        self._remote_exc_args = args

    def __getattribute__(self, attr):
        if attr.startswith('_remote'):
            return super(Exception, self).__getattribute__(attr)
        return self._remote_exc.__getattribute__(attr)

    def __str__(self):
        header = 'RPC call "{}" remote traceback:\n'.format(self._remote_exc_args[0])
        dump = ''.join(traceback.format_list(self._remote_exc_stack))
        info = '%s: %s' % (self._remote_exc.__class__.__name__, str(self._remote_exc))
        return header + dump + info

    def __repr__(self):
        return 'RemoteException({})'.format(repr(self._remote_exc))


class RPCError(Exception):
    pass


class NotConnectedError(RPCError):
    """
    Raised when an attempt is made to call a method on a channel that is not connected.
    """
    pass


class AuthenticationError(RPCError):
    pass


class InvalidCallError(RPCError):
    pass


class Server:
    """
    RPC server class.  RPC servers accept incoming connections from client,
    however RPC calls can be issued in either direction on channels.
    """
    def __init__(self, auth_secret=b'', *, loop=None):
        super().__init__()
        self.signals = Signals('client-connected')
        self._auth_secret = tobytes(auth_secret)
        self._server = None
        self.objects = []


    def _make_client_channel(self):
        client = Channel(self._auth_secret, challenge=True, loop=self._loop)
        client.client_type = 'client'
        for obj in self.objects:
            client.register(obj)
        self._client=client
        return client


    @asyncio.coroutine
    def start(self, address='', *, loop=None):
        if not loop:
            loop = asyncio.get_event_loop()
        self._loop = loop
        self._server = yield from loop.create_unix_server(self._make_client_channel, address)


    def stop(self):
        if self._server:
            self._server.close()
            self._client.close()


    def register(self, obj):
        """
        Registers one or more previously exposed callables to any clients
        connecting to this RPC Server.

        :param obj: callable(s) to be accessible to connected clients.
        :type obj: callable, module, or class instance

        If a module is given, all exposed callables.
        """
        self.objects.append(obj)


    def disconnect(self, obj):
        """
        Disconnects a previously connected object.
        """
        try:
            self.objects.remove(obj)
        except ValueError:
            pass


class Channel(asyncio.Protocol):
    """
    Channel object for two point communication, implementing the custom rpc
    protocol. The server creates a Channel object for each incoming client
    connection.  Client itself is also a Channel.
    """
    channel_type = 'client'

    def __init__(self, auth_secret=b'', challenge=False, *, loop=None):
        super().__init__()
        self.signals = Signals('authenticated', 'closed')
        self._authenticated = False
        self._transport = None
        self._loop = loop or asyncio.get_event_loop()

        # Buffer containing packets deferred until after authentication.
        self._write_buffer_deferred = []
        self._read_buffer = []
        self._callbacks = {}
        self._next_seq = 1
        self._rpc_futures = {}
        self._auth_secret = tobytes(auth_secret)
        self._challenge = challenge
        self._pending_challenge = None


    def connection_made(self, transport):
        """
        Callback when a new client connects.
        """
        self._transport = transport
        if self._challenge:
            self._send_auth_challenge()


    @property
    def connected(self):
        return bool(self._transport)


    def register(self, obj):
        """
        Registers one or more previously exposed callables to the peer

        :param obj: callable(s) to be accessible to.
        :type obj: callable, module, or class instance

        If a module is given, all exposed callables.
        """
        if type(obj) == types.FunctionType:
            callables = [obj]
        elif type(obj) == types.ModuleType:
            callables = [ getattr(obj, func) for func in dir(obj) if not func.startswith('_')]
        else:
            callables = [ getattr(obj, func) for func in dir(obj) ]

        for func in callables:
            if callable(func) and hasattr(func, '_toolkit_rpc'):
                self._callbacks[func._toolkit_rpc] = func


    def _call(self, future, cmd, args, kwargs):
        if not self.connected:
            raise NotConnectedError()

        seq = self._next_seq
        self._next_seq += 1
        # create InProgress object
        payload = pickle.dumps((cmd, args, kwargs), PICKLE_PROTOCOL)
        self._send_packet(seq, 'CALL', payload)
        self._rpc_futures[seq] = (future, cmd)


    def call(self, cmd, *args, **kwargs):
        """
        Call the remote command and return InProgress.
        """
        future = asyncio.Future()
        self._loop.call_soon_threadsafe(self._call, future, cmd, args, kwargs)
        return future


    def close(self):
        """
        Forcefully close the RPC channel.
        """
        self._transport.close()


    def _write(self, data):
        """
        Writes data to the channel.
        """
        self._transport.write(data)


    def connection_lost(self, exc):
        self._transport = None

        if not self._authenticated:
            # Socket closed before authentication completed.  We assume it's
            # because authentication failed (though it may not be).
            log.error('rpc peer closed before authentication completed; probably incorrect shared secret.')

        log.debug('close socket for %s', self)
        for future, cmd in self._rpc_futures.values():
            future.set_exception(NotConnectedError('rpc channel closed'))
        self._rpc_futures.clear()
        if not self._authenticated:
            self.signals['authenticated'].emit(AuthenticationError())
        else:
            self._authenticated = False
        self.signals['closed'].emit(exc)


    def data_received(self, data):
        """
        Invoked when a new chunk is read from the socket.
        """
        self._read_buffer.append(data)
        # Before we start into the loop, make sure we have enough data for
        # a full packet.  For very large packets (if we just received a huge
        # pickled object), this saves the string.join() which can be very
        # expensive.  (This is the reason we use a list for our read buffer.)
        buflen = sum(len(x) for x in self._read_buffer)
        if buflen < RPC_PACKET_HEADER_SIZE:
            return

        if not self._authenticated and buflen > 1024:
            # Because we are not authenticated, we shouldn't have more than 1k
            # in the buffer.  If we do it's because the remote has sent a
            # large amount of data before completing authentication.
            log.warning("Too much data received from remote end before authentication; disconnecting")
            self.close()
            return

        # Ensure the first block in the read buffer is big enough for a full
        # packet header.  If it isn't, then we must have more than 1 block in
        # the buffer, so keep merging blocks until we have a block big enough
        # to be a header.  If we're here, it means that buflen >=
        # RPC_PACKET_HEADER_SIZE, so we can safely loop.
        while len(self._read_buffer[0]) < RPC_PACKET_HEADER_SIZE:
            self._read_buffer[0] += self._read_buffer.pop(1)

        # Make sure the the buffer holds enough data as indicated by the
        # payload size in the header.
        header = self._read_buffer[0][:RPC_PACKET_HEADER_SIZE]
        payload_len = struct.unpack("I4sI", header)[2]
        if buflen < payload_len + RPC_PACKET_HEADER_SIZE:
            return

        # At this point we know we have enough data in the buffer for the
        # packet, so we merge the array into a single buffer.
        strbuf = b''.join(self._read_buffer)
        self._read_buffer = []
        while True:
            if len(strbuf) <= RPC_PACKET_HEADER_SIZE:
                if len(strbuf) > 0:
                    self._read_buffer.append(tobytes(strbuf))
                break
            header = strbuf[:RPC_PACKET_HEADER_SIZE]
            seq, packet_type, payload_len = struct.unpack("I4sI", header)
            if len(strbuf) < payload_len + RPC_PACKET_HEADER_SIZE:
                # We've also received portion of another packet that we
                # haven't fully received yet.  Put back to the buffer what
                # we have so far, and we can exit the loop.
                self._read_buffer.append(strbuf)
                break

            # Grab the payload for this packet, and shuffle strbuf to the
            # next packet.
            payload = strbuf[RPC_PACKET_HEADER_SIZE:RPC_PACKET_HEADER_SIZE + payload_len]
            strbuf = strbuf[RPC_PACKET_HEADER_SIZE + payload_len:]
            if not self._authenticated:
                self._handle_packet_before_auth(seq, packet_type, payload)
            else:
                self._handle_packet_after_auth(seq, packet_type, payload)


    def _send_packet(self, seq, packet_type, payload):
        """
        Send a packet (header + payload) to the other side.
        """
        header = struct.pack("I4sI", seq, tobytes(packet_type), len(payload))
        if not self._authenticated and tobytes(packet_type) not in (b'RESP', b'AUTH'):
            self._write_buffer_deferred.append(header + payload)
        else:
            self._write(header + payload)


    def _send_answer(self, answer, seq):
        """
        Send delayed answer when callback returns InProgress.
        """
        payload = pickle.dumps(answer, PICKLE_PROTOCOL)
        self._send_packet(seq, 'RETN', payload)


    def _send_exception(self, exc, seq):
        """
        Send delayed exception when callback returns InProgress.
        """
        stack = traceback.extract_tb(exc.__traceback__)
        try:
            payload = pickle.dumps((exc, stack), PICKLE_PROTOCOL)
        except pickle.UnpickleableError:
            payload = pickle.dumps((Exception(tobytes(exc)), stack), PICKLE_PROTOCOL)
        self._send_packet(seq, 'EXCP', payload)


    def _handle_packet_after_auth(self, seq, packet_type, payload):
        """
        Handle incoming packet (called from _handle_write) after
        authentication has been completed.
        """
        if packet_type == b'CALL':
            # Remote function call, send answer
            function, args, kwargs = pickle.loads(payload)
            try:
                if function not in self._callbacks:
                    raise InvalidCallError('RPC function "{}" does not exist'.format(function))
                if self._callbacks[function]._toolkit_rpc_param[0]:
                    args = [ self ] + list(args)
                result = self._callbacks[function](*args, **kwargs)
            except Exception as e:
                self._send_exception(e, seq)
                return

            if asyncio.iscoroutine(result):
                # The registered function is a coroutine so wrap it in a Task
                # so it continues to execute.   Since Task subclasses Future,
                # the next block of code will add a done callback to send the
                # result back to the caller.
                result = asyncio.Task(result)
            if isinstance(result, asyncio.Future):
                def _done_cb(future):
                    try:
                        self._send_answer(future.result(), seq)
                    except Exception as e:
                        self._send_exception(e, seq)

                result.add_done_callback(_done_cb)
            else:
                self._send_answer(result, seq)

        elif packet_type == b'RETN':
            # RPC return
            payload = pickle.loads(payload)
            future, cmd = self._rpc_futures.get(seq)
            if future is None:
                return True
            del self._rpc_futures[seq]
            future.set_result(payload)
            return True

        elif packet_type == b'EXCP':
            # Exception for remote call
            try:
                exc_value, stack = pickle.loads(payload)
            except Exception as e:
                exc_value, stack = e, ''
            future, cmd = self._rpc_futures.get(seq)
            if future is None:
                return True
            del self._rpc_futures[seq]
            remote_exc = RemoteException(exc_value, stack, cmd)
            future.set_exception(remote_exc)
            return True

        else:
            log.error('unknown packet type %s', packet_type)


    def _handle_packet_before_auth(self, seq, packet_type, payload):
        """
        This function handles any packet received by the remote end while we
        are waiting for authentication.  It responds to AUTH or RESP packets
        (auth packets) while closing the connection on all other packets (non-
        auth packets).

        Design goals of authentication:
           * prevent unauthenticated connections from executing RPC commands
             other than 'auth' commands.
           * prevent unauthenticated connections from causing denial-of-
             service at or above the RPC layer.
           * prevent third parties from learning the shared secret by
             eavesdropping the channel.

        Non-goals:
           * provide any level of security whatsoever subsequent to successful
             authentication.
           * detect in-transit tampering of authentication by third parties
             (and thus preventing successful authentication).

        The parameters 'seq' and 'packet_type' are untainted and safe.  The parameter
        payload is potentially dangerous and this function must handle any
        possible malformed payload gracefully.

        Authentication is a 4 step process and once it has succeeded, both
        sides should be assured that they share the same authentication secret.
        It uses a challenge-response scheme similar to CRAM.  The party
        responding to a challenge will hash the response with a locally
        generated salt to prevent a Chosen Plaintext Attack.  (Although CPA is
        not very practical, as they require the client to connect to a rogue
        server.) The server initiates authentication.

           1. Server sends challenge to client (AUTH packet)
           2. Client receives challenge, computes response, generates a
              counter-challenge and sends both to the server in reply (RESP
              packet with non-null challenge).
           3. Server receives response to its challenge in step 1 and the
              counter-challenge from server in step 2.  Server validates
              client's response.  If it fails, server logs the error and
              disconnects.  If it succeeds, server sends response to client's
              counter-challenge (RESP packet with null challenge).  At this
              point server considers client authenticated and allows it to send
              non-auth packets.
           4. Client receives server's response and validates it.  If it fails,
              it disconnects immediately.  If it succeeds, it allows the server
              to send non-auth packets.

        Step 1 happens when a new connection is initiated.  Steps 2-4 happen in
        this function.  3 packets are sent in this handshake (steps 1-3).

        WARNING: once authentication succeeds, there is implicit full trust.
        There is no security after that point, and it should be assumed that
        the client can invoke arbitrary calls on the server, and vice versa,
        because no effort is made to validate the data on the channel.

        Also, individual packets aren't authenticated.  Once each side has
        sucessfully authenticated, this scheme cannot protect against
        hijacking or denial-of-service attacks.

        One goal is to restrict the code path taken packets sent by
        unauthenticated connections.  That path is:

           _handle_read() -> _handle_packet_before_auth()

        Therefore these functions must be able to handle malformed and/or
        potentially malicious data on the channel, and as a result they are
        highly paranoid.  When these methods calls other functions, it must do
        so only with untainted data.  Obviously one assumption is that the
        underlying python calls made in these methods (particularly
        struct.unpack) aren't susceptible to attack.
        """
        def panic(message):
            # Aborts pending connect inprogress with exc and closes the connection.
            self.signals['authenticated'].emit(AuthenticationError(message))
            log.warning(message)
            self.close()


        if packet_type not in (b'AUTH', b'RESP'):
            # Received a non-auth command while expecting auth.
            return panic('got %s before authentication is complete; closing socket.' % packet_type)

        try:
            # Payload could safely be longer than 20+20+20 bytes, but if it
            # is, something isn't quite right.  We'll be paranoid and
            # disconnect unless it's exactly 60 bytes.
            assert(len(payload) == 60)

            # Unpack the auth packet payload into three separate 20 byte
            # strings: the challenge, response, and salt.  If challenge is
            # not NULL (i.e. '\x00' * 20) then the remote is expecting a
            # a response.  If response is not NULL then salt must also not
            # be NULL, and the salt is used along with the previously sent
            # challenge to validate the response.
            challenge, response, salt = struct.unpack("20s20s20s", payload)
        except (AssertionError, struct.error):
            return panic('Malformed authentication packet from remote; disconnecting.')

        # At this point, challenge, response, and salt are 20 byte strings of
        # arbitrary binary data.  They're considered benign.

        if packet_type == b'AUTH':
            # Step 2: We've received a challenge.  If we've already sent a
            # challenge (which is the case if _pending_challenge is not None),
            # then something isn't right.  This could be a DoS so we'll
            # disconnect immediately.
            if self._pending_challenge:
                self._pending_challenge = None
                self.close()
                return

            # Otherwise send the response, plus a challenge of our own.
            response, salt = self._get_challenge_response(challenge)
            self._pending_challenge = self._get_rand_value()
            payload = struct.pack("20s20s20s", self._pending_challenge, response, salt)
            self._send_packet(seq, 'RESP', payload)
            log.debug('Got initial challenge from server, sending response.')
            return

        elif packet_type == b'RESP':
            # We've received a reply to an auth request.

            if self._pending_challenge == None:
                # We've received a response packet to auth, but we haven't
                # sent a challenge.  Something isn't right, so disconnect.
                return panic('Unexpectedly received authentication reply to unissued challenge; disconnecting.')

            # Step 3/4: We are expecting a response to our previous challenge
            # (either the challenge from step 1, or the counter-challenge from
            # step 2).  First compute the response we expect to have received
            # based on the challenge sent earlier, our shared secret, and the
            # salt that was generated by the remote end.

            expected_response = self._get_challenge_response(self._pending_challenge, salt)[0]
            # We have our response, so clear the pending challenge.
            self._pending_challenge = None
            # Now check to see if we were sent what we expected.
            if response != expected_response:
                return panic('Peer failed authentication.')

            # Challenge response was good, so the remote is considered
            # authenticated now.
            self._authenticated = True
            log.debug('Valid response received, remote authenticated.')

            # If remote has issued a counter-challenge along with their
            # response (step 2), we'll respond.  Unless something fishy is
            # going on, this should always succeed on the remote end, because
            # at this point our auth secrets must match.  A challenge is
            # considered issued if it is not NULL ('\x00' * 20).  If no
            # counter-challenge was received as expected from step 2, then
            # authentication is only one-sided (we trust the remote, but the
            # remote won't trust us).  In this case, things won't work
            # properly, but there are no negative security implications.
            if len(challenge.strip(b'\x00')) != 0:
                response, salt = self._get_challenge_response(challenge)
                payload = struct.pack("20s20s20s", b'', response, salt)
                self._send_packet(seq, 'RESP', payload)
                log.debug('Sent response to challenge from client.')

            # Empty deferred write buffer now that we're authenticated.
            self._write(b''.join(self._write_buffer_deferred))
            self._write_buffer_deferred = []
            self._handle_connected()


    def _handle_connected(self):
        """
        Called when the channel is authenticated and ready to be used
        """
        self.signals['authenticated'].emit()


    def _get_rand_value(self):
        """
        Returns a 20 byte value which is computed as a SHA hash of the
        current time concatenated with 64 bytes from /dev/urandom.  This
        value is not by design a nonce, but in practice it probably is.
        """
        rbytes = os.urandom(64)
        return hashlib.sha1(tobytes(time.time(), coerce=True) + rbytes).digest()


    def _send_auth_challenge(self):
        """
        Send challenge to remote end to initiate authentication handshake.
        """
        self._pending_challenge = self._get_rand_value()
        payload = struct.pack("20s20s20s", self._pending_challenge, b'', b'')
        self._send_packet(0, 'AUTH', payload)


    def _get_challenge_response(self, challenge, salt = None):
        """
        Generate a response for the challenge based on the auth secret supplied
        to the constructor.  This essentially implements CRAM, as defined in
        RFC 2195, using SHA-1 as the hash function, however the challenge is
        concatenated with a locally generated 20 byte salt to form the key,
        and the resulting key is padded to the SHA-1 block size, as with HMAC.

        If salt is not None, it is the value generated by the remote end that
        was used in computing their response.  If it is None, a new 20-byte
        salt is generated and used in computing our response.
        """
        # Make function to XOR each character in string s with byte.
        def xor(s, byte):
            return b''.join(bytes([x ^ byte]) for x in s)

        def H(s):
            # Returns the 20 byte SHA-1 digest of string s.
            return hashlib.sha1(s).digest()

        if not salt:
            salt = self._get_rand_value()

        # block size of SHA-1 is 512 bits (64 bytes)
        B = 64
        # Key is auth secret concatenated with salt
        K = self._auth_secret + salt
        if len(K) > B:
            # key is larger than B, so first hash.
            K = H(K)
        # Pad K to be of length B
        K = K + b'\x00' * (B - len(K))

        return H(xor(K, 0x5c) + H(xor(K, 0x36) + challenge)), salt


    def __repr__(self):
        tp = self.channel_type
        if not self._transport:
            return '<rpc.Channel (%s) - disconnected>' % tp
        return '<rpc.Channel (%s) %s>' % (tp, self._transport.get_extra_info('socket'))


class Client(Channel):
    DISCONNECTED = 'DISCONNECTED'
    CONNECTING = 'CONNECTING'
    CONNECTED = 'CONNECTED'

    def __init__(self, auth_secret=b'', *, loop=None):
        super().__init__(auth_secret, loop=loop)
        self.state = Client.DISCONNECTED
        self.signals.add('connected')


    @asyncio.coroutine
    def _connect(self, address):
        try:
            self.state = Client.CONNECTING
            transport, channel = yield from self._loop.create_unix_connection(lambda: self, address)
            yield from channel.signals['authenticated'].future()
            self.state = Client.CONNECTED
        except:
            self.state = Client.DISCONNECTED
            raise


    @asyncio.coroutine
    def _connect_with_retry(self, address, retry=None, return_on_connect=True):
        while True:
            try:
                if self.state == Client.DISCONNECTED:
                    yield from self._connect(address)
                    if return_on_connect:
                        return
                else:
                    yield from self.signals['closed'].future()
                    self.state = Client.DISCONNECTED
            except Exception as e:
                if not retry:
                    raise
            yield from asyncio.sleep(retry)


    @asyncio.coroutine
    def connect(self, address, retry=None):
        yield from self._connect_with_retry(address, retry)
        if retry:
            asyncio.Task(self._connect_with_retry(address, retry, return_on_connect=False))
        return self


@asyncio.coroutine
def connect(address, auth_secret=b'', retry=None, *, loop=None):
    client = Client(auth_secret, loop=loop)
    yield from client.connect(address, retry)
    return client


def expose(command=None, add_client=False):
    """
    Decorator to expose a function. If add_client is True, the client
    object will be added to the command list as first argument.
    """
    def decorator(func):
        func._toolkit_rpc = command or func.__name__
        func._toolkit_rpc_param = (add_client,)
        return func
    return decorator
