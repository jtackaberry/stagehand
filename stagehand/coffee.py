import os
import re
import asyncio
import logging
import hashlib
from subprocess import Popen, PIPE

from .toolbox.utils import which
from .toolbox import tostr, tobytes

log = logging.getLogger('stagehand.web.coffee')

class CSCompileError(ValueError):
    pass


def cscompile(src, dst=None, is_html=None):
    if not hasattr(cscompile, 'coffee'):
        # Poorman's static variable
        cscompile.coffee = which('coffee')
    if not cscompile.coffee:
        raise CSCompileError('coffee compiler not found in $PATH', None)

    csargs = [cscompile.coffee, '-p', '-s']
    with open(src) as f:
        data = f.read()
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
        stdout, stderr = proc.communicate(tobytes(script))
        if stderr:
            # Compile failed. Figure out what line caused the error.
            stderr = tostr(stderr)
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
            return "<script type='text/javascript'%s>\n%s</script>" % (attrs, tostr(stdout))
        else:
            return tostr(stdout)

    comment = 'This is a generated file. Edits will be lost.'
    data = cre.sub(subfunc, data)
    # Handle <script type='text/coffescript' src='foo.coffee'>
    data = re.sub(r'(<script[^>]*)text/coffeescript', '\\1text/javascript', data)
    if not is_html:
        data = '// %s\n' % comment + data
    #data = ('<!-- %s -->\n' if is_html else '// %s\n') % comment + data
    if dst:
        with open(dst, 'w') as f:
            f.write(data)
    return data


def cscompile_with_cache(src, cachedir, is_html=None):
    log = logging.getLogger('stagehand.web.coffee')
    compiled = src + '.compiled'
    cached = os.path.join(cachedir, hashlib.md5(tobytes(src, fs=True)).hexdigest())

    if os.path.isfile(compiled) and os.path.getmtime(src) <= os.path.getmtime(compiled):
        #log.debug2('Using system compiled %s', compiled)
        with open(compiled) as f:
            return True, f.read()
    elif os.path.isfile(cached) and os.path.getmtime(src) <= os.path.getmtime(cached):
        #log.debug2('Using local cached %s', cached)
        with open(cached) as f:
            return True, f.read()
    else:
        data = cscompile(src, cached, is_html)
        log.debug('Compiling %s -> %s', src, cached)
        return False, data
