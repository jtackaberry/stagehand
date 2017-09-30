import logging
import urllib
import time
import os
import json
import asyncio

from .base import ProviderBase, ProviderSearchResultBase, ProviderError
from ..toolbox import db
from ..toolbox.net import download
from ..config import config

__all__ = ['Provider']

log = logging.getLogger('stagehand.providers.tvmaze')


class ProviderSearchResult(ProviderSearchResultBase):
    @property
    def pid(self):
        return str(self._attrs.get('id'))

    @property
    def name(self):
        return self._attrs.get('name')

    @property
    def names(self):
        yield self.name

    @property
    def overview(self):
        return self._attrs.get('summary')

    @property
    def imdb(self):
        return self._attrs['externals'].get('imdb')

    @property
    def year(self):
        started = self.premiered
        if started and len(started.split('-')) == 3:
            return started.split('-')[0]
        else:
            return started

    @property
    def started(self):
        return self._attrs.get('premiered')


    @property
    def banner(self):
        return None

    @property
    def poster(self):
        if 'image' in self._attrs:
            return self._attrs.get('original')


class Provider(ProviderBase):
    NAME = 'tvmaze'
    NAME_PRINTABLE = 'TVmaze'
    IDATTR = 'tvmazeid'
    CACHEATTR = 'tvmazecache'

    def __init__(self, db):
        super().__init__(db)
        self.hostname = 'http://api.tvmaze.com'

        db.register_object_type_attrs('series',
            tvmazeid = (str, db.ATTR_SEARCHABLE | db.ATTR_INDEXED),
            tvmazecache = (dict, db.ATTR_SIMPLE)
        )

        db.register_object_type_attrs('episode',
            tvmazeid = (str, db.ATTR_SEARCHABLE),
        )


    @asyncio.coroutine
    def _api(self, path):
        status, data = yield from download(self.hostname + path, retry=4)
        log.debug('API %s returned status %d', path, status)
        if status != 200:
            log.debug('API %s returned status %d', path, status)
        return status, json.loads(data.decode('utf8')) if data else None


    @asyncio.coroutine
    def search(self, name):
        results = []
        quoted = urllib.parse.quote(name.replace('-', ' ').replace('_', ' '))
        log.info('searching TVmaze for %s', name)
        status, response = yield from self._api('/search/shows?q=' + quoted)
        if status == 200:
            if not isinstance(response, list):
                log.warning('response malformed (expected list, got %s)', type(response))
            else:
                for result in response:
                    results.append(ProviderSearchResult(self, result['show']))
        return results


    @asyncio.coroutine
    def get_series(self, id):
        log.debug('retrieving series data for %s', id)
        if not self.get_last_updated():
            # DB doesn't know about server time.  Set to current time so that
            # subsequent calls to get_changed_series_ids() have a reference
            # point.
            self.db.set_metadata('tvmaze::servertime', int(time.time()))

        series = {'episodes': []}
        log.info('fetching series %s from TVmaze', id)
        status, response = yield from self._api('/shows/' + id)
        if status != 200:
            return series
        elif 'id' not in response:
            log.warning('id element missing from response')
            return series

        try:
            series['runtime'] = response['runtime']
        except KeyError:
            pass
        try:
            timetuple = time.strptime(response['schedule']['time'], '%I:%M %p')
            series['airtime'] = tostr(time.strftime('%H:%M', timetuple))
        except (KeyError, ValueError):
            pass

        # Get any existing series and see if we need to fetch banner data.
        # TODO: use /series/{id}/images to pick the highest rated banner
        # and fetch the poster as well.
        existing = self.db.get_series_by_id('tvmaze:{}'.format(response['id']))
        missing = not existing or not existing.banner_data
        try:
            image_url = response['image']['original']
        except KeyError:
            image_url = None
        else:
            if missing:
                log.debug('refresh series banner %s', image_url)
                status, banner_data = yield from download(image_url, retry=3)
                if status == 200:
                    series['banner_data'] = banner_data
                else:
                    log.error('banner download failed for series %s', response.get('name', response['id']))

        from ..tvdb import Series
        status_str = response.get('status', '').lower()
        if status_str.startswith('run'):
            status = Series.STATUS_RUNNING
        elif status_str.startswith('end'):
            status = Series.STATUS_ENDED
        else:
            status = Series.STATUS_UNKNOWN

        series.update({
            'id': str(response['id']),
            'name': response.get('name'),
            'poster': image_url,
            'overview': response.get('summary'),
            'genres': [g.strip().lower() for g in response.get('genres', []) if g],
            'started': response.get('premiered'),
            'status': status,
            'imdbid': response.get('externals', {}).get('imdb')
        })

        status, response = yield from self._api('/shows/{}/episodes'.format(id))
        if status == 200 and isinstance(response, list):
            for episode in response:
                series['episodes'].append({
                    'id': str(episode['id']),
                    'name': episode.get('name'),
                    'season': int(episode['season']),
                    'episode': int(episode['number']),
                    'airdate': episode.get('airdate'),
                    'overview': episode.get('summary')
                })
        else:
            log.warning('bad response for episodes list (status=%d type=%s)', status, type(response))

        return series


    @asyncio.coroutine
    def get_changed_series_ids(self):
        servertime = self.get_last_updated()
        if not servertime:
            # No servertime stored, so there must not be any series in db.
            return

        now = int(time.time())
        ids = []
        status, response = yield from self._api('/updates/shows')
        if status == 200 and isinstance(response, dict):
            for id, timestamp in response.items():
                if timestamp > servertime:
                    ids.append(id)
            self.db.set_metadata('tvmaze::servertime', now)
            log.debug('set servertime %s', now)
        else:
            log.warning('bad response for changed series (status=%d type=%s)', status, type(response))
        return ids


    def get_last_updated(self):
        return int(self.db.get_metadata('tvmaze::servertime', 0))
