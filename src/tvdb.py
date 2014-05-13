from __future__ import absolute_import
import os
import sys
import time
import logging
import re
import itertools
import difflib
from datetime import datetime, timedelta

# kaa imports
import kaa
import kaa.db
from kaa.strutils import UNICODE_TYPE

from .utils import fixsep, fixquotes, name_to_url_segment, remove_stop_words
from .config import config
from .providers import plugins, ProviderError

# get logging object
log = logging.getLogger('stagehand.tvdb')

# XXX: testing python based ObjectRow
#kaa.db.ObjectRow = kaa.db.PyObjectRow

class Episode(object):
    """
    Represents an episode in the database.

    An Episode object isn't necessarily tightly coupled to an episode name,
    or an episode code (e.g. s01e04), or an air date.  Different metadata
    providers may disagree on one or more of these attributes.

    A relationship is established between episode metadata across providers,
    and the Episode object provides the attributes of the currently preferred
    provider for the Series.  If the preferred provider is changed (using the
    Series.change_provider() method) then the attributes of this object for a
    given episode may change to reflect the relationship::

        >>> series = db.get_series_by_id('thetvdb:73244')
        >>> series.provider.NAME
        'thetvdb'
        >>> series.name
        u'The Office (US)'
        >>> ep = series.get_episode_by_code('s05e16')
        >>> ep.airdate, ep.code, ep.name
        (datetime.datetime(2009, 3, 5, 0, 0), 's05e16', u'Blood Drive')

    TheTVDB thinks s05e16 is called Blood Drive.  The episode object is tied to
    this episode in an abstract sense, but not to the attributes::

        >>> series.change_provider('tvrage')
        >>> ep.airdate, ep.code, ep.name
        (datetime.datetime(2009, 3, 5, 0, 0), 's05e18', u'Blood Drive')

    TVRage agrees with the name and air date, but notice the episode code
    changed.  It does think s05e16 is a different episode::

        >>> series.get_episode_by_code('s05e16').name
        u'Lecture Circuit (Part 1)'

    Change it back. Same episode object, but the attributes are different::

        >>> series.change_provider('thetvdb')
        >>> ep.airdate, ep.code, ep.name
        (datetime.datetime(2009, 3, 5, 0, 0), 's05e16', u'Blood Drive')
    """
    # Constants for episode status
    STATUS_NONE = 0
    STATUS_NEED = 1
    STATUS_HAVE = 2
    # Fetch even if air date is unknown or in the future.
    STATUS_NEED_FORCED = 3
    STATUS_IGNORE = 4

    def __init__(self, db, series, season, dbrow):
        self._dbrow = dbrow
        self._db = db
        self._version = db._version
        self.series = series
        self.season = season

    def __repr__(self):
        return '<%s %s %s at 0x%x>' % (self.__class__.__name__, self.series.name, self.code, id(self))


    def __eq__(self, other):
        if isinstance(other, Episode) and self._dbattr('id') == other._dbattr('id'):
            if self is not other:
                # XXX: temporary (debugging)
                # This can happen normally when the db gets updated from
                # providers and caches are invalidated.  It's not a bug.
                log.warning('episode %s has multiple instances', self)
            return True
        else:
            return False


    def __hash__(self):
        return hash((Episode, self._dbattr('id')))


    def _dbattr(self, attr):
        if self._version != self._db._version:
            # dbrow cache may be stale, refresh.
            self._dbrow = self._db.get(self._dbrow)
            self._version = self._db._version
        return self._dbrow[attr]


    def _update(self, **kwargs):
        self._db.update(self._dbrow, **kwargs)
        self._dbrow = self._db.get(self._dbrow)


    @property
    def providers(self):
        """
        A list of providers that have supplied metadata for this episode.

        The series preferred provider is the primary source for episide attributes.
        Other providers will be consulted only if the preferred provider lacks an
        attribute, or if the preferred provider doesn't know about this episode.
        """
        return [p for p in self._db.providers.values() if self._dbattr(p.IDATTR)]


    @property
    def id(self):
        return '%s:%s' % (self.series.provider.NAME, self.pid)


    @property
    def pid(self):
        return self._dbattr(self.series.provider.IDATTR)


    @property
    def ids(self):
        """
        All provider ids for this episode.
        """
        return ['%s:%s' % (p.NAME, self._dbattr(p.IDATTR)) for p in self.providers]

    @property
    def name(self):
        return self._dbattr('name')

    @property
    def code(self):
        return 's%02de%02d' % (self.season.number, self.number)

    @property
    def overview(self):
        return self._dbattr('overview')

    @property
    def number(self):
        return self._dbattr('episode')

    @property
    def airdate(self):
        airdate = self._dbattr('airdate')
        if airdate:
            try:
                return datetime.strptime(airdate, '%Y-%m-%d')
            except ValueError:
                pass

    @property
    def airdatetime(self):
        dt = self.airdate
        if self.series.airtime and dt:
            hour, minute = self.series.airtime.split(':')
            dt = dt.replace(hour=int(hour), minute=int(minute))
        return dt


    @property
    def status(self):
        """
        One of the STATUS_* constants.
        """
        return self._dbattr('status')

    @status.setter
    def status(self, value):
        self._update(status=value)


    @property
    def filename(self):
        return self._dbattr('filename')

    @filename.setter
    def filename(self, value):
        self._update(filename=value)


    @property
    def preferred_filename(self):
        # This is a filename, so convert unicode series and episode names to
        # bytes
        fmt = kaa.py3_b(config.naming.episode_format)
        dt = self.airdate
        if self.series.cfg.identifier == 'date' and dt:
            style = kaa.py3_b(config.naming.date_style)
            code = dt.strftime(style)
        else:
            style = kaa.py3_b(config.naming.code_style)
            code = style.format(season=self.season.number, episode=self.number)
        return fmt.format(show=kaa.py3_b(fixsep(self.series.name)), code=code, title=kaa.py3_b(fixsep(self.name)))

    @property
    def preferred_path(self):
        return os.path.join(self.season.path, self.preferred_filename)

    @property
    def path(self):
        """
        The actual full path to the file if it has been downloaded.

        If it hasn't been downloaded, None.
        """
        if self.filename:
            return os.path.join(self.season.path, self.filename)

    @property
    def search_result(self):
        return self._dbattr('search_result')

    @search_result.setter
    def search_result(self, value):
        self._update(search_result=value)

    @property
    def ready(self):
        """
        True if the episode is considered ready for download.

        It is ready if the episode has aired but hasn't aired so long ago that
        it's considered obsolete, or if the user has forced the status to need.

        Episodes in "season 0" are not considered ready unless explicitly set
        to STATUS_NEED_FORCED.
        """
        if self.status == self.STATUS_NEED_FORCED:
            return True
        elif self.airdate and self.status in (self.STATUS_NEED, self.STATUS_NONE):
            return self.aired and not self.obsolete and self.season.number != 0
        else:
            return False


    @property
    def aired(self):
        """
        True if the episode has aired.
        """
        # TODO: timezone
        airdatetime = self.airdatetime
        if airdatetime:
            return datetime.now() >= airdatetime + timedelta(minutes=self.series.runtime)
        else:
            return False


    @property
    def obsolete(self):
        # Check if we're past the air date but the air date is within two
        # weeks.  FIXME: two weeks should be a global configurable.
        if self.status not in (self.STATUS_NONE, self.STATUS_IGNORE) or not self.airdate:
            return False
        return self.is_older_than(14)


    def is_older_than(self, days):
        """
        True if the episode is considered obsolete, meaning it has aired sufficiently
        long ago that we're not interested in it.
        """
        # TODO: timezone
        airdatetime = self.airdatetime
        cutoff = datetime.now() - timedelta(days=days)
        return airdatetime and datetime.now() >= airdatetime and self.airdatetime <= cutoff


    def get_id_for_provider(self, name):
        for p in self.providers:
            if p.NAME == name:
                return self._dbattr(p.IDATTR)
        else:
            raise ValueError('invalid provider given')



