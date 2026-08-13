"""Base comum dos comandos do portal.

Existe por um motivo: durante o modo de manutenção os comandos de cron
(sincronização com o Maximo, envio/reenvio de e-mail, upload pro OCI) precisam
abortar sem rodar nada. Concentrar a checagem aqui evita repetir o `if` em
cada `handle()` e garante que comandos novos herdem o comportamento.
"""

import logging

from django.core.management.base import BaseCommand, OutputWrapper

from tickets.models import ModoManutencao

logger = logging.getLogger(__name__)


class ComandoPortal(BaseCommand):

    """BaseCommand que aborta enquanto o modo de manutenção estiver ligado.

    A checagem fica em `execute()` (e não em `handle()`) para valer também
    quando o comando é chamado via `call_command`. Use
    `--ignorar-manutencao` para rodar manualmente durante a janela.
    """

    def create_parser(self, prog_name, subcommand, **kwargs):
        # Em create_parser (e não em add_arguments): subclasses que definem
        # add_arguments não precisam lembrar de chamar super().
        parser = super().create_parser(prog_name, subcommand, **kwargs)
        parser.add_argument(
            "--ignorar-manutencao",
            action="store_true",
            help="Roda mesmo com o modo de manutenção ligado (uso manual).",
        )
        return parser

    def execute(self, *args, **options):
        # call_command(..., stdout=buf) passa o destino aqui; sem isto o aviso
        # de manutenção iria pro sys.stdout e sumiria de quem chamou.
        if options.get("stdout"):
            self.stdout = OutputWrapper(options["stdout"])

        if not options.get("ignorar_manutencao") and ModoManutencao.esta_ativo():
            aviso = (
                f"Modo de manutenção ativo: comando '{self.__module__.rsplit('.', 1)[-1]}' "
                f"não executado. Use --ignorar-manutencao para forçar."
            )
            logger.warning(aviso)
            self.stdout.write(self.style.WARNING(aviso))
            return

        return super().execute(*args, **options)
