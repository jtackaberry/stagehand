import os
import time
import hashlib
import functools
import mimetypes
import itertools
import time
import logging
import asyncio
from datetime import datetime, timedelta


from ..toolbox.config import get_description

from . import server as web
from . import api
from .async import asyncweb, webcoroutine
from .settings import rename_example
from .utils import SessionPlugin, CachePlugin, shview, static_file_from_zip, abspath_to_zippath
from ..utils import episode_status_icon_info
from ..coffee import cscompile_with_cache
from ..config import config

log = logging.getLogger('stagehand.web.app')

web.install(SessionPlugin())
web.install(CachePlugin())


@web.get('/static/:filename#.*#')
def static(filename):
    manager = web.request['stagehand.manager']
    root = os.path.join(manager.paths.data, 'web')
    ziproot = abspath_to_zippath(root)
    response = None

    if ziproot:
        try:
            target = filename + '.compiled' if filename.endswith('.coffee') else filename
            response = static_file_from_zip(ziproot, target)
        except AttributeError:
            pass

    if not response:
        # Load static file from filesystem.
        if filename.endswith('.coffee'):
            # This is CoffeeScript, so we need to return the compiled JavaScript
            # instead.  Ok, not exactly static, strictly speaking. Close enough.
            src = os.path.abspath(os.path.join(root, filename))
            if not src.startswith(root):
                raise web.HTTPError(403, 'Access denied.')
            elif not os.path.exists(src):
                # Before we give up, is there a pre-compiled version?  If not,
                # static_file() will return a 404.
                response = web.static_file(filename + '.compiled', root=root)
            else:
                cached, data = cscompile_with_cache(src, web.request['coffee.cachedir'])
                web.response.logextra = '(CS %s)' % 'cached' if cached else 'compiled on demand'
                web.response.content_type = 'application/javascript'
                web.response['Cache-Control'] = 'max-age=3600'
                return data
        else:
            response = web.static_file(filename, root=root)

    if filename.endswith('.gz') and not isinstance(response, web.HTTPError):
        # static_file() does the right thing with respect to Content-Type
        # and Content-Encoding for gz files.  But if the client doesn't have
        # gzip in Accept-Encoding, we need to decompress it on the fly.
        if 'gzip' not in web.request.headers.get('Accept-Encoding', ''):
            import gzip
            response.body = gzip.GzipFile(fileobj=response.body)
    #elif filename.endswith('.coffee'):
    #    response['X-SourceMap'] = '/static/' + filename + '.map'
    return response


@web.get('/')
@shview('home.tmpl')
def home():
    return {}



@web.get('/tv/')
@shview('tv/library.tmpl')
def tv_library():
    return {}

@web.get('/tv/<id>', method='GET')
@shview('tv/show.tmpl')
def tv_show(id):
    tvdb = web.request['stagehand.manager'].tvdb
    series = tvdb.get_series_by_id(id)
    if not series:
        raise web.HTTPError(404, 'Invalid show.')
    return {
        'series': series,
        'providers': tvdb.providers.values()
    }


@web.get('/tv/add', method='GET')
@shview('tv/add.tmpl')
def tv_add():
    return {}


@web.get('/tv/upcoming')
@shview('tv/upcoming.tmpl')
def tv_upcoming():
    return {}



@web.get('/downloads/')
@shview('downloads/downloads.tmpl')
def downloads():
    manager = web.request['stagehand.manager']
    weeks = int(web.request.query.weeks) if web.request.query.weeks.isdigit() else 1
    status = web.request.query.status or 'have'

    # Construct a list of episodes, sorted by air date, that are either needed
    # or match the criteria for inclusion (based on status and weeks).  Episodes
    # in the download queue aren't included as they're displayed separately.
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    # The most recent past Sunday (or today, if today is Sunday)
    sunday = today if today.weekday() == 6 else today - timedelta(days=today.weekday() + 1)
    episodes = []
    for s in manager.tvdb.series:
        for ep in s.episodes:
            if ep.status != ep.STATUS_NEED_FORCED and (not ep.aired or manager.is_episode_queued_for_retrieval(ep)):
                continue
            if s.cfg.paused:
                # Don't show episodes for paused series, even if they are needed.
                continue
            icon, title = episode_status_icon_info(ep)
            if ep.airdate:
                # week 0 is anything on or after sunday
                week = (max(0, (sunday - ep.airdate).days) + 6) // 7
                if (icon in ('ignore', 'have') and week >= weeks) or (icon == 'ignore' and status == 'have'):
                    continue
            else:
                # Episode is STATUS_NEED_FORCED without an airdate.
                week = None
            episodes.append((ep, icon, title, week))
    # For episodes without an airdate, just use 1900-01-01 for sorting
    # purposes, so they sorted last.
    episodes.sort(key=lambda i: (i[0].airdatetime or datetime(1900, 1, 1), i[0].name), reverse=True)
    return {
        'weeks': weeks,
        'status': status,
        'episodes': episodes
    }


@web.get('/settings/')
@shview('settings/general.tmpl')
def settings_general():
    return {
        'desc': lambda x: get_description(x).replace('\n\n', '<br/><br/>'),
        'rename_example':
            rename_example(config.misc.tvdir, config.naming.separator, config.naming.season_dir_format,
                           config.naming.code_style, config.naming.episode_format)
    }

@web.get('/settings/rename_example')
def settings_rename_example():
    q = web.request.query
    return rename_example(config.misc.tvdir, q.separator, q.season_dir_format,
                          q.code_style, q.episode_format)


@web.get('/settings/searchers')
@shview('settings/searchers.tmpl')
def settings_searchers():
    return {}

@web.get('/settings/retrievers')
@shview('settings/retrievers.tmpl')
def settings_retrievers():
    return {}

@web.get('/settings/notifiers')
@shview('settings/notifiers.tmpl')
def settings_notifiers():
    return {}



@web.get('/log/')
@shview('log/application.tmpl')
def log_application():
    return {}

@web.get('/log/web')
@shview('log/web.tmpl')
def log_web():
    return {}
