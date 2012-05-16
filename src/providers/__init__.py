from __future__ import absolute_import
import kaa

from ..utils import load_plugins, invoke_plugins
from .base import ProviderError 

plugins, plugins_broken = load_plugins('providers', globals())


@kaa.coroutine()
def start(manager):
    """
    Called when the manager is starting.
    """
    yield invoke_plugins(plugins, 'start', manager)
