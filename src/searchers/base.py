from __future__ import absolute_import
import os
import re
import urllib
import kaa

from ..config import config
from ..utils import remove_stop_words

class SearcherError(Exception):
    pass

class SearcherBase(object):
    # Values must be supplied by subclasses.
    # The internal name of the plugin (lowercase, no spaces).
    NAME = None
    # The human-readable name for the plugin.
    PRINTABLE_NAME = None
    # The type of retriever plugin required to fetch search results of this
    # searcher.
    TYPE = None
    # False if the user may disable the plugin, or True if it is always active.
    ALWAYS_ENABLED = False


    # Constants for clean_title()
    CLEAN_APOSTROPHE_LEAVE = 0
    CLEAN_APOSTROPHE_REMOVE = 1
    CLEAN_APOSTROPHE_REGEXP = 2


    def _parse_hsize(self, size):
        if isinstance(size, (int, long)):
            return size

        parts = size.lower().split()
        if not parts:
            return 0
        sz = float(parts[0].replace(',', ''))
        if len(parts) == 2:
            mult = {
                'gib': 1024*1024*1024,
                'gb': 1000*1000*1000,
                'mib': 1024*1024,
                'mb': 1000*1000,
                'kib': 1024,
                'kb': 1000
            }.get(parts[1], 1)
            return int(sz * mult)
        return int(sz)


    def _cmp_result(self, a, b, ep, ideal_size):
        # Hideous and improperly hardcoded logic follows.
        inf = float('inf')
        exts = {
            # Want.
            'mkv': 3, 'mp4': 2, 'avi': 1,
            # Don't want.
            'wmv': -inf, 'mpg': -inf, 'ts': -inf, 'rar': -inf
        }
        av = {
            (r'[xh]\.?264', r'(ac-?3|dts)'): 10,
            (r'[xh]\.?264', None): 9,
            (None,  r'(ac-?3|dts)'): 8,
            (None, r'aac\.?2?'): -1
        }
        res = {'1080p': 2, '720p': 1}
        mods = {r'blu-?ray': 10, 'proper': 9, r're-?pack': 7, 'immerse': 6,
                'dimension': 5, 'nlsubs': 4, 'web-?dl': 3}

        aname = a.filename.lower()
        bname = b.filename.lower()
        aext = os.path.splitext(aname)[-1].lstrip('.')
        bext = os.path.splitext(bname)[-1].lstrip('.')

        # Prefer results that match filename over subject.
        ascore = self._is_name_for_episode(a.filename, ep)
        bscore = self._is_name_for_episode(b.filename, ep)
        if ascore != bscore:
            return 1 if bscore else -1

        # Sort by extension
        ascore = bscore = 0
        for ext, score in exts.items():
            if aext == ext:
               ascore = score
            if bext == ext:
               bscore = score
        if ascore == -inf:
            a.disqualified = True
        if bscore == -inf:
            b.disqualified = True
        if ascore != bscore:
            return 1 if bscore > ascore else -1

        # Sort by A/V format
        ascore = bscore = 0
        for (vformat, aformat), score in av.items():
            vsearch = re.compile(r'[-. ]%s[-. $]' % vformat).search if vformat else bool
            asearch = re.compile(r'[-. ]%s[-. $]' % aformat).search if aformat else bool
            # Negative scores stick, but positive scores are replaced with
            # higher positive scores.
            if ascore >= 0 and vsearch(aname) and asearch(aname):
                ascore = score if score > ascore or score < 0 else ascore
            if bscore >= 0 and vsearch(bname) and asearch(bname):
                bscore = score if score > bscore or score < 0 else bscore
        if ascore != bscore:
            return 1 if bscore > ascore else -1

        # Sort by ideal size (if specified).
        if ideal_size:
            aratio = a.size / float(ideal_size)
            bratio = b.size / float(ideal_size)
            # If both sizes are within 20% of each other, treat them the same.
            if 0.8 < a.size / float(b.size) < 1.2:
                pass
            # If both sizes are within 40% of ideal, prefer the larger one
            elif 0.6 < aratio < 1.4 and 0.6 < bratio < 1.4:
                return 1 if b.size > a.size else -1
            # Otherwise prefer the one closest to ideal.
            else:
                return 1 if abs(1-aratio) > abs(1-bratio) else -1

        def score_by_search(items):
            ascore = bscore = 0
            for substr, score in items:
                restr = re.compile(r'[-. ]%s[-. $]' % substr)
                if restr.search(aname):
                    ascore = score
                if restr.search(bname):
                    bscore = score
            return ascore, bscore

        # Sort by resolution
        ascore, bscore = score_by_search(res.items())
        if ascore != bscore:
            return 1 if bscore > ascore else -1

        # Sort by other modifiers
        ascore, bscore = score_by_search(mods.items())
        if ascore != bscore:
            return 1 if bscore > ascore else -1

        # Sort by date, preferring the newest (or the one which actually has a date)
        if a.date != b.date:
            return 1 if b.date and not a.date or (b.date and a.date and b.date > a.date) else -1
        return 0


    def _get_episode_codes_regexp(self, episodes, codes=True, dates=True):
        parts = []
        for ep in episodes or ():
            if codes:
                parts.append(ep.code)
                parts.append('{0}x{1:02}'.format(ep.season.number, ep.number))
            if dates:
                dt = ep.airdatetime
                if dt:
                    parts.append(r'{0}[-.]?{1:02}[-.]?{2:02}'.format(dt.year, dt.month, dt.day))

        if not parts:
            return ''
        elif len(parts) == 1:
            return parts[0]
        else:
            return '(%s)' % '|'.join(parts)


    def _is_name_for_episode(self, name, ep):
        recode = re.compile(r'\b{0}\b'.format(self._get_episode_codes_regexp([ep])), re.I)
        if recode.search(name):
            # Epcode matches, check for title.
            title = ep.series.cfg.search_string or ep.series.name
            title = self.clean_title(title, apostrophe=self.CLEAN_APOSTROPHE_REGEXP)
            # Ensure each word in the title matches, but don't require them to be in
            # the right order.
            for word in title.split():
                if not re.search(r'\b%s\b' % word, name, re.I):
                    break
            else:
                return True
        return False


    def clean_title(self, title, apostrophe=CLEAN_APOSTROPHE_LEAVE, parens=True):
        """
        Strips punctutation and (optionally) parentheticals from a title to
        improve searching.

        :param title: the string to massage
        :param apostrophe: one of the CLEAN_APOSTROPHE_* constants (below)
        :param parens: if True, remove anything inside round parens. Otherwise,
                       the parens will be stripped but the contents left.

        *apostrophe* can be:
            * CLEAN_APOSTROPHE_LEAVE: don't do anything: foo's -> foo's
            * CLEAN_APOSTROPHE_REMOVE: strip them: foo's -> foos
            * CLEAN_APOSTROPHE_REGEXP: convert to regexp: foo's -> (foos|foo's)
        """
        if parens:
            # Remove anything in parens from the title (e.g. "The Office (US)")
            title = re.sub(r'\s*\([^)]*\)', '', title)
        # Substitute certain punctuation with spaces
        title = re.sub(r'[&()\[\]*+,-./:;<=>?@\\^_{|}"]', ' ', title)
        # And outright remove others
        title = re.sub(r'[!"#$%:;<=>`]', '', title)
        # Treat apostrophe separately
        if apostrophe == self.CLEAN_APOSTROPHE_REMOVE:
            title = title.replace("'", '')
        elif apostrophe == self.CLEAN_APOSTROPHE_REGEXP:
            # Replace "foo's" with "(foos|foo's)"
            def replace_apostrophe(match):
                return '(%s|%s)' % (match.group(1).replace("'", ''), match.group(1))
            title = re.sub(r"(\S+'\S*)", replace_apostrophe, title)

        title = remove_stop_words(title)
        # Clean up multiple and trailing spaces.
        return re.sub(r'\s+', ' ', title).strip()


    @kaa.coroutine()
    def _search(self, title, episodes, date, min_size, quality):
        """
        Must return a dict of episode -> [list of SearchResult objects].  A
        special key of None means the SearchResult list is not yet mapped
        to an episode object, and it will be up to the caller (i.e. the main
        search() method) to determine that.

        Subclasses must override this method.
        """
        raise NotImplementedError


    @kaa.coroutine()
    def search(self, series, episodes, date=None, min_size=None, ideal_size=None, quality='HD'):
        results = yield self._search(series, episodes, date, min_size, quality)
        # Categorize SearchResults not assigned to episodes.
        if None in results:
            for result in results[None]:
                for ep in episodes:
                    if self._is_name_for_episode(result.filename, ep):
                        results.setdefault(ep, []).append(result)
                        break
                else:
                    # We couldn't match the filename for this result against any
                    # episode.  Try matching against subject.  FIXME: we need to
                    # be careful because subject may include other codes (e.g.
                    # "Some Show s01e01-s01e23" in the case of an archive
                    # bundle)
                    if result.subject:
                        for ep in episodes:
                            if self._is_name_for_episode(result.subject, ep):
                                results.setdefault(ep, []).append(result)
                                break
            del results[None]

        # Sort, remove disqualified results, and set common result attributes.
        for ep, l in results.items():
            # Sorting also sets the disqualified attribute on the bad
            # results.
            l.sort(cmp=kaa.Callable(self._cmp_result, ep, ideal_size))
            for result in l[:]:
                if result.disqualified or result.size < min_size:
                    l.remove(result)
                else:
                    result.searcher = self.NAME
                    # We str(quality) because it may be a kaa.config Var object which can't
                    # be pickled, and we do need to be able to pickle SearchResult objects.
                    result.quality = str(quality)
            if not l:
                # We ended up disqualifying all the results.  So remove this episode
                # from the result set.
                del results[ep]

        yield results


    @kaa.coroutine()
    def _get_retriever_data(self, search_result):
        """
        Returns type-specific retriever data for the given search result.

        See :meth:`SearchResult.get_retriever_data`
        """
        raise NotImplementedError


    def _check_results_equal(self, a, b):
        raise NotImplementedError


