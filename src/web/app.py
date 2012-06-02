from __future__ import absolute_import
import os
import time
import hashlib
import functools
import mimetypes
import itertools
import time
import logging
from datetime import datetime, timedelta

import kaa, kaa.config

from . import server as web
from . import api
from .async import asyncweb, webcoroutine
from .settings import rename_example
from .utils import SessionPlugin, shview
from ..utils import download, episode_status_icon_info
from ..config import config

log = logging.getLogger('stagehand.web.app')

web.install(SessionPlugin())


@web.route('/static/:filename#.*#')
def static(filename):
    manager = web.request['stagehand.manager']
    root = os.path.join(manager.datadir, 'web')

    if filename.endswith('.coffee'):
        # This is CoffeeScript, so we need to return the compiled JavaScript
        # instead.  Ok, not exactly static, strictly speaking. Close enough.
        src = os.path.abspath(os.path.join(root, filename))
        if not src.startswith(root):
            raise web.HTTPError(403, 'Access denied.')
        elif not os.path.exists(src):
            raise web.HTTPError(404, 'File does not exist.')
        else:
            cached, data = web.cscompile_with_cache(src, web.request['coffee.cachedir'])
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
                response.output = gzip.GzipFile(fileobj=response.output)

        #if not isinstance(response, web.HTTPError):
        return response


@web.route('/')
@shview('home.tmpl')
def home():
    return {}



@web.route('/library/')
@shview('library/tvseries.tmpl')
def library_tvseries():
    return {}

@web.route('/library/<id>', method='GET')
@shview('library/show.tmpl')
def library_show(id):
    series = web.request['stagehand.manager'].tvdb.get_series_by_id(id)
    if not series:
        raise web.HTTPError(404, 'Invalid show.')
    return {
        'series': series
    }


@web.route('/library/add', method='GET')
@shview('library/add.tmpl')
def library_add():
    return {}


@web.route('/library/import')
@shview('library/import.tmpl')
def library_import():
    return {}



@web.route('/schedule/')
@shview('schedule/upcoming.tmpl')
def schedule():
    return {}


@web.route('/schedule/aired')
@shview('schedule/aired.tmpl')
def schedule_aired():
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


@web.route('/settings/')
@shview('settings/general.tmpl')
def settings_general():
    return {
        'desc': lambda x: kaa.config.get_description(x).replace('\n\n', '<br/><br/>'),
        'rename_example':
            rename_example(config.misc.tvdir, config.naming.separator, config.naming.season_dir_format,
                           config.naming.code_style, config.naming.episode_format)
    }

@web.route('/settings/rename_example')
def settings_rename_example():
    q = web.request.query
    return rename_example(config.misc.tvdir, q.separator, q.season_dir_format,
                          q.code_style, q.episode_format)


@web.route('/settings/searchers')
@shview('settings/searchers.tmpl')
def settings_searchers():
    return {}

@web.route('/settings/retrievers')
@shview('settings/retrievers.tmpl')
def settings_retrievers():
    return {}

@web.route('/settings/notifiers')
@shview('settings/notifiers.tmpl')
def settings_notifiers():
    return {}



@web.route('/log/')
@shview('log/application.tmpl')
def log_application():
    return {}

@web.route('/log/web')
@shview('log/web.tmpl')
def log_web():
    return {}
