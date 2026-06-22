import logging

import requests
from requests.adapters import HTTPAdapter, Retry

from django.core.management.base import BaseCommand
from django.conf import settings

from tickets.models import Ticket
from tickets.management.commands.sincronizar_maximo import Command as SyncCommand, MATCH_BUFFER

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Audita (somente leitura) vínculos suspeitos entre tickets locais e SRs "
        "do Maximo. Sinaliza tickets cujo SR vinculado tem reportdate anterior à "
        "criação do ticket (assinatura de vínculo legado errado) e sugere o SR correto."
    )

    def handle(self, *args, **options):
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        http = requests.Session()
        http.mount("https://", adapter)
        http.mount("http://", adapter)

        API_URL = getattr(settings, 'MAXIMO_API_URL', None)
        API_KEY = getattr(settings, 'MAXIMO_API_KEY', None)

        if not API_URL or not API_KEY:
            self.stdout.write(self.style.ERROR("ERRO: MAXIMO_API_URL ou MAXIMO_API_KEY não configurados."))
            return

        params = {
            "_dropnulls": 0,
            "lean": 1,
            "oslc.select": "TICKETID,DESCRIPTION,STATUS,REPORTDATE",
            "oslc.pageSize": 1000,
        }
        headers = {"apikey": API_KEY, "Content-Type": "application/json"}

        self.stdout.write("--- Auditoria de Vínculos Maximo (somente leitura) ---")

        try:
            verify_ssl = getattr(settings, 'MAXIMO_VERIFY_SSL', True)
            response = http.get(API_URL, params=params, headers=headers, verify=verify_ssl, timeout=60)
            response.raise_for_status()
            items = response.json().get('member', [])
        except Exception as e:
            logger.error(f"Erro na auditoria: {e}")
            self.stdout.write(self.style.ERROR(f"Erro Crítico ao buscar API: {e}"))
            return

        if not items:
            self.stdout.write("API retornou lista vazia. Nada a auditar.")
            return

        self.auditar(items)

    def auditar(self, items: list) -> None:
        parse = SyncCommand()._parse_maximo_date

        # Índices da API
        sr_por_id = {}            # ticketid -> item
        itens_por_desc = {}       # description.lower() -> [item, ...]
        for item in items:
            sr_id = str(item.get('ticketid', '')).strip()
            if not sr_id:
                continue
            sr_por_id[sr_id] = item
            desc = (item.get('description') or '').strip().lower()
            itens_por_desc.setdefault(desc, []).append(item)

        # Tickets locais já vinculados
        vinculados = Ticket.objects.exclude(maximo_id__isnull=True).exclude(maximo_id='')
        self.stdout.write(f"Tickets vinculados analisados: {vinculados.count()}\n")

        suspeitos = 0
        sr_nao_encontrado = 0

        for t in vinculados:
            mx_id = t.maximo_id.strip()
            sr = sr_por_id.get(mx_id)

            if sr is None:
                sr_nao_encontrado += 1
                self.stdout.write(self.style.WARNING(
                    f"[? SR ausente] Ticket #{t.id} '{t.sumario}' -> SR {mx_id} não veio na API "
                    f"(pode estar paginado ou ter sido removido)."
                ))
                continue

            sr_reportdate = parse(sr.get('reportdate'))
            if sr_reportdate is None:
                self.stdout.write(self.style.WARNING(
                    f"[? sem data] Ticket #{t.id} '{t.sumario}' -> SR {mx_id} sem reportdate parseável."
                ))
                continue

            # Assinatura do vínculo legado errado: SR criado ANTES do ticket (− buffer)
            if sr_reportdate >= t.data_criacao - MATCH_BUFFER:
                continue  # vínculo coerente, pula

            suspeitos += 1
            self.stdout.write(self.style.ERROR(
                f"\n[SUSPEITO] Ticket #{t.id} '{t.sumario}' (status local: {t.status_maximo})"
            ))
            self.stdout.write(
                f"   criado:       {t.data_criacao.isoformat()}"
            )
            self.stdout.write(
                f"   SR vinculado: {mx_id} | status {sr.get('status')} | "
                f"reportdate {sr_reportdate.isoformat()}  <-- ANTERIOR à criação"
            )

            # Sugere o SR correto: mesmo nome, reportdate >= criação - buffer, mais próximo
            candidatos = [
                c for c in itens_por_desc.get(t.sumario.strip().lower(), [])
                if (rd := parse(c.get('reportdate'))) is not None
                and rd >= t.data_criacao - MATCH_BUFFER
            ]
            candidatos.sort(key=lambda c: parse(c.get('reportdate')))

            if candidatos:
                melhor = candidatos[0]
                melhor_id = str(melhor.get('ticketid')).strip()
                self.stdout.write(self.style.SUCCESS(
                    f"   -> SR correto provável: {melhor_id} | status {melhor.get('status')} | "
                    f"reportdate {parse(melhor.get('reportdate')).isoformat()}"
                ))
                self.stdout.write(
                    f"      corrigir:  Ticket.objects.filter(id={t.id}).update("
                    f"maximo_id='{melhor_id}', status_maximo='{melhor.get('status')}')"
                )
            else:
                self.stdout.write(self.style.WARNING(
                    "   -> Nenhum SR de mesmo nome com reportdate compatível. Revisar manualmente."
                ))

        # Resumo
        self.stdout.write("\n--- Resumo ---")
        self.stdout.write(self.style.ERROR(f"Vínculos SUSPEITOS (legado): {suspeitos}"))
        if sr_nao_encontrado:
            self.stdout.write(self.style.WARNING(f"SRs ausentes na API: {sr_nao_encontrado}"))
        if suspeitos == 0:
            self.stdout.write(self.style.SUCCESS("Nenhum vínculo suspeito encontrado."))
