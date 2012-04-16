from __future__ import absolute_import
import os
import time
import mimetypes
import time
import logging

import kaa, kaa.config

from . import server as web
from .async import asyncweb, webcoroutine
from ..utils import download, episode_status_icon_info
from ..tvdb import Episode
from ..config import config

log = logging.getLogger('stagehand.web.app')

def get_series_from_request(id):
    series = web.request['stagehand.manager'].tvdb.get_series_by_id(id)
    if not series:
        raise web.HTTPError(404, 'Invalid show.')
    return series


@web.route('/api/shows/<id>', method='PUT')
@webcoroutine()
def show_add(job, id):
    manager = web.request['stagehand.manager']
    job.notify('Adding series', 'Retrieving episode information for this series ...')
    try:
        series = yield manager.add_series(id)
        #yield kaa.delay(1)
        #series = manager.tvdb.get_series_by_substring('aliforni')
    except Exception, e:
        job.notify_after('Failed to add series', str(e), timeout=2)
    else:
        job.notify_after('Series added', 'Added series %s to database.' % series.name, timeout=2)
    yield {}


@web.route('/api/shows/<id>', method='DELETE')
@webcoroutine()
def show_delete(job, id):
    manager = web.request['stagehand.manager']
    name = get_series_from_request(id).name
    manager.delete_series(id)
    # Notify the session about the removal, but do it via a timer so that
    # the notification happens on the next page load rather than in response
    # to the API call.
    job.notify_after('Series Deleted', 'Series <b>%s</b> was removed from the database' % name)
    yield {}


@web.route('/api/shows/<id>/banner', cache=3600*24*7)
def show_banner(id):
    # TODO: support cache check
    series = get_series_from_request(id)
    if not series.banner_data:
        raise web.HTTPError(404, 'Invalid show, or no banner for this show.')
    
    web.response['Content-Length'] = len(series.banner_data)
    mimetype, encoding = mimetypes.guess_type(series.banner)
    if mimetype:
        web.response.content_type = mimetype
    return series.banner_data


@web.route('/api/shows/<id>/provider', method='POST')
def show_provider(id):
    series = get_series_from_request(id)
    provider = web.request.forms.get('provider')
    if provider:
        series.change_provider(provider)
        series.cfg.provider = provider


@web.route('/api/shows/<id>/refresh', method='POST')
@webcoroutine()
def show_refresh(job, id):
    series = get_series_from_request(id)
    yield series.refresh()


@web.route('/api/shows/<id>/settings', method='POST')
def show_settings(id):
    series = get_series_from_request(id)
    settings = web.request.forms
    series.cfg.quality = settings.quality
    series.cfg.path = settings.path
    series.cfg.upgrade = True if settings['upgrade'] == 'true' else False
    series.cfg.paused = True if settings['paused'] == 'true' else False
    series.cfg.flat = True if settings['flat'] == 'true' else False


@web.route('/api/shows/<id>/overview', method='GET')
def show_overview(id):
    series = get_series_from_request(id)
    return {'overview': series.overview}


@web.route('/api/shows/<id>/<code>/overview', method='GET')
def show_episode_overview(id, code):
    series = get_series_from_request(id)
    ep = series.get_episode_by_code(code)
    return {'overview': ep.overview if ep else 'Episode not found'}


@web.route('/api/shows/search')
@webcoroutine(interval=500)
def show_search(job):
    q = web.request.query
    manager = web.request['stagehand.manager']
    #yield kaa.delay(5)
    #yield {'results': [{'name': u'Nikita', 'started': u'2010-09-09', 'overview': u"When she was a deeply troubled teenager, Nikita was rescued from death row by a secret U.S. agency known only as Division, who faked her execution and told her she was being given a second chance to start a new life and serve her country. What they didn't tell her was that she was being trained as a spy and assassin. Ultimately, Nikita was betrayed and her dreams shattered by the only people she thought she could trust. Now, after three years in hiding, Nikita is seeking retribution and making it clear to her former bosses that she will stop at nothing to expose and destroy their covert operation. For the time being, however, Division continues to recruit and train other young people and turning them into cold and efficient killers. One of these new recruits, Alex, is just beginning to understand what lies ahead for her and why the legendary Nikita made the desperate decision to run.", 'year': u'2010', 'imdb': u'tt1592154', 'provider': 'thetvdb', 'id': u'thetvdb:164301'}, {'name': u'La Femme Nikita', 'started': u'1997-01-13', 'overview': u"Based on the cult motion picture of the same name, the sexy, stylish spy series La Femme Nikita ran from 1997 to 2001 on the USA network. Starring Peta Wilson as Nikita, the series saw a young woman framed for murder and given a choice: be sentenced to life imprisonment, or work for Section One, a clandestine anti-terrorism organization. Nikita chose life, soon discovering that she is just the latest pawn in Section's games...", 'year': u'1997', 'imdb': u'tt0118379', 'provider': 'thetvdb', 'id': u'thetvdb:78527'}]}

    t0 = time.time()
    results = yield manager.tvdb.search(q.name)
    job.notify('Search results', 'Search took %.3fs' % (time.time() - t0))
    # JSONify the SearchResult objects
    dictlist = []
    for r in results:
        dictlist.append({
            'id': r.id,
            'name': r.name,
            'overview': r.overview,
            'year': r.year,
            'imdb': r.imdb,
            'provider': r.provider.NAME,
            'started': r.started
        })
    print(dictlist)
    yield {'results': dictlist}


@web.route('/api/shows/check', method='GET')
@webcoroutine(interval=500)
def show_check(job):
    manager = web.request['stagehand.manager']
    if web.request.query.id:
        only = [get_series_from_request(web.request.query.id)]
    else:
        only = []

    #yield kaa.delay(1);need={1:[1,2]}; found=[1,2]
    need, found = yield manager.check_new_episodes(only=only)
    yield {'need': sum(len(eps) for eps in need.values()), 'found': len(found)}


@web.route('/api/shows/<id>/episodes/<epcode>/status', method='POST')
def show_episodes_status(id, epcode):
    series = get_series_from_request(id)
    eps = [series.get_episode_by_code(code) for code in epcode.split(',')]
    if None in eps:
        raise web.HTTPError(404, 'Unknown episode for this show.')

    status_map = {
        'need': Episode.STATUS_NEED,
        'ignore': Episode.STATUS_IGNORE,
        'delete': Episode.STATUS_IGNORE
    }
    try:
        status_val = status_map[web.request.query.value]
    except KeyError:
        raise web.HTTPError(404, 'Invalid status code.')

    statuses = {}
    for ep in eps:
        if ep.status == Episode.STATUS_HAVE and web.request.query.value != 'delete':
            # Asked to ignore or retrieve an episode we already have.  Do nothing.
            pass
        else:
            ep.status = status_val
        statuses[ep.code] = episode_status_icon_info(ep)

    return {'statuses': statuses}


@web.route('/api/restart')
def restart():
    web.restart()
