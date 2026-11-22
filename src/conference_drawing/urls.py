from django.contrib import admin
from django.urls import path

from conference_drawing import ops
from conference_drawing.raffle.views import public

urlpatterns = [
    path("health", ops.health, name="health"),
    path("version", ops.version, name="version"),
    path("c/<slug:slug>/", public.entry, name="entry"),
    path("c/<slug:slug>/thanks/", public.thanks, name="thanks"),
    path("privacy/", public.privacy, name="privacy"),
    path("admin/", admin.site.urls),
]
