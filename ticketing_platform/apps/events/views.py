"""
Events views — public attendee views, organizer CRUD, V4 check-in, V5 bulk import.

Ownership scoping rule: every organizer view filters by organizer=request.user
or owner=request.user at the queryset level. This means a crafted request
with a different organizer's pk gets a 404, not a 403, to avoid confirming
whether that object exists.
"""

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import F
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic.edit import FormView

from apps.accounts.decorators import organizer_required
from apps.tickets.models import Ticket, TicketType

from .forms import BulkImportForm, EventForm, VenueForm
from .models import Event, Venue, WaitlistEntry


# ---------------------------------------------------------------------------
# Public / Attendee views
# ---------------------------------------------------------------------------

def home(request):
    """
    Public event dashboard — shows all published events.
    Anonymous users can browse but must log in to purchase.
    """
    events = Event.objects.filter(status=Event.Status.PUBLISHED)
    return render(request, "events/event_list.html", {"events": events})


def event_detail(request, pk):
    """
    Public event detail page with ticket tier selection.
    Only published events are accessible via this view.
    """
    event        = get_object_or_404(Event, pk=pk, status=Event.Status.PUBLISHED)
    ticket_types = event.ticket_types.all()
    return render(
        request,
        "events/event_detail.html",
        {"event": event, "ticket_types": ticket_types},
    )


# ---------------------------------------------------------------------------
# Organizer: Venue CRUD
# ---------------------------------------------------------------------------

@organizer_required
def venue_list(request):
    """List all venues owned by the current organizer."""
    venues = Venue.objects.filter(owner=request.user)
    return render(request, "events/venue_list.html", {"venues": venues})


@organizer_required
def venue_create(request):
    """Create a new venue. Owner is set server-side — never trusted from POST."""
    if request.method == "POST":
        form = VenueForm(request.POST, request.FILES)
        if form.is_valid():
            venue       = form.save(commit=False)
            venue.owner = request.user  # Force ownership — ignore any crafted input
            venue.save()
            messages.success(request, f"Venue '{venue.name}' created.")
            return redirect("events:venue_detail", pk=venue.pk)
    else:
        form = VenueForm()
    return render(request, "events/venue_form.html", {"form": form, "title": "New Venue"})


@organizer_required
def venue_detail(request, pk):
    """Show a single venue and all events held there. 404 if not the owner."""
    venue  = get_object_or_404(Venue, pk=pk, owner=request.user)
    events = venue.events.all()
    return render(request, "events/venue_detail.html", {"venue": venue, "events": events})


@organizer_required
def venue_update(request, pk):
    """Edit a venue. 404 if not the owner."""
    venue = get_object_or_404(Venue, pk=pk, owner=request.user)
    if request.method == "POST":
        form = VenueForm(request.POST, request.FILES, instance=venue)
        if form.is_valid():
            form.save()
            messages.success(request, f"Venue '{venue.name}' updated.")
            return redirect("events:venue_detail", pk=venue.pk)
    else:
        form = VenueForm(instance=venue)
    return render(
        request,
        "events/venue_form.html",
        {"form": form, "title": f"Edit {venue.name}"},
    )


@organizer_required
def venue_delete(request, pk):
    """
    Delete a venue. Will raise ProtectedError if any events still reference it
    (Event.venue FK has on_delete=PROTECT) — the template warns about this.
    """
    venue = get_object_or_404(Venue, pk=pk, owner=request.user)
    if request.method == "POST":
        name = venue.name
        venue.delete()
        messages.success(request, f"Venue '{name}' deleted.")
        return redirect("events:venue_list")
    return render(request, "events/venue_confirm_delete.html", {"venue": venue})


# ---------------------------------------------------------------------------
# Organizer: Event CRUD
# ---------------------------------------------------------------------------

@organizer_required
def organizer_event_list(request):
    """List all events owned by the current organizer."""
    events = Event.objects.filter(organizer=request.user).select_related("venue")
    return render(request, "events/organizer_event_list.html", {"events": events})


@organizer_required
def organizer_event_detail(request, pk):
    """
    Organizer's event management page — shows ticket tiers and capacity stats.
    used_capacity = sum of all tiers' quantity_total for the event.
    """
    event        = get_object_or_404(Event, pk=pk, organizer=request.user)
    ticket_types = event.ticket_types.all()
    used_capacity = TicketType.allocated_total(event)
    return render(
        request,
        "events/organizer_event_detail.html",
        {"event": event, "ticket_types": ticket_types, "used_capacity": used_capacity},
    )


@organizer_required
def event_create(request):
    """Create a new event. Organizer is set server-side."""
    if request.method == "POST":
        form = EventForm(request.POST, request.FILES, organizer=request.user)
        if form.is_valid():
            event            = form.save(commit=False)
            event.organizer  = request.user
            event.save()
            messages.success(request, f"Event '{event.name}' created.")
            return redirect("events:organizer_event_list")
    else:
        form = EventForm(organizer=request.user)
    return render(request, "events/event_form.html", {"form": form, "title": "New Event"})


@organizer_required
def event_update(request, pk):
    """Edit an event. 404 if not the organizer."""
    event = get_object_or_404(Event, pk=pk, organizer=request.user)
    if request.method == "POST":
        form = EventForm(request.POST, request.FILES, instance=event, organizer=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, f"Event '{event.name}' updated.")
            return redirect("events:organizer_event_list")
    else:
        form = EventForm(instance=event, organizer=request.user)
    return render(
        request,
        "events/event_form.html",
        {"form": form, "title": f"Edit {event.name}"},
    )


