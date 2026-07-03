import requests
import logging
from requests.adapters import HTTPAdapter, Retry
from datetime import datetime, timedelta
from django.utils import timezone

from django.core.management.base import BaseCommand
from django.conf import settings
from tickets.models import Ticket

# Configuração de Log
logger = logging.getLogger(__name__)

# Guarda de match por texto
MATCH_BUFFER = timedelta(minutes=5)          # tolera clock skew portal<->Maximo

class Command(BaseCommand):
    help = 'Sincroniza status, ID e OWNER dos tickets com o IBM Maximo'

    def handle(self, *args, **options):
        # --- Configuração de Conexão e Retry ---
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
            self.stdout.write(self.style.ERROR("ERRO: MAXIMO_API_URL ou MAXIMO_API_KEY não configurados no settings."))
            return
        
        # Parâmetros da API
        params = {
            "_dropnulls": 0,
            "lean": 1,
            "oslc.select": "TICKETID,DESCRIPTION,STATUS,OWNER,REPORTDATE",
        }

        headers = {
            "apikey": API_KEY,
            "Content-Type": "application/json"
        }

        self.stdout.write("--- Iniciando Sincronização (Modo Debug) ---")
        logger.info("Início sincronização Maximo")

        try:
            verify_ssl = getattr(settings, 'MAXIMO_VERIFY_SSL', True)
            
            response = http.get(
                API_URL, 
                params=params, 
                headers=headers, 
                verify=verify_ssl,
                timeout=30
            )
            response.raise_for_status()
            
            data = response.json()
            items = data.get('member', [])
            
            if not items:
                self.stdout.write("API Maximo retornou lista vazia ou nenhum ticket encontrado.")
                logger.info("Fim sincronização Maximo: API retornou lista vazia")
                return

            self.processar_tickets(items)

        except Exception as e:
            logger.error(f"Erro na sincronização: {e}")
            self.stdout.write(self.style.ERROR(f"Erro Crítico: {e}"))

    def processar_tickets(self, items: list) -> None:
        total_vinculados = 0
        total_alterados = 0
        
        # 1. Carrega tickets locais (exclui fechados definitivamente no Django)
        # Ajuste conforme seus status locais finais se houver
        tickets_locais = Ticket.objects.exclude(status_maximo='CLOSED')
        
        self.stdout.write(f"Tickets locais carregados para verificação: {tickets_locais.count()}")

        # 2. Indexação e Listas
        tickets_por_id = {}
        tickets_sem_id = [] 

        for t in tickets_locais:
            if t.maximo_id and t.maximo_id.strip():
                tickets_por_id[t.maximo_id.strip()] = t
            else:
                tickets_sem_id.append(t)

        for item in items:
            mx_id = str(item.get('ticketid', ''))
            mx_desc_clean = item.get('description', '').strip().lower()
            mx_reportdate = self._parse_maximo_date(item.get('reportdate'))

            if not mx_id:
                continue

            tickets_para_processar = []

            if mx_id in tickets_por_id:
                # Já vinculado: atualiza sempre (inclusive fechamento legítimo)
                tickets_para_processar.append(tickets_por_id[mx_id])

            # Descoberta por texto (ticket sem maximo_id vinculado)
            else:
                matches_encontrados = []

                for t_local in list(tickets_sem_id):
                    local_sumario_clean = t_local.sumario.strip().lower()

                    # Só match EXATO (substring removido: ímã de falso-positivo)
                    if local_sumario_clean != mx_desc_clean:
                        continue

                    # Guarda de data: SR não pode preceder a criação do ticket
                    if mx_reportdate is None:
                        self.stdout.write(f"   SKIP SR {mx_id}: sem reportdate parseável")
                        continue
                    if mx_reportdate < t_local.data_criacao - MATCH_BUFFER:
                        self.stdout.write(
                            f"   SKIP SR {mx_id}: reportdate {mx_reportdate.isoformat()} "
                            f"anterior à criação do ticket #{t_local.id}"
                        )
                        continue

                    matches_encontrados.append(t_local)
                    self.stdout.write(self.style.SUCCESS(f"MATCH EXATO ENCONTRADO para SR {mx_id}!"))
                    self.stdout.write(f"   Ticket Local #{t_local.id} ('{local_sumario_clean}')")

                for t_match in matches_encontrados:
                    self._vincular_id(t_match, mx_id)
                    total_vinculados += 1
                    tickets_para_processar.append(t_match)
                    if t_match in tickets_sem_id:
                        tickets_sem_id.remove(t_match)

            for ticket in tickets_para_processar:
                if self._atualizar_ticket(ticket, item):
                    total_alterados += 1
                    self.stdout.write(f"Ticket #{ticket.id} [ATUALIZADO] SR {mx_id}")

        msg_final = f"Sincronização concluída. Novos Vínculos: {total_vinculados} | Tickets Alterados: {total_alterados}"
        logger.info(f"Fim sincronização Maximo: {msg_final}")

        if total_vinculados > 0 or total_alterados > 0:
            self.stdout.write(self.style.SUCCESS(msg_final))
        else:
            self.stdout.write(msg_final)

    def _parse_maximo_date(self, raw) -> "datetime | None":
        """Parseia reportdate do Maximo para datetime aware. None se inválido/vazio."""
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw))
        except (ValueError, TypeError):
            return None
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt)
        return dt

    def _vincular_id(self, ticket: Ticket, novo_maximo_id: str):

        """Salva apenas o ID no banco."""
        logger.info(f"VINCULO: Ticket Local #{ticket.id} agora ligado ao Maximo ID {novo_maximo_id}")
        ticket.maximo_id = novo_maximo_id
        
        # Importante salvar para refletir no objeto em memória se necessário depois
        ticket.save(update_fields=['maximo_id'])

    def _atualizar_ticket(self, ticket: Ticket, item_api: dict) -> bool:
        """
        Verifica mudanças de Status e OWNER.
        Retorna True se houve alteração salva no banco.
        """
        alterou = False
        
        # 1. Extração Segura dos Dados
        novo_status = item_api.get("status")
        novo_owner = item_api.get("owner") 

        # Tratamento para garantir string vazia se for None
        if novo_owner is None:
            novo_owner = ""

        # 2. Verifica Status
        if ticket.status_maximo != novo_status and novo_status:
            logger.info(f"STATUS Ticket #{ticket.id}: {ticket.status_maximo} -> {novo_status}")
            ticket.status_maximo = novo_status
            alterou = True

        # 3. Verifica Owner (Proprietário)
        if ticket.owner != novo_owner:
            logger.info(f"OWNER Ticket #{ticket.id}: '{ticket.owner}' -> '{novo_owner}'")
            ticket.owner = novo_owner
            alterou = True

        # 4. Salva APENAS se houve alteração
        if alterou:
            ticket.save()
            
        return alterou