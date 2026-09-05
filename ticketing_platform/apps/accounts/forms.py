"""
Accounts forms — signup form that extends Django's built-in UserCreationForm
with a role field so users self-select organizer or attendee at registration.
"""

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm

from .models import Profile

User = get_user_model()


class SignupForm(UserCreationForm):
    """
    Signup form adding a role choice to the standard username/password fields.

    The role is saved to the user's Profile after the User object is created
    (see accounts/views.py::signup).
    """

    role = forms.ChoiceField(
        choices=Profile.Role.choices,
        initial=Profile.Role.ATTENDEE,
        label="I am signing up as",
    )

    class Meta:
        model = User
        # email is not in UserCreationForm by default — we add it here and
        # make it required below so every account has a contact address.
        fields = ("username", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].required = True