class SearchResult(object):
    # Type of search result.  Only retrievers that support results of this
    # type will be used.
    type = None
    # This is the name of the plugin that provided the result.
    searcher = None
    filename = None
    subject = None
    # Size is in bytes
    size = None
    date = None
    newsgroup = None
    # The quality level expected for this result (retrievers may verify).
    quality = None
    disqualified = False

    def __init__(self, searcher, **kwargs):
        self.type = searcher.TYPE
        self.searcher = searcher.NAME
        [setattr(self, k, v) for k, v in kwargs.items()]

        # The cached entity from get_retriever_data().  This must not be
        # pickled, since it could reference data that is not accessible between
        # invocations.  Just use NotImplemented as a sentinel to indicate it
        # has not been populated.
        self._rdata = NotImplemented

    def __repr__(self):
        return '<%s %s at 0x%x>' % (self.__class__.__name__, self.filename, id(self))


    def __getstate__(self):
        # Return all attributes except _rdata which mustn't be pickled.
        d = self.__dict__.copy()
        del d['_rdata']
        return d


    def __setstate__(self, state):
        self.__dict__.update(state)
        self._rdata = NotImplemented


    def _get_searcher(self):
        """
        Return a new instance of the searcher plugin that provided this search
        result.

        It's possible that the plugin that provided the search result is no
        longer available (because, e.g. the SearchResult object was pickled and
        unpickled between invocations of Stagehand where the searcher plugin
        has since failed to load).

        It might be tempting to have searcher plugins subclass SearchResult and
        implement the result-specific logic there rather than taking this
        approach.  But because the SearchResults are pickled and stored in the
        database, and because plugins can fail (and so must be considered
        transient), unpickling would fail.  So we must only ever pickle core
        SearchResult objects.
        """
        # We commit this cardinal sin of importing inside a function in order
        # to prevent an import loop, since __init__ imports us for SearcherError.
        # It's safe from the usual pitfalls (i.e. importing inside a thread) since
        # the module is guaranteed to already be loaded.
        from . import plugins
        if self.searcher not in plugins:
            raise SearcherError('search result for unknown searcher plugin %s' % self.searcher)
        return plugins[self.searcher].Searcher()


    def __eq__(self, other):
        if not isinstance(other, SearchResult) or self.type != other.type:
            return False
        return self._get_searcher()._check_results_equal(self, other)


    @kaa.coroutine()
    def get_retriever_data(self, force=False):
        """
        Fetch whatever data is needed for a retriever to fetch this result.

        The actual return value is dependent on the searcher type, and no
        format is assumed or enforced here.  It is a contract between the
        searcher plugin and a retriever plugin.

        :param force: if False (default), the data is cached so that subsequent
                      invocations don't call out to the plugin.  If True,
                      it wil ask the plugin regardless of whether the value
                      was cached.
        :returns: a type-specific object from the searcher plugin, guaranteed
                  to be non-zero
        """
        if self._rdata is NotImplemented or force:
            # Fetch the data and cache it for subsequent calls.  We cache because
            # retrievers may call get_retriever_data() multiple times (for
            # multiple retriever plugins) but the actual operation could be
            # expensive (e.g. fetching a torrent or nzb file off the network).
            self._rdata = yield self._get_searcher()._get_retriever_data(self)

        if not self._rdata:
            # This shouldn't happen.  It's a bug in the searcher, which should
            # have raised SearcherError instead.
            raise SearcherError('searcher plugin did not provide retriever data for this result')

        yield self._rdata
