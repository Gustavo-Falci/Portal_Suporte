from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from .models import Ticket
from .services import NotificationService
import logging

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=Ticket)
def monitorar_mudancas_ticket(sender, instance: Ticket, **kwargs):
    """
    Monitora alterações no Ticket (ex: Mudança de Status).
    Otimização: Realiza apenas UMA consulta ao banco para comparar o estado anterior.
    """
    # Se é criação (sem ID), ignoramos pois a view/service de criação já trata
    if not instance.pk:
        return

    try:
        old_instance = Ticket.objects.get(pk=instance.pk)
    except Ticket.DoesNotExist:
        return

    # Verifica mudança de status
    if old_instance.status_maximo != instance.status_maximo:
        logger.info(
            f"Status Ticket #{instance.id}: {old_instance.status_maximo} -> {instance.status_maximo}"
        )

        try:
            # O Service agora cuida do E-mail E da Notificação Interna
            NotificationService.notificar_mudanca_status(
                instance, old_instance.get_status_maximo_display()
            )
        except Exception as e:
            logger.error(f"Erro notificação status (Ticket {instance.id}): {e}")


def post_save_interacao(sender, instance, created, **kwargs):
    """
    Disparado após salvar uma mensagem no chat.
    """
    if created:
        try:
            NotificationService.notificar_nova_interacao(instance.ticket, instance)
        except Exception as e:
            logger.error(f"Erro notificação interação (ID {instance.id}): {e}")
