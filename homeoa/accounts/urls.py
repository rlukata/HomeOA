from django.urls import path
from django.utils.translation import gettext_lazy as _

from homeoa.ui.views import placeholder

from . import passkey_views, views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("login/verify/<str:token>/", views.verify_view, name="verify"),
    path("login/mfa/", views.mfa_challenge_view, name="mfa_challenge"),
    path("login/passkey/options/", passkey_views.login_options, name="passkey_login_options"),
    path("login/passkey/", passkey_views.login_finish, name="passkey_login"),
    path("account/passkeys/", passkey_views.manage, name="passkeys"),
    path(
        "account/passkeys/options/",
        passkey_views.register_options,
        name="passkey_register_options",
    ),
    path("account/passkeys/register/", passkey_views.register_finish, name="passkey_register"),
    path("logout/", views.logout_view, name="logout"),
    path("account/mfa/", views.mfa_setup_view, name="mfa_setup"),
    path("account/", placeholder, {"title": _("Your account")}, name="index"),
]
