import os
import hashlib
import functools
import logging
import re
import time
import mimetypes
import json
import inspect
import asyncio

from . import bottle
from . import server as web
from .async import asyncweb
from ..config import config
from ..coffee import cscompile_with_cache, CSCompileError
from ..utils import abspath_to_zippath, get_file_from_zip

log = logging.getLogger('stagehand.web.app')


class SessionPlugin:
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


class CachePlugin:
    name = 'cache'
    api = 2
    def apply(self, callback, context):
        cache = context.config.get('cache', None)
        revalidate = ', must-revalidate' if context.config.get('revalidate') else ''
        if cache is False:
            cache = 'max-age=0,no-cache,no-store'
        elif isinstance(cache, int) and not isinstance(cache, bool):
            cache = 'max-age=%d%s' % (cache, revalidate)
        elif isinstance(cache, str):
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


class CSTemplate(bottle.SimpleTemplate):
    """
    Implements Bottle's SimpleTemplate but supports inline CoffeeScript,
    compiling to Javascript if necessary.

    If an up-to-date compiled version (either locally cached or part of the
    distribution) exists, then we use that directly.  Otherwise the template
    is compiled and locally cached, and any inline CoffeeScript is compiled to
    Javascript.  This is convenient for development.

    The template file can exist inside an executable zip, but in this case
    dynamic compiling isn't supported, and the compiled version that's in the
    zip bundle is used without even looking for the non-compiled version and
    comparing mtime.  (It's assumed that the non-compiled version of the
    template isn't even included in the zip bundle.)
    """
    def search(self, name, lookup=None):
        for path in lookup:
            path = abspath_to_zippath(path)
            if path:
                # We are running out of an executable zip, and the template
                # path is inside the zip.  See if the given name in compiled
                # form can be found there.
                try:
                    info, mtime, f = get_file_from_zip(path, name + '.compiled')
                    self.source = f.read()
                    f.close()
                    return os.path.join(path, name) + '.compiled'
                except FileNotFoundError:
                    # Not inside the zip, keep looking.
                    pass

        # If we're here, use the superclass implementation to try to find the
        # non-compiled template on the filesystem.
        path = super().search(name, lookup)
        if not path:
            path = super().search(name + '.compiled', lookup)
        return path


    def execute(self, _stdout, kwargs):
        # Preserve environment for later include/rebase (in subtemplate())
        self._env = kwargs
        if not self.source:
            # Non-zip bundle loading.  We have an opportunity to dynamically
            # compile.  If the filename ends with .compiled then search() wasn't able
            # to find the raw, uncompiled template.
            if not self.filename.endswith('.compiled'):
                # Raw template found, so we can use cscompile_with_cache() to dynamically
                # compile (if necessary).
                cachedir = bottle.request['coffee.cachedir']
                try:
                    cached, self.source = cscompile_with_cache(self.filename, cachedir, is_html=True)
                except CSCompileError as e:
                    raise bottle.HTTPError(500, e.args[0], traceback=e.args[1])
                bottle.response.logextra = '(CS %s)' % 'cached' if cached else 'compiled'
                # Before super does eval(), set filename to .compiled form so any exceptions
                # raised show proper lines.
                self.filename += '.compiled'
        return super().execute(_stdout, kwargs)


    def subtemplate(self, subtpl, _stdout, *args, **kwargs):
        # Merge environment of parent template into subtemplate, but do not
        # replace existing attributes.
        if args:
            for k, v in self._env.items():
                if k not in args[0]:
                    args[0][k] = v
        return super().subtemplate(subtpl, _stdout, *args, **kwargs)


def _render_cstemplate(fname, kwargs):
    # All templates get these special names exposed.
    kwargs.update({
        'manager': web.request['stagehand.manager'],
        'config': config,
        'json': json.dumps,
        'root': config.web.proxied_root if 'X-Forwarded-Host' in web.request.headers else ''
    })
    session = web.request.cookies['stagehand.session']
    if session:
        kwargs['async'] = asyncweb.pop_finished_jobs(session)
    # Now render the template.
    return bottle.template(fname, template_adapter=CSTemplate, **kwargs)



def shview(fname):
    """
    Custom "Stagehand view" decorator that supports templates containing
    CoffeeScript, and supports decorated functions being generators for
    asyncio.
    """
    def decorator(func):
        if inspect.isgeneratorfunction(func):
            # Decorated function is a generator, so wrap with a generator that
            # can yield back to the main loop before rendering the template.
            def wrapper(*args, **kwargs):
                res = func(*args, **kwargs)
                if isinstance(res, asyncio.Future) or inspect.isgenerator(res):
                    res = yield from res
                return _render_cstemplate(fname, res)
        else:
            # Decorated function is a normal function.
            def wrapper(*args, **kwargs):
                return _render_cstemplate(fname, func(*args, **kwargs))
        return functools.wraps(func)(wrapper)
    return decorator


def _seekless_file_iter_range(fp, offset, bytes, maxread=1024*1024):
    # ZipFile doesn't support seek(), so read until we reach the requested
    # seek position.
    while offset > 0:
        offset -= len(fp.read(min(offset, maxread)))

    while bytes > 0:
        part = fp.read(min(bytes, maxread))
        if not part: break
        bytes -= len(part)
        yield part


def static_file_from_zip(root, filename):
    """
    A reimplementation of bottle.static_file() that supports serving static
    content from zip bundles.
    """
    ims = bottle.request.environ.get('HTTP_IF_MODIFIED_SINCE')
    if ims:
        ims = bottle.parse_date(ims.split(";")[0].strip())

    try:
        info, mtime, f = get_file_from_zip(root, filename, ims)
    except FileNotFoundError:
        raise web.HTTPError(404, 'File does not exist.')

    headers = {
        'Content-Length': info.file_size,
        'Last-Modified': mtime.strftime("%a, %d %b %Y %H:%M:%S GMT")
    }

    mimetype, encoding = mimetypes.guess_type(filename)
    if mimetype:
        headers['Content-Type'] = mimetype
        if encoding:
            headers['Content-Encoding'] = encoding

    if not f:
        headers['Date'] = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
        return bottle.HTTPResponse(status=304, **headers)

    body = '' if bottle.request.method == 'HEAD' else f
    headers["Accept-Ranges"] = "bytes"
    if 'HTTP_RANGE' in bottle.request.environ:
        ranges = list(bottle.parse_range_header(bottle.request.environ['HTTP_RANGE'], info.file_size))
        if not ranges:
            return bottle.HTTPError(416, "Requested Range Not Satisfiable")
        offset, end = ranges[0]
        headers["Content-Range"] = "bytes %d-%d/%d" % (offset, end -1, info.file_size)
        headers["Content-Length"] = str(end-offset)
        if body:
            body = _seekless_file_iter_range(body, offset, end - offset)
        return bottle.HTTPResponse(body, status=206, **headers)
    return bottle.HTTPResponse(body, **headers)

