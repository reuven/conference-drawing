from django.contrib import admin
from django.urls import path

from conference_drawing import ops

urlpatterns = [
    path("health", ops.health, name="health"),
    path("version", ops.version, name="version"),
    path("admin/", admin.site.urls),
]
