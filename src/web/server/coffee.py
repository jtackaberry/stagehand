from __future__ import absolute_import
import os
import re
import functools
import hashlib
import logging
from subprocess import Popen, PIPE

import kaa
from kaa.utils import which

from . import bottle

log = logging.getLogger('stagehand.web.coffee')

class CSTemplate(bottle.SimpleTemplate):
    def execute(self, _stdout, *args, **kwargs):
        # Preserve environment for later include/rebase
        self._env = args[0]
        cachedir = bottle.request['coffee.cachedir']
        # Strip any .compiled from end of filename.
        f = re.sub(r'\.compiled$', '', self.filename)
        cached, self.source = cscompile_with_cache(f, cachedir, is_html=True)
        bottle.response.logextra = '(CS %s)' % 'cached' if cached else 'compiled'
        # Before super does eval(), set filename to .compiled form so any exceptions
        # raised show proper lines.
        self.filename = f + '.compiled'
        return super(CSTemplate, self).execute(_stdout, *args, **kwargs)

    def subtemplate(self, subtpl, _stdout, *args, **kwargs):
        # Merge environment of parent template into subtemplate, but do not
        # replace existing attributes.
        if args:
            for k, v in self._env.items():
                if k not in args[0]:
                    args[0][k] = v
        return super(CSTemplate, self).subtemplate(subtpl, _stdout, *args, **kwargs)


csview = functools.partial(bottle.view, template_adapter=CSTemplate)


class CSCompileError(ValueError):
    pass


def cscompile(src, dst=None, is_html=None):
    if not hasattr(cscompile, 'coffee'):
        # Poorman's static variable
        cscompile.coffee = which('coffee')
        if not cscompile.coffee:
            raise CSCompileError('coffee compiler not found in $PATH', None)
    
    csargs = [cscompile.coffee, '-p', '-s']
    data = open(src).read()
    if is_html is None:
        # Try to detect (lamely) if src is HTML
        is_html = data[0:100].lstrip()[0] == '<'
    if is_html:
        cre = re.compile(r'''<script\s+type\s*=\s*['"]text/coffeescript['"]([^>]*)>(.*?)</script>''', re.S | re.I)
    else:
        cre = re.compile('^()(.*)$', re.S)

    def subfunc(match):
        # TODO: readd indentation
        attrs, script = match.groups()
        if not script.strip():
            return "<script type='text/javascript'%s></script>" % attrs

        proc = Popen(csargs, stdin=PIPE, stdout=PIPE, stderr=PIPE)
        stdout, stderr = proc.communicate(kaa.py3_b(script))
        if stderr:
            # Compile failed. Figure out what line caused the error.
            errmsg = stderr.lstrip().splitlines()[0]
            error = re.sub(r'\s+on line \d+', '', stderr.lstrip().splitlines()[0])
            linematch = re.search('on line (\d+)', stderr)
            if linematch:
                # This is the line relative to the coffescript portion
                linenum = int(linematch.group(1))
                # Grab the bad line.
                badline = script.splitlines()[linenum-1]
                # Now translate the number it into an absolute line from the
                # source file.  Count the number of newlines in the source up
                # to the match start.
                linenum += data.count('\n', 0, match.start())
                dump = 'File "%s", line %d\n    %s\n\n%s' % (src, linenum, badline.lstrip(), error)
            else:
                dump = 'Unable to determine line number.  Full coffee output follows:\n\n' + stderr
            raise CSCompileError('CoffeeScript ' + error, dump)

        if is_html:
            return "<script type='text/javascript'%s>\n%s</script>" % (attrs, kaa.py3_str(stdout))
        else:
            return kaa.py3_str(stdout)

    comment = 'This is a generated file. Edits will be lost.'
    data = cre.sub(subfunc, data)
    # Handle <script type='text/coffescript' src='foo.coffee'>
    data = re.sub(r'(<script[^>]*)text/coffeescript', '\\1text/javascript', data)
    if not is_html:
        data = '// %s\n' % comment + data
    #data = ('<!-- %s -->\n' if is_html else '// %s\n') % comment + data
    if dst:
        open(dst, 'w').write(data)
    return data


def cscompile_with_cache(src, cachedir, is_html=None):
    compiled = src + '.compiled'
    cached = os.path.join(cachedir, hashlib.md5(kaa.py3_b(src, fs=True)).hexdigest())

    if os.path.isfile(compiled) and os.path.getmtime(src) <= os.path.getmtime(compiled):
        #log.debug2('Using system compiled %s', compiled)
        return True, open(compiled, 'r').read()
    elif os.path.isfile(cached) and os.path.getmtime(src) <= os.path.getmtime(cached):
        #log.debug2('Using local cached %s', cached)
        return True, open(cached, 'r').read()
    else:
        try:
            data = cscompile(src, cached, is_html)
        except CSCompileError, (err, line):
            raise bottle.HTTPError(500, err, traceback=line)
        else:
            log.debug('Compiling %s -> %s', src, cached)
        return False, data
