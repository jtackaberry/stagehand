from __future__ import absolute_import
import kaa

from .base import NotifierBase, NotifierError
from .. import web

__all__ = ['Notifier']

class Notifier(NotifierBase):
    @kaa.coroutine()
    def _notify(self, episodes):
        # TODO: don't spam individual notifications if # of episodes is more
        # than say 5.
        for ep in episodes:
            web.notify('Episode retrieved', 'Downloaded %s %s' % (ep.series.name, ep.code))
        yield
