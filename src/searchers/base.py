from __future__ import absolute_import
import os
import re
import urllib
import kaa

class SearcherError(Exception):
    pass

class SearcherBase(object):
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
            mult = {'gb': 1024*1024*1024, 'mb': 1024*1024, 'kb': 1024}.get(parts[1], 1)
            return int(sz * mult)
        return int(sz)


    def _cmp_result(self, a, b, ep, ideal_size):
        # Ugh.  This function is hideous and in serious need of refactoring.
        inf = float('inf')
        exts = {'mkv': 2, 'avi': 1, 'wmv': -inf, 'mpg': -inf, 'ts': -inf}
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


    def _get_episode_codes_regexp(self, episodes):
        parts = []
        for ep in episodes or ():
            parts.append(ep.code)
        if not parts:
            return ''
        elif len(parts) == 1:
            return parts[0]
        else:
            return '(%s)' % '|'.join(parts)


    def _is_name_for_episode(self, name, ep):
        recode = re.compile(r'\b(%s|%dx%02d)\b' % (ep.code, ep.season.number, ep.number), re.I)
        # TODO: verify title
        return True if recode.search(name) else False



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
        # Clean up multiple and trailing spaces.
        return re.sub(r'\s+', ' ', title).strip()
        # TODO: also remove stop words like 'a' and 'the'


    @kaa.coroutine()
    def _search(self, title, episodes, date, min_size, quality):
        """
        Must return a dict of episode -> [list of SearchResult objects].  A
        special key of None means the SearchResult list is not yet mapped
        to an episode object, and it will be up to the caller (i.e. the main
        search() method) to determine that.
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

        # Remove disqualified results, set common result attributes, and then sort.
        for ep, l in results.items():
            for result in l[:]:
                if result.disqualified or result.size < min_size:
                    l.remove(result)
                else:
                    result.searcher = self.NAME
                    # We str(quality) because it may be a kaa.config Var object which can't
                    # be pickled, and we do need to be able to pickle SearchResult objects.
                    result.quality = str(quality)
            l.sort(cmp=kaa.Callable(self._cmp_result, ep, ideal_size))

        yield results


    @kaa.coroutine()
    def get_search_entity(self, search_result):
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

    def __repr__(self):
        return '<%s %s at 0x%x>' % (self.__class__.__name__, self.filename, id(self))
