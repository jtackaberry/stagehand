# Bootstrap entry into stagehand.main.
#
# Here we aim to be compatible with both Python 2 and Python 3 so that if the
# user's version isn't compatible, we can actually report that.

import sys
import os

if sys.platform == 'win32' and 'pythonw' in sys.executable.lower():
    # If we are running under pythonw (consoleless) we must avoid
    # writing to the stderr lest we get nuked from orbit.  Redirect
    # stdout for good measure (it doesn't go anywhere anyway).
    #
    # See http://bugs.python.org/issue13582.
    nul = open(os.devnull, 'w')
    sys.stdout = sys.stderr = nul

# Add bundled third party libraries to module path.
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '../external')))


def win32_version_error():
    """
    Pops up a message box on Windows indicating the Python version is
    incompatible.
    """
    from ctypes import windll
    if sys.hexversion >= 0x03000000:
        # Python 3 strings are unicode, so use the Unicode variant.
        MessageBox = windll.user32.MessageBoxW
    else:
        # Python 2 strings are non-unicode, so use the ANSI variant.
        MessageBox = windll.user32.MessageBoxA
    ver = sys.version.split()[0]
    MessageBox(None,
    	      'Stagehand requires Python 3.3 or later, but you have %s.\n\n'
              'You can download Python at www.python.org.' % ver,
              'Incompatible Python Version',
              0x00000010)

# Are we running a new enough Python?
if sys.hexversion < 0x03030000:
    if sys.platform == 'win32':
        win32_version_error()
    else:
        print('fatal: Python 3.3 or later is required')
        print('\nNote you can always run stagehand directly through an interpreter:')
        print('$ python3.4 {}'.format(sys.argv[0]))
    sys.exit(1)


# If we get this far, preflight checks pass. Let's start.
from stagehand.main import main
import stagehand
main()
