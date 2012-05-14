NAME = 'stagehand'
VERSION = '0.1.3'
REQUIRES = ['kaa-base>=0.99.2dev-380-d84b7045', 'kaa-metadata', 'BeautifulSoup']

import sys
import os
import platform
from distutils.command.build_scripts import build_scripts as _build_scripts

if 'pip-egg-info' in sys.argv:
    # Installing via pip; ensure dependencies are visible.
    from setuptools import setup
    setup(name=NAME, version=VERSION, install_requires=REQUIRES)
    sys.exit(0)

# aptitude install python-dev libglib2.0-dev python-beautifulsoup


class build_scripts(_build_scripts):
    """ 
    Distutils hook to compile CoffeeScript into JavaScript, which happens during
    build and (particularly) sdist.
    """
    def run(self):
        _build_scripts.run(self)
        self._compile_coffee()

    def _compile_coffee(self):
        bdir = 'lib.%s-%s-%s' % (platform.system().lower(), platform.machine(), platform.python_version().rsplit('.', 1)[0])
        sys.path.insert(0, os.path.join(os.getcwd(), 'build', bdir, 'stagehand'))

        # If the user extracts a tarball without preserving mtime, it may be possible
        # for a not-actually-stale compiled coffee file to have a slightly older mtime
        # than the source.  Here we determine the mtimes of newest and oldest data
        # files and if the range is within 10 seconds we assume that's the case and
        # avoid compiling, since we don't want to require users to have coffee
        # installed.
        oldest = newest = 0
        for path, files in self.distribution.data_files:
            for f in files:
                age = os.path.getmtime(f)
                oldest = min(oldest, age) or age
                newest = max(newest, age) or age
        # Assume untarring isn't going to take more than 10 seconds, so if
        # the range is within this then we don't compile.
        force_no_compile = newest - oldest < 10

        from web.server import coffee
        for path, files in self.distribution.data_files:
            for f in files:
                if os.path.splitext(f)[1] not in ('.tmpl', '.coffee'):
                    continue
                fc = f + '.compiled'
                if os.path.exists(fc) and force_no_compile:
                    # Touch the compiled file to ensure it is newer than the source.
                    os.utime(fc, None)
                elif not os.path.exists(fc) or os.path.getmtime(f) > os.path.getmtime(fc):
                    is_html = False if f.endswith('.coffee') else True
                    print('compiling coffeescript %s -> %s' % (f, fc))
                    try:
                        coffee.cscompile(f, fc, is_html)
                    except coffee.CSCompileError as e:
                        print('\n--> CoffeeScript compile error: %s\n%s' % e.args)
                        sys.exit(1)
        sys.path.pop(0)


def lsdata():
    for root, dirs, names in os.walk('data'):
        if names:
            yield 'share/stagehand/' + root[5:], [os.path.join(root, name) for name in names]

try:
    # kaa base imports
    import kaa
    from kaa.distribution.core import Extension, setup
except ImportError:
    print('kaa.base not installed')
    sys.exit(1)


# Verify kaa-base version
if kaa.__version__ < REQUIRES[0].split('=')[1]:
    print('Error: kaa.base %s is too old.  Try this to upgrade it:' % kaa.__version__)
    print('sudo pip install -U git+git://github.com/freevo/kaa-base.git')
    sys.exit(1)


# Automagically construct version.  If installing from a git clone, the
# version is based on the number of revisions since the last tag.  Otherwise,
# if PKG-INFO exists, assume it's a dist tarball and pull the version from
# that.
version = VERSION
if os.path.isdir('.git'):
    # Current tag object id and name
    tagid = os.popen('git rev-list --tags --max-count=1').read().strip()
    tagname = os.popen('git describe --tags %s' % tagid).read().strip()
    # Fetch all revisions since last tag.  The first item is the current HEAD object name
    # and the last is the tag object name.
    revs = os.popen('git rev-list --all | grep -B9999 %s' % tagid).read().splitlines()
    if len(revs) > 1 or version != tagname:
        # We're at least one commit past the last tag or there were no new
        # commits but the current version doesn't match the tagged version, so
        # this is considered a dev release.
        version = '%sdev-%d-%s' % (VERSION, len(revs)-1, revs[0][:8])
elif os.path.isfile('PKG-INFO'):
    ver = [l.split(':')[1].strip() for l in open('PKG-INFO') if l.startswith('Version')]
    if ver:
        version = ver[0]
else:
    # Lack of PKG-INFO means installation was not from an official dist bundle,
    # so treat it as a development version.
    version += 'dev'

setup(
    cmdclass={'build_scripts': build_scripts},
    name=NAME,
    version=version,
    auto_changelog=True,
    license='MIT',
    scripts = ['bin/stagehand'],
    data_files=list(lsdata()),
    opts_2to3 = {
        'nofix': {
            '*.py': ['import'],
        }
    },

)
