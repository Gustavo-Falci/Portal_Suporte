from django.db import migrations


def copiar_cliente_para_clientes(apps, schema_editor):
    Area = apps.get_model("tickets", "Area")
    for area in Area.objects.all():
        if area.cliente_id:
            area.clientes.add(area.cliente_id)


def reverter_clientes_para_cliente(apps, schema_editor):
    Area = apps.get_model("tickets", "Area")
    for area in Area.objects.all():
        primeiro = area.clientes.order_by("pk").first()
        if primeiro:
            area.cliente_id = primeiro.pk
            area.save(update_fields=["cliente"])


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0026_area_clientes"),
    ]

    operations = [
        migrations.RunPython(
            copiar_cliente_para_clientes,
            reverter_clientes_para_cliente,
        ),
    ]
