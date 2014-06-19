import os
import urllib
import logging
import re
import asyncio
from bs4 import BeautifulSoup

from ..config import config
from ..toolbox import dateutils
from ..toolbox.net import download
from .base import SearcherBase, SearchResult, SearcherError
from .easynews_config import config as modconfig


__all__ = ['Searcher', 'modconfig']

log = logging.getLogger('stagehand.searchers.easynews')


class Searcher(SearcherBase):
    NAME = 'easynews'
    PRINTABLE_NAME = 'Easynews Global Search'
    TYPE = 'http'

    DEFAULT_URL_GLOBAL5 = 'https://secure.members.easynews.com/global5/index.html?gps={keywords}&sbj={subject}&from=&ns=&fil=&fex=&vc=&ac=&s1=nsubject&s1d=%2B&s2=nrfile&s2d=%2B&s3=dsize&s3d=%2B&pby=500&u=1&svL=&d1={date}&d1t=&d2=&d2t=&b1={size}&b1t=&b2=&b2t=&px1={res}&px1t=&px2=&px2t=&fps1=&fps1t=&fps2=&fps2t=&bps1=&bps1t=&bps2=&bps2t=&hz1=&hz1t=&hz2=&hz2t=&rn1=&rn1t=&rn2=&rn2t=&fly=2&pno=1&sS=5'


    @asyncio.coroutine
    def _search_global5(self, title, codes, size, date, res):
        if not modconfig.username or not modconfig.password:
            raise ValueError('Configuration lacks username and/or password')

        if 0 and os.path.exists('result.rss'):
            print('Using cached result.rss')
            return file('result.rss').read()

        url = modconfig.url or Searcher.DEFAULT_URL_GLOBAL5
        url = url.format(keywords=urllib.parse.quote_plus(title), subject=codes,
                         date=urllib.parse.quote_plus(date), size=size, res=res)
        status, rss = yield from download(url, retry=modconfig.retries,
                                          auth=(modconfig.username, modconfig.password))
        if status != 200:
            # TODO: handle status codes like 401 (unauth)
            raise SearcherError('HTTP status not ok (%d)' % status)
        #file('result.rss', 'w').write(rss)
        return rss


    @asyncio.coroutine
    def _search(self, series, episodes, date, min_size, quality):
        title = series.cfg.search_string or series.name
        # Strip problem characters from the title, and substitute alternative apostrophe
        title = self.clean_title(title, apostrophe=Searcher.CLEAN_APOSTROPHE_REGEXP)
        size = '%dM' % (min_size / 1048576) if min_size else '100M'
        res = '1x540' if quality == 'HD' else ''

        results = []
        for i in range(0, len(episodes), 10):
            batch = episodes[i:i+10]
            codelist = [code for episode in batch \
                             for code in self._get_episode_codes_regexp_list([episode])]
            codes = '|'.join(codelist)
            log.debug('searching for %d episodes, minimum size %s and res %s, keywords=%s subject=%s',
                      len(batch), size, res or 'any', title, codes)
            rss = yield from self._search_global5(title, codes, size, date or '', res)
            soup = BeautifulSoup(rss, 'html.parser')
            for item in soup.find_all('item'):
                result = SearchResult(self)
                result.filename = urllib.parse.unquote(os.path.split(item.enclosure['url'])[-1])
                result.size = self._parse_hsize(item.enclosure['length'])
                result.date = dateutils.from_rfc822(item.pubdate.contents[0])
                result.subject = ''.join(item.title.contents)
                result.url = item.enclosure['url']
                log.debug('result: %s', result)
                # TODO: parse out newsgroup
                results.append(result)

        return {None: results}


    @asyncio.coroutine
    def _get_retriever_data(self, search_result):
        return {
            'url': search_result.url,
            'username': modconfig.username,
            'password': modconfig.password,
            'retry': modconfig.retries
        }


    def _check_results_equal(self, a, b):
        try:
            # Easynews URLs contain hashes of the file, which is a convenient
            # value to compare, because it means that even different URLs can
            # end up being the same file.
            a_hash = re.search(r'/([0-9a-f]{32,})', a.url).group(1)
            b_hash = re.search(r'/([0-9a-f]{32,})', b.url).group(1)
            return a_hash == b_hash
        except AttributeError:
            # Wasn't able to find hash in URL, so compare the URLs directly.
            return a.url == b.url


def enable(manager):
    """
    Called by the web interface when the plugin is enabled where it was
    previously disabled.
    """
    # http retriever is always enabled, so no special action is needed
    # when the easynews searcher is enabled.
    pass


def get_config_template(manager):
    return os.path.join(manager.paths.data, 'web', 'settings', 'easynews.tmpl')