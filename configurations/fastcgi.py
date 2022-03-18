from . import importer
from .error_handling import with_error_handler

importer.install()

from django.core.servers.fastcgi import dj_runfastcgi  # noqa

runfastcgi = with_error_handler(dj_runfastcgi)
