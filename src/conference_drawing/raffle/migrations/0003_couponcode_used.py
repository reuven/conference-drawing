from django.db import migrations, models


def mark_assigned_codes_used(apps, schema_editor):  # type: ignore[no-untyped-def]
    """Codes already assigned to a pick count as used from now on."""
    apps.get_model("raffle", "CouponCode").objects.filter(pick__isnull=False).update(
        used=True
    )


class Migration(migrations.Migration):

    dependencies = [
        ("raffle", "0002_drawing_closed_by_owner"),
    ]

    operations = [
        migrations.AddField(
            model_name="couponcode",
            name="used",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(mark_assigned_codes_used, migrations.RunPython.noop),
    ]
