# -----------------------------------------------------------------------------
# inotify.py - Inotify interface
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
import os
import struct
import logging
import select
import errno
import socket
import string
import itertools
import ctypes, ctypes.util
import asyncio

from .core import Signals, Signal
from .utils import *

log = logging.getLogger('toolbox.inotify')


class INotify:
    """
    Monitor files and directories, invoking callbacks when changes occur.

    Monitors only live as long as the INotify object is alive, so it is the
    caller's responsibility to keep a reference.  If the INotify object has no
    more referrants and is deleted, all monitors are automatically removed.

    Multiple instances of this class can be created, but note that there is
    a per-user limit of the number of INotify instances allowed, which is
    controlled by /proc/sys/fs/inotify/max_user_instances
    """
    # INotify constants
    ACCESS = 1
    ALL_EVENTS = 4095
    ATTRIB = 4
    CLOSE = 24
    CLOSE_NOWRITE = 16
    CLOSE_WRITE = 8
    CREATE = 256
    DELETE = 512
    DELETE_SELF = 1024
    IGNORED = 32768
    ISDIR = 1073741824
    MODIFY = 2
    MOVE = 192
    MOVED_FROM = 64
    MOVED_TO = 128
    MOVE_SELF = 2048
    ONESHOT = 2147483648
    OPEN = 32
    Q_OVERFLOW = 16384
    UNMOUNT = 8192

    WATCH_MASK = MODIFY | ATTRIB | DELETE | CREATE | DELETE_SELF | UNMOUNT | \
                 MOVE | MOVE_SELF | MOVED_FROM | MOVED_TO
    CHANGE     = MODIFY | ATTRIB

    @staticmethod
    def mask_to_string(mask):
        """
        Converts a bitmask of events to a human-readable string.

        :param mask: the bitmask of events
        :type mask: int
        :returns: a string in the form EVENT1 | EVENT2 | EVENT3 ...
        """
        events = []
        for attr in itertools(['CHANGE'], INotify.__dict__.keys()):
            if attr == 'WATCH_MASK' or attr[0] not in string.ascii_uppercase:
                continue
            event = getattr(INotify, attr)
            if mask & event == event:
                events.append(attr)
                mask &= ~event
        return ' | '.join(events)


    def __init__(self):
        super().__init__()
        self._fd = 0
        try:
            import fcntl
            self._libc = ctypes.CDLL(ctypes.util.find_library("c"))
            # System libc supports INotify, so setup args/restypes for INotify calls.
            self._libc.inotify_init.restype = ctypes.c_int
            self._libc.inotify_init.argtypes = []
            self._libc.inotify_add_watch.restype = ctypes.c_int
            self._libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            self._libc.inotify_rm_watch.restype = ctypes.c_int
            self._libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        except (TypeError, AttributeError, ImportError):
            # We could use syscall() as a fallback (which used to be the case
            # when we used a C module instead of ctypes), but is it worth it?
            raise OSError(errno.ENOSYS, 'INotify not available on platform')

        self.signals = Signals('event')
        self._watches = {}
        self._watches_by_path = {}
        # We keep track of recently removed watches so we don't get confused
        # if an event callback removes a watch while we're currently
        # processing a batch of events and we receive an event for a watch
        # we just removed.
        self._watches_recently_removed = []
        self._read_buffer = b''
        self._move_state = None  # For MOVED_FROM events
        self._moved_timer = None

        self._fd = self._libc.inotify_init()

        if self._fd < 0:
            raise OSError(errno.ENOSYS, 'INotify support not detected on this system.')

        fcntl.fcntl(self._fd, fcntl.F_SETFL, os.O_NONBLOCK)
        asyncio.get_event_loop().add_reader(self._fd, self._handle_data)


    def __del__(self):
        if os and self._fd >= 0:
            asyncio.get_event_loop().remove_reader(self._fd)
            os.close(self._fd)


    def watch(self, path, mask=None):
        """
        Begin monitoring a file or directory for specific events.

        :param path: the full path to the file or directory to be monitored
        :type path: str
        :param mask: a bitmask of events for which to notify, or None
                     to use the default mask (see below).
        :type mask: int
        :returns: :class:`~Signal` object that is emitted when an event occurs
                  on ``path``.


        The default mask is anything that causes a change (new file, deleted
        file, modified file, or attribute change on the file).

        Callbacks connected to the returned signal are invoked with the same
        arguments as the :attr:`~INotify.signals.event` signal.

        The total number of watches (across all INotify instances) is controlled
        by /proc/sys/fs/inotify/max_user_watches
        """
        path = os.path.realpath(fsname(path))
        if path in self._watches_by_path:
            return self._watches_by_path[path][0]

        if mask == None:
            mask = INotify.WATCH_MASK

        wd = self._libc.inotify_add_watch(self._fd, tobytes(path, fs=True), mask)
        if wd < 0:
            raise IOError('Failed to add watch on "%s"' % path)

        signal = Signal()
        self._watches[wd] = [signal, path]
        self._watches_by_path[path] = [signal, wd]
        return signal


    def ignore(self, path):
        """
        Removes a watch on the given path.

        :param path: the path that had been previously passed to
                     :meth:`~INotify.watch`
        :type path: str
        :returns: True if a matching monitor was removed, or False otherwise.
        """
        path = os.path.realpath(fsname(path))
        if path not in self._watches_by_path:
            return False

        wd = self._watches_by_path[path][1]
        self._libc.inotify_rm_watch(self._fd, wd)
        del self._watches[wd]
        del self._watches_by_path[path]
        self._watches_recently_removed.append(wd)
        return True


    def has_watch(self, path):
        """
        Determine if the given path is currently watched by the INotify object.

        :param path: the path that had been previously passed to
                     :meth:`~INotify.watch`
        :type path: str
        :returns: True if there is a matching monitor, or False otherwise.
        """
        path = os.path.realpath(fsname(path))
        return path in self._watches_by_path


    def get_watches(self):
        """
        Returns a list of all paths monitored by the object.

        :returns: list of strings
        """
        return self._watches_by_path.keys()


    def _emit_last_move(self):
        """
        Emits the last move event (MOVED_FROM), if it exists.
        """
        if not self._move_state:
            return

        prev_wd, prev_mask, dummy, prev_path = self._move_state
        self._watches[prev_wd][0].emit(prev_mask, prev_path)
        self.signals["event"].emit(prev_mask, prev_path, None)
        self._move_state = None
        self._stop_moved_timer()


    def _stop_moved_timer(self):
        if self._moved_timer:
            self._moved_timer.cancel()
            self._moved_timer = None


    def _handle_data(self):
        try:
            self._read_buffer += os.read(self._fd, 32768)
        except (OSError, IOError, socket.error) as e:
            if e.errno == errno.EAGAIN:
                # select(2) man page tells us that on Linux, there may be
                # "circumstances in which a file descriptor is spuriously
                # reported as ready."  EAGAIN is safe to ignore.
                return
            else:
                # Other errors aren't silently ignorable.
                return log.exception('error reading from INotify')

        event_len = struct.calcsize('IIII')
        while True:
            if len(self._read_buffer) < event_len:
                if self._move_state:
                    # We received a MOVED_FROM event with no matching
                    # MOVED_TO.  If we don't get a matching MOVED_TO in 0.1
                    # seconds, emit the MOVED_FROM event.
                    self._stop_moved_timer()
                    self._moved_timer = asyncio.get_event_loop().call_later(0.1, self._emit_last_move)
                break

            wd, mask, cookie, size = struct.unpack("IIII", self._read_buffer[0:event_len])
            if size:
                name = self._read_buffer[event_len:event_len+size].rstrip(b'\0')
            else:
                name = None

            self._read_buffer = self._read_buffer[event_len+size:]
            if wd not in self._watches:
                if wd not in self._watches_recently_removed:
                    # Weird, received an event for an unknown watch; this
                    # shouldn't happen under sane circumstances, so log this as
                    # an error.
                    log.error("INotify received event for unknown watch.")
                continue

            path = self._watches[wd][1]
            if name:
                path = os.path.join(path, fsname(name))

            if self._move_state:
                # Last event was a MOVED_FROM. So if this is a MOVED_TO and the
                # cookie matches, emit once specifying both paths. If not,
                # we will end up emitting two separate MOVED_FROM and MOVED_TO
                # events.
                if mask & INotify.MOVED_TO and cookie == self._move_state[2]:
                    # Great, they match. Fire a MOVE signal with both paths.
                    mask |= INotify.MOVED_FROM
                    prev_wd, dummy, dummy, prev_path = self._move_state
                    self._watches[wd][0].emit(mask, prev_path, path)
                    if prev_wd != wd:
                        # The src and target watch descriptors are different.
                        # Not entirely sure if this can happen, but if it can,
                        # we should emit on both signal.s
                        self._watches[prev_wd][0].emit(mask, prev_path, path)
                    self.signals["event"].emit(mask, prev_path, path)
                    self._move_state = None
                    self._stop_moved_timer()
                    continue

                # No match, fire the earlier MOVED_FROM signal now
                # with no target.
                self._emit_last_move()

            if mask & INotify.MOVED_FROM:
                # This is a MOVED_FROM. Don't emit the signals now, let's wait
                # for a MOVED_TO, which we expect to be next.
                self._move_state = wd, mask, cookie, path
                continue

            self._watches[wd][0].emit(mask, path, None)
            self.signals["event"].emit(mask, path, None)

            if mask & INotify.IGNORED:
                # Self got deleted, so remove the watch data.
                del self._watches[wd]
                del self._watches_by_path[path]
                self._watches_recently_removed.append(wd)

        if not self._read_buffer and len(self._watches_recently_removed) and \
           not select.select([self._fd], [], [], 0)[0]:
            # We've processed all pending inotify events.  We can reset the
            # recently removed watches list.
            self._watches_recently_removed = []
