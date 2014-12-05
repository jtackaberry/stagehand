import logging
import urllib
import zipfile
import time
import os
import re

import asyncio

from ..toolbox.utils import tempfile
from .base import ProviderBase, ProviderSearchResultBase, ProviderError, parse_xml

__all__ = ['Provider']

log = logging.getLogger('stagehand.providers.tvrage')

def parse_server_time(t):
    months = 'jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'
    try:
        # Avoid strptime() because it uses locale for month abbreviations,
        # whereas the server is always English.
        month, day, year = t.lower().split('/')
        return '%s-%02d-%02d' % (year, months.index(month[:3]) + 1, int(day))
    except (ValueError, TypeError):
        # No valid airdate.
        return


class ProviderSearchResult(ProviderSearchResultBase):
    @property
    def pid(self):
        return self._attrs.get('showid')

    @property
    def name(self):
        return self._attrs.get('name')

    @property
    def overview(self):
        return self._attrs.get('summary')

    def imdb(self):
        return None

    @property
    def year(self):
        m = re.search(r'(\d{4})', self._attrs.get('started', ''))
        if m:
            return m.group(1)


    @property
    def started(self):
        return parse_server_time(self._attrs.get('started', ''))


    @property
    def banner(self):
        return None



class Provider(ProviderBase):
    NAME = 'tvrage'
    NAME_PRINTABLE = 'TVRage'
    IDATTR = 'tvrageid'
    CACHEATTR = 'tvragecache'

    def __init__(self, db):
        super(Provider, self).__init__(db)
        self.hostname = 'http://services.tvrage.com'

        db.register_object_type_attrs('series',
            tvrageid = (str, db.ATTR_SEARCHABLE | db.ATTR_INDEXED),
            tvragecache = (dict, db.ATTR_SIMPLE)
        )

        db.register_object_type_attrs('episode',
            tvrageid = (str, db.ATTR_SEARCHABLE | db.ATTR_INDEXED),
        )



    @asyncio.coroutine
    def search(self, name):
        results = []
        name = urllib.parse.quote(name.replace('.', ' ').replace('-', ' ').replace('_', ' '))
        url = self.hostname + '/feeds/full_search.php?show=%s' % name
        for tag, attrs, data in (yield from parse_xml(url)):
            if tag == 'show':
                results.append(ProviderSearchResult(self, data))
        return results


    @asyncio.coroutine
    def get_series(self, id):
        log.debug('retrieving series data for %s', id)
        if not self.get_last_updated():
            # DB doesn't know about server time.  Fetch and set, so that
            # subsequent calls to get_changed_series_ids() have a reference
            # point.
            self.db.set_metadata('tvrage::servertime', int(time.time()))

        series = {'id': None, 'episodes': []}
        season = 0
        url = self.hostname + '/feeds/full_show_info.php?sid=' + id
        log.debug2('fetching series data from %s', url)

        STAY_LOCAL = os.getenv('STAY_LOCAL', 0)
        if STAY_LOCAL:
            tmpname = tempfile('%s-tvrage.xml' % id)
            if os.path.exists(tmpname):
                url = tmpname

        eps = []
        # These tags we want to recurse into while walking the tree
        nested_tags = 'Episodelist', 'Season', 'episode', 'genres', 'genre'
        for tag, attrs, data in (yield from parse_xml(url, nest=nested_tags)):
            if tag == 'showid':
                series['id'] = data
            elif tag in ('name', 'airtime'):
                series[tag] = data
            elif tag == 'summary':
                series['overview'] = data
            elif tag == 'started':
                series['started'] = parse_server_time(data)
            elif tag == 'status':
                from ..tvdb import Series
                status = data.lower()
                # Returning Series, New Series, Final Season all indicate currently running
                if status.startswith('return') or status.startswith('new') or status.startswith('final'):
                    series['status'] = Series.STATUS_RUNNING
                elif 'ended' in status:
                    series['status'] = Series.STATUS_ENDED
                else:
                    series['status'] = Series.STATUS_UNKNOWN
            elif tag == 'runtime':
                try:
                    series['runtime'] = int(data)
                except Valueerror:
                    pass
            elif tag in ('classification', 'genre'):
                # Empty genres are parsed as empty dicts.
                if data:
                    series.setdefault('genres', []).append(data.strip().lower())
            elif tag == 'Season':
                season = int(attrs['no'])
                for ep in eps:
                    ep['season'] = season
                series['episodes'].extend(eps)
                eps = []
            elif tag == 'episode':
                # XXX: we try to construct a unique, unchanging id.  I've seen
                # duplicate epnum, but the last part of link seems unique.
                # Hopefully it is always present.  If it isn't, fall back to epnum,
                # which is very dangerous because if the link is ever added, it
                # will cause the uid to change.
                epid = (data['link'] or '').rsplit('/', 1)[-1] or data['epnum']
                eps.append({
                    'id': '%s-%s' % (series['id'], epid),
                    'name': data.get('title'),
                    'episode': int(data['seasonnum']),
                    'airdate': data.get('airdate'),
                    'overview': data.get('summary')
                })
        return series


    @asyncio.coroutine
    def get_changed_series_ids(self):
        servertime = self.get_last_updated()
        if not servertime:
            # No servertime stored, so there must not be any series in db.
            return
        ids = []

        # Grab all series ids currently in the DB.
        series = set([o[self.IDATTR] for o in self.db.query(type='series', attrs=[self.IDATTR])])

        url = self.hostname + '/feeds/last_updates.php?since=%s' % servertime
        # Fetch all updates since the last stored servertime
        for tag, attrs, data in (yield from parse_xml(url)):
            if tag == 'show' and data['id'] in series:
                ids.append(data['id'])

        # FIXME: use <updates at="1323307394" found="212" sorting="latest_updates" showing="Last 24H">
        now = int(time.time())
        self.db.set_metadata('tvrage::servertime', now)
        log.debug('set servertime %s', now)
        return ids


    def get_last_updated(self):
        return int(self.db.get_metadata('tvrage::servertime', 0))
