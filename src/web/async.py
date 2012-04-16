from __future__ import absolute_import
import os
import time
import hashlib
import functools
import mimetypes
import itertools
import time
import logging

import kaa

from . import server as web

log = logging.getLogger('stagehand.web.app')

def renumerate(i):
    return itertools.izip(reversed(xrange(len(i))), reversed(i))


class AsyncWebJob(object):
    def __init__(self, **kwargs):
        self.timestamp = time.time()
        self.finished = False
        self.error = None
        self.result = None
        [setattr(self, k, v) for k, v in kwargs.items()]

    def notify(self, title, text, **kwargs):
        asyncweb.notify(title, text, session=self.session, id=self.id, **kwargs)

    def notify_after(self, title, text, **kwargs):
        asyncweb.notify_after(title, text, session=self.session, **kwargs)


    def finish(self, ip):
        try:
            self.result = ip.result
        except Exception, e:
            self.error = {'message': '%s: %s' % (e.__class__.__name__, ', '.join(str(s) for s in e.args))}
            log.exception('webcoroutine exception')
        self.finished = True


class AsyncWeb(object):
    job_timeout = 1800
    notification_timeout = 60

    def __init__(self):
        self._job_queue = {}
        self._notification_queue = {}
        # Begin job ids at the current timestamp to prevent conflicting
        # job ids between instance restarts.
        self._next_id = itertools.count(int(time.time())).next
        self._cleanup_timer = kaa.Timer(self._cleanup)


    def _cleanup(self):
        now = time.time()
        def purge(q, timeout):
            [q.pop(i) for i, job in renumerate(q) if now - job.timestamp > timeout]
            return q
        for session, queue in self._job_queue.items():
            if not purge(queue, self.job_timeout):
                log.debug('purging timed out job: %s', self._job_queue[session])
                del self._job_queue[session]
        for session, queue in self._notification_queue.items():
            if not purge(queue, self.notification_timeout):
                del self._notification_queue[session]
        if not self._job_queue and not self._notification_queue:
            # No more cleanup needed.
            self._cleanup_timer.stop()

    def new_job(self):
        session = web.request.cookies['stagehand.session']
        job = AsyncWebJob(session=session, id=self._next_id())
        self._job_queue.setdefault(session, []).append(job)
        self._cleanup_timer.start(60)
        return job


    def watch_job(self, ip, job):
        cb = kaa.Callable(job.finish, ip)
        cb.ignore_caller_args = True
        ip.connect_both(cb)


    def pop_finished_jobs(self, session, jobs=''):
        if not session:
            return []
        jobs = [int(j) for j in (jobs or '').split(',') if j.strip()]
        response = {'jobs': [], 'notifications': []}

        # It's safe to pop from a list you're iterating provided you do
        # it in reverse (otherwise your index would reference a different
        # element after popping).  Ugly, but efficient.
        if session in self._job_queue:
            q = self._job_queue[session]
            for i, job in renumerate(q):
                if job.finished and job.id in jobs:
                    if job.error:
                        response['jobs'].append({'id': job.id, 'error': job.error})
                    else:
                        response['jobs'].append({'id': job.id, 'result': job.result})
                    q.pop(i)
            if not q:
                del self._job_queue[session]

        # Get all notifications for this session.
        if session in self._notification_queue:
            q = self._notification_queue[session]
            response['notifications'].extend(q.pop(i).result for i, n in renumerate(q)
                                                             if n.id is None or n.id in jobs)
            if not q:
                del self._notification_queue[session]

        # Get all global notifications not seen by this session.
        global_notifications = self._notification_queue.get(None, [])
        for i, n in renumerate(global_notifications):
            if session not in n.seen:
                response['notifications'].insert(0, n.result)
                n.seen.add(session)
            if time.time() - n.timestamp > self.notification_timeout:
                global_notifications.pop(i)

        return response


    def notify(self, title, text, session=None, id=None, **kwargs):
        n = AsyncWebJob(session=session, id=id, seen=set())
        n.result = {
            'title': title,
            'text': text,
            'nonblock': kwargs.get('nonblock', False),
            'type': kwargs.get('type', 'notice'),
            'animation': kwargs.get('animation', 'fade'),
            'closer': kwargs.get('closer', True),
            'delay': kwargs.get('delay', 8000)
        }
        self._notification_queue.setdefault(session, []).append(n)
        self._cleanup_timer.start(60)
     
    def notify_after(self, *args, **kwargs):
        kaa.OneShotTimer(self.notify, *args, **kwargs).start(kwargs.get('timeout', 0))


asyncweb = AsyncWeb()


def webcoroutine(interval=1000):
    def decorator(func):
        coroutine = kaa.coroutine()(func)
        def wrapper(*args, **kwargs):
            job = asyncweb.new_job()
            response = {'jobid': job.id, 'interval': interval, 'pending': False}

            try:
                ip = coroutine(job, *args, **kwargs)
            except Exception:
                ip = kaa.InProgress()
                ip.throw()

            if ip.finished:
                job.finish(ip)
            else:
                asyncweb.watch_job(ip, job)
                response['pending'] = True

            # Get job results for all supplied job ids plus this new one.
            jobs = web.request.query.jobs + ',%s' % job.id
            response.update(asyncweb.pop_finished_jobs(job.session, jobs))
            log.debug('webcoroutine response: %s', response)
            return response
        return wrapper
    return decorator


@web.route('/api/jobs')
def async():
    session = web.request.cookies['stagehand.session']
    response = asyncweb.pop_finished_jobs(session, web.request.query.jobs)
    # This gets executed quite frequently by the client, so lower the log level
    # to reduce spamminess.
    web.response.loglevel = logging.DEBUG
    return response
