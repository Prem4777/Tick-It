"""
Accounts views — signup and login.

Login reuses Django's built-in LoginView (just overrides the template path).
Signup is a custom function view so we can save the role to the Profile after
the User is created — Django's UserCreationForm doesn't know about Profile.
"""

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect, render

from .forms import SignupForm


class UserLoginView(LoginView):
    """Standard Django login — only the template path is customised."""
    template_name = "accounts/login.html"


def signup(request):
    """
    Handle new user registration.

    GET  — render the signup form.
    POST — validate, create the User, set the role on their Profile,
           log them in immediately, and redirect to the event dashboard.

    Authenticated users who visit /signup/ are redirected away — they already
    have an account.
    """
    # Redirect already-authenticated users away from the signup page.
    if request.user.is_authenticated:
        return redirect("events:home")

    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            user = form.save()
            # The post_save signal already created a Profile — we just update
            # the role the user selected on the form.
            user.profile.role = form.cleaned_data["role"]
            user.profile.save()
            # Log the new user in so they don't have to sign in again.
            login(request, user)
            messages.success(request, f"Welcome, {user.username}!")
            return redirect("events:home")
    else:
        form = SignupForm()

    return render(request, "accounts/signup.html", {"form": form})
