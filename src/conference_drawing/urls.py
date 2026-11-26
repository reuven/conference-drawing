from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path
from django_ratelimit.decorators import ratelimit

from conference_drawing import ops
from conference_drawing.auth import AdminLoginForm, BoothLoginForm
from conference_drawing.raffle.views import booth, display, export, flyer, public

# Password guessing on the admin login would tie up the sync workers the booth
# needs, so login POSTs get the entry form's per-client limiter (and its 429),
# tighter. Listed before admin.site.urls, so this path wins and "admin:login"
# still reverses to the same URL.
admin.site.login_form = AdminLoginForm
admin_login = ratelimit(key=public.client_ip, rate="10/m", method="POST", block=True)(
    admin.site.login
)

# The booth's own sign-in, for helpers who are not staff. Its own limiter
# counter (group), so guessing here never uses up the admin login's budget.
booth_login = ratelimit(
    key=public.client_ip, rate="10/m", method="POST", block=True, group="booth-login"
)(
    auth_views.LoginView.as_view(
        template_name="raffle/booth/login.html",
        authentication_form=BoothLoginForm,
        redirect_authenticated_user=True,
    )
)

urlpatterns = [
    path("", public.home, name="home"),
    path("robots.txt", ops.robots_txt, name="robots-txt"),
    path("health", ops.health, name="health"),
    path("version", ops.version, name="version"),
    path("c/<slug:slug>/", public.entry, name="entry"),
    path("c/<slug:slug>/thanks/", public.thanks, name="thanks"),
    path("privacy/", public.privacy, name="privacy"),
    path("display/<str:token>/", display.display, name="display"),
    path("display/<str:token>/panel/", display.panel, name="display-panel"),
    path("booth/", booth.home, name="booth-home"),
    path("booth/login/", booth_login, name="booth-login"),
    path(
        "booth/logout/",
        auth_views.LogoutView.as_view(next_page="booth-login"),
        name="booth-logout",
    ),
    path("booth/drawing/<int:pk>/", booth.drawing_page, name="booth-drawing"),
    path("booth/drawing/<int:pk>/start/", booth.start, name="booth-start"),
    path("booth/drawing/<int:pk>/close/", booth.close, name="booth-close"),
    path("booth/pick/<int:pk>/claim/", booth.claim, name="booth-claim"),
    path("booth/pick/<int:pk>/not-here/", booth.not_here, name="booth-not-here"),
    path("booth/pick/<int:pk>/undo/", booth.undo, name="booth-undo"),
    path("booth/<slug:slug>/export.csv", export.export_csv, name="booth-export"),
    path("booth/<slug:slug>/flyer/", flyer.flyer, name="booth-flyer"),
    path("booth/<slug:slug>/wrap-up/", booth.wrapup_page, name="booth-wrapup"),
    path(
        "booth/<slug:slug>/delete-entrants/",
        booth.delete_entrants_page,
        name="booth-delete-entrants",
    ),
    path(
        "booth/<slug:slug>/retry-failed/",
        booth.retry_failed,
        name="booth-retry-failed",
    ),
    path("booth/<slug:slug>/", booth.conference_page, name="booth-conference"),
    path("admin/login/", admin_login, name="admin-login"),
    path("admin/", admin.site.urls),
]

# static() is empty unless DEBUG; in production Caddy serves /media/ itself.
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
