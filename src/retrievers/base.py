from __future__ import absolute_import
import logging
import kaa

# Make kaa.metadata optional (for now)
try:
    import kaa.metadata
except ImportError:
    kaa.metadata = None

log = logging.getLogger('stagehand.retrievers')

class RetrieverError(Exception):
    pass

class RetrieverAbortedError(RetrieverError, kaa.InProgressAborted):
    pass

class RetrieverBase(object):
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

    @kaa.coroutine()
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
        if not kaa.metadata:
            log.warning('kaa.metadata module not available; skipping file verification')
            return
        info = kaa.metadata.parse(outfile)
        if not info:
            return False
        if info.media != u'MEDIA_AV':
            raise RetrieverError('file is not a video file (is %s)' % info.media)
        elif not info.video:
            raise RetrieverError('file has no video tracks')

        # Find the video track with the highest (y) resolution.
        # TODO: don't hardcode these
        video = sorted(info.video, key=lambda track: track.height)[-1]
        if (result.quality == 'SD' and (video.height < 240 or video.height > 540)) or \
           (result.quality == 'HD' and video.height < 700):
            raise RetrieverError('video resolution %dx%d does not satisfy requested %s quality' % \
                                 (video.width, video.height, result.quality))

        # TODO: verify codecs too
        log.debug('%s looks good (%dx%d %s)', result.filename, video.width, video.height, video.codec)
        return True
