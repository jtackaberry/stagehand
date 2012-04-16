from __future__ import absolute_import
import os
import hashlib
import functools
import logging

from . import server as web
from .async import asyncweb
from ..config import config

log = logging.getLogger('stagehand.web.app')

class SessionPlugin(object):
    name = 'session'
    api = 2

    def apply(self, func, context):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if 'stagehand.session' not in web.request.cookies:
                path = config.web.proxied_root if 'X-Forwarded-Host' in web.request.headers else '/'
                id = hashlib.md5(os.urandom(32)).hexdigest()
                web.request.cookies['stagehand.session'] = id
                web.response.set_cookie('stagehand.session', id, path=path)
            return func(*args, **kwargs)
        return wrapper

def shview(fname):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            d = func(*args, **kwargs)
            d.update({
                'manager': web.request['stagehand.manager'],
                'config': config,
                'json': web.json_dumps,
                'root': config.web.proxied_root if 'X-Forwarded-Host' in web.request.headers else ''
            })
            session = web.request.cookies['stagehand.session']
            if session:
                d['async'] = asyncweb.pop_finished_jobs(session)
            return d
        return web.csview(fname)(wrapper)
    return decorator
