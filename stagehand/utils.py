import os
import sys
import re
import codecs
from io import StringIO
import xml.sax
import xml.sax.saxutils
import asyncio
import importlib
from zipfile import ZipFile
from datetime import datetime

from .toolbox.config import get_description


class Element:
    """
    Simple XML element that can have either a text content or child
    elements. This is common for config files, Freevo fxd files, XMPP
    stanzas, and many other use cases. It is possible to access the
    attribute or the (first) child by accessing a member variable with
    that name. If the name exists as attribute and child or if there
    are several children by that name, additional helper functions are
    provided. The element's name can be accessed using the tagname
    member variable.

    A special variable is 'content'. If provided to __init__, it can
    contain text element's children (either a list of Elements or one
    Element) or the text of the element. The same is true when
    accessing Element.content. If the element has children, the
    attribute or child with that name is returned (or None if it does
    not exist). If it is a node without children, Element.content will
    refer to the text content.

    SAX parser have to use the internal variables _children, _attr,
    and _content.
    """
    def __init__(self, tagname, xmlns=None, content=None, **attr):
        self.tagname = tagname
        self.xmlns = xmlns
        self._content = ''
        self._children = []
        self._attr = attr
        if content:
            if isinstance(content, (list, tuple)):
                self._children = content
            elif isinstance(content, Element):
                self._children = [ content ]
            else:
                self._content = content


    @property
    def attributes(self):
        return self._attr.keys()


    def append(self, element):
        """
        Append an element to the list of children.
        """
        self._children.append(element)

    def add_child(self, tagname, xmlns=None, content=None, **attr):
        """
        Append an element to the list of children.
        """
        element = Element(tagname, xmlns, content, **attr)
        self._children.append(element)
        return element

    def has_child(self, name):
        """
        Return if the element has at least one child with the given element name.
        """
        return self.get_child(name) is not None

    def get_child(self, name):
        """
        Return the first child with the given name or None.
        """
        for child in self._children:
            if child.tagname == name:
                return child
        return None

    def get_children(self, name=None):
        """
        Return a list of children with the given name.
        """
        if name is None:
            return self._children[:]
        children = []
        for child in self._children:
            if child.tagname == name:
                children.append(child)
        return children

    def __iter__(self):
        """
        Iterate through the children.
        """
        return self._children.__iter__()

    def get(self, item, default=None):
        """
        Get the given attribute value or None if not set.
        """
        return self._attr.get(item, default)

    def __getitem__(self, item):
        """
        Get the given attribute value or raise a KeyError if not set.
        """
        return self._attr[item]

    def __setitem__(self, item, value):
        """
        Set the given attribute to a new value.
        """
        self._attr[item] = value

    def __getattr__(self, attr):
        """
        Magic function to return the attribute or child with the given name.
        """
        if attr == 'content' and not self._children:
            return self._content
        result = self._attr.get(attr)
        if result is not None:
            return result
        result = self.get_child(attr)
        if result is not None:
            return result
        if '_' in attr:
            return getattr(self, attr.replace('_', '-'))

    def __cmp__(self, other):
        if isinstance(other, (str, unicode)):
            return cmp(self.tagname, other)
        return object.__cmp__(self, other)

    def __repr__(self):
        """
        Python representation string
        """
        return '<Element %s>' % self.tagname

    def __str__(self):
        """
        Convert the element into an XML unicode string.
        """
        result = '<%s' % self.tagname
        if self.xmlns:
            result += ' xmlns="%s"' % self.xmlns
        for key, value in self._attr.items():
            if value is None:
                continue
            value = tostr(value)
            result += ' %s=%s' % (key, xml.sax.saxutils.quoteattr(value))
        if not self._children and not self._content:
            return result + '/>'
        result += '>'
        for child in self._children:
            if not isinstance(child, Element):
                child = child.__xml__()
            result += tostr(child)
        return result + xml.sax.saxutils.escape(self._content.strip()) + '</%s>' % self.tagname


    def __bytes__(self):
        return tobytes(self.__str__())


class ElementParser(xml.sax.ContentHandler):
    """
    Handler for the SAX parser. The member function 'handle' will be
    called everytime an element given on init is closed. The parameter
    is the tree with this element as root. An element can either have
    children or text content. The ElementParser is usefull for simple
    xml files found in config files and information like epg data.
    """
    def __init__(self, *names):
        """
        Create handler with a list of element names.
        """
        self._names = names
        self._elements = []
        self.attr = {}

    def startElement(self, name, attr):
        """
        SAX callback
        """
        element = Element(name)
        element._attr = dict(attr)
        if len(self._elements):
            self._elements[-1].append(element)
        else:
            self.attr = dict(attr)
        self._elements.append(element)

    def endElement(self, name):
        """
        SAX callback
        """
        element = self._elements.pop()
        element._content = element._content.strip()
        if name in self._names or (not self._names and len(self._elements) == 1):
            self.handle(element)
        if not self._elements:
            self.finalize()

    def characters(self, c):
        """
        SAX callback
        """
        if len(self._elements):
            self._elements[-1]._content += c

    def handle(self, element):
        """
        ElementParser callback for a complete element.
        """
        pass

    def finalize(self):
        """
        ElementParser callback at the end of parsing.
        """
        pass


