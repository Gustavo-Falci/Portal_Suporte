import os
import django
import sys

# 1. Adiciona a pasta do projeto ao caminho do Python (caso não esteja)
sys.path.append('/home/ubuntu/portal_suporte/tickets/management/commands/sincronizar_maximo.py')

# 2. Aponta para o arquivo settings.py
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'portal_suporte.settings')

# 3. Inicia o Django
django.setup()

import requests
import logging
from django.core.management.base import BaseCommand
from django.conf import settings
from tickets.models import Ticket, MAXIMO_STATUS_CHOICES
from requests.adapters import HTTPAdapter, Retry


# Configuração de Log
logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Sincroniza status, ID e descrição dos tickets com o IBM Maximo'

    def handle(self, *args, **options):

        retry_strategy = Retry(
            total=3,
            backoff_factor=1, # Espera 1s, 2s, 4s...
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        http = requests.Session()
        http.mount("https://", adapter)
        http.mount("http://", adapter)

        API_URL = getattr(settings, 'MAXIMO_API_URL', None)
        API_KEY = getattr(settings, 'MAXIMO_API_KEY', None)
        
        # Parâmetros da API
        params = {
            "_dropnulls": 0,
            "lean": 1,
            "oslc.select": "TICKETID,DESCRIPTION,STATUS",
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
                verify=verify_ssl, # Controlado por settings
                timeout=30 # Timeout reduzido para não travar workers
            )
            response.raise_for_status()
            
            data = response.json()
            items = data.get('member', [])
            
            if not items:
                self.stdout.write("API Maximo retornou lista vazia.")
                return

            self.processar_tickets(items)

        except Exception as e:
            logger.error(f"Erro na sincronização: {e}")
            self.stdout.write(self.style.ERROR(f"Erro Crítico: {e}"))

    def processar_tickets(self, items: list) -> None:
        total_vinculados = 0
        total_status_alterados = 0
        
        # 1. Carrega tickets locais (exclui fechados)
        tickets_locais = Ticket.objects.exclude(status_maximo__in=['CLOSED', 'CANCELLED'])
        
        self.stdout.write(f"Tickets locais carregados para verificação: {tickets_locais.count()}")

        # 2. Indexação e Listas
        tickets_por_id = {}
        tickets_sem_id = [] # Lista mutável de tickets aguardando ID

        # Separa quem tem ID de quem não tem
        for t in tickets_locais:
            if t.maximo_id and t.maximo_id.strip():
                tickets_por_id[t.maximo_id.strip()] = t
            else:
                tickets_sem_id.append(t)

        # Debug: Listar tickets que estão esperando vínculo
        if tickets_sem_id:
            self.stdout.write("--- Tickets Locais aguardando vínculo (Sem ID) ---")
            for t in tickets_sem_id:
                self.stdout.write(f"   Local ID: {t.id} | Sumário: '{t.sumario}'")
            self.stdout.write("--------------------------------------------------")

        for item in items:
            mx_id = str(item.get('ticketid', ''))
            mx_status = item.get('status', '')
            mx_desc_raw = item.get('description', '')
            
            mx_desc_clean = mx_desc_raw.strip().lower()

            if not mx_id:
                continue

            # Lista para processar atualizações nesta iteração do Maximo
            tickets_para_processar = []

            # --- ESTRATÉGIA 1: ID do Maximo (Já vinculado) ---
            if mx_id in tickets_por_id:
                # Se já tem ID, adiciona à lista de processamento
                tickets_para_processar.append(tickets_por_id[mx_id])
            
            # --- ESTRATÉGIA 2: Busca por Texto (Múltiplos Vínculos) ---
            else:
                # Encontrar TODOS os tickets compatíveis, não apenas o primeiro
                matches_encontrados = []
                
                for t_local in tickets_sem_id:
                    local_sumario_clean = t_local.sumario.strip().lower()
                    
                    match_exato = (local_sumario_clean == mx_desc_clean)
                    match_parcial = (len(local_sumario_clean) > 5 and local_sumario_clean in mx_desc_clean)

                    if match_exato or match_parcial:
                        matches_encontrados.append(t_local)
                        
                        tipo_match = "EXATO" if match_exato else "PARCIAL"
                        self.stdout.write(self.style.SUCCESS(f"MATCH {tipo_match} ENCONTRADO para SR {mx_id}!"))
                        self.stdout.write(f"   Ticket Local #{t_local.id} ('{local_sumario_clean}')")

                # Processa os vínculos encontrados
                for t_match in matches_encontrados:
                    self._vincular_id(t_match, mx_id)
                    total_vinculados += 1
                    
                    # Adiciona à lista de processamento de status
                    tickets_para_processar.append(t_match)
                    
                    # Remove da lista principal para não processar novamente em outro loop
                    # (Importante: removemos da lista original 'tickets_sem_id')
                    if t_match in tickets_sem_id:
                        tickets_sem_id.remove(t_match)

            # --- PROCESSAMENTO DE ATUALIZAÇÃO (Para todos os tickets vinculados a este SR) ---
            for ticket in tickets_para_processar:
                # Verifica se houve alteração de status ou confirmação do ID
                if self._atualizar_ticket(ticket, mx_status, mx_id):
                    total_status_alterados += 1
                    self.stdout.write(f"Ticket #{ticket.id} [ATUALIZADO] -> Status: {mx_status} (SR {mx_id})")

        # Resumo Final
        msg_final = f"Sincronização concluída. Novos Vínculos: {total_vinculados} | Status Alterados: {total_status_alterados}"
        
        if total_vinculados > 0 or total_status_alterados > 0:
            self.stdout.write(self.style.SUCCESS(msg_final))
        else:
            self.stdout.write(msg_final)

    def _vincular_id(self, ticket: Ticket, novo_maximo_id: str):
        """Salva apenas o ID no banco."""
        logger.info(f"VINCULO: Ticket Local #{ticket.id} agora ligado ao Maximo ID {novo_maximo_id}")
        ticket.maximo_id = novo_maximo_id
        ticket.save(update_fields=['maximo_id'])

    def _atualizar_ticket(self, ticket: Ticket, novo_status: str, maximo_id: str) -> bool:
        """
        Verifica mudanças de Status e ID. 
        Retorna True se houve alteração salva no banco.
        """
        alterou = False

        # Verifica ID (redundância de segurança caso não tenha vindo do _vincular_id)
        if ticket.maximo_id != maximo_id:
            ticket.maximo_id = maximo_id
            alterou = True

        # Verifica Status
        if ticket.status_maximo != novo_status:
            # Validação se o status existe na lista do Django
            status_valido = any(choice[0] == novo_status for choice in MAXIMO_STATUS_CHOICES)
            
            if status_valido:
                ticket.status_maximo = novo_status
                alterou = True
            else:
                logger.warning(f"Status desconhecido recebido do Maximo: '{novo_status}' para ticket #{ticket.id}. Ignorado.")

        if alterou:
            ticket.save()
        
        return alterou