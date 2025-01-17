import os
import re
import logging
import asyncio

# Make kaa.metadata optional (for now)
try:
    import metadata
except ImportError:
    metadata = None

from ..config import config


log = logging.getLogger('stagehand.retrievers')

class RetrieverError(Exception):
    pass


class RetrieverSoftError(RetrieverError):
    """
    Generic retriever error indicating failure fetching a specific result
    (e.g. file not found).  Other search results for this retriever could
    be tried.
    """


class RetrieverHardError(RetrieverError):
    """
    Generic retriever error indicating a error not with the specific search
    result but with the retriever itself or its configuration (e.g. invalid
    credentials). No other search results for this retriever should be tried.
    """
    pass


class RetrieverAborted(RetrieverError):
    pass


class RetrieverAbortedSoft(RetrieverAborted, RetrieverSoftError):
    """
    A retrieve() InProgress may be aborted with this exception to abort the
    retrieval of the active search result.  If there are other search results
    for the episode being downloaded, they will be tried.
    """
    pass


class RetrieverAbortedHard(RetrieverAborted, RetrieverHardError):
    """
    Like RetrieverAbortedSoft, except no further results will be tried for the
    episode.
    """
    pass


class RetrieverBase:
    # Values must be populated by subclasses.
    # The internal name of the plugin (lowercase, no spaces).
    NAME = None
    # The human-readable name for the plugin.
    PRINTABLE_NAME = None
    # A list of supported types the retriever plugins supports (e.g. http, nzb,
    # torrent).
    SUPPORTED_TYPES = ()
    # False if the user may disable the plugin, or True if it is always active.
    ALWAYS_ENABLED = False

    def __init__(self, loop=None):
        super().__init__()
        self._loop = loop or asyncio.get_event_loop()


    @asyncio.coroutine
    def _retrieve(self, progress, episode, result, outfile):
        """
        Retrieve the given SearchResult object.
        """
        raise NotImplementedError


    def retrieve(self, progress, episode, result, outfile):
        return self._retrieve(progress, episode, result, outfile)


    def verify_result_file(self, episode, result, outfile):
        """
        Ensure the given video file matches the user-requested criteria with
        respect to resolution, codec, etc.

        :returns: False if the file could not be parsed, or True if result is verified.
        :raises: RetrieverError if the video file was valid did not pass
                 verification, which will cause the transfer to abort
        """
        if not metadata:
            log.warning('kaa.metadata module not available; skipping file verification')
            return
        info = metadata.parse(outfile)
        if not info:
            if os.path.getsize(outfile) > 1024*1024*10:
                # We have 10MB of the file and still can't parse it.  Consider
                # it corrupt or unknown.
                raise RetrieverError('could not identify file after 10MB')
            return False
        if info.media != 'MEDIA_AV':
            raise RetrieverError('file is not a video file (is %s)' % info.media)
        elif not info.video:
            raise RetrieverError('file has no video tracks')

        # Find the video track with the highest (y) resolution.
        # TODO: don't hardcode these
        video = sorted(info.video, key=lambda track: track.height)[-1]
        if (result.quality == 'SD' and (video.height < 240 or video.height > 540)) or \
           (result.quality == 'HD' and video.height < 600) or \
           (result.quality == 'UHD' and video.height < 1600):
            raise RetrieverError('video resolution %dx%d does not satisfy requested %s quality' % \
                                 (video.width, video.height, result.quality))

        if info.length < 300:
            raise RetrieverError('file duration %s is not long enough' % info.length)

        # See if the audio is the right language.  If the language is specified in any of
        # the tracks, make sure one of them is the user-preferred language.
        langs = set(track.langcode or 'und' for track in info.audio)
        if langs == set(['und']):
            # No languages specified in any track. Disqualify if 'dubbed' appears in
            # the result filename.
            if re.search(r'\bdubbed\b', result.filename, re.I):
                raise RetrieverError('filename indicates dubbed audio but no language codes specified')
        else:
            lang2to3 = dict((c[1], c[0]) for c in metadata.language.codes if len(c) == 3)
            lang = episode.series.cfg.language or config.misc.language
            threecode = lang2to3.get(lang) if len(lang) == 2 else lang
            log.info('looking for preferred audio track language %s (%s)', threecode, lang)
            if (not threecode or threecode not in langs) and lang not in langs:
                # There are audio tracks with defined languages, but none of
                # them are the language we want.  Heuristic (based on real
                # world content): if the desired language is English and one
                # of the tracks is undefined, assume it's English.
                if threecode == 'eng' and 'und' in langs:
                    log.warning("no English track found, but assuming 'und' track is English")
                else:
                    raise RetrieverError('no %s audio track in file' % threecode)

        # TODO: verify codecs too
        log.debug('%s looks good (%dx%d %s, audio languages: %s)', result.filename, video.width, video.height, video.codec,
                  ', '.join(langs))

        return True
