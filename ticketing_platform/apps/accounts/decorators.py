"""
Accounts decorators — access control for organizer-only views.

Using a decorator keeps the ownership check in one place rather than
repeating the same if-block at the top of every organizer view.
"""

from functools import wraps
from urllib.parse import quote

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse


def organizer_required(view_func):
    """
    Restricts a view to authenticated organizers only.

    - Unauthenticated users are redirected to login with a ?next= parameter
      so they land back on the original page after signing in.
    - Authenticated non-organizers (attendees) are redirected to the home page
      with an explanatory error message.
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        # Not logged in — send to login page, preserving the intended destination.
        if not request.user.is_authenticated:
            next_url = quote(request.get_full_path())
            return redirect(f"{reverse('accounts:login')}?next={next_url}")

        # Logged in but not an organizer — deny with a helpful message.
        if not request.user.profile.is_organizer():
            messages.error(request, "You need an Organizer account to do that.")
            return redirect("events:home")

        return view_func(request, *args, **kwargs)

    return wrapper