def load_plugins(type, names):
    #filter = lambda name: name != 'base' and '_config' not in name
    #plugins = get_plugins(group='stagehand.'+type, location=scope['__file__'], filter=filter, scope=scope)
    from .config import config

    valid, invalid = {}, {}
    for name in names:
        try:
            module = importlib.import_module('.' + name, 'stagehand.' + type)
        except Exception as e:
            invalid[name] = e
            continue

        #name = module.__name__.split('.')[-1]
        if hasattr(module, 'modconfig'):
            getattr(config, type).add_variable(name, module.modconfig)
        valid[name] = module
        if hasattr(module, 'load'):
            module.load()
    return valid, invalid


def invoke_plugins(plugins, func, *args):
    """
    Invokes an async function in parallel on all supplied plugins.

    :param plugins: a dict of plugins (name -> plugin)
    :param func: the name of the function to invoke if it exists
    :type func: str
    :param *args: the arguments to pass to the plugin function
    :returns: Future instance
    """
    asyncfuncs = [getattr(p, func)(*args) for p in plugins.values() if hasattr(p, func)]
    return asyncio.gather(*asyncfuncs)


def fixsep(s, path=True):
    """
    Applies the configured separator policy to the given string.

    :param path: if True, return a result that can be used as a path name on the
                 filesystem (replace path separators and colons)
    """
    from .config import config
    if path:
        # TODO: fix / : " ? * < > |
        s = s.replace(os.path.sep, ' ')
        # Colons are problematic on CIFS mounts.  Even on filesystems where
        # they're allowed, they'll cause problems if the user wants to copy a
        # file to a filesystem where they're not.  e.g. Linux with an ntfs-3g
        # mount will happily let you copy a file with a colon in its name, and
        # then the directory won't be readable by an actual Windows machine.
        # So, just replace them with spaces.
        s = s.replace(':', ' ')
        # For the same reason, strip question marks.
        s = s.replace('?', '')
        # Now condense multiple spaces.
        s = re.sub(r'\s+', ' ', s)
    s = s.replace(' ', config.naming.separator)
    return s


def fixquotes(u):
    """
    Given a unicode string, replaces "smart" quotes, ellipses, etc.
    with ASCII equivalents.
    """
    if not u:
        return u
    # Double quotes
    u = u.replace('\u201c', '"').replace('\u201d', '"')
    # Single quotes
    u = u.replace('\u2018', "'").replace('\u2019', "'")
    # Endash
    u = u.replace('\u2014', '--')
    # Ellipses
    u = u.replace('\u2026', '...')
    return u


def remove_stop_words(s):
    """
    Removes (English) stop words from the given string.
    """
    stopwords = 'a', 'the'
    words = [word for word in s.split() if word not in stopwords]
    # Join remaining words and remove whitespace.
    return ' '.join(words)


def cfgdesc2html(item):
    """
    Given a config item, returns the description with paragraph breaks
    (double newline) converted to <br> for use in HTML.
    """
    desc = get_description(item)
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



def abspath_to_zippath(path):
    try:
        zippath = abspath_to_zippath.zippath
    except AttributeError:
        try:
            zippath = abspath_to_zippath.zippath = os.path.abspath(__loader__.archive)
        except AttributeError:
            zippath = abspath_to_zippath.zippath = None

    if zippath and path.startswith(zippath + os.path.sep):
        relpath = path[len(zippath) + 1:]
        # Zip path separator is always /, even on Windows.
        return relpath.replace(os.path.sep, '/')


def get_file_from_zip(root, filename, ims=None):
    try:
        zipfile, files = get_file_from_zip.zipfile, get_file_from_zip.files
    except AttributeError:
        get_file_from_zip.zipfile = zipfile = ZipFile(__loader__.archive)
        get_file_from_zip.files = files = dict((i.filename, i) for i in get_file_from_zip.zipfile.infolist())

    # Don't use os.path.join here because zipfile uses / as a path sep even on
    # Windows.
    filename = (root.rstrip(os.path.sep) + '/' + filename).replace(os.path.sep, '/')
    if filename not in files:
        raise FileNotFoundError

    info = files[filename]
    mtime = datetime(*info.date_time)
    if ims and datetime.utcfromtimestamp(ims) >= mtime:
        return info, mtime, None
    else:
        return info, mtime, zipfile.open(filename)
