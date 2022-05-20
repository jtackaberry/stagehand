#!/usr/bin/env python3
NAME = 'stagehand'
VERSION = '0.3.3'

import sys
import os
import platform
import glob
import shutil

from distutils.command.build_scripts import build_scripts as _build_scripts
from distutils.command.build_py import build_py as _build_py
from distutils.cmd import Command
from distutils.core import setup
from distutils import log

# Import a couple modules from the source tree needed to build, but disable
# generation of spurious pyc files.
sys.dont_write_bytecode = True
from stagehand.toolbox import xmlconfig
from stagehand import coffee
sys.dont_write_bytecode = False

if sys.hexversion < 0x03030000:
    print('fatal: Python 3.3 or later is required')
    sys.exit(1)

class build_py(_build_py):
    def build_packages(self):
        super().build_packages()

        # Dynamically create script for non-zip installs.
        with open(os.path.join('build', 'stagehand'), 'w') as f:
            f.write('#!/usr/bin/env python3\nimport stagehand.bootstrap\n')

        for package in self.packages:
            package_dir = self.get_package_dir(package)
            for f in glob.glob(os.path.join(package_dir, '*.cxml')):
                module = os.path.splitext(os.path.basename(f))[0]
                outfile = self.get_module_outfile(self.build_lib, package.split('.'), module)
                log.info('generating %s -> %s', f, outfile)
                xmlconfig.convert(f, outfile, package, 'stagehand.toolbox.config')


class bdist_zip(build_py):
    description = 'build a single runnable binary'

    def copy_data(self, datadir):
        for root, dirs, names in os.walk(datadir):
            dst_dir = os.path.join(self.build_lib, root)
            os.makedirs(dst_dir, exist_ok=True)
            for name in names:
                if os.path.splitext(name)[1] in ('.tmpl', '.coffee', '.swp'):
                    continue
                src = os.path.join(root, name)
                dst = os.path.join(dst_dir, name)
                if os.path.exists(dst) and os.path.getmtime(src) > os.path.getmtime(dst):
                    print('copying {} -> {}'.format(src, dst))
                shutil.copy2(src, dst)


    def run(self):
        super().run()
        build_scripts = self.get_finalized_command('build_scripts')
        build_scripts.run()

        bdist = self.get_finalized_command('bdist')
        os.makedirs(bdist.bdist_base, exist_ok=True)
        os.makedirs(bdist.dist_dir, exist_ok=True)

        self.copy_data('data')

        print('generating __init__.py')
        with open('stagehand/__init__.py', 'w') as f:
            f.write("# Auto-generated from setup.py\n__version__ = '{}'\n".format(version))

        print('generating __main__.py')
        with open(os.path.join(self.build_lib, '__main__.py'), 'w') as f:
            f.write('# This is a generated file.\nimport stagehand.bootstrap\n')

        name = self.distribution.metadata.name
        zipfile = os.path.join(bdist.bdist_base, name)
        log.info('creating %s.zip', zipfile)
        zipfile = shutil.make_archive(zipfile, 'zip', self.build_lib)

        distfile = os.path.join(bdist.dist_dir, name)
        if sys.platform == 'win32':
            distfile += '.pyw'
        log.info('creating %s', distfile)
        with open(distfile, 'wb') as f:
            f.write(b'#!/usr/bin/env python3\n')
            f.write(open(zipfile, 'rb').read())
        os.chmod(distfile, 0o755)



class build_scripts(_build_scripts):
    """
    Distutils hook to compile CoffeeScript into JavaScript.
    """
    def run(self):
        _build_scripts.run(self)
        self._compile_coffee()

    def _compile_coffee(self):
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


def find_packages(*roots, **kwargs):
    packages = []
    package_dir = {}
    for root in roots:
        for dirpath, dirnames, files in os.walk(root):
            for key, value in kwargs.get('plugins', {}).items():
                if dirpath.startswith(value):
                    python_dirpath = key + dirpath[len(value):].replace('/', '.')
                    break
            else:
                python_dirpath = dirpath.replace('/', '.')
            if '__init__.py' in files or python_dirpath.endswith('plugins'):
                package_dir[python_dirpath] = dirpath
                packages.append(python_dirpath)
    return packages, package_dir


def lsdata():
    for root, dirs, names in os.walk('data'):
        if names:
            yield 'share/stagehand/' + root[5:], [os.path.join(root, name) for name in names]

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

packages, package_dir = find_packages('stagehand', 'external')
setup(
    cmdclass={
        'build_py': build_py,
        'build_scripts': build_scripts,
        'bdist_zip': bdist_zip
    },
    name=NAME,
    version=version,
    license='MIT',
    scripts=['build/stagehand'],
    packages=packages,
    package_dir=package_dir,
    data_files=list(lsdata())

)
