import logging

from django.db.models.signals import pre_save, post_save
from django.contrib.auth.signals import (
    user_logged_in,
    user_logged_out,
    user_login_failed,
)
from django.dispatch import receiver
from .models import Ticket
from .services import NotificationService
from . import audit

logger = logging.getLogger(__name__)


@receiver(user_logged_in)
def log_login(sender, request, user, **kwargs):
    audit.registrar(user, "efetuou login")


@receiver(user_logged_out)
def log_logout(sender, request, user, **kwargs):
    audit.registrar(user, "efetuou logout")


@receiver(user_login_failed)
def log_login_falha(sender, credentials, request, **kwargs):
    email = credentials.get("username") or credentials.get("email") or "?"
    audit.registrar(None, f"falha de login para {email}")


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