class Season(object):
    """
    Represents a season for a specific series.

    This doesn't correspond to any database object, but provides a convenient
    interface to select a subset of episodes.
    """
    def __init__(self, db, series, season):
        self._episode_cache = []
        self._episode_cache_ver = None
        self._db = db
        self.series = series
        self.number = season

    def __repr__(self):
        return '<%s %s s%02d at 0x%x>' % (self.__class__.__name__, self.series.name, self.number, id(self))


    def __eq__(self, other):
        if isinstance(other, Season) and self.series == other.series and self.number == other.number:
            if self is not other:
                # XXX: temporary (debugging)
                log.warning('season %s has multiple instances', self)
            return True
        else:
            return False


    def __hash__(self):
        return hash((Season, self.series._dbattr('id')), self.number)


    @property
    def path(self):
        if not self.series.cfg.flat:
            season_dir = kaa.py3_b(config.naming.season_dir_format).format(season=self.number)
            return os.path.join(self.series.path, season_dir)
        else:
            return self.series.path


    @property
    def episodes(self):
        """
        A list of all episodes as Episode objects for this season.  The list is
        in order, such that the list index corresponds with the episode number,
        starting at 0 (i.e. episodes[0] is episode 1).
        """
        if self._episode_cache_ver == self._db._version:
            return self._episode_cache

        # Get only rows from the preferred provider (provider id is not null) except
        # for season 0 (specials), where we take all episodes.
        if self.number > 0:
            idmap = {self.series.provider.IDATTR: kaa.db.QExpr('>', u'')}
        else:
            idmap = {}
        dbrows = self._db.query(type='episode', parent=self.series._dbrow, season=self.number, **idmap)
        self._episode_cache = [Episode(self._db, self.series, self, dbrow) for dbrow in dbrows]
        self._episode_cache.sort(key=lambda e: e.number)
        self._episode_cache_ver = self._db._version
        return self._episode_cache



class Series(object):
    """
    Represents a series in the database.

    Multiple instances of a Series object may exist for the same database
    record, but in practice as long as you get Series instances via the TVDB
    API, you should always get back the same Series object for a given series.
    """
    # Constants for conflict attribute
    # There are no conflicts for this series.
    CONFLICT_NONE = 0
    # There is a conflict between at least two providers which the user has
    # not acknowledged.
    CONFLICT_UNACKED = 0
    # There is a conflict but the user has acknowledged.
    CONFLICT_ACKED = 1

    # Constants for status attribute
    STATUS_UNKNOWN = 0
    STATUS_RUNNING = 1
    STATUS_SUSPENDED = 1
    STATUS_ENDED = 2

    def __init__(self, db, dbrow):
        self._db = db
        self._dbrow = dbrow
        self._version = db._version
        self._cfg = None
        self._season_cache = []
        self._season_cache_ver = None

    def __repr__(self):
        return '<%s %s at 0x%x>' % (self.__class__.__name__, self.name, id(self))


    def __eq__(self, other):
        if isinstance(other, Series) and self._dbattr('id') == other._dbattr('id'):
            if self is not other:
                # XXX: temporary (debugging)
                log.warning('series %s has multiple instances', self)
            return True
        else:
            return False


    def __hash__(self):
        return hash((Series, self._dbattr('id')))


    def _dbattr(self, attr):
        if self._version != self._db._version:
            # dbrow cache may be stale, refresh.
            self._dbrow = self._db.get(self._dbrow)
            self._version = self._db._version
        return self._dbrow[attr]

    def _update(self, **kwargs):
        self._db.update(self._dbrow, **kwargs)
        self._dbrow = self._db.get(self._dbrow)

    def get_conflicts(self):
        pseries = dict((p, self._dbattr(p.CACHEATTR)) for p in self.providers if self._dbattr(p.CACHEATTR))
        return self._db._get_conflicts(pseries)

    @kaa.coroutine()
    def change_provider(self, provider):
        old = self.provider
        try:
            new = self._db.providers[provider] if isinstance(provider, basestring) else provider
        except KeyError:
            raise ValueError('%s is not a known provider' % provider)
        if new not in self.providers:
            raise ValueError('%s is not currently a provider for this series' % new.NAME)
        elif old != new:
            log.info('changing provider for %s (%s) from %s to %s', self.id, self.name, old.NAME, new.NAME)
            yield self._db._update_series(old, self._dbattr(old.IDATTR), dirty=[], preferred=new, fast=True)


    @kaa.coroutine()
    def refresh(self):
        """
        Refreshes metadata from the series providers.
        """
        yield self._db._update_series(self.provider, self._dbattr(self.provider.IDATTR), dirty=self.providers)


    @property
    def providers(self):
        """
        A list of providers that have supplied metadata for this series.
        """
        return [p for p in self._db.providers.values() if self._dbattr(p.IDATTR)]

    @property
    def provider(self):
        """
        The preferred provider for this series.

        Episodes that indicate multiple providers will have their attributes
        from this provider as a primary source, using other providers only
        if the preferred provider did not supply the attribute.
        """
        return self._db.providers[self._dbattr('provider')]

    @property
    def id(self):
        return '%s:%s' % (self.provider.NAME, self.pid)

    @property
    def pid(self):
        return self._dbattr(self.provider.IDATTR)

    @property
    def ids(self):
        """
        All provider ids for this series.
        """
        return ['%s:%s' % (p.NAME, self._dbattr(p.IDATTR)) for p in self.providers]

    @property
    def conflict(self):
        return self._dbattr('conflict')

    @conflict.setter
    def conflict(self, value):
        self._update(conflict=value)

    @property
    def conflict_info(self):
        return self._dbattr('conflict_info')

    @property
    def path(self):
        dir = self.cfg.path or kaa.py3_b(fixsep(self.name))
        if dir.startswith('/'):
            return dir
        else:
            return os.path.join(os.path.expanduser(config.misc.tvdir), dir)

    @property
    def cfg(self):
        if not self._cfg:
            self._cfg = self._db.get_config_for_series(self.id)
            if not self._cfg:
                raise AttributeError('Configuration for series id %s not found' % self.id)
        return self._cfg


    @property
    def name(self):
        return self._dbattr('name')

    @property
    def name_as_url_segment(self):
        return name_to_url_segment(self.name)


    @property
    def imdbid(self):
        return self._dbattr('imdbid')

    @property
    def status(self):
        return self._dbattr('status')

    @property
    def overview(self):
        return self._dbattr('overview')

    @property
    def runtime(self):
        return self._dbattr('runtime')

    @property
    def airtime(self):
        return self._dbattr('airtime')

    @property
    def banner(self):
        return self._dbattr('banner')

    @property
    def banner_data(self):
        return self._dbattr('banner_data')


    @property
    def poster(self):
        return self._dbattr('poster')

    @property
    def poster_data(self):
        return self._dbattr('poster_data')


    @property
    def genres(self):
        return self._dbattr('genres') or []


    @property
    def seasons(self):
        """
        A list of all seasons as Season objects for this series.  The list is
        in order, such that the list index corresponds with the series number,
        starting at 0.  Note that some series may have a "season 0" (usually
        comprised of special episodes) which would be at index 0.
        """
        if self._season_cache_ver == self._db._version:
            return self._season_cache

        # Find out how many seasons in this series by fetching the highest season.
        seasons = self._db.query(type='episode', parent=self._dbrow, attrs=['season'], distinct=True)
        self._season_cache = [Season(self._db, self, row['season']) for row in seasons]
        self._season_cache_ver = self._db._version
        return self._season_cache


    @property
    def episodes(self):
        """
        A list of episodes for all episodes in this series, for all seasons.
        """
        episodes = []
        for season in self.seasons:
            episodes.extend(season.episodes)
        return episodes


    def get_episode_by_code(self, code):
        if 'x' in code:
            # Convert NxM -> sNNeMM
            s, ep = code.split('x')
            code = 's%02de%02d' % (int(s), int(ep))
        for ep in self.episodes:
            if ep.code == code:
                return ep


    def get_id_for_provider(self, name):
        for p in self.providers:
            if p.NAME == name:
                return self._dbattr(p.IDATTR)
        else:
            raise ValueError('invalid provider given')


    def has_genre(self, *genres, **kwargs):
        startswith = kwargs.get('startswith', True)
        all = kwargs.get('all', False)
        matches = []
        for requested in genres:
            requested = requested.lower()
            for genre in self.genres:
                if startswith and genre.startswith(requested) or genre == requested:
                    matches.append(requested)
        return bool((all and len(matches) == len(genres)) or matches)


