"""
Accounts models — Profile extends Django's built-in User with a role field.

We intentionally avoid a custom User model; Profile is a OneToOneField so
request.user.profile is always available once the post_save signal creates it.
"""

from django.conf import settings
from django.db import models


class Profile(models.Model):
    """
    Extends auth.User with a role.

    Role is set once at signup and never changed — it determines whether a
    user sees the attendee dashboard or the organizer back-office.
    """

    class Role(models.TextChoices):
        ORGANIZER = "organizer", "Organizer"
        ATTENDEE  = "attendee",  "Attendee"

    # One profile per user. Deleting a User cascades to delete the Profile.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )

    # Default is attendee — the safer, lower-privilege role.
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.ATTENDEE,
    )

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"

    def is_organizer(self):
        """Convenience method used in templates and the organizer_required decorator."""
        return self.role == self.Role.ORGANIZER
