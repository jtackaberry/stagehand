from __future__ import absolute_import
import logging
import os
import time
from . import bottle
from .bottle import (app, route, view, request, response, TEMPLATE_PATH, TEMPLATES,
                     static_file, abort, redirect, debug, cookie_encode, install,
                     cookie_decode, cookie_is_encoded, HTTPError, SimpleTemplate,
                     json_dumps)
 
from .wsgi import Server, log
from .coffee import csview, cscompile_with_cache


class CachePlugin(object):
    name = 'cache'
    api = 2
    def apply(self, callback, context):
        cache = context.config.get('cache', None)
        revalidate = ', must-revalidate' if context.config.get('revalidate') else ''
        if cache is False:
            cache = 'max-age=0,no-cache,no-store'
        elif isinstance(cache, int) and not isinstance(cache, bool):
            cache = 'max-age=%d%s' % (cache, revalidate)
        elif isinstance(cache, basestring):
            cache = cache
        elif cache not in (True, None):
            raise ValueError('Invalid cache value')

        def wrapper(*args, **kwargs):
            if cache and cache is not True:
                bottle.response.headers['Cache-Control'] = cache
            response = callback(*args, **kwargs)
            if isinstance(response, dict) and not cache:
                # Response is JSON, so unless cache was explicitly True in the decorator,
                # we prevent the client (IE, I'm looking at you) from caching it.
                bottle.response.headers['Cache-Control'] = 'max-age=0,no-cache,no-store'
            return response

        return wrapper

install(CachePlugin())

_s = Server()
start = _s.start
stop = _s.stop
restart = _s.restart
is_running = _s.is_running
