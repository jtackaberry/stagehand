from __future__ import absolute_import
import kaa

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
