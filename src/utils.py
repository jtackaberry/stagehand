from __future__ import absolute_import
import logging
import re
import os
import cStringIO

import kaa
import kaa.config
from kaa.utils import get_plugins

from .config import config
try:
    from . import curl
except ImportError:
    curl = None

log = logging.getLogger('stagehand')

def load_plugins(type, scope):
    filter = lambda name: name != 'base' and '_config' not in name
    plugins = get_plugins(group='stagehand.'+type, location=scope['__file__'], filter=filter, scope=scope)
    valid, invalid = {}, {}
    for name, plugin in plugins.items():
        if isinstance(plugin, Exception):
            invalid[name] = plugin
            log.error('failed to load %s plugin %s: %s', type[:-1], name, plugin)
        else:
            if hasattr(plugin, 'modconfig'):
                getattr(config, type).add_variable(name, plugin.modconfig)
            valid[name] = plugin
            if hasattr(plugin, 'load'):
                plugin.load()
    return valid, invalid


def invoke_plugins(plugins, func, *args):
    """
    Invokes an async function (coroutine or threaded function) in parallel on
    all supplied plugins.

    :param plugins: a dict of plugins (name -> plugin)
    :param func: the name of the function to invoke if it exists
    :type func: str
    :param *args: the arguments to pass to the plugin function
    :returns: InProgressAll instance
    """
    return kaa.InProgressAll(getattr(p, func)(*args) for p in plugins.values() if hasattr(p, func))


def fixsep(s, path=True):
    """
    Applies the configured separator policy to the given string.

    :param path: if True, also replace path separators with _
    """
    s = s.replace(' ', config.naming.separator)
    if path:
        s = s.replace(os.path.sep, '_')
    return s


def fixquotes(u):
    """
    Given a unicode string, replaces "smart" quotes, ellipses, etc.
    with ASCII equivalents.
    """
    if not u:
        return u
    # Double quotes
    u = u.replace(u'\u201c', '"').replace(u'\u201d', '"')
    # Single quotes
    u = u.replace(u'\u2018', "'").replace(u'\u2019', "'")
    # Endash
    u = u.replace(u'\u2014', '--')
    # Ellipses
    u = u.replace(u'\u2026', '...')
    return u


def cfgdesc2html(item):
    """
    Given a kaa.config item, returns the description with paragraph breaks
    (double newline) converted to <br> for use in HTML.
    """
    desc = kaa.config.get_description(item)
    return re.sub(r'\n\s*\n', '<br/><br/>', desc)


def name_to_url_segment(name):
    """
    Given some kind of name, return a lower case string without any punctuation
    or spaces, sutiable for use as a URL path segment.
    """
    name = name.lower().replace('&', 'and').replace(' ', '_')
    if name.startswith('the_'):
        name = name[4:]
    return re.sub(r'\W', '', name)


def episode_status_icon_info(ep):
    """
    Given an Episode object, returns the icon details for the episode's status.
    
    :params ep: Episode object
    :returns: (status, title)

    status is one of 'forced', 'have', 'ignore', 'need', or 'future' and title
    is a printable string for the state.
    """
    if ep.status == ep.STATUS_NEED_FORCED:
        return 'need-forced', 'Needed (Forced by User)'
    elif ep.status == ep.STATUS_HAVE:
        return 'have', 'Downloaded'
    elif ep.status == ep.STATUS_IGNORE or ep.obsolete or ep.season.number == 0:
        return 'ignore', 'Ignored'
    elif ep.aired:
        return 'need', 'Needed'
    else:
        return 'future', 'Not Aired'


if curl:
    class Curl(curl.Curl):
        """
        Wraps the generic Curl module to provide a Curl object that initializes
        settings based on the config.
        """
        def __init__(self, **props):
            defaults = {}
            # TODO: proxy
            if config.misc.bind_address:
                defaults['bind_address'] = config.misc.bind_address
            defaults.update(props)
            super(Curl, self).__init__(**defaults)


@kaa.coroutine()
def download(url, target=None, resume=True, retry=0, progress=None, noraise=True, **props):
    """
    Convenience function that uses Curl to download a URL.

    :param url: the URL to retrieve
    :param target: the filename to store the contents to; if None, then
                   the contents will be returned.
    :param noraise: catch CurlError and return status=0
    :param props: see Curl class
    :returns: (status, bytes_read) if target is specified, otherwise,
              (status, contents) where contents is a string.
    """
    if not target:
        target = cStringIO.StringIO()
    c = Curl(**props)
    if progress:
        c.signals['progress'].connect(progress)

    for i in range(retry + 1):
        try:
            status = yield c.get(url, target, resume=resume)
        except curl.CurlError as e:
            if i == retry:
                # Done retrying, reraise this exception.
                if noraise:
                    yield 0, str(e)
                else:
                    raise
            errmsg = str(e)
        else:
            errmsg = 'status %d' % status
            if status < 500 or status >= 600:
                # Not a temporary error, break out of the retry loop.
                break
        log.warning('download failed (%d/%d): %s', i+1, retry+1, errmsg)

    if isinstance(target, cStringIO.OutputType):
        yield status, target.getvalue()
    else:
        yield status, c