@organizer_required
def event_delete(request, pk):
    """Delete an event and all its ticket types (CASCADE). 404 if not the organizer."""
    event = get_object_or_404(Event, pk=pk, organizer=request.user)
    if request.method == "POST":
        name = event.name
        event.delete()
        messages.success(request, f"Event '{name}' deleted.")
        return redirect("events:organizer_event_list")
    return render(request, "events/event_confirm_delete.html", {"event": event})


# ---------------------------------------------------------------------------
# V5: Bulk CSV import
# ---------------------------------------------------------------------------

class BulkImportView(FormView):
    """
    Organizer-only view that processes a CSV of VIP guests.

    The CSV is parsed by apps.events.tasks.process_bulk_import() which:
      - Creates Ticket records directly (bypassing checkout/payment).
      - Adds rows to the WaitlistEntry if the event is already at capacity.
      - Runs the whole batch in a single transaction — partial imports are rejected.
    """

    template_name = "events/bulk_import.html"
    form_class    = BulkImportForm

    def dispatch(self, request, *args, **kwargs):
        # Verify the event exists and belongs to this organizer before
        # rendering the form or processing the upload.
        self.event = get_object_or_404(
            Event, pk=kwargs["pk"], organizer=request.user
        )
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        return reverse("events:organizer_event_detail", kwargs={"pk": self.event.pk})

    def get_context_data(self, **kwargs):
        kwargs = super().get_context_data(**kwargs)
        kwargs["event"] = self.event
        return kwargs

    def form_valid(self, form):
        csv_file    = form.cleaned_data["csv_file"]
        csv_content = csv_file.read().decode("utf-8")

        from .tasks import process_bulk_import
        results = process_bulk_import(self.event.pk, csv_content)

        messages.success(
            self.request,
            f"Bulk import complete: {results['allocated']} tickets allocated, "
            f"{results['waitlisted']} added to waitlist, "
            f"{len(results['errors'])} errors.",
        )
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# V4: Check-in
# ---------------------------------------------------------------------------

def _check_authorize_event(request, event):
    """
    Verify the requesting user is the organizer of the given event.

    Returns (True, None) on success, or (False, JsonResponse) on failure.
    Extracted as a helper so both single and bulk check-in views share
    the same auth logic.
    """
    if request.user.profile.role != "organizer":
        return False, JsonResponse({"error": "Organizer role required"}, status=403)
    if event.organizer_id != request.user.id:
        return False, JsonResponse(
            {"error": "You are not authorized to check-in tickets for this event"},
            status=403,
        )
    return True, None


@login_required
def checkin_single(request, event_pk):
    """
    Single QR code check-in endpoint.

    GET  — renders the check-in UI page (scanner + manual entry).
    POST — accepts either a JSON body {"qr_code": "..."} from the JS scanner
           or a standard HTML form submission, calls checkin_ticket() in
           services.py, and returns JSON or a redirect accordingly.
    """
    event = get_object_or_404(Event, pk=event_pk)

    authorized, error = _check_authorize_event(request, event)
    if error:
        if request.method == "GET":
            messages.error(request, "You are not authorized to check-in tickets for this event.")
            return redirect("events:organizer_event_detail", pk=event.pk)
        return error

    if request.method == "GET":
        return render(request, "events/checkin.html", {"event": event})

    # Parse the QR code from either JSON body or form POST.
    try:
        body    = json.loads(request.body)
        qr_code = body.get("qr_code", "").strip() if body else ""
    except json.JSONDecodeError:
        qr_code = request.POST.get("qr_code", "").strip()

    is_json = request.content_type == "application/json"

    if not qr_code:
        if is_json:
            return JsonResponse({"error": "QR code is required"}, status=400)
        messages.error(request, "QR code is required.")
        return redirect(request.path)

    from apps.tickets.services import checkin_ticket
    result = checkin_ticket(qr_code, event_pk, request.user)

    # Return JSON for the JS scanner, redirect for plain HTML form submissions.
    if is_json:
        status_code = 200 if result.get("status") == "checked_in" else 400
        return JsonResponse(result, status=status_code)

    if result.get("status") == "checked_in":
        messages.success(request, result.get("message", "Ticket checked in successfully."))
    else:
        messages.error(request, result.get("message", "Validation failed."))

    return redirect(request.path)


@login_required
def checkin_bulk(request, event_pk):
    """
    Bulk QR code check-in endpoint (POST only, JSON body).

    Accepts {"qr_codes": ["code1", "code2", ...]} and processes each code
    independently — one failure does not roll back the others.
    Returns per-code results so the client can display granular feedback.

    GET requests are redirected to the single check-in UI page.
    """
    event = get_object_or_404(Event, pk=event_pk)

    authorized, error = _check_authorize_event(request, event)
    if error:
        if request.method == "GET":
            messages.error(request, "You are not authorized to check-in tickets for this event.")
            return redirect("events:organizer_event_detail", pk=event.pk)
        return error

    # Gracefully handle browser GET requests (e.g. user types the URL directly).
    if request.method == "GET":
        return redirect("events:checkin_single", event_pk=event.pk)

    try:
        body = json.loads(request.body)
    except Exception:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    qr_codes = body.get("qr_codes", [])

    if not isinstance(qr_codes, list) or len(qr_codes) == 0:
        return JsonResponse(
            {"error": "qr_codes array is required and must not be empty"},
            status=400,
        )

    if len(qr_codes) > 100:
        return JsonResponse(
            {"error": "Batch size exceeds maximum of 100 QR codes"},
            status=400,
        )

    from apps.tickets.services import bulk_checkin_tickets
    result = bulk_checkin_tickets(qr_codes, event_pk, request.user)
    return JsonResponse(result, status=200)
