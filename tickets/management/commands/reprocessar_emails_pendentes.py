import logging

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.core.management.base import BaseCommand
from django.utils import timezone

from tickets.models import EmailPendente

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Reenvia os e-mails de notificação que falharam (EmailPendente). "
        "Roda no cron a cada 5 min; a linha é apagada quando o envio dá certo."
    )

    def handle(self, *args, **options):
        pendentes = list(EmailPendente.objects.filter(desistiu=False))

        if not pendentes:
            return

        enviados = 0
        falhas = 0

        connection = get_connection()
        try:
            for pendente in pendentes:
                if self._enviar(pendente, connection):
                    enviados += 1
                else:
                    falhas += 1
        finally:
            connection.close()

        resumo = f"Reprocessamento: {enviados} enviado(s) | {falhas} falha(s)"
        logger.info(resumo)
        self.stdout.write(resumo)

    def _enviar(self, pendente: EmailPendente, connection) -> bool:
        """Reenvia um pendente. True = enviado (linha apagada)."""
        try:
            email = EmailMessage(
                subject=pendente.assunto,
                body=pendente.corpo_html,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[pendente.destinatario],
                connection=connection,
            )
            email.content_subtype = "html"
            email.send()

        except Exception as e:
            pendente.tentativas += 1
            pendente.ultimo_erro = str(e)
            pendente.ultima_tentativa_em = timezone.now()
            pendente.save(
                update_fields=["tentativas", "ultimo_erro", "ultima_tentativa_em"]
            )
            logger.error(
                f"Retry falhou para {pendente.destinatario} "
                f"(tentativa {pendente.tentativas}): {e}"
            )
            return False

        # Fora do try: o e-mail já saiu. Se o delete falhar, é erro de banco e
        # deve estourar como tal — não pode virar "falha de envio" no ultimo_erro.
        pendente.delete()
        return True
