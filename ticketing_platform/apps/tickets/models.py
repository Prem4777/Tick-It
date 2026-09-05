"""
Tickets models — TicketType, Reservation, and Ticket.

Concurrency safety:
  - reserve_tickets() in services.py uses select_for_update() on TicketType
    to prevent overselling under simultaneous checkouts.
  - available_count() excludes both expired and active-but-pending reservations
    at query time so inventory is always accurate even between cron runs.
  - A DB-level CheckConstraint ensures quantity_sold never exceeds quantity_total.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.events.models import Event


class TicketType(models.Model):
    """
    A pricing tier within an event (e.g. "VIP", "General Admission").

    Each tier has its own price and quantity ceiling. The sum of all tiers'
    quantity_total must not exceed Event.allocated_capacity — enforced in
    validate_ticket_type_capacity() called from clean().
    """

    event          = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name="ticket_types",
    )
    name           = models.CharField(max_length=100)
    price          = models.DecimalField(max_digits=10, decimal_places=2)
    quantity_total = models.PositiveIntegerField()
    quantity_sold  = models.PositiveIntegerField(default=0)
    # Short description shown on the tier card.
    description    = models.CharField(max_length=300, blank=True, help_text="One-line description, e.g. 'Standard entry with cash bar access'.")
    # Newline-separated list of benefits shown as bullet points on the tier card.
    benefits       = models.TextField(blank=True, help_text="One benefit per line, e.g.\nAccess to main floor\nCash bar\nFree T-shirt")

    class Meta:
        ordering = ("price", "name")
        constraints = [
            # DB-level safety net — application layer also checks this in clean().
            models.CheckConstraint(
                condition=models.Q(quantity_sold__lte=models.F("quantity_total")),
                name="tickettype_sold_lte_total",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.event.name})"

    def benefit_list(self):
        """Return benefits as a clean list of strings."""
        return [b.strip() for b in self.benefits.splitlines() if b.strip()] if self.benefits else []

    def reserved_count(self):
        """
        Count tickets currently held in active, non-expired reservations.
        These are counted against availability even though they haven't been
        paid for yet, to prevent overselling during the checkout window.
        """
        return self.reservations.filter(
            status=Reservation.Status.ACTIVE,
            expires_at__gt=timezone.now(),
        ).aggregate(total=Coalesce(Sum("quantity"), 0))["total"]

    def available_count(self):
        """
        Tickets that can still be reserved right now.
        = total allocated - already sold - currently held in carts
        """
        return self.quantity_total - self.quantity_sold - self.reserved_count()

    def clean(self):
        super().clean()
        validate_ticket_type_capacity(self)

    @classmethod
    def allocated_total(cls, event):
        """Sum of quantity_total across all tiers for an event. Used by the
        organizer detail view to show how much capacity has been allocated."""
        return cls.objects.filter(event=event).aggregate(
            total=Coalesce(Sum("quantity_total"), 0)
        )["total"]


class Reservation(models.Model):
    """
    A temporary hold on tickets during the checkout window (10 minutes).

    Reservations that expire are excluded from availability calculations at
    query time so inventory auto-releases without a background job.
    The expire_reservations management command formally flips status to
    'expired' for reporting clarity — it's not required for correctness.
    """

    class Status(models.TextChoices):
        ACTIVE    = "active",    "Active"
        EXPIRED   = "expired",   "Expired"
        CONVERTED = "converted", "Converted"  # Turned into a real Ticket on checkout

    ticket_type = models.ForeignKey(
        TicketType,
        on_delete=models.CASCADE,
        related_name="reservations",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reservations",
    )
    quantity   = models.PositiveIntegerField()
    expires_at = models.DateTimeField()   # now() + RESERVATION_LOCK_MINUTES
    status     = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} x{self.quantity} {self.ticket_type}"

    @property
    def is_active(self):
        """True only if status is ACTIVE and the hold hasn't expired yet."""
        return (
            self.status == self.Status.ACTIVE
            and self.expires_at > timezone.now()
        )


def validate_ticket_type_capacity(ticket_type):
    """
    Enforces three capacity rules at the application layer:
      1. quantity_total >= quantity_sold (can't undo sales).
      2. quantity_total >= quantity_sold + active_held (can't shrink below
         what's committed or in-flight in carts).
      3. Sum of all tiers <= Event.allocated_capacity (event ceiling).
    """
    if not ticket_type.quantity_total or not ticket_type.event_id:
        return

    # Rule 1 — can't go below what's already sold.
    if ticket_type.quantity_total < ticket_type.quantity_sold:
        raise ValidationError(
            {
                "quantity_total": (
                    "Cannot be less than the number already sold "
                    f"({ticket_type.quantity_sold})."
                )
            }
        )

    # Rule 2 — can't go below sold + currently held in active reservations.
    held = ticket_type.reserved_count() if ticket_type.pk else 0
    if ticket_type.quantity_total < ticket_type.quantity_sold + held:
        raise ValidationError(
            {
                "quantity_total": (
                    "Cannot be less than tickets sold "
                    f"({ticket_type.quantity_sold}) plus tickets currently "
                    f"held in carts ({held})."
                )
            }
        )

    # Rule 3 — the event's total allocation must not exceed its capacity ceiling.
    other = ticket_type.event.ticket_types.all()
    if ticket_type.pk:
        other = other.exclude(pk=ticket_type.pk)
    other_total = other.aggregate(total=Coalesce(Sum("quantity_total"), 0))["total"]
    total = ticket_type.quantity_total + other_total
    if total > ticket_type.event.allocated_capacity:
        raise ValidationError(
            {
                "quantity_total": (
                    f"Total across tiers would be {total}, exceeding the "
                    f"event's allocated capacity of "
                    f"{ticket_type.event.allocated_capacity}."
                )
            }
        )


class Ticket(models.Model):
    """
    A purchased ticket — the actual admission pass.

    Created during checkout_cart() in services.py. Each ticket gets a
    cryptographically random unique_code that is encoded as a QR image and
    stored in MEDIA_ROOT/qr/. The unique_code is also the check-in lookup key.
    """

    class Status(models.TextChoices):
        ACTIVE    = "active",    "Active"
        USED      = "used",      "Used"
        REFUNDED  = "refunded",  "Refunded"
        CANCELLED = "cancelled", "Cancelled"

    ticket_type = models.ForeignKey(
        TicketType,
        on_delete=models.PROTECT,  # Don't allow deleting a tier that has tickets
        related_name="tickets",
    )
    event = models.ForeignKey(
        Event,
        on_delete=models.PROTECT,
        related_name="tickets",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tickets",
    )
    # High-entropy random token — doubles as both QR payload and DB lookup key.
    unique_code = models.CharField(max_length=64, unique=True)
    # PNG of the QR code stored under MEDIA_ROOT/qr/.
    qr_image    = models.ImageField(upload_to="qr/", blank=True, null=True)
    status      = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    purchased_at  = models.DateTimeField(auto_now_add=True)
    # Set atomically by checkin_ticket() in services.py — NULL means not yet scanned.
    checked_in_at = models.DateTimeField(null=True, blank=True)
    # Which staff member scanned the ticket — for the audit log.
    checked_in_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
    )

    class Meta:
        ordering = ("-purchased_at",)

    @property
    def is_checked_in(self):
        """True if the ticket has been scanned at the door."""
        return bool(self.checked_in_at)

    def __str__(self):
        return f"{self.unique_code} ({self.ticket_type.name} @ {self.event.name})"
