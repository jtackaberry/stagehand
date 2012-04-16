from __future__ import absolute_import

class NotifierError(Exception):
    pass

class NotifierBase(object):
    def _notify(self, episodes):
        raise NotImplementedError

    def notify(self, episodes):
        return self._notify(episodes)
