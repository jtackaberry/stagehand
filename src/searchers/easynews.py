from __future__ import absolute_import
import os
import urllib
import StringIO
import logging
import kaa
import kaa.dateutils
from BeautifulSoup import BeautifulStoneSoup

from ..utils import download
from ..curl import CurlError
from ..config import config
from .base import SearcherBase, SearchResult, SearcherError
from .easynews_config import config as modconfig

__all__ = ['Searcher', 'modconfig']

log = logging.getLogger('stagehand.searchers.easynews')


class Searcher(SearcherBase):
    TYPE = 'easynews'
    NAME = 'easynews'

    # TODO: basically the same URLs here: g4 has hInfo and hthm extra, otherwise they are the same.
    DEFAULT_URL_GLOBAL5 = 'https://secure.members.easynews.com/global5/index.html?gps=&sbj={subject}&from=&ns=&fil=&fex=&vc=&ac=&s1=nsubject&s1d=%2B&s2=nrfile&s2d=%2B&s3=dsize&s3d=%2B&pby=500&u=1&svL=&d1={date}&d1t=&d2=&d2t=&b1={size}&b1t=&b2=&b2t=&px1=&px1t=&px2=&px2t=&fps1=&fps1t=&fps2=&fps2t=&bps1=&bps1t=&bps2=&bps2t=&hz1=&hz1t=&hz2=&hz2t=&rn1=&rn1t=&rn2=&rn2t=&fly=2&pno=1&sS=5'
    DEFAULT_URL_GLOBAL4 = 'https://secure.members.easynews.com/global4/search.html?gps=&sbj={subject}&from=&ns=&fil=&fex=&vc=&ac=&s1=nsubject&s1d=%2B&s2=nrfile&s2d=%2B&s3=dsize&s3d=%2B&pby=500&pno=1&sS=5&u=1&hthm=1&hInfo=1&svL=&d1={date}&d1t=&d2=&d2t=&b1={size}&b1t=&b2=&b2t=&px1=&px1t=&px2=&px2t=&fps1=&fps1t=&fps2=&fps2t=&bps1=&bps1t=&bps2=&bps2t=&hz1=&hz1t=&hz2=&hz2t=&rn1=&rn1t=&rn2=&rn2t=&fly=2'

    @kaa.coroutine()
    def _search_global5(self, title, size, date):
        if not modconfig.username or not modconfig.password:
            raise ValueError('Configuration lacks username and/or password')

        if os.path.exists('result.rssx'):
            print('Using cached result.rss')
            yield file('result.rss').read()

        url = modconfig.url or Searcher.DEFAULT_URL_GLOBAL4
        url = url.format(subject=urllib.quote_plus(title), date=urllib.quote_plus(date), size=size)
        status, rss = yield download(url, userpwd='%s:%s' % (modconfig.username, modconfig.password))
        if status != 200:
            # TODO: handle status codes like 401 (unauth)
            raise SearcherError('HTTP status not ok (%d)' % status)
        #file('result.rss', 'w').write(rss)
        yield rss


    @kaa.coroutine()   
    def _search(self, series, episodes, date, min_size, quality):
        # Strip problem characters from the title, and substitute alternative apostrophe
        title = self.clean_title(series.name, apostrophe=Searcher.CLEAN_APOSTROPHE_REGEXP)
        size = '%dM' % (min_size / 1048576) if min_size else '100M'
        query = '%s %s' % (title, self._get_episode_codes_regexp(episodes))
        if quality == 'HD':
            query += ' (720p|1080p)'

        """
        log.debug('generating bogus results')
        results = []
        for ep in episodes:
            results.append(SearchResult(self, filename='Acme.Show.%s.720p.HDTV.X264-DIMENSION.mkv' % ep.code, size=1198*1024*1024, date=kaa.dateutils.from_rfc822('Thu, 12 Apr 2012 17:06:02 -0700'), url='https://boost4-downloads.secure.members.easynews.com/news/8/4/7/847b553db83e1b0ff14796adfa38694d014e73e3c.mkv/Awake.S01E07.720p.HDTV.X264-DIMENSION.mkv'))
        yield {None: results}
        """
        
        log.debug('searching for "%s" with minimum size %s', query, size)
        for i in range(modconfig.retries or 1):
            try:
                rss = yield self._search_global5(query, size, date or '')
                break
            except CurlError, e:
                # TODO: don't retry on permanent errors
                log.warning('query failed (%s), retrying %d of 5', e.args[0], i+1)
        else:
            raise SearcherError('query failed too many times')

        soup = BeautifulStoneSoup(rss)
        results = []
        for item in soup.findAll('item'):
            result = SearchResult(self)
            result.filename = urllib.unquote(os.path.split(item.enclosure['url'])[-1])
            result.size = self._parse_hsize(item.enclosure['length'])
            result.date = kaa.dateutils.from_rfc822(item.pubdate.contents[0])
            result.subject = ''.join(item.title.contents)
            result.url = item.enclosure['url']
            # TODO: parse out newsgroup
            results.append(result)
        yield {None: results}


    @kaa.coroutine()
    def get_search_entity(self, search_result):
        yield search_result.url


def enable(manager):
    """
    Called by the web interface when the plugin is enabled where it was
    previously disabled.
    """
    # Ensure easynews is one of the active retrievers, or this searcher will
    # be useless.
    if 'easynews' not in config.retrievers.enabled:
        # TODO: copy relative priority of where easynews is in searchers.
        config.retrievers.enabled.append('easynews')
        # TODO: need a mechanism to communicate to web user that this change
        # was made.


def get_config_template(manager):
    return os.path.join(manager.datadir, 'web', 'settings', 'easynews.tmpl')
