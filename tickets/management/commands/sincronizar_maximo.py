import os
import django
import sys
import requests
import logging
from requests.adapters import HTTPAdapter, Retry

# 1. Setup do Ambiente (Caso rode via Crontab/Script direto)
sys.path.append('/home/ubuntu/portal_suporte/tickets/management/commands/sincronizar_maximo.py')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'portal_suporte.settings')
django.setup()

from django.core.management.base import BaseCommand
from django.conf import settings
from tickets.models import Ticket

# Configuração de Log
logger = logging.getLogger(__name__)

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
            "oslc.select": "TICKETID,DESCRIPTION,STATUS,OWNER", 
        }

        headers = {
            "apikey": API_KEY,
            "Content-Type": "application/json"
        }

        self.stdout.write("--- Iniciando Sincronização (Modo Debug) ---")

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
            mx_desc_raw = item.get('description', '')
            mx_desc_clean = mx_desc_raw.strip().lower()

            if not mx_id:
                continue

            tickets_para_processar = []

            if mx_id in tickets_por_id:
                tickets_para_processar.append(tickets_por_id[mx_id])
            
            else:
                matches_encontrados = []
                
                # Iteramos sobre uma cópia ou cuidamos na remoção
                for t_local in list(tickets_sem_id): 
                    local_sumario_clean = t_local.sumario.strip().lower()
                    
                    match_exato = (local_sumario_clean == mx_desc_clean)
                    # Aumentei o filtro parcial para evitar falsos positivos em palavras curtas
                    match_parcial = (len(local_sumario_clean) > 10 and local_sumario_clean in mx_desc_clean)

                    if match_exato or match_parcial:
                        matches_encontrados.append(t_local)
                        tipo_match = "EXATO" if match_exato else "PARCIAL"
                        self.stdout.write(self.style.SUCCESS(f"MATCH {tipo_match} ENCONTRADO para SR {mx_id}!"))
                        self.stdout.write(f"   Ticket Local #{t_local.id} ('{local_sumario_clean}')")

                # Vincula os encontrados
                for t_match in matches_encontrados:
                    self._vincular_id(t_match, mx_id)
                    total_vinculados += 1
                    
                    tickets_para_processar.append(t_match)
                    
                    # Remove da lista de sem_id para não processar de novo
                    if t_match in tickets_sem_id:
                        tickets_sem_id.remove(t_match)

            for ticket in tickets_para_processar:
                if self._atualizar_ticket(ticket, item):
                    total_alterados += 1
                    self.stdout.write(f"Ticket #{ticket.id} [ATUALIZADO] SR {mx_id}")

        msg_final = f"Sincronização concluída. Novos Vínculos: {total_vinculados} | Tickets Alterados: {total_alterados}"
        
        if total_vinculados > 0 or total_alterados > 0:
            self.stdout.write(self.style.SUCCESS(msg_final))
        else:
            self.stdout.write(msg_final)

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