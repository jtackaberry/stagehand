import os
import urllib
import time
import threading
import kaa
from kaa.core import notifier, CoreThreading

import pycurl

class CurlError(Exception):
    pass


class CurlSSLError(CurlError):
    pass


class curlprop(property):
    READONLY = 1
    WRITEONLY = 2
    READWRITE = 3

    def __init__(self, const, perm=READONLY, initial=None, filter=None):
        super(curlprop, self).__init__(self.getter, self.setter if perm != curlprop.READONLY else None)
        self._const = const
        self._initial = initial
        self._perm = perm
        self._filter = filter

    def getter(self, c):
        if self._perm == curlprop.WRITEONLY:
            return c._curl_opts.get(self, self._initial)
        else:
            return c._curl.getinfo(self._const)

    def setter(self, c, value):
        if self._filter:
            value = self._filter(c, value)
        c._curl_opts[self] = value
        c._curl.setopt(self._const, value)


class Curl(kaa.Object):
    """
    A Kaa-aware abstraction on top of pycurl.

    This uses libcurl's multi objects to manage all transactions, but hooks
    into Kaa's socket monitoring to dispatch to the pycurl multi handler when
    there is socket activity.

    It doesn't buy us anything to run the handler (_perform()) from a loop
    inside a separate thread, because we return back into Python space too much
    to benefit from parallelism.
    
    From that perspective, it might actually be much better to use libcurl's
    easy interface, which is entirely blocking but it's all in C space where it
    would release the GIL.  This should scale across multiple cores and also
    result in simpler code, but it means a separate thread for each HTTP
    transaction, which is morally objectionable. :)  (Also, aborting does not
    look possible with the easy API, probably due to a pycurl bug.  See below.)
    """
    STATE_READY = 'READY'
    STATE_ACTIVE = 'ACTIVE'
    STATE_DONE = 'DONE'
    STATE_ABORTED = 'ABORTED'

    __kaasignals__ = {
        # (self, state, position, total, speed)
        'progress': ''
    }

    # TODO: form stuff http://curl.haxx.se/libcurl/c/curl_formadd.html

    def __init__(self, **props):
        super(Curl, self).__init__()
        self._curl_opts = {}
        # Lock for the multi object.  It doesn't like being poked and prodded
        # (e.g. with close()) when perform() is running in another thread.
        self._lock = threading.RLock()
        self._reinit_curl()

        defaults = {
            'follow_location': True,
            'max_redirs': 5,
            'connect_timeout': 30,
            # FIXME: broken in libcurl/pycurl, need to roll own.
            'timeout': 0
        }
        defaults.update(props)
        for prop, value in defaults.items():
            setattr(self, prop, value)

        self._curl.setopt(pycurl.NOSIGNAL, True)
        self._state = Curl.STATE_READY
        self._rfds = set()
        self._wfds = set()
        self._progress_interval = 0.5
        self._inprogress = self._make_inprogress()
        # We do our own lame timer-based progress polling rather than using
        # pycurl's progress support which appears to introduce a slew of new
        # problems around trying to stop the process gracefully (e.g. ctrl-c).
        self._progress_check_timer = kaa.WeakTimer(self._progress_check)
        self._speed_sample_timer = kaa.WeakTimer(self._speed_sample)
        self._speed_up_samples = []
        self._speed_down_samples = []
        self.signals['progress'].changed_cb = self._progress_signal_changed
        kaa.signals['shutdown'].connect_weak(self.abort)


    def __inprogress__(self):
        return self._inprogress


    def _make_inprogress(self):
        ip = kaa.InProgress()
        ip.signals['abort'].connect_weak(self._abort)
        return ip

    @property
    def progress_interval(self):
        return self._progress_interval

    @progress_interval.setter
    def progress_interval(self, interval):
        self._progress_interval = interval
        if self._progress_check_timer.active and self._progress_check_timer.interval != interval:
            self._progress_check_timer.start(interval)


    @property
    def position(self):
        if self.content_length_download == -1:
            return 0
        else:
            return (self.resume_from or 0) + self.size_download

    @property
    def content_length_download_total(self):
        if self.content_length_download == -1:
            return 0
        else:
            return (self.resume_from or 0) + self.content_length_download

    @property
    def state(self):
        return self._state


    def _progress_check(self):
        status = self.size_download, self.size_upload
        if status == self._last_progress_check:
            return
        self._last_progress_check = status
        self._emit_progress()


    def _calculate_speed(self, samples):
        if self.state != Curl.STATE_ACTIVE or not samples:
            return 0
        deltas = [samples[i] - samples[i-1] for i in range(1, len(samples))]
        return sum(deltas) / len(deltas)


    @property
    def speed_download(self):
        return self._calculate_speed(self._speed_down_samples)

    @property
    def speed_upload(self):
        return self._calculate_speed(self._speed_up_samples)

    def _speed_sample(self):
        if len(self._speed_down_samples) == 10:
            self._speed_down_samples.pop(0)
        if len(self._speed_up_samples) == 10:
            self._speed_up_samples.pop(0)
        self._speed_down_samples.append(self.size_download)
        self._speed_up_samples.append(self.size_upload)


    def _emit_progress(self):
        self.signals['progress'].emit(self, self._state, self.position, 
                                      self.content_length_download_total, self.speed_download)


    def _progress_signal_changed(self, signal, action):
        if self._state != Curl.STATE_ACTIVE:
            return
        if action == kaa.Signal.DISCONNECTED and len(signal) == 0:
            self._progress_check_timer.stop()
            self._speed_sample_timer.stop()
        elif action == kaa.Signal.CONNECTED and len(signal):
            self._progress_check_timer.start(self._progress_interval)
            self._speed_sample_timer.start(1)


    def _reinit_curl(self):
        # So, we use a separate CurlMulti() for every Curl object.  This is
        # obviously silly in terms of how curl is designed, except that
        # there is some braindamaged bug (probably in pycurl) that makes it
        # impossible to properly abort a transfer.  You could return -1
        # from WRITEFUNC or PROGRESSFUNC, or maybe curl.close(), but pycurl
        # just dumps some error to the console and then proceeds to block the
        # whole thread, reading all data from the server and pegging a core at
        # 100% until it's finished. *grmbl*
        #
        # multi.close() is the only problem-free approach I've found, but of
        # course it would stop any Curl objects associated with it, and so we're
        # forced to have 1:1.
        self._multi = pycurl.CurlMulti()
        self._curl = pycurl.Curl()
        self._curl._obj = self # XXX: weakref instead?
        self._multi.add_handle(self._curl)

        # Reinitialize curl options.
        for prop, value in self._curl_opts.items():
            prop.setter(self, value)
            pass

    def get(self, url, target, resume=True):
        if self._state == Curl.STATE_ACTIVE:
            raise ValueError('Curl is active')
        else:
            self._state = Curl.STATE_ACTIVE

        if not self._multi:
            self._reinit_curl()

        if isinstance(target, basestring):
            mode = 'w'
            if resume == True and os.path.exists(target):
                self.resume_from = os.path.getsize(target)
                mode = 'a'
            self._target = file(target, mode)
            self._target_needs_close = True
        elif hasattr(target, 'write'):
            self._target = target
            self._target_needs_close = False
        else:
            raise ValueError('Invalid target: must be filename or file object')

        if isinstance(self._target, file):
            self._curl.setopt(pycurl.WRITEDATA, self._target)
        else:
            self._curl.setopt(pycurl.WRITEFUNCTION, self._target.write)

        if self._inprogress.finished:
            self._inprogress = self._make_inprogress()

        self._last_progress_check = -1, -1
        del self._speed_down_samples[:]
        del self._speed_up_samples[:]
        self._progress_check_timer.stop()
        self._curl.setopt(pycurl.URL, kaa.py3_b(url))
        self._perform()
        # state may become inactive here indirectly via _perform(), e.g.  if
        # DNS resolution fails, it can complete immediately.  So we need to
        # test it even though we just set it to ACTIVE above before starting the
        # progress timer.
        if self._state == Curl.STATE_ACTIVE and len(self.signals['progress']):
            self._progress_check_timer.start(self._progress_interval)
            self._speed_sample_timer.start(1)
        return self._inprogress


    def _abort(self, exc):
        """
        Callback for InProgress abort.
        """
        self._done(aborted=True)


    def abort(self):
        if not self._inprogress.finished:
            self._inprogress.abort()


    def _done(self, errno=None, msg=None, aborted=False):
        if self._state != Curl.STATE_ACTIVE:
            return
        self._state = Curl.STATE_DONE
        self._progress_check_timer.stop()
        self._speed_sample_timer.stop()
        with self._lock:
            self._multi.close()
        self._multi = None
        if self._target and self._target_needs_close and not self._target.closed:
            self._target.close()
        self._update_all_fds()
        self._target = None

        if errno == 18 and self.response_code == 416 and (self.resume_from or 0) > 0:
            # If the file is fully on disk, the server returns 416 (requested
            # range not satisfiable) which curl treats as an error (transfer
            # closed with outstanding read data remaining).  If we get HTTP 416
            # with errno 18 and we have a non-zero resume_from, assume we've
            # hit this bug.  Also see:
            # http://sourceforge.net/tracker/?func=detail&atid=100976&aid=1053287&group_id=976
            errno = None

        if self._inprogress.finished:
            return
        if not aborted:
            if errno:
                errcls = CurlError
                if msg.startswith('SSL'):
                    errcls = CurlSSLError
                self._inprogress.throw(errcls, errcls('[Errno %d] %s' % (errno, msg)), None)
            else:
                # Emit progress signal one last time before finishing.
                self._emit_progress()
                self._inprogress.finish(self.response_code)
        else:
            # We've been aborted, and although the IP is not finished, it will
            # be handled for us by InProgress.abort().  For now, just do one final
            # emission of the progress signal.
            self._state = Curl.STATE_ABORTED
            self._emit_progress()


    def _perform(self, fd=None):
        while True:
            with self._lock:
                ret, n = self._multi.perform()
            if ret != pycurl.E_CALL_MULTI_PERFORM:
                break

        while True:
            num_q, ok, err = self._multi.info_read()
            for c in ok:
                c._obj._done()
            for c, errno, msg in err:
                c._obj._done(errno, msg)
            if num_q == 0:
                break

        if self._last_progress_check == (-1, -1):
            # We haven't done a progress update yet, the download has clearly
            # just started.  Emit now that we should have something to report.
            self._progress_check()

        # TODO: now that multi:easy is 1:1 this can be made more efficient.
        self._update_all_fds()
        return True


    def _update_all_fds(self):
        if self._state != Curl.STATE_ACTIVE:
            rfd = wfd = ()
        else:
            rfd, wfd, efd = self._multi.fdset()
        self._update_fdset(self._rfds, set(rfd), 0)
        self._update_fdset(self._wfds, set(wfd), 1)


    def _update_fdset(self, cur, new, action):
        if cur == new:
            return
        for fd in cur.difference(new):
            notifier.socket_remove(fd, action)
            cur.remove(fd)
        for fd in new.difference(cur):
            notifier.socket_add(fd, self._perform, action)
            cur.add(fd)
        # We're using the notifier directly, bypassing the wakeup bit in
        # kaa.IOMonitor.  If we're not in the main thread, we need to wake up
        # the main loop so it can start listening to any fds we added.
        if not CoreThreading.is_mainthread():
            CoreThreading.wakeup()


    def _prop_filter_bind_address(self, value):
        if not value:
            self._curl.setopt(pycurl.IPRESOLVE, pycurl.IPRESOLVE_WHATEVER)
            # FIXME: what can we pass to libcurl to make it reset INTERFACE?
            # This value forces IPv4.
            return '0.0.0.0'
        elif ':' in value:
            self._curl.setopt(pycurl.IPRESOLVE, pycurl.IPRESOLVE_V6)
        elif '.' in value:
            self._curl.setopt(pycurl.IPRESOLVE, pycurl.IPRESOLVE_V4)
        return value


    # Curl properties
    verbose = curlprop(pycurl.VERBOSE, curlprop.WRITEONLY, False)
    content_length_download = curlprop(pycurl.CONTENT_LENGTH_DOWNLOAD)
    content_length_upload = curlprop(pycurl.CONTENT_LENGTH_UPLOAD)
    # TODO: content length download that takes into account resume_from,
    # like position we need a total size
    size_download = curlprop(pycurl.SIZE_DOWNLOAD)
    size_upload = curlprop(pycurl.SIZE_UPLOAD)
    resume_from = curlprop(pycurl.RESUME_FROM, curlprop.WRITEONLY)
    response_code = curlprop(pycurl.RESPONSE_CODE)
    follow_location = curlprop(pycurl.FOLLOWLOCATION, curlprop.WRITEONLY)
    max_redirs = curlprop(pycurl.MAXREDIRS, curlprop.WRITEONLY)
    connect_timeout = curlprop(pycurl.CONNECTTIMEOUT, curlprop.WRITEONLY)
    timeout = curlprop(pycurl.TIMEOUT, curlprop.WRITEONLY)
    userpwd = curlprop(pycurl.USERPWD, curlprop.WRITEONLY)
    effective_url = curlprop(pycurl.EFFECTIVE_URL, curlprop.READONLY)
    bind_address = curlprop(pycurl.INTERFACE, curlprop.WRITEONLY, filter=_prop_filter_bind_address)
