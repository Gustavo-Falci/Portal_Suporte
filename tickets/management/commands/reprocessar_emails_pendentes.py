import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.core.management.base import BaseCommand
from django.utils import timezone

from tickets.models import EmailPendente

logger = logging.getLogger(__name__)

# Prazo até desistir de uma linha. 72h cobre com folga o incidente real de 31h
# (SMTP 535 de 2026-07-14 09:55 a 2026-07-15 ~17:00) e um fim de semana.
PRAZO_DESISTENCIA = timedelta(hours=72)

# Teto por execução: cada tentativa contra um SMTP doente custa ~10s (medido no
# incidente de 2026-07-15). Com o cron a cada 5 min, 50 linhas cabem no tick;
# o resto vai no seguinte.
TETO_POR_EXECUCAO = 50


class Command(BaseCommand):
    help = (
        "Reenvia os e-mails de notificação que falharam (EmailPendente). "
        "Roda no cron a cada 5 min; a linha é apagada quando o envio dá certo."
    )

    def handle(self, *args, **options):
        pendentes = list(
            EmailPendente.objects.filter(desistiu=False)[:TETO_POR_EXECUCAO]
        )

        if not pendentes:
            return

        limite = timezone.now() - PRAZO_DESISTENCIA
        enviados = 0
        falhas = 0
        desistidos = 0

        # Uma conexão para o lote inteiro: no incidente de 2026-07-15 foram 12
        # conexões em 15 min e o host passou a recusar (Errno 111).
        connection = get_connection()
        try:
            connection.open()
        except Exception as e:
            logger.error(f"Reprocessamento abortado, SMTP indisponível: {e}")
            return

        try:
            for pendente in pendentes:
                if pendente.criado_em < limite:
                    pendente.desistiu = True
                    pendente.save(update_fields=["desistiu"])
                    desistidos += 1
                    logger.error(
                        f"Desistindo de {pendente.destinatario} após "
                        f"{PRAZO_DESISTENCIA}: {pendente.assunto}"
                    )
                    continue

                if self._enviar(pendente, connection):
                    enviados += 1
                else:
                    falhas += 1
        finally:
            connection.close()

        resumo = (
            f"Reprocessamento: {enviados} enviado(s) | "
            f"{falhas} falha(s) | {desistidos} desistido(s)"
        )
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
