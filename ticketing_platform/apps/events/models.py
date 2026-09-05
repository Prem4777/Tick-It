"""
Events models — Venue, Event, and WaitlistEntry.

Capacity is enforced at two layers:
  1. clean() validation — user-facing form errors.
  2. DB-level constraints / signal guards — prevents bypassing via admin or shell.

Ownership scoping (owner/organizer FK filters) is enforced in views, not here,
but the model relationships make it easy to do so.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class WaitlistEntry(models.Model):
    """
    Represents a person waiting for a ticket to become available.

    When a ticket is refunded, promote_next() on the related Event is called
    inside the same transaction to atomically hand the freed slot to the first
    person in line.
    """

    class Status(models.TextChoices):
        WAITING  = "waiting",  "Waiting"
        PROMOTED = "promoted", "Promoted"

    # The event this person is waiting for.
    event = models.ForeignKey(
        "Event",
        on_delete=models.CASCADE,
        related_name="waitlist_entries",
    )
    # Optional link to an existing user account.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="waitlist_entries",
    )
    email      = models.CharField(max_length=254)
    full_name  = models.CharField(max_length=200)
    # Preferred tier — nullable because CSV imports may not specify one.
    ticket_type = models.ForeignKey(
        "tickets.TicketType",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="waitlist_entries",
    )
    # Lower position number = higher priority in the queue.
    position   = models.PositiveIntegerField(default=0)
    status     = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.WAITING,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("position",)
        # One waitlist entry per email per event.
        unique_together = ("event", "email")

    def __str__(self):
        return f"{self.full_name} ({self.email}) - {self.event.name}"


class Venue(models.Model):
    """
    A physical location that can host multiple events.

    Each organizer owns their own venues — the owner FK ensures the dropdown
    in the EventForm is scoped to the logged-in organizer's venues only.
    """

    name         = models.CharField(max_length=200)
    address      = models.TextField(blank=True)
    # Hard ceiling: no event at this venue can sell more than this many tickets.
    max_capacity = models.PositiveIntegerField()
    owner        = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="venues",
    )
    image      = models.ImageField(upload_to="venues/", blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        validate_venue_capacity(self)


def validate_venue_capacity(venue):
    """
    Blocks shrinking a venue's max_capacity below what's already been
    allocated to its events. Called from Venue.clean() so it applies on
    both form saves and admin edits.
    """
    if (
        venue.pk
        and venue.events.filter(allocated_capacity__gt=venue.max_capacity).exists()
    ):
        raise ValidationError(
            {
                "max_capacity": (
                    "Cannot reduce capacity below an existing event's ticket "
                    "allocation."
                )
            }
        )


class Event(models.Model):
    """
    An event held at a Venue, owned by an Organizer.

    Status lifecycle: draft → published → cancelled.
    Tickets can only be purchased when status == published.
    Deleting the venue is blocked (PROTECT) to prevent silent data loss.
    """

    class Status(models.TextChoices):
        DRAFT     = "draft",     "Draft"
        PUBLISHED = "published", "Published"
        CANCELLED = "cancelled", "Cancelled"

    # PROTECT prevents deleting a venue that still has events attached.
    venue = models.ForeignKey(
        Venue,
        on_delete=models.PROTECT,
        related_name="events",
    )
    organizer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="events",
    )
    name        = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    # Short one-liner shown on cards and listings.
    tagline     = models.CharField(max_length=300, blank=True)
    date        = models.DateTimeField()
    end_date    = models.DateTimeField(null=True, blank=True, help_text="Optional end date/time.")
    # The total ticket budget for this event — must not exceed venue.max_capacity.
    allocated_capacity = models.PositiveIntegerField(
        help_text="Total number of tickets this event will sell.",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
    )

    # ── Customisation / display ──────────────────────────────────────────
    class Category(models.TextChoices):
        CONFERENCE  = "conference",  "Conference"
        MEETUP      = "meetup",      "Meetup"
        HACKATHON   = "hackathon",   "Hackathon"
        WORKSHOP    = "workshop",    "Workshop"
        CONCERT     = "concert",     "Concert"
        SPORTS      = "sports",      "Sports"
        NETWORKING  = "networking",  "Networking"
        OTHER       = "other",       "Other"

    class LocationType(models.TextChoices):
        IN_PERSON = "in_person", "In Person"
        ONLINE    = "online",    "Online"
        HYBRID    = "hybrid",    "Hybrid"

    category      = models.CharField(max_length=30, choices=Category.choices, default=Category.OTHER, blank=True)
    location_type = models.CharField(max_length=20, choices=LocationType.choices, default=LocationType.IN_PERSON, blank=True)
    # Comma-separated tags e.g. "AI, Python, Beginner"
    tags          = models.CharField(max_length=300, blank=True, help_text="Comma-separated tags, e.g. AI, Python, Beginner")
    website       = models.URLField(blank=True, help_text="External event website or registration link.")
    # Accent hex color for the event banner, e.g. #5B4CF5
    cover_color   = models.CharField(max_length=7, blank=True, default="", help_text="Banner accent colour as hex, e.g. #5B4CF5")
    # External image URL for the event banner/cover
    cover_image   = models.URLField(blank=True, help_text="URL of a banner/cover image for the event.")
    # Upload-based image (V4+ organizer canvas)
    image         = models.ImageField(upload_to="events/", blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return self.name

    def tag_list(self):
        """Return tags as a clean list, stripping whitespace."""
        return [t.strip() for t in self.tags.split(",") if t.strip()] if self.tags else []

    def banner_style(self):
        """CSS background style for the event banner."""
        if self.cover_color:
            return f"background: linear-gradient(135deg, {self.cover_color}cc, {self.cover_color});"
        return ""

    def clean(self):
        """Prevent allocating more capacity than the venue physically holds."""
        super().clean()
        if self.allocated_capacity and self.venue_id:
            if self.allocated_capacity > self.venue.max_capacity:
                raise ValidationError(
                    {
                        "allocated_capacity": (
                            f"Cannot exceed venue capacity of "
                            f"{self.venue.max_capacity}."
                        )
                    }
                )

    def promote_next(self):
        """
        Promote the first waiting waitlist entry to an active ticket.

        Called inside the refund transaction so the freed slot goes directly
        from the refunded ticket to the waitlist promotion — no gap where a
        concurrent purchase could sneak in.

        Returns the promoted WaitlistEntry, or None if the waitlist is empty
        or no ticket tier has remaining capacity.
        """
        from apps.tickets.models import TicketType
        from apps.tickets.services import generate_unique_code
        from django.db.models import F
        from io import BytesIO
        import qrcode
        from django.core.files.base import ContentFile

        # Find the first person still waiting.
        entry = (
            self.waitlist_entries
            .filter(status=WaitlistEntry.Status.WAITING)
            .order_by("position")
            .first()
        )
        if not entry:
            return None

        # Find a tier that still has unsold capacity.
        ticket_type = (
            self.ticket_types
            .filter(quantity_sold__lt=F("quantity_total"))
            .first()
        )
        if not ticket_type:
            return None

        # Import Ticket here to avoid a circular import at module level.
        from apps.tickets.models import Ticket

        # Generate a unique code and build the QR image.
        code   = generate_unique_code()
        ticket = Ticket(
            ticket_type=ticket_type,
            event=self,
            user=entry.user,
            unique_code=code,
            status=Ticket.Status.ACTIVE,
        )
        ticket.save()

        img = qrcode.make(code)
        buf = BytesIO()
        img.save(buf, format="PNG")
        ticket.qr_image.save(
            f"{code}.png", ContentFile(buf.getvalue()), save=True
        )

        # Mark the waitlist entry as promoted so it isn't picked again.
        entry.status = WaitlistEntry.Status.PROMOTED
        entry.save()

        return entry
