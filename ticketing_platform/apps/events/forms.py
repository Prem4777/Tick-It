from django import forms
from django.core.exceptions import ValidationError

from .models import Event, Venue


class VenueForm(forms.ModelForm):
    class Meta:
        model  = Venue
        fields = ("name", "address", "max_capacity")


class EventForm(forms.ModelForm):
    date = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M"],
        required=True,
    )
    end_date = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M"],
        required=False,
        label="End date / time (optional)",
    )
    cover_color = forms.CharField(
        required=False,
        label="Banner colour",
        widget=forms.TextInput(attrs={"type": "color", "style": "width:60px;height:36px;padding:2px 4px;cursor:pointer;"}),
        help_text="Pick a colour for the event banner.",
    )

    class Meta:
        model  = Event
        fields = (
            # Core — required
            "name", "venue", "date", "allocated_capacity",
            # Core — optional
            "tagline", "description", "end_date", "status",
            # Customisation
            "category", "location_type", "tags",
            "website", "cover_color", "cover_image",
        )
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        organizer = kwargs.pop("organizer", None)
        super().__init__(*args, **kwargs)

        # Only required fields
        required = {"name", "venue", "date", "allocated_capacity"}
        for field_name, field in self.fields.items():
            if field_name not in required:
                field.required = False

        if organizer is not None:
            self.fields["venue"].queryset = Venue.objects.filter(owner=organizer)

    def clean_cover_color(self):
        val = self.cleaned_data.get("cover_color", "").strip()
        if val and not val.startswith("#"):
            val = "#" + val
        if val and len(val) not in (4, 7):
            raise ValidationError("Enter a valid hex colour, e.g. #5B4CF5.")
        return val


class BulkImportForm(forms.Form):
    csv_file = forms.FileField(
        label="CSV File",
        help_text="Max 5MB. Columns: email, full_name, ticket_type (optional).",
    )

    def clean_csv_file(self):
        csv_file = self.cleaned_data.get("csv_file")
        if csv_file:
            if csv_file.size > 5 * 1024 * 1024:
                raise ValidationError("File must be under 5MB.")
        return csv_file
