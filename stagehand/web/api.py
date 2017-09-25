import os
import time
import mimetypes
import time
import logging
import asyncio

from . import server as web
from .async import asyncweb, webcoroutine
from ..utils import episode_status_icon_info
from ..tvdb import Episode

log = logging.getLogger('stagehand.web.app')

def get_series_from_request(id):
    series = web.request['stagehand.manager'].tvdb.get_series_by_id(id)
    if not series:
        raise web.HTTPError(404, 'Invalid show.')
    return series


@web.put('/api/shows/<id>')
@webcoroutine()
def show_add(job, id):
    manager = web.request['stagehand.manager']
    job.notify('alert', title='Adding series', text='Retrieving episode information for this series ...')
    try:
        series = yield from manager.add_series(id)
    except Exception as e:
        log.exception('failed to add series')
        job.notify_after('alert', title='Failed to add series', text=str(e), timeout=2)
    else:
        job.notify_after('alert', title='Series added', text='Added series %s to database.' % series.name, timeout=2)
    return {}


@web.delete('/api/shows/<id>')
@webcoroutine()
def show_delete(job, id):
    manager = web.request['stagehand.manager']
    name = get_series_from_request(id).name
    manager.delete_series(id)
    # Notify the session about the removal, but do it via a timer so that
    # the notification happens on the next page load rather than in response
    # to the API call.
    job.notify_after('alert', title='Series Deleted', text='Series <b>%s</b> was removed from the database' % name)
    return {}


@web.get('/api/shows/<id>/banner', cache=3600*24*7)
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


@web.post('/api/shows/<id>/provider')
@webcoroutine()
def show_provider(job, id):
    series = get_series_from_request(id)
    provider = web.request.forms.get('provider')
    if provider:
        yield from series.change_provider(provider)
        series.cfg.provider = provider


@web.post('/api/shows/<id>/refresh')
@webcoroutine()
def show_refresh(job, id):
    series = get_series_from_request(id)
    yield from series.refresh()


@web.post('/api/shows/<id>/settings')
def show_settings(id):
    series = get_series_from_request(id)
    settings = web.request.forms
    series.cfg.quality = settings.quality
    series.cfg.path = settings.path
    series.cfg.search_string = settings.search_string
    series.cfg.language = settings.language
    #series.cfg.upgrade = True if settings['upgrade'] == 'true' else False
    series.cfg.paused = True if settings['paused'] == 'true' else False
    series.cfg.flat = True if settings['flat'] == 'true' else False
    series.cfg.identifier = settings.identifier

    # TODO: if pausing a series that has queued episodes, remove them and
    # notify user.


@web.get('/api/shows/<id>/overview')
def show_overview(id):
    series = get_series_from_request(id)
    return {'overview': series.overview}


@web.get('/api/shows/<id>/<code>/overview')
def show_episode_overview(id, code):
    series = get_series_from_request(id)
    ep = series.get_episode_by_code(code)
    return {'overview': ep.overview if ep else 'Episode not found'}


@web.get('/api/shows/search')
@webcoroutine(interval=500)
def show_search(job):
    q = web.request.query
    manager = web.request['stagehand.manager']

    t0 = time.time()
    results = yield from manager.tvdb.search(q.name)
    job.notify('alert', title='Search results', text='Search took %.3fs' % (time.time() - t0))
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
    return {'results': dictlist}


@web.get('/api/shows/check')
@webcoroutine(interval=500)
def show_check(job):
    manager = web.request['stagehand.manager']
    if web.request.query.id:
        only = [get_series_from_request(web.request.query.id)]
    else:
        only = []

    need, found = yield from manager.check_new_episodes(only=only)
    return {'need': sum(len(eps) for eps in need.values()), 'found': len(found)}


@web.post('/api/shows/<id>/episodes/<epcode>/status')
@webcoroutine()
def show_episodes_status(job, id, epcode):
    manager = web.request['stagehand.manager']
    series = get_series_from_request(id)
    eps = [series.get_episode_by_code(code) for code in epcode.split(',')]
    if None in eps:
        raise web.HTTPError(404, 'Unknown episode for this show.')

    action = web.request.query.value
    status_map = {
        'need': Episode.STATUS_NEED,
        'ignore': Episode.STATUS_IGNORE,
        'delete': Episode.STATUS_IGNORE
    }
    try:
        status_val = status_map[action]
    except KeyError:
        raise web.HTTPError(404, 'Invalid status code.')

    statuses = {}
    do_check_new_episodes = False
    for ep in eps:
        if ep.status == Episode.STATUS_HAVE and action != 'delete':
            # Asked to ignore or retrieve an episode we already have.  Do nothing.
            pass
        elif status_val == Episode.STATUS_NEED and ep.season.number == 0:
            # Special case: user scheduled a special episode for download.  Normally
            # a special episode set as STATUS_NEED is ignored.  So we set to NEED_FORCED
            # instead.
            ep.status = Episode.STATUS_NEED_FORCED
        else:
            ep.status = status_val
            # Clear any stored search result
            ep.search_result = None

        if ep.status == Episode.STATUS_IGNORE:
            manager.cancel_episode_retrieval(ep)
        elif (ep.status == Episode.STATUS_NEED and ep.aired) or ep.status == Episode.STATUS_NEED_FORCED:
            # Episode either forced or marked as needed and is aired.  Ask the manager to do a search.
            do_check_new_episodes = True
        if action == 'delete' and ep.filename and os.path.isfile(ep.path):
            try:
                os.unlink(ep.path)
            except OSError as e:
                job.notify('alert', title='Delete Episode', text='Failed to delete %s: %s' % (ep.path, e), type='error')
            else:
                job.notify('alert', title='Delete Episode', text='%s deleted' % ep.filename)
                ep.filename = None

        statuses[ep.code] = episode_status_icon_info(ep)

    if do_check_new_episodes:
        asyncio.async(manager.check_new_episodes(), loop=manager.loop)
    return {'statuses': statuses}


@web.get('/api/restart')
def restart():
    web.restart()


@web.get('/api/shutdown')
def shutdown():
    asyncio.get_event_loop().stop()

@web.get('/api/pid')
def pid():
    return {'pid': os.getpid()}