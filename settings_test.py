# -*- coding: utf-8 -*-
from settings import *  # noqa

import atexit
import tempfile

from django.utils.functional import lazy


_tmpdirs = set()


def _cleanup():
    try:
        import sys
        import shutil
    except ImportError:
        return
    tmp = None
    try:
        for tmp in _tmpdirs:
            shutil.rmtree(tmp)
    except Exception, exc:
        sys.stderr.write("\n** shutil.rmtree(%r): %s\n" % (tmp, exc))

atexit.register(_cleanup)


def _polite_tmpdir():
    tmp = tempfile.mkdtemp()
    _tmpdirs.add(tmp)
    return tmp

# Make sure the app needed to test translations is present.
INSTALLED_APPS += TEST_INSTALLED_APPS

# See settings.py for documentation:
IN_TEST_SUITE = True
MEDIA_ROOT = _polite_tmpdir()
TMP_PATH = _polite_tmpdir()

# Don't call out to persona in tests.
AUTHENTICATION_BACKENDS = (
    'olympia.users.backends.TestUserBackend',
)

CELERY_TASK_ALWAYS_EAGER = True
DEBUG = False

# We won't actually send an email.
SEND_REAL_EMAIL = True

PAYPAL_PERMISSIONS_URL = ''

SITE_URL = 'http://testserver'

# COUNT() caching can't be invalidated, it just expires after x seconds. This
# is just too annoying for tests, so disable it.
CACHE_COUNT_TIMEOUT = -1

# We don't want to share cache state between processes. Always use the local
# memcache backend for tests.
#
# Note: Per settings.py, this module can cause deadlocks when running as a web
# server. It's safe to use in tests, since we don't use threads, and there's
# no opportunity for contention, but it shouldn't be used in the base settings
# until we're sure the deadlock issues are fixed.
CACHES = {
    'default': {
        'BACKEND': 'caching.backends.locmem.LocMemCache',
        'LOCATION': 'olympia',
    }
}

# Overrides whatever storage you might have put in local settings.
DEFAULT_FILE_STORAGE = 'olympia.amo.utils.LocalFileStorage'

ALLOW_SELF_REVIEWS = True

# Make sure the debug toolbar isn't used during the tests.
INSTALLED_APPS = [app for app in INSTALLED_APPS if app != 'debug_toolbar']

TASK_USER_ID = 1337

ES_DEFAULT_NUM_REPLICAS = 0
ES_DEFAULT_NUM_SHARDS = 3

# Ensure that exceptions aren't re-raised.
DEBUG_PROPAGATE_EXCEPTIONS = False

# Set to True if we're allowed to use X-SENDFILE.
XSENDFILE = True

# Don't enable the signing by default in tests, many would fail trying to sign
# empty or bad zip files, or try posting to the endpoints. We don't want that.
SIGNING_SERVER = ''

# Disable addon signing for unittests, too many would fail trying to sign
# corrupt/bad zip files. These will be enabled explicitly for unittests.
ENABLE_ADDON_SIGNING = False

# Limit logging in tests.
LOGGING = {
    'loggers': {}
}

###############################################################################
# Only if running on a CI server.
###############################################################################

if os.environ.get('RUNNING_IN_CI'):
    import product_details
    from datetime import datetime

    LOG_LEVEL = logging.ERROR

    class MockProductDetails:
        """Main information we need in tests.

        We don't want to rely on the product_details that are automatically
        downloaded in manage.py for the tests. Also, downloading all the
        information is very long, and we don't want that for each test build on
        travis for example.

        So here's a Mock that can be used instead of the real product_details.

        """
        last_update = datetime.now()
        languages = dict((lang, {'native': lang}) for lang in AMO_LANGUAGES)
        firefox_versions = {"LATEST_FIREFOX_VERSION": "33.1.1"}
        thunderbird_versions = {"LATEST_THUNDERBIRD_VERSION": "31.2.0"}
        firefox_history_major_releases = {'1.0': '2004-11-09'}

        def __init__(self):
            """Some tests need specifics languages.

            This is an excerpt of lib/product_json/languages.json.

            """
            self.languages.update({
                u'el': {
                    u'native': u'Ελληνικά',
                    u'English': u'Greek'},
                u'hr': {
                    u'native': u'Hrvatski',
                    u'English': u'Croatian'},
                u'sr': {
                    u'native': u'Dolnoserbšćina',
                    u'English': u'Serbian'},
                u'en-US': {
                    u'native': u'English (US)',
                    u'English': u'English (US)'},
                u'en-GB': {
                    u'native': u'English (GB)',
                    u'English': u'English (GB)'},
                u'tr': {
                    u'native': u'Türkçe',
                    u'English': u'Turkish'},
                u'cy': {
                    u'native': u'Cymraeg',
                    u'English': u'Welsh'},
                u'sr-Latn': {
                    u'native': u'Srpski',
                    u'English': u'Serbian'},
                u'es': {
                    u'native': u'Español',
                    u'English': u'Spanish'},
                u'dbg': {
                    u'English': u'Debug Robot',
                    u'native': u'Ḓḗƀŭɠ Řǿƀǿŧ'}
            })

    product_details.product_details = MockProductDetails()
