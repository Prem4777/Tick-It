from django.contrib.auth.views import LogoutView
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.urls import path

from . import views

app_name = "accounts"

@csrf_exempt
def logout_view(request):
    logout(request)
    return redirect('events:home')

urlpatterns = [
    path("signup/", views.signup, name="signup"),
    path("login/", views.UserLoginView.as_view(), name="login"),
    path("logout/", logout_view, name="logout"),
]