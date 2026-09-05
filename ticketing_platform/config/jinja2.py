"""
Jinja2 environment configuration for TickIt.

Django's Jinja2 backend doesn't automatically expose Django template helpers
(url, static) as globals — this module registers them so every Jinja2 template
can call {{ url('name') }} and {{ static('path') }} without importing anything.
"""

from django.contrib.staticfiles.storage import staticfiles_storage
from django.urls import reverse
from django.middleware.csrf import get_token
from jinja2 import Environment


def url(viewname, *args, **kwargs):
    """Wrapper around Django's reverse() so templates can call url('name', arg)."""
    return reverse(viewname, args=args, kwargs=kwargs)


def environment(**options):
    """
    Factory called by Django's Jinja2 backend when it initialises the engine.
    Registers project-wide globals onto the Environment instance.
    """
    env = Environment(**options)
    env.globals.update(
        {
            # url() — resolves a named URL pattern to its path string.
            "url": url,
            # static() — returns the hashed/CDN URL for a static asset.
            "static": staticfiles_storage.url,
        }
    )
    return env
