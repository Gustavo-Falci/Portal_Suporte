import logging
from datetime import datetime

from django.conf import settings
from django.core.mail import EmailMessage
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.html import escape

from tickets.models import Notificacao

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Reenvia por e-mail as notificações criadas numa janela de tempo, para "
        "recuperar envios perdidos numa falha de SMTP. Dry-run por padrão."
    )

    def add_arguments(self, parser):
        parser.add_argument("--desde", required=True,
                            help="Início da janela (ISO 8601). Ex: 2026-07-15T16:30:00")
        parser.add_argument("--ate", required=True,
                            help="Fim da janela (ISO 8601).")
        parser.add_argument("--enviar", action="store_true",
                            help="Envia de verdade. Sem esta flag, apenas lista (dry-run).")
        parser.add_argument("--para", default=None,
                            help="Reenvia só para este e-mail. Use quando a janela pega vários "
                                 "destinatários do mesmo lote e só um precisa ser reenviado.")

    def handle(self, *args, **options):
        desde = self._parse_arg_date(options["desde"], "--desde")
        ate = self._parse_arg_date(options["ate"], "--ate")

        if desde >= ate:
            raise CommandError("--desde precisa ser anterior a --ate.")

        enviar = options["enviar"]

        notificacoes = (
            Notificacao.objects
            .filter(data_criacao__gte=desde, data_criacao__lte=ate)
            .select_related("destinatario", "ticket")
            .order_by("data_criacao")
        )

        para = options["para"]
        if para:
            notificacoes = notificacoes.filter(destinatario__email__iexact=para)

        modo = "ENVIO REAL" if enviar else "DRY-RUN (nada será enviado)"
        self.stdout.write(f"--- Reenvio de notificações | {modo} ---")
        self.stdout.write(f"Janela: {desde.isoformat()} até {ate.isoformat()}")
        if para:
            self.stdout.write(f"Filtro de destinatário: {para}")

        total_enviados = 0
        total_sem_email = 0

        for notificacao in notificacoes:
            destinatario = notificacao.destinatario

            if not destinatario.email:
                total_sem_email += 1
                continue

            assunto = self._montar_assunto(notificacao)
            self.stdout.write(
                f"[{notificacao.data_criacao.isoformat()}] {destinatario.email} | {assunto}"
            )

            if not enviar:
                continue

            if self._enviar(destinatario.email, assunto, self._montar_corpo(notificacao)):
                total_enviados += 1

        if not enviar:
            self.stdout.write(self.style.WARNING(
                f"\nDry-run: {notificacoes.count()} notificação(ões) na janela. "
                "Repita com --enviar para disparar os e-mails."
            ))
            return

        resumo = f"Reenvio concluído. Enviados: {total_enviados} | Sem e-mail cadastrado: {total_sem_email}"
        logger.info(resumo)
        self.stdout.write(self.style.SUCCESS(resumo))

    def _parse_arg_date(self, raw: str, flag: str) -> datetime:
        try:
            dt = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            raise CommandError(f"{flag}: data inválida '{raw}'. Use ISO 8601.")
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt)
        return dt

    def _montar_assunto(self, notificacao: Notificacao) -> str:
        ticket = notificacao.ticket
        if ticket:
            ref = ticket.maximo_id or ticket.id
            return f"[Portal Suporte] {notificacao.titulo} - Ticket #{ref}"
        return f"[Portal Suporte] {notificacao.titulo}"

    def _montar_corpo(self, notificacao: Notificacao) -> str:
        """
        Reconstrói o e-mail a partir dos dados congelados na Notificacao.
        Não lê o estado atual do ticket de propósito: o ticket pode ter mudado
        desde o fato, e o e-mail precisa relatar o que aconteceu na época.
        """
        destinatario = notificacao.destinatario
        nome_dest = escape(destinatario.first_name or destinatario.username)
        titulo_safe = escape(notificacao.titulo)
        mensagem_safe = escape(notificacao.mensagem)
        quando = timezone.localtime(notificacao.data_criacao).strftime("%d/%m/%Y às %H:%M")

        base_url = getattr(settings, "SITE_URL", "http://localhost:8000").rstrip("/")
        full_link = f"{base_url}{notificacao.link}" if notificacao.link else base_url

        contexto_ticket = ""
        if notificacao.ticket:
            ref_safe = escape(str(notificacao.ticket.maximo_id or notificacao.ticket.id))
            sumario_safe = escape(notificacao.ticket.sumario)
            contexto_ticket = f"Ticket <strong>#{ref_safe}</strong> - {sumario_safe}<br><br>"

        return f"""
        Olá, {nome_dest}.<br><br>

        Esta notificação é de <strong>{quando}</strong> e chega com atraso: uma falha
        no nosso servidor de e-mail impediu o envio no horário original. O conteúdo
        abaixo é o do momento do registro e pode ter evoluído desde então.<br><br>

        {contexto_ticket}
        <div style="background-color: #f4f4f4; padding: 15px; border-left: 4px solid #0f62fe;">
            <strong>{titulo_safe}</strong><br>
            {mensagem_safe}
        </div>
        <br>
        <a href="{full_link}">Clique aqui para ver a situação atual no portal.</a>
        """

    def _enviar(self, endereco: str, assunto: str, corpo_html: str) -> bool:
        try:
            email = EmailMessage(
                subject=assunto,
                body=corpo_html,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[endereco],
            )
            email.content_subtype = "html"
            email.send()
            return True
        except Exception as e:
            logger.error(f"Falha no reenvio para {endereco}: {e}")
            self.stdout.write(self.style.ERROR(f"   FALHA para {endereco}: {e}"))
            return False
