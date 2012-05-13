NAME = 'stagehand'
VERSION = '0.1.2'
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

setup(
    cmdclass={'build_scripts': build_scripts},
    name=NAME,
    version=VERSION,
    license='GPL',
    scripts = ['bin/stagehand'],
    data_files=list(lsdata()),
    opts_2to3 = {
        'nofix': {
            '*.py': ['import'],
        }
    },

)
