from django.conf import settings
from django.db import migrations, models

"""Remove a FK Area.cliente após a cópia dos dados para o M2M (0027).

Atenção: reverter esta migração recria a coluna cliente_id como NOT NULL
sem default — em tabela populada o PostgreSQL rejeita. Para rollback,
re-adicionar manualmente a coluna como nullable antes de reverter a 0027.
"""


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0027_migrar_area_cliente_para_clientes"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="area",
            name="cliente",
        ),
        migrations.AlterField(
            model_name="area",
            name="clientes",
            field=models.ManyToManyField(
                related_name="areas",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Clientes com acesso",
            ),
        ),
    ]
