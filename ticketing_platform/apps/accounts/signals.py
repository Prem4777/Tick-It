"""
Accounts signals — automatically create a Profile whenever a User is saved.

Using a post_save signal means we never have to manually call
Profile.objects.create() after creating a user — it happens implicitly.
This guarantees request.user.profile always exists downstream.
"""

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Profile


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_user_profile(sender, instance, created, **kwargs):
    """
    On first save (created=True): create a new Profile for the user.
    On subsequent saves: save the existing profile to keep it in sync.
    """
    if created:
        Profile.objects.create(user=instance)
    else:
        instance.profile.save()
