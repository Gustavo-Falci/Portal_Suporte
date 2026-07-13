from django.db import migrations

from tickets.backfill import inscrever_colegas_interagentes


def forwards(apps, schema_editor):
    inscrever_colegas_interagentes(apps)


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0035_interacaoanexo_maximo_doclink_id"),
    ]

    operations = [
        # Reverse = noop: não remove vínculos (evita apagar inscrições manuais).
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
