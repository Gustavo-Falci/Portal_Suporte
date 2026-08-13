"""Liga, desliga e consulta o modo de manutenção pela linha de comando.

Herda BaseCommand (e NÃO ComandoPortal) de propósito: é o comando que precisa
funcionar justamente enquanto a manutenção está ligada.
"""

import logging

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from tickets.models import Cliente, ModoManutencao

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Controla o modo de manutenção do portal. "
        "Ex.: manage.py modo_manutencao ligar --mensagem '...' --previsao 2026-08-13T22:00"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "acao",
            choices=["ligar", "desligar", "status"],
            help="ligar | desligar | status",
        )
        parser.add_argument(
            "--mensagem",
            default="",
            help="Aviso exibido aos usuários (só em 'ligar'). Vazio usa o padrão.",
        )
        parser.add_argument(
            "--previsao",
            default="",
            help="Previsão de retorno em ISO 8601 (ex.: 2026-08-13T22:00). Opcional.",
        )
        parser.add_argument(
            "--por",
            default="",
            help="Username de quem está ligando (registro de auditoria). Opcional.",
        )

    def handle(self, *args, **options):
        acao = options["acao"]
        modo = ModoManutencao.get_solo()

        if acao == "status":
            self._mostrar(modo)
            return

        if acao == "desligar":
            modo.desligar()
            logger.warning("Modo de manutenção DESLIGADO via management command")
            self.stdout.write(self.style.SUCCESS("Modo de manutenção DESLIGADO."))
            return

        # ligar
        previsao = self._parse_previsao(options["previsao"])
        por = self._buscar_usuario(options["por"])

        modo.ligar(mensagem=options["mensagem"], por=por, previsao=previsao)
        logger.warning(
            f"Modo de manutenção LIGADO via management command "
            f"(por={por.username if por else 'n/d'})"
        )
        self.stdout.write(self.style.WARNING("Modo de manutenção LIGADO."))
        self._mostrar(modo)

    def _mostrar(self, modo: ModoManutencao) -> None:
        estado = "ATIVO" if modo.ativo else "desligado"
        self.stdout.write(f"Estado: {estado}")
        if modo.ativo:
            self.stdout.write(f"Mensagem: {modo.mensagem}")
            if modo.previsao_retorno:
                self.stdout.write(f"Previsão de retorno: {modo.previsao_retorno}")
            if modo.ativado_em:
                self.stdout.write(f"Ligado em: {modo.ativado_em}")
            if modo.ativado_por:
                self.stdout.write(f"Ligado por: {modo.ativado_por.username}")

    def _parse_previsao(self, raw: str):
        if not raw:
            return None

        valor = parse_datetime(raw)
        if valor is None:
            raise CommandError(f"--previsao: data inválida '{raw}'. Use ISO 8601.")

        # Sem tz na string: assume o fuso do projeto (USE_TZ=True guardaria naive
        # e o Django emitiria RuntimeWarning ao comparar).
        if timezone.is_naive(valor):
            valor = timezone.make_aware(valor)
        return valor

    def _buscar_usuario(self, username: str) -> Cliente | None:
        if not username:
            return None
        try:
            return Cliente.objects.get(username=username)
        except Cliente.DoesNotExist:
            raise CommandError(f"--por: usuário '{username}' não existe.")
