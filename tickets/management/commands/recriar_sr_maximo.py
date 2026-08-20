"""Recria no Maximo SRs de tickets do portal que sumiram.

Cenário de origem: restore do Maximo com backup de 13/08/2026. Tudo que foi
aberto depois voltou a não existir do lado do Maximo, mas o ticket continua
íntegro no portal (texto, anexos, chat). Este comando manda de novo a
requisição de criação para esses tickets e regrava o `maximo_id` com o
ticketid novo (a sequência do Maximo é outra depois do restore).

BaseCommand e não ComandoPortal de propósito: é um comando de REPARO manual e
normalmente precisa rodar exatamente durante a janela de manutenção, quando
ComandoPortal abortaria.
"""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from tickets.models import InteracaoAnexo, Ticket
from tickets.services import MaximoSenderService

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Recria no Maximo a SR dos tickets informados (novo ticketid) e regrava "
        "o maximo_id. Por padrão reenvia também os anexos da abertura, o "
        "histórico do chat como worklog e os anexos do chat."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "ids",
            nargs="+",
            type=int,
            help="IDs dos tickets do portal (ex: 2429 2430 2431 2432 2433).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Só mostra o que faria (nenhum POST no Maximo, nenhum save).",
        )
        parser.add_argument(
            "--sem-anexos",
            action="store_true",
            help="Não reenvia anexos (abertura e chat).",
        )
        parser.add_argument(
            "--sem-worklogs",
            action="store_true",
            help="Não reenvia o histórico do chat como worklog.",
        )
        parser.add_argument(
            "--forcar",
            action="store_true",
            help=(
                "Recria mesmo que a SR atual ainda exista no Maximo. Sem esta "
                "flag o ticket é pulado (proteção contra SR duplicada)."
            ),
        )

    def handle(self, *args, **options):
        ids = options["ids"]
        dry_run = options["dry_run"]
        apikey = getattr(settings, "MAXIMO_API_KEY", "")

        if not getattr(settings, "MAXIMO_API_URL", "") or not apikey:
            self.stdout.write(
                self.style.ERROR("MAXIMO_API_URL ou MAXIMO_API_KEY não configurados.")
            )
            return

        tickets = list(
            Ticket.objects.filter(id__in=ids)
            .select_related("cliente", "ambiente", "area")
            .order_by("id")
        )

        faltando = sorted(set(ids) - {t.id for t in tickets})
        if faltando:
            self.stdout.write(
                self.style.WARNING(f"IDs inexistentes no portal, ignorados: {faltando}")
            )

        if dry_run:
            self.stdout.write(self.style.WARNING("--- DRY-RUN: nada será enviado ---"))

        recriados, pulados, falhas = 0, 0, 0

        for ticket in tickets:
            self.stdout.write("")
            self.stdout.write(
                f"Ticket #{ticket.id} | maximo_id atual: {ticket.maximo_id or '-'} "
                f"| {ticket.sumario[:60]}"
            )

            # Proteção contra duplicata: se o ticketid antigo ainda responde no
            # Maximo, a SR sobreviveu ao restore e recriar geraria SR dobrada.
            if ticket.maximo_id and not options["forcar"]:
                href = MaximoSenderService._get_member_href(str(ticket.maximo_id), apikey)
                if href:
                    self.stdout.write(
                        self.style.WARNING(
                            f"  SR {ticket.maximo_id} ainda existe no Maximo -> pulado "
                            f"(use --forcar para recriar assim mesmo)."
                        )
                    )
                    pulados += 1
                    continue
                self.stdout.write(f"  SR {ticket.maximo_id} não existe mais no Maximo.")

            if dry_run:
                self.stdout.write(
                    f"  [dry-run] criaria SR para solicitante "
                    f"{ticket.cliente.username} (person_id={ticket.cliente.person_id}, "
                    f"location={ticket.cliente.location}), "
                    f"ativo={getattr(ticket.ambiente, 'numero_ativo', None)}, "
                    f"area={getattr(ticket.area, 'nome_area', None)}, "
                    f"prioridade={ticket.prioridade}"
                )
                anexos_abertura = self._anexos_abertura(ticket)
                self.stdout.write(
                    f"  [dry-run] anexos abertura: {len(anexos_abertura)} | "
                    f"worklogs: {ticket.interacoes.count()} | "
                    f"anexos do chat: "
                    f"{InteracaoAnexo.objects.filter(interacao__ticket=ticket).count()}"
                )
                continue

            # 1. Cria a SR. O solicitante é o dono do ticket (não quem roda o
            #    comando): affectedpersonid/reportedby precisam continuar dele.
            sr = MaximoSenderService.criar_sr(ticket, ticket.cliente)
            if not sr:
                self.stdout.write(
                    self.style.ERROR("  FALHA ao criar a SR (ver log). Ticket intacto.")
                )
                falhas += 1
                continue

            maximo_id_antigo = ticket.maximo_id
            ticket.maximo_id = sr["ticketid"]
            ticket.save(update_fields=["maximo_id"])
            recriados += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"  SR recriada: {maximo_id_antigo or '-'} -> {ticket.maximo_id}"
                )
            )
            logger.info(
                f"Ticket #{ticket.id}: SR recriada no Maximo "
                f"({maximo_id_antigo or '-'} -> {ticket.maximo_id}) via recriar_sr_maximo."
            )

            # 2. Anexos da abertura -> DOCLINKS da SR nova (href vem na resposta).
            if not options["sem_anexos"]:
                doclinks_url = (sr.get("doclinks") or {}).get("href")
                if not doclinks_url and sr.get("href"):
                    doclinks_url = f'{sr["href"]}/doclinks'

                anexos_abertura = self._anexos_abertura(ticket)
                if anexos_abertura and doclinks_url:
                    ok = MaximoSenderService.enviar_anexos_criacao(
                        doclinks_url, anexos_abertura
                    )
                    Ticket.objects.filter(pk=ticket.id).update(anexos_sincronizados=ok)
                    estilo = self.style.SUCCESS if ok else self.style.ERROR
                    self.stdout.write(
                        estilo(
                            f"  Anexos da abertura: {len(anexos_abertura)} enviado(s), "
                            f"sucesso={ok}"
                        )
                    )
                elif anexos_abertura:
                    Ticket.objects.filter(pk=ticket.id).update(anexos_sincronizados=False)
                    self.stdout.write(
                        self.style.ERROR(
                            "  SR criada sem doclinks href; anexos da abertura NÃO enviados."
                        )
                    )

            # 3. Histórico do chat -> worklogs, em ordem cronológica.
            if not options["sem_worklogs"]:
                interacoes = ticket.interacoes.select_related("autor").order_by(
                    "data_criacao"
                )
                enviados, erros = 0, 0
                for interacao in interacoes:
                    if MaximoSenderService.enviar_interacao(ticket, interacao):
                        enviados += 1
                    else:
                        erros += 1
                if enviados or erros:
                    estilo = self.style.SUCCESS if not erros else self.style.ERROR
                    self.stdout.write(
                        estilo(f"  Worklogs: {enviados} enviado(s), {erros} falha(s)")
                    )

            # 4. Anexos do chat -> DOCLINKS. Regrava maximo_doclink_id: os ids
            #    antigos apontam para doclinks que sumiram junto com a SR.
            if not options["sem_anexos"]:
                anexos_chat = list(
                    InteracaoAnexo.objects.filter(interacao__ticket=ticket).order_by(
                        "data_envio"
                    )
                )
                if anexos_chat:
                    ok = MaximoSenderService.enviar_anexos(ticket, anexos_chat)
                    estilo = self.style.SUCCESS if ok else self.style.ERROR
                    self.stdout.write(
                        estilo(
                            f"  Anexos do chat: {len(anexos_chat)} enviado(s), sucesso={ok}"
                        )
                    )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"--- Fim: {recriados} recriado(s), {pulados} pulado(s), "
                f"{falhas} falha(s) ---"
            )
        )
        if recriados and not dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "As SRs novas nascem com status NEW e sem designação. O próximo "
                    "sincronizar_maximo trará esse status para o portal: reaplique "
                    "status e owner no Maximo antes de rodar o sync."
                )
            )

    @staticmethod
    def _anexos_abertura(ticket: Ticket) -> list:
        """Arquivos da abertura: documento de requisição + evidências (TicketAnexo)."""
        arquivos = []
        if ticket.documento_requisicao:
            arquivos.append(ticket.documento_requisicao)
        arquivos.extend(anexo.arquivo for anexo in ticket.anexos.all())
        return arquivos
