from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path

from conference_drawing import ops
from conference_drawing.raffle.views import booth, display, export, flyer, public

urlpatterns = [
    path("health", ops.health, name="health"),
    path("version", ops.version, name="version"),
    path("c/<slug:slug>/", public.entry, name="entry"),
    path("c/<slug:slug>/thanks/", public.thanks, name="thanks"),
    path("privacy/", public.privacy, name="privacy"),
    path("display/<str:token>/", display.display, name="display"),
    path("display/<str:token>/panel/", display.panel, name="display-panel"),
    path("booth/", booth.home, name="booth-home"),
    path("booth/drawing/<int:pk>/", booth.drawing_page, name="booth-drawing"),
    path("booth/drawing/<int:pk>/start/", booth.start, name="booth-start"),
    path("booth/drawing/<int:pk>/close/", booth.close, name="booth-close"),
    path("booth/pick/<int:pk>/claim/", booth.claim, name="booth-claim"),
    path("booth/pick/<int:pk>/not-here/", booth.not_here, name="booth-not-here"),
    path("booth/pick/<int:pk>/undo/", booth.undo, name="booth-undo"),
    path("booth/<slug:slug>/export.csv", export.export_csv, name="booth-export"),
    path("booth/<slug:slug>/flyer/", flyer.flyer, name="booth-flyer"),
    path("booth/<slug:slug>/", booth.conference_page, name="booth-conference"),
    path("admin/", admin.site.urls),
]

# static() is empty unless DEBUG; in production Caddy serves /media/ itself.
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
