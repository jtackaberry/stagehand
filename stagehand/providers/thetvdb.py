import logging
import urllib
import time
import os
import json
import asyncio
import itertools

from .base import ProviderBase, ProviderSearchResultBase, ProviderError
from ..toolbox import db
from ..toolbox.net import download
from ..toolbox.utils import tostr
from ..config import config

__all__ = ['Provider']

log = logging.getLogger('stagehand.providers.thetvdb')


class ProviderSearchResult(ProviderSearchResultBase):
    @property
    def pid(self):
        return str(self._attrs.get('id'))

    @property
    def name(self):
        return self._attrs.get('seriesName')

    @property
    def names(self):
        yield self.name

    @property
    def overview(self):
        return self._attrs.get('overview')

    @property
    def imdb(self):
        # Not available in search results
        return None

    @property
    def year(self):
        started = self.started
        if started and len(started.split('-')) == 3:
            return started.split('-')[0]
        else:
            return started

    @property
    def started(self):
        return self._attrs.get('firstAired')


    @property
    def banner(self):
        if 'banner' in self._attrs:
            return self.provider.hostname + '/banners/' + self._attrs['banner']



class Provider(ProviderBase):
    NAME = 'thetvdb'
    NAME_PRINTABLE = 'TheTVDB'
    IDATTR = 'thetvdbid'
    CACHEATTR = 'thetvdbcache'

    # It's actually 24 hours but we trim it a bit just to be safe.
    TOKEN_LIFETIME_SECONDS = 23 * 3600

    def __init__(self, db):
        super().__init__(db)
        self.hostname = 'https://www.thetvdb.com'
        self._apikey = '1E9534A23E6D7DC0'
        self._token = None
        self._token_time = 0

        db.register_object_type_attrs('series',
            thetvdbid = (str, db.ATTR_SEARCHABLE | db.ATTR_INDEXED),
            thetvdbcache = (dict, db.ATTR_SIMPLE)
        )

        db.register_object_type_attrs('episode',
            thetvdbid = (str, db.ATTR_SEARCHABLE),
        )


    @asyncio.coroutine
    def _rawapi(self, path, token=None, method='GET', body=None):
        url = 'https://api.thetvdb.com' + path
        headers = {
            'Accept': 'application/json',
            'Accept-Language' : config.misc.language.lower()
        }
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if body:
            headers['Content-Type'] = 'application/json'
        status, data = yield from download(url, retry=4, method=method, headers=headers, data=body)
        return status, json.loads(data.decode('utf8')) if data else None


    @asyncio.coroutine
    def _login(self):
        body = json.dumps({'apikey': self._apikey})
        status, response = yield from self._rawapi('/login', method='POST', body=body)
        if status == 200 and 'token' in response:
            return response['token']
        else:
            log.error('thetvdb login failed: %s', response)
            raise ProviderError('thetvdb API login failed')


    @asyncio.coroutine
    def _api(self, path, method='GET', body=None):
        """
        Invokes an API method, logging in or refreshing the token if necessary.
        """
        now = time.time()
        if not self._token or now - self._token_time > Provider.TOKEN_LIFETIME_SECONDS:
            # Acquire a new token
            self._token = yield from self._login()
            self._token_time = now

        status, response = yield from self._rawapi(path, self._token, method, body)
        log.debug('API %s returned status %d', path, status)
        if status == 401:
            if self._token_time == now:
                raise ProviderError('thetvDB API refused token')
            else:
                # Token was refused before expiry.  Clear token and recurse to cause relogin.
                self._token = None
                status, response = yield from self._api(path,method, body)
        elif status != 200:
            log.debug('API %s returned status %d', path, status)
        return status, response


    @asyncio.coroutine
    def search(self, name):
        results = []
        quoted = urllib.parse.quote(name.replace('-', ' ').replace('_', ' '))
        log.info('searching TheTVDB for %s', name)
        status, response = yield from self._api('/search/series?name=' + quoted)
        if status == 200:
            if 'data' not in response:
                log.warning('data element missing from response')
            else:
                for result in response['data']:
                    results.append(ProviderSearchResult(self, result))
        return results


    @asyncio.coroutine
    def get_series(self, id):
        log.debug('retrieving series data for %s', id)
        if not self.get_last_updated():
            # DB doesn't know about server time.  Set to current time so that
            # subsequent calls to get_changed_series_ids() have a reference
            # point.
            self.db.set_metadata('thetvdb::servertime', int(time.time()))

        series = {'episodes': []}
        log.info('fetching series %s from TheTVDB', id)
        status, response = yield from self._api('/series/' + id)
        if status != 200:
            return series
        elif 'data' not in response:
            log.warning('data element missing from response')
            return series

        data = response['data']

        try:
            series['runtime'] = int(data['runtime'])
        except (ValueError, KeyError):
            pass
        try:
            # XXX: is Airs_Time guaranteed to be well formatted?
            # Should we be more robust?
            timetuple = time.strptime(data.get('airsTime', ''), '%I:%M %p')
            series['airtime'] = tostr(time.strftime('%H:%M', timetuple))
        except ValueError:
            pass

        # Get any existing series and see if we need to fetch banner data.
        # TODO: use /series/{id}/images to pick the highest rated banner
        # and fetch the poster as well.
        existing = self.db.get_series_by_id('thetvdb:{}'.format(data['id']))
        missing = not existing or not existing.banner_data
        if missing and data.get('banner'):
            # Need to fetch banner, either because it changed (different banner with
            # a higher rating?) or because we never had one.
            url = self.hostname + '/banners/' + data['banner']
            log.debug('refresh series banner %s', url)
            status, banner_data = yield from download(url, retry=3)
            if status == 200:
                series['banner_data'] = banner_data
            else:
                log.error('banner download failed for series %s', data.get('seriesName', data['id']))

        from ..tvdb import Series
        status_str = data.get('status', '').lower()
        if status_str.startswith('cont'):  # continuing
            status = Series.STATUS_RUNNING
        elif status_str.startswith('on'):  # on hiaitus
            status = Series.STATUS_SUSPENDED
        elif status_str.startswith('end'):  # ended
            status = Series.STATUS_ENDED
        else:
            status = Series.STATUS_UNKNOWN

        series.update({
            'id': str(data['id']),
            'name': data.get('seriesName'),
            'poster': self.hostname + '/banners/' + data['poster'] if data.get('poster') else None,
            'banner': self.hostname + '/banners/' + data['banner'] if data.get('banner') else None,
            'overview': data.get('overview'),
            'genres': [g.strip().lower() for g in data.get('genres', []) if g],
            # TODO: do a sanity check on FirstAired format.
            'started': data.get('firstAired'),
            'status': status,
            'imdbid': data.get('imdbId')
        })

        # Iterate over all pages of episodes.
        for page in itertools.count(1):
            status, response = yield from self._api('/series/{}/episodes?page={}'.format(id, page))
            if status != 200:
                break
            elif 'data' not in response:
                log.warning('data element missing from episodes response')
                break

            for episode in response['data']:
                series['episodes'].append({
                    'id': str(episode['id']),
                    'name': episode.get('episodeName'),
                    'season': int(episode['airedSeason']),
                    'episode': int(episode['airedEpisodeNumber']),
                    # TODO: do a sanity check on FirstAired format.
                    'airdate': episode.get('firstAired'),
                    'overview': episode.get('overview')
                })

            if 'links' not in response or response['links'].get('last', page) == page:
                break
        return series


    @asyncio.coroutine
    def get_changed_series_ids(self):
        servertime = self.get_last_updated()
        if not servertime:
            # No servertime stored, so there must not be any series in db.
            return
        now = int(time.time())

        # Grab all series ids currently in the DB.
        series = set([o[self.IDATTR] for o in self.db.query(type='series', attrs=[self.IDATTR])])
        if now - servertime > 60*60*24*7:
            log.warning("haven't updated in over a week, returning all series")
            # Haven't updated in over a week (which is the upper bound for the API), so refresh all series.
            self.db.set_metadata('thetvdb::servertime', now)
            return list(series)

        ids = []
        status, response = yield from self._api('/updated/query?fromTime={}'.format(servertime))
        if status == 200:
            if 'data' not in response:
                log.warning('data element missing from response')
            elif response['data']:
                for result in response['data']:
                    ids.append(str(result['id']))
            self.db.set_metadata('thetvdb::servertime', now)
            log.debug('set servertime %s', now)
        return ids


    def get_last_updated(self):
        return int(self.db.get_metadata('thetvdb::servertime', 0))
