from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0025_alter_ticket_status_maximo"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="area",
            name="clientes",
            field=models.ManyToManyField(
                related_name="areas_m2m_temp",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Clientes com acesso",
            ),
        ),
    ]
