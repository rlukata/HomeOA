from django.urls import path

from . import contact_views

app_name = "contacts"

urlpatterns = [
    path("community/messages/", contact_views.inbox, name="inbox"),
    path("settings/contact/", contact_views.board_contact, name="board"),
    path("me/contacts/", contact_views.portal_contacts, name="portal"),
]