class SearchResult(object):
    def __init__(self, db, attrs):
        self._db = db
        self._attrs = attrs

    @property
    def id(self):
        return self._attrs.get('seriesid')

    @property
    def name(self):
        return self._attrs.get('SeriesName')

    @property
    def overview(self):
        return self._attrs.get('Overview')

    @property
    def imdb(self):
        return self._attrs.get('IMDB_ID')

    @property
    def year(self):
        airdate = self._attrs.get('FirstAired')
        if airdate and len(airdate.split('-')) == 3:
            return airdate.split('-')[0]
        else:
            return airdate

    @property
    def started(self):
        return self._attrs.get('FirstAired')


    @property
    def banner(self):
        if 'banner' in self._attrs:
            return self._db.hostname + '/banners/' + self._attrs['banner']



class TVDB(kaa.db.Database):
    """
    Database object for TV series and episodes.

    All provider plugins are consulted for series metadata.  Each series
    object has a preferred provider that takes precedence for episode
    numbering.
    """
    def __init__(self, dbfile):
        super(TVDB, self).__init__(dbfile)
        self._version = 0
        self._series_cache = {}
        self._series_cache_list = []
        self._series_cache_ver = None
        # A list of series ids to ignore in the database.
        self._series_ignore = []

        self.register_inverted_index('keywords', min=2, max=40)
        self.register_inverted_index('genres')
        # TODO: miniseries status
        self.register_object_type_attrs('series',
            provider = (UNICODE_TYPE, kaa.db.ATTR_SEARCHABLE),
            conflict = (int, kaa.db.ATTR_SEARCHABLE),
            conflict_info = (dict, kaa.db.ATTR_SIMPLE),
            imdbid = (UNICODE_TYPE, kaa.db.ATTR_SEARCHABLE),
            name = (UNICODE_TYPE, kaa.db.ATTR_SEARCHABLE | kaa.db.ATTR_INVERTED_INDEX, 'keywords'),
            status = (int, kaa.db.ATTR_SEARCHABLE),
            started = (UNICODE_TYPE, kaa.db.ATTR_SEARCHABLE),  # YYYY-MM-DD
            runtime = (int, kaa.db.ATTR_SEARCHABLE),
            airtime = (UNICODE_TYPE, kaa.db.ATTR_SEARCHABLE),
            overview = (UNICODE_TYPE, kaa.db.ATTR_SEARCHABLE | kaa.db.ATTR_INVERTED_INDEX, 'keywords'),
            banner = (UNICODE_TYPE, kaa.db.ATTR_SEARCHABLE),
            banner_data = (kaa.db.RAW_TYPE, kaa.db.ATTR_SIMPLE),
            poster = (UNICODE_TYPE, kaa.db.ATTR_SEARCHABLE),
            poster_data = (kaa.db.RAW_TYPE, kaa.db.ATTR_SIMPLE),
            genres = (list, kaa.db.ATTR_SIMPLE | kaa.db.ATTR_INVERTED_INDEX, 'genres')
        )

        self.register_object_type_attrs('episode',
            name = (UNICODE_TYPE, kaa.db.ATTR_SEARCHABLE | kaa.db.ATTR_INVERTED_INDEX, 'keywords'),
            overview = (UNICODE_TYPE, kaa.db.ATTR_SEARCHABLE | kaa.db.ATTR_INVERTED_INDEX, 'keywords'),
            season = (int, kaa.db.ATTR_SEARCHABLE),
            episode = (int, kaa.db.ATTR_SEARCHABLE),
            airdate = (UNICODE_TYPE, kaa.db.ATTR_SEARCHABLE),
            status = (int, kaa.db.ATTR_SEARCHABLE),
            filename = (str, kaa.db.ATTR_SEARCHABLE),
            search_result = (SearchResult, kaa.db.ATTR_SIMPLE),
            # A list of SearchResults that have been blacklisted (user decided
            # the result was not appropriate for this episode).
            blacklist = (list, kaa.db.ATTR_SIMPLE),
            keywords = (list, kaa.db.ATTR_SIMPLE | kaa.db.ATTR_INVERTED_INDEX, 'keywords')
        )

        self.lazy_commit = 5
        self.providers = dict((name, p.Provider(self)) for name, p in plugins.items())
        if not self.providers:
            raise RuntimeError('No TV metadata providers initialized successfully')
        #log.info('Using kaa.db.ObjectRow %s', kaa.db.ObjectRow)


    def _parse_id(self, id):
        """
        Returns (provider, id) for the given id
        """
        try:
            name, pid = id.split(':', 1)
        except ValueError:
            raise ValueError('id %s is not in the form provider:pid' % id)
        if name not in self.providers:
            raise ValueError('no such provider "%s"' % name)
        return self.providers[name], kaa.py3_str(pid)


    def _build_series_cache(self):
        if self._series_cache_ver == self._version:
            return
        cache = {}
        for data in self.query(type='series'):
            # Is this already in the series cache?
            series = self._series_cache.get(data['id']) if self._series_cache else None
            if not series:
                series = Series(self, data)
            # See if any of the pids for this series are in the ignore list.
            for id in series.ids:
                if id in self._series_ignore:
                    break
            else:
                # If we're here, the series is not in the ignore list, so add all
                # known ids to the series cache.
                for id in series.ids:
                    cache[id] = series

        self._series_cache = cache
        self._series_cache_ver = self._version
        self._series_cache_list = list(set(cache.values()))


    @kaa.coroutine()
    def _invoke_providers(self, method, *args, **kwargs):
        """
        Invokes a method on all providers simultaenously, waits until all
        are finished, and returns a list of 2-tuples (provider, results).

        The 'which' kwarg is a list of providers to invoke the method on.
        If which is a dict, then the key is the provider and the value is
        the args to pass to the provider.

        If not specified, the method will invoke on all providers.
        """
        which = kwargs.pop('which', self.providers.values())
        bubble = kwargs.pop('bubble', False)
        if isinstance(which, dict):
            ips = [getattr(p, method)(*args) for p, args in which.items()]
            which = which.keys()
        else:
            ips = [getattr(p, method)(*args, **kwargs) for p in which]
        yield kaa.InProgressAll(*ips)
        results = []
        for provider, ip in zip(which, ips):
            # Provider method could have raised an asynchronous exception,
            # in which case accessing ip.result will raise.  If bubble is False
            # (default) then we catch it and log it rather than letting it
            # bubble up.
            try:
                results.append((provider, ip.result))
            except Exception as e:
                if bubble:
                    raise
                log.exception('%s from provider %s failed', method, provider.NAME)
        yield results


    def _add_or_update(self, type, idmap, addattrs, **kwargs):
        kwargs.update(idmap)
        obj = self.query_one(type=type, parent=kwargs.get('parent'), orattrs=idmap.keys(), **idmap)
        if obj:
            self.update(obj, **kwargs)
        else:
            kwargs.update(addattrs)
            obj = self.add(type, parent=kwargs.pop('parent', None), **kwargs)
        return obj


    def _normalize_name(self, name, aggressive=False):
        """
        Given a name, remove stop words and non-word characters to construct a
        normalized string that represents an episode or series name.
        """
        stopwords = 'the', 'a'
        if aggressive:
            # Remove anything in brackets.
            name = re.sub(r'\([^)]+\)', '', name)
            # Some shows have a "with Firstname Lastname" suffix, like "The Daily Show
            # with Jon Stewart".  Strip this out.
            # FIXME: hardcoded English
            name = re.sub(r'with +\w+ +\w+\b', '', name)

        # Replace & with 'and' and remove other non-word characters
        name = re.sub(r'\W', ' ', name.replace('&', 'and').replace('.', '').lower())
        # Remove stop words and remove whitespace.
        return remove_stop_words(name).replace(' ', '')


    def _get_conflicts(self, pseries):
        # Abandon all hope, ye who enter here.
        #
        # This is a horrifyingly obtuse function that tries to solve a sticky problem.
        # There are six stages:
        #
        #   1. Group episodes by normalized name, and group again by air date.
        #   2. Move to conflicts any episodes with the same name and epcode but different
        #      air dates.
        #   3. Any remaining episodes with the same air date are moved to matches if their
        #      names match fuzzily, and to conflicts if they don't.  Episodes on a given
        #      air date without episodes from other providers on that date are moved to
        #      unmatched.
        #   4. Go through the unmatched list and try to match up the unmatched episodes
        #      from step 3 by name.
        #   5. Go through the matches list and move to conflicts any previously matched
        #      episodes that disagree on episode code.
        #   6. Finally, walk through the conflicts list try to find matches using a much
        #      fuzzier name search for episodes that otherwise agree on air date and
        #      episode code.  Remaining single episodes that are left in the conflicts
        #      list are moved to unmatched.
        #
        # This is probably solvable with fewer and/or simpler steps, but for now this
        # seems to work well on real world data.

        conflicts = {} # (name, airdate) -> [(provider, epdict), ...]
        matches = {}   # (name, airdate) -> [(provider, epdict), ...]
        unmatched = [] # [(provider, normalized name, epdict), ...]
        names = {}     # normalized name -> [(provider, epdict), ...]
        dates = {}     # air date -> [(provider, normalized name, epdict), ...]

        # Step 1: map all episodes by normalized name (names dict) and air dates (dates dict)
        for provider, series in pseries.items():
            for ep in series['episodes']:
                if ep['name']:
                    nn = self._normalize_name(ep['name'])
                    names.setdefault(nn, []).append((provider, ep))
                    dates.setdefault(ep['airdate'], []).append((provider, nn, ep))

        # Step 2: handle the case where the episodes with the same name and
        # episode code have different air dates.  We want to handle that now
        # because the search by air date later could categorize one of the
        # episodes in unmatched, and others in conflicts.
        for nn, episodes in names.items():
            codes = set((ep['season'], ep['episode']) for provider, ep in episodes)
            airdates = set(ep['airdate'] for provider, ep in episodes)
            if len(codes) == 1:
                if len(airdates) == 2 and None in airdates:
                    # This isn't a real conflict: there is only one actual air date
                    # but one or more episodes have no air date yet.  Episode codes
                    # agree, so let's add this to matches.
                    matches.setdefault((nn, None), []).extend(episodes)
                elif len(airdates) > 1:
                    # All episodes for this name agree on epcode but not on air
                    # date: move to conflict dict with airdate=None.
                    conflicts.setdefault((nn, None), []).extend(episodes)

        # Step 3: remaining episodes with the same air date are matched if their
        # names match fuzzily, and to conflicts if they don't.  Episodes on a given
        # air date without episodes from other providers on that date are moved to
        # unmatched.
        for airdate, eps in dates.items():
            for provider, nn, ep in eps:
                if (provider, ep) in conflicts.get((nn, None), ()) or (provider, ep) in matches.get((nn, None), ()):
                    # This episode is already in the conflict (or match) list due to
                    # different air date.
                    continue
                # List of normalized names for all other episodes in other providers
                nnames = [xnn for xprv, xnn, xep in eps if xprv != provider and ep != xep]
                if not nnames:
                    # Well, there _are_ no other episodes from other providers, so this is unmatched.
                    unmatched.append((provider, nn, ep))
                    continue

                matched_nn = difflib.get_close_matches(nn, nnames, 10, 0.8)
                if matched_nn:
                    # Fuzzy name match for an episode from another provider on
                    # the same air date.  Since it's a fuzzy match, the matched
                    # name could be different than the current normalized name.
                    # We need to group these together, so always pick the
                    # lowest ordered (lexically) nn.
                    nn = min(matched_nn[0], nn)
                    matches.setdefault((nn, ep['airdate']), []).append((provider, ep))
                else:
                    # There are episodes from other providers with this air date,
                    # but none match this name.  So add this to the conflict list.
                    conflicts.setdefault((None, ep['airdate']), []).append((provider, ep))


        # Step 4: try to match any unmatched episodes (due to different air dates) by name.
        for a_provider, nn, a_ep in unmatched[:]:
            for b_provider, b_ep in names[nn]:
                if a_provider == b_provider:
                    # Another episode from the same provider, not relevant.
                    continue
                # TODO: fuzzy date match (date differences are within say 6 days)
                a_air, b_air = a_ep['airdate'], b_ep['airdate']
                if not a_air or not b_air or a_air[:4] == b_air[:4]:
                    # One (or both) of the episodes have no air date, so we permit
                    # this match by name.  Or, the year is the same.
                    unmatched.remove((a_provider, nn, a_ep))
                    date = a_air[:4] if a_air else (a_air or b_air)
                    matches.setdefault((nn, date), []).append((a_provider, a_ep))
                    # Managed to match it, so stop looping over names[nn] list.
                    break


        # Step 5: now go through matches (which up to this point match on air date and
        # name), check season/episode numbers, and move to conflicts any
        # disagreements.
        for (nn, airdate), episodes in matches.items():
            codes = set((ep['season'], ep['episode']) for provider, ep in episodes)
            if len(codes) > 1:
                conflicts.setdefault((nn, airdate), []).extend(matches.pop((nn, airdate)))


        # Step 6: final pass over conflicts list (removing episodes that aren't
        # conflicting after all) and count the number of remaining conflicts
        # that are of seasons other than 0 (which are considered special
        # features).
        n_real_conflicts = 0
        for (nn, airdate), episodes in conflicts.items():
            if nn is None:
                # Air date for these episodes matched but name didn't.  Group all episodes
                # for the same code ...
                codes = {}
                for provider, ep in episodes:
                    codes.setdefault((ep['season'], ep['episode']), []).append((provider, ep))
                # ... and do a much fuzzier name match for these episodes before we declare them to
                # be a conflict.  Essentially if the names are remotely similar we probably want to
                # consider them a match because the other important attributes (air date and episode
                # code) are the same.
                for epcode, eps_for_code in codes.items():
                    if len(eps_for_code) <= 1:
                        # Only one episode for this code, so no hope of a match.
                        continue
                    # List of aggressively normalized names for all episodes in this group
                    nnames = [self._normalize_name(ep['name'], aggressive=True) for provider, ep in eps_for_code]

                    # Allow unnamed episodes ("TBA" or "Season 1, Episode 1") to match other unnamed
                    # episodes or the real named episode in this group.  XXX: this is really kludgy,
                    # even by my standards. :(
                    #
                    # Get a list of non-unnamed episodes, or just use 'tba' if there are none.
                    real = [x for x in nnames if x != 'tba' and not re.match(r'season\d+episode\d+', x)] or ['tba']
                    # Replace unnamed episode names with the first real named episode.
                    for idx in range(len(nnames)):
                        if nnames[idx] == 'tba' or re.match(r'season\d+episode\d+', nnames[idx]):
                            nnames[idx] = real[0]

                    for idx, (provider, ep) in enumerate(eps_for_code[:]):
                        matched_nn = difflib.get_close_matches(nnames[idx], nnames[:idx] + nnames[idx+1:], 10, 0.7)
                        if matched_nn:
                            # Fuzzy match, pick the smallest of all matches.  We can remove this episode
                            # from conflicts now.
                            matched_nn = min(nnames[idx], *matched_nn)
                            # XXX: add epcode to force differeny key (kludge, find a cleaner way)
                            matches.setdefault((matched_nn, airdate + str(epcode)), []).append((provider, ep))
                            episodes.remove((provider, ep))
                if not episodes:
                    # All conflicts removed.
                    del conflicts[(nn, airdate)]
                    continue

            # Not elif because the above conditional block may have paired down
            # a number of conflicts to 1.
            if len(episodes) == 1 or len(set(provider for provider, ep in episodes)) == 1:
                # Only one provider for this episode or all remaining episodes
                # part of the same provider, so obviously not a conflict.  Move
                # to unmatched.
                for provider, ep in episodes:
                    unmatched.append((provider, nn, ep))
                del conflicts[(nn, airdate)]
            elif airdate and set(ep['season'] for provider, ep in episodes) != set([0]):
                # At least one of the episodes isn't season 0, so this is a conflict.
                # We also require airdate to be set, because if it's None, this is
                # an air date conflict but the name and epcode matches, so it's not
                # a serious conflict (not for our purposes anyway).
                n_real_conflicts += 1

        #return n_real_conflicts, conflicts, matches, unmatched

        #for (nn, airdate), episodes in sorted(matches.items(), key=lambda x:x[0][1]):
        #    for provider, ep in episodes:
        #        print '  +', provider.NAME, ep['airdate'], ep['season'], ep['episode'], ep['name']
        """
        for (nn, airdate), episodes in sorted(matches.items(), key=lambda x:x[0][1]):
            if len(episodes) > len(self.providers):
                print airdate, nn, len(episodes)
                for provider, ep in episodes:
                    print '  +', provider.NAME, ep['airdate'], ep['season'], ep['episode'], ep['name']

        for (nn, airdate), episodes in sorted(conflicts.items(), key=lambda x:x[0][1]):
            print airdate, nn, len(episodes)
            for provider, ep in episodes:
                print '  *', provider.NAME, ep['airdate'], ep['season'], ep['episode'], ep['name']

        for (provider, nn, ep) in sorted(unmatched, key=lambda x: x[2]['airdate']):
            print '- ', provider.NAME, ep['season'], ep['episode'], ep['airdate'], ep['name']
        """

        n_episodes = max(len(series['episodes']) for series in pseries.values())
        # XXX: pay attention to unmatched too, if the # episodes is high it suggests we
        # are not comparing the same series.
        categorized = sum(len(eplist) for eplist in matches.values() + conflicts.values()) + len(unmatched)
        # Raw count ignore eps with no name
        raw = sum(1 for series in pseries.values() for ep in series['episodes'] if ep['name'])
        print('n_real_conflicts=%d matches=%d conflicts=%d unmatched=%d max=%d categorized=%d raw=%d' % (n_real_conflicts, len(matches), len(conflicts), len(unmatched), n_episodes, categorized, raw))
        if categorized != raw:
            alleps = list(itertools.chain(*[series['episodes'] for series in pseries.values()]))
            print('************* ERROR!  _get_conflicts() lost episiodes', len(alleps))
            for eplist in matches.values() + conflicts.values():
                for p, ep in eplist:
                    try:
                        alleps.remove(ep)
                    except ValueError:
                        print('[NOT] MISSING', ep)
            for p, nn, ep in unmatched:
                alleps.remove(ep)
            for ep in alleps:
                print('MISSING', ep)


        return n_real_conflicts, conflicts, matches, unmatched


    @kaa.coroutine(policy=kaa.POLICY_SYNCHRONIZED)
    def _update_series(self, provider, id, dirty=[], preferred=None, fast=False, completed=None):
        """
        Add or update a series with the local database, retrieving metadata from
        one or more providers as needed.

        :param provider: the provider object the id applies to
        :param id: the unique series id for the provider
        :param dirty: a list of provider objects known to have changes relative to the
                      local cached copy in the database.  (This list is ignored if
                      the series is not yet in the database.)
        :param preferred: the provider object which should be considered the authoritative
                          source of episode data for this series
        :param fast: if True, return as soon as series data has been retrieved from
                     the given provider (if necessary), and the other providers
                     will be contacted in the background; if False, all providers
                     will be reached before returning.
        :param completed: a kaa.Signal object that will be emitted once finished
                          (this is mainly used internally for fast=True)
        :returns: None if the series is fully updated, or a kaa.Signal object if
                  fast=True and there is more work to do. The signal will be
                  emitted once all work is complete.
        """
        log.debug('updating series %s:%s (fast=%s)', provider.NAME, id, fast)
        assert(not isinstance(provider, basestring) and not isinstance(preferred, basestring))

        # Series dicts from providers, populated later.
        pseries = {}    # provider -> series dict
        # These are the provider ids we need to pull from the server
        #pids = {provider: unicode(id)} if provider in dirty else {}
        pids = {}       # provider -> series id
        # These are the providers for which we don't know the series id and
        # will need to do search by name.  Start off with all providers other
        # than the one given (obviously we know that id); we'll remove from the
        # set as we add to the pids dict later.
        missing = set(p for p in self.providers.values() if p != provider)
        # Does this series already exist?
        existing = self.query_one(type='series', **{provider.IDATTR: kaa.py3_str(id)})
        if existing:
            # FIXME: no, get this from config now.
            preferred = preferred or self.providers.get(existing['provider'], provider)
            for p in self.providers.values():
                if not existing[p.IDATTR]:
                    continue
                if p in missing:
                    missing.remove(p)
                if not existing[p.CACHEATTR] or p in dirty:
                    # We have no cached series dict for this provider or it's dirty,
                    # but fortunately we do already know the provider id for it.
                    pids[p] = existing[p.IDATTR]
                else:
                    # We have a cached series dict and it's not dirty, so we
                    # can use it.
                    pseries[p] = existing[p.CACHEATTR]
        else:
            # No existing object, so even if this provider isn't in the dirty
            # list, it's the only id we have, and so we must fetch it.
            pids[provider] = kaa.py3_str(id)
            # Assume the given provider is the preferred one
            # FIXME: no, get this from config now.
            preferred = preferred or provider

        if fast:
            print('FAST', pids, dirty, missing)
            if provider not in pseries:
                # Need to fetch series data for this provider.  This happens when
                # there is no existing object, no cache for the provider, or the
                # provider is in the dirty list.
                pseries[provider] = yield provider.get_series(id)
                del pids[provider]
                if provider in dirty:
                    dirty.remove(provider)
            # Update the DB now with this provider.  Even if provider was
            # already in pseries (and so series already exists in db) we may
            # need to do this to change preferred provider.
            self._update_db_with_pseries(pseries, preferred)
            if missing or pids:
                # We need to hit more providers.  Update the database with
                # what we have now, call ourselves back with fast=False, and
                # return back to the caller.
                completed = kaa.Signal()
                ip = self._update_series(provider, id, dirty, fast=False, completed=completed)
                # Generally, ip.finished won't be True, although this happens during testing
                # where we used locally cached copies of series metadata, so coroutines
                # don't need to yield on the network.
                yield completed if not ip.finished else None
            else:
                yield None

        # XXX:
        if os.getenv('STAY_LOCAL', 0):
            for zot in (u'73141', u'2594'), (u'79349', u'7926'), (u'75897', u'5266'), (u'73762', u'3741'), (u'95011', u'22622'), (u'73255', u'3908'), (u'73244', u'6061'), (u'71663', u'6190'), (u'164301', u'25189'):
                if id in zot:
                    missing=[]; pids = {self.providers['thetvdb']: zot[0], self.providers['tvrage']: zot[1]}
                    break
        if missing:
            # We have providers we don't know the id for.  Do we know the series
            # name and start date?
            if existing:
                name, started = existing['name'], existing['started']
            else:
                # No, and before we can search for the series on other
                # providers we need to get its name and start date.  If
                # this fails we can't continue, so no point catching
                # any exception get_series() raises.
                pseries[provider] = s = yield provider.get_series(id)
                name, started = s['name'], s['started']
                # We just fetched it, so delete provider from pids.
                del pids[provider]

            # Issue a search by name for all providers missing series ids.  First remove
            # any year or date in brackets from the name.
            name = re.sub('\([\d-]+\)', '', name).strip()
            log.info('searching for %s on provider(s) %s', name, ', '.join(p.NAME for p in missing))
            for p, results in (yield self._invoke_providers('search', name, which=missing)):
                normname = self._normalize_name(name)
                normnameaggr = self._normalize_name(name, aggressive=True)
                # List of series dicts that we consider a match
                matches = []   # [(priority, dict), ...]
                # List of series dicts that could be a match, but we need to fetch them
                # up front to be sure.
                maybe = []     # [dict, ...]
                for result in results:
                    # TODO: use difflib and allow fuzzy matches iff start airdate is exact match.
                    # Year matches require exact names.  Similarly, we can do a fuzzy date match
                    # if name is an exact match.
                    normresult = self._normalize_name(result.name)
                    normresultaggr = self._normalize_name(result.name, aggressive=True)
                    log.debug2('%s: result %s [%s] (want %s [%s]) started %s (want %s)', p.NAME, normresult,
                               normresultaggr, normname, normnameaggr, result.started, started)
                    if normname == normresult:
                        if started == result.started:
                            # The non-aggressively normalized name matches and the airdate
                            # matches, this is a solid match.
                            matches.append((0, result))
                        else:
                            # We have an exact title match but the airdate doesn't match.
                            maybe.append(result)
                    elif normnameaggr == normresultaggr:
                        # If the aggressively normalized name matches and the start year
                        # matches, then add this to the maybe list for a more thorough
                        # comparison.
                        if started[:4] == result.year:
                            maybe.append(result)
                if matches:
                    # We match based on full air date or just year, but prefer
                    # the more specific match if it's available.
                    matches.sort(key=lambda i: i[0])
                    pids[p] = self._parse_id(matches[0][1].id)[1]
                elif maybe:
                    log.debug('no definitive matches for %s from %s, trying less likely options.', name, p.NAME)
                    for result in maybe:
                        log.debug('retrieving possible match %s (%s) from %s', result.name, result.id, p.NAME)
                        s = yield p.get_series(self._parse_id(result.id)[1])
                        tpseries = dict(pseries.items() + [(p, s)])
                        if not pseries and existing and preferred.CACHEATTR in existing:
                            # We have existing series data but pseries is empty, which means
                            # that all existing series data was marked as dirty.  But we
                            # need some frame of reference to compare the series data
                            # we just fetched.  So include the series data from
                            # the preferred provider for the conflicts check.
                            tpseries[preferred] = existing[preferred.CACHEATTR]
                        n_real_conflicts, conflicts, matched, unmatched = self._get_conflicts(tpseries)
                        # Count the number of unmatched episodes other that aren't season 0.
                        n_real_unmatched = sum(1 for (provider, nn, ep) in unmatched if ep['season'] != 0)
                        # Assume that if we have more matched than unmatched then the result is valid.
                        if len(matched) > n_real_unmatched:
                            log.debug('result seems to match (%d > %d)', len(matched), n_real_unmatched)
                            pseries[p] = s
                        else:
                            log.debug('result has too many unmatched episodes, skipping')

            for p in missing:
                if p not in pids and p not in pseries:
                    # A missing provider failed or returned no results for the
                    # series.
                    log.warning('no match for series %s (%s) on provider %s', name, started, p.NAME)

        if pids:
            # We need to fetch series from the server.
            which = dict((p, (id,)) for p, id in pids.items())
            # FIXME: if some providers failed, we need a retry mechanism.
            results = yield self._invoke_providers('get_series', which=which)
            if not results and not pseries:
                # No results from server and no existing (cached) series dict.
                # We're stuck.
                raise ProviderError('no providers returned results')
            else:
                for p, s in results:
                    pseries[p] = s

        self._update_db_with_pseries(pseries, preferred)
        if completed:
            completed.emit()



    def _update_db_with_pseries(self, pseries, preferred):
        # Now we have a series dict (provider -> dict) containing at least one
        # provider, hopefully all of them.
        if len(pseries) > 1:
            # Multiple providers, check for conflicts.
            n_real_conflicts, conflicts, matches, unmatched = self._get_conflicts(pseries)
            # Construct a list of episodes from preferred provider, but add ep id
            # from other providers for matched episodes.
            episodes = []
            for eplist in conflicts.values() + matches.values():
                # Get an episode count per provider.
                counts = {}
                for p, ep in eplist:
                    counts[p] = counts.get(p, 0) + 1
                if max(counts.values()) > 1:
                    # At least one provider has multiple episodes for this
                    # conflict or match.  We can't possibly know how to merge
                    # these episodes, so we must just add them individually to
                    # the episode list.
                    episodes.extend(({p.IDATTR: ep['id']}, ep) for p, ep in eplist)
                else:
                    idmap = dict((p.IDATTR, ep['id']) for p, ep in eplist)
                    # Merge all attributes from episodes, prioritizing the
                    # preferred provider if the attribute exists.
                    merged = {}
                    for p, ep in sorted(eplist, key=lambda (xp, xep): xp != preferred):
                        for k, v in ep.items():
                            if k not in merged or (v and merged.get(k) is None):
                                merged[k] = v
                    episodes.append((idmap, merged))
            for p, nn, ep in unmatched:
                episodes.append(({p.IDATTR: ep['id']}, ep))
        else:
            # One provider, get all episodes with names.
            episodes = [({preferred.IDATTR: ep['id']}, ep) for ep in pseries[preferred]['episodes'] if ep['name']]
            n_real_conflicts = 0

        # Store series dicts in the cache attributes for the providers, as well
        # as provider ids.
        idmap = dict((p.IDATTR, s['id']) for p, s in pseries.items())
        kwargs = dict((p.CACHEATTR, s) for p, s in pseries.items())
        if 'banner_data' in pseries[preferred]:
            # XXX: banner_data from ANY provider if preferred has none
            # Banner data is not storted in the cache attributes.
            kwargs['banner_data'] = pseries[preferred].pop('banner_data')
        series = pseries[preferred]

        # Before we actually update the database, verify the config object for
        # this series still exists.  Fixes the case where delete_series() is called
        # while the _update_series() coroutine is still running.  FIXME: proper
        # solution is to track per-series InProgress for _update_series() and
        # cancel it on delete_series().
        #if not self.get_config_for_series('%s:%s' % (preferred.NAME, series['id'])):
        #    print '!!!!!!!!! ABORTING UPDATE!!!!!!!!!!'
        #    return

        parent = self._add_or_update(
            'series', idmap, addattrs={},
            provider=kaa.py3_str(preferred.NAME), name=fixquotes(series['name']),
            poster=series.get('poster'), banner=series.get('banner'),
            overview=fixquotes(series.get('overview')), started=series.get('started'),
            runtime=series.get('runtime'), airtime=series.get('airtime'),
            imdbid=series.get('imdbid'), status=series.get('status', Series.STATUS_UNKNOWN),
            genres=series.get('genres'), conflict=Series.CONFLICT_UNACKED, **kwargs
        )

        # XXX: should remove conflicts from non-preferred providers?
        max_season = max(ep['season'] for idmap, ep in episodes)
        # Keep track of database ids for episodes added/updated
        dbids = []
        for idmap, ep in episodes:
            obj = self._add_or_update(
                'episode', idmap, addattrs={'status': Episode.STATUS_NONE}, parent=parent,
                name=fixquotes(ep['name']), season=ep['season'], episode=ep['episode'],
                airdate=ep.get('airdate'), overview=fixquotes(ep.get('overview')),
                keywords=series['name']
            )
            dbids.append(obj['id'])

        # Now fetch all episodes in the database that _weren't_ modified.  These were
        # likely removed on the providers' databases and we need to remove them locally.
        #
        # Another scenario is that an episode was previously unmatched between two or
        # more providers which generated multiple rows, but has since been matched
        # (perhaps due to updates to the air date and/or episode name that allowed for
        # the match).  In this case, the first instance of the episode in the table
        # will get updated above to sync the provider ids, causing the remaining row(s)
        # to be orphaned.
        #
        # There may be (probably are) other corner cases as well that could cause
        # orphaned rows.
        orphans = self.query(type='episode', parent=parent, id=kaa.db.QExpr('not in', dbids))
        for orphan in orphans:
            epinfo = '%s s%02de%02d: %s' % (series['name'], orphan['season'], orphan['episode'], orphan['name'])
            log.debug('removing orphaned entry for %s', epinfo)
            dupes = self.query(type='episode', parent=parent, id=kaa.db.QExpr('!=', orphan['id']),
                               season=orphan['season'], episode=orphan['episode'])
            if dupes:
                # FIXME: ugly, and will only get uglier as we add other attributes to check
                if (orphan['status'] != Episode.STATUS_NONE and orphan['status'] not in (d['status'] for d in dupes)) or \
                   (orphan['filename'] and orphan['filename'] not in (d['filename'] for d in dupes)) or \
                   (orphan['search_result'] and orphan['search_result'] not in (d['search_result'] for d in dupes)):
                    # FIXME: we might need to reconcile local attributes like status, filename,
                    # blacklist, search_result.  This might not actually be a problem, but until we
                    # handle it properly, log a warning.
                    log.warning('FIXME: removal of obsolete episode (%s) discards local attributes', epinfo)
            self.delete(orphan)

        self.purge_caches()


    @property
    def series(self):
        """
        A list of Series objects of all series in the database.
        """
        self._build_series_cache()
        return self._series_cache_list


    @property
    def episodes(self):
        """
        A list of episodes for all episodes in all series.
        """
        episodes = []
        for series in self.series:
            episodes.extend(series.episodes)
        return episodes


    def get_series_by_id(self, id):
        """
        Fetch a series by provider id.
        """
        self._build_series_cache()
        if id in self._series_cache:
            return self._series_cache[id]


    def get_series_by_substring(self, substr):
        """
        Fetch a series by the occurrence of a substring in the name.  If
        multiple series match, this returns the first match.

        This is primarily a convenience function for testing at the interactive
        interpreter.
        """
        self._build_series_cache()
        for series in self._series_cache.values():
            if substr.lower() in series.name.lower():
                return series


    @kaa.coroutine()
    def search(self, name, provider='thetvdb'):
        """
        Search for a series
        """
        which = [self.providers[provider]]
        results = yield self._invoke_providers('search', name, which=which, bubble=True)
        yield list(itertools.chain.from_iterable(l for p, l in results))


    @kaa.coroutine()
    def add_series_by_id(self, id, fast=True):
        """
        Adds the TV series specified by the provider id to the local database.

        :param id: the provider id for the series in the form "provider:id"
        :returns: a Series object representing the series

        Assumes the given provider is the preferred provider for this series.
        All new episodes are added with STATUS_NONE; it is up to the caller
        to initialize episode status as needed.
        """
        if id in self._series_ignore:
            self._series_ignore.remove(id)
        provider, pid = self._parse_id(id)
        series = self.query_one(type='series', **{provider.IDATTR: pid})

        if not series:
            yield self._update_series(provider, pid, fast=fast)
            series = self.query_one(type='series', **{provider.IDATTR: pid})
            if not series:
                yield
        yield Series(self, series)



    @kaa.coroutine(policy=kaa.POLICY_SYNCHRONIZED)
    def sync(self, force=False):
        """
        Sync database with all metadata providers.
        """
        # Go through all changed ids for all providers and construct a dict
        # that maps Series objects to a list of dirty providers.
        log.info('syncing with TV metadata providers')
        changed = {}  # series -> [provider, ...]
        self._build_series_cache()
        for provider, ids in (yield self._invoke_providers('get_changed_series_ids')):
            if not ids:
                continue
            for id in ids:
                series = self._series_cache.get('%s:%s' % (provider.NAME, id))
                if series:
                    changed.setdefault(series, []).append(provider)

        # Now update all changed series.
        # (mine series cache for ids)
        for series, dirty in changed.items():
            log.info('refreshing "%s" for provider(s) %s', series.name, ', '.join(p.NAME for p in dirty))
            yield self._update_series(dirty[0], series._dbattr(dirty[0].IDATTR), dirty)
        log.info('updated %d series from providers', len(changed))
        yield len(changed)


    @kaa.coroutine()
    def add_series_by_search_result(self, result):
        """
        Adds a new series given a SearchResult to the database.
        """
        yield (yield self.add_series_by_id(result.id))


    def delete_series(self, series):
        """
        Deletes a series from the database.

        :param series: the series to remove
        :type series: Series object
        """
        self.delete_by_query(parent=series._dbrow)
        self.delete(series._dbrow)
        self.purge_caches()


    def ignore_series_by_id(self, id):
        if id in self._series_ignore:
            return
        self._series_ignore.append(id)
        self.purge_caches()


    def purge_caches(self):
        # Objects based on DB metadata (Episode, Series, etc.) cache the
        # DB ObjectRow once it's retrieved.  They will go back to the DB
        # if the version has changed, so we just need to bump the version
        # to effectively purge cache.
        self._version += 1
        self._build_series_cache()


    def get_last_updated(self):
        """
        Returns the earliest update time from any of the metadata providers.
        """
        try:
            return min(p.get_last_updated() for p in self.providers.values())
        except ValueError:
            return 0


    def get_config_for_series(self, id=None, series=None):
        assert(id or series)
        if not series:
            series = self._series_cache[id]
        for cfg in config.series:
            if cfg.id.lower().strip() in series.ids:
                return cfg
