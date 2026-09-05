from django import forms
from .models import TicketType


class TicketTypeForm(forms.ModelForm):
    class Meta:
        model  = TicketType
        fields = ("name", "price", "quantity_total", "description", "benefits")
        widgets = {
            "description": forms.TextInput(attrs={"placeholder": "e.g. Standard entry with cash bar access"}),
            "benefits": forms.Textarea(attrs={
                "rows": 5,
                "placeholder": "One benefit per line:\nAccess to main floor\nCash bar\nFree T-shirt",
            }),
        }
        labels = {
            "description": "Short description (optional)",
            "benefits":    "Benefits — one per line (optional)",
        }


class AddToCartForm(forms.Form):
    ticket_type = forms.ModelChoiceField(
        queryset=TicketType.objects.all(),
        error_messages={"invalid_choice": "Invalid ticket tier."},
    )
    quantity = forms.IntegerField(min_value=1)
