"""Backfill idempotente: inscreve como colega_notificado todo autor de
interação que seja colega elegível da empresa do solicitante.

Espelha a regra de `_colegas_elegiveis` (tickets/views.py), mas usa
`apps.get_model` para funcionar tanto numa data migration (modelos
históricos) quanto num teste com o registry real do Django.
"""

GMAIL_SUFFIXO = "@gmail.com"


def inscrever_colegas_interagentes(apps) -> int:
    """Para cada ticket, adiciona a `colegas_notificados` os autores de
    interação que sejam colegas elegíveis. Retorna o total de vínculos
    adicionados. Idempotente (`.add()` não duplica)."""
    Ticket = apps.get_model("tickets", "Ticket")
    Cliente = apps.get_model("tickets", "Cliente")

    total = 0
    for ticket in Ticket.objects.select_related("cliente").iterator():
        dono = ticket.cliente
        loc = (dono.location or "").strip()
        if not loc:
            continue

        autor_ids = list(
            ticket.interacoes.exclude(autor__isnull=True)
            .values_list("autor_id", flat=True)
            .distinct()
        )
        if not autor_ids:
            continue

        elegiveis = (
            Cliente.objects.filter(pk__in=autor_ids, location__iexact=loc)
            .exclude(pk=dono.pk)
            .exclude(is_staff=True)
            .exclude(is_superuser=True)
            .exclude(groups__name__in=["Consultores", "lider_suporte"])
            .distinct()
        )
        dono_gmail = (dono.email or "").strip().lower().endswith(GMAIL_SUFFIXO)
        if dono_gmail:
            elegiveis = elegiveis.filter(email__iendswith=GMAIL_SUFFIXO)
        else:
            elegiveis = elegiveis.exclude(email__iendswith=GMAIL_SUFFIXO)

        elegiveis = list(elegiveis)
        if elegiveis:
            ticket.colegas_notificados.add(*elegiveis)
            total += len(elegiveis)

    return total
