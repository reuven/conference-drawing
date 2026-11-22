"""Owner-only CSV download of a conference's entrants."""

import io

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET

from ..csv_export import export_rows, write_csv
from ..models import Conference, Question


@login_required
@require_GET
def export_csv(request: HttpRequest, slug: str) -> HttpResponse:
    """Send the conference's entrants as `<slug>-entrants.csv`.

    The body is UTF-8 with a byte-order mark: without it Excel guesses a legacy
    code page and garbles accented, emoji and right-to-left names. It is built
    in memory (a conference has hundreds to low thousands of rows) and marked
    `private, no-store` because it holds every entrant's email and answers.
    """
    conf = get_object_or_404(Conference, slug=slug)
    questions = list(Question.objects.filter(conference=conf))
    out = io.StringIO(newline="")
    out.write("﻿")
    write_csv(export_rows(conf), questions, out)
    response = HttpResponse(
        out.getvalue().encode("utf-8"), content_type="text/csv; charset=utf-8"
    )
    response["Content-Disposition"] = f'attachment; filename="{conf.slug}-entrants.csv"'
    response["Cache-Control"] = "private, no-store"
    return response
