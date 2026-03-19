import logging
import requests
import re
import urllib3
import html
from django.core.management.base import BaseCommand
from django.conf import settings
from django.utils.html import strip_tags
from django.contrib.auth import get_user_model
from django.utils.dateparse import parse_datetime
from requests.adapters import HTTPAdapter, Retry
from tickets.models import Ticket, TicketInteracao

# 1. Silenciar erros de SSL e Avisos
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)
User = get_user_model()

class Command(BaseCommand):
    help = 'Importa Worklogs do IBM Maximo para o Chat do Portal (Apenas CLIENTNOTE)'

    def handle(self, *args, **options):
        self.stdout.write("--- Iniciando Importação de Logs do Maximo ---")
        
        # 2. Configurar Sessão HTTP (SSL Desativado e Sem Proxy)
        retry_strategy = Retry(
            total=3, 
            backoff_factor=1, 
            status_forcelist=[429, 500, 502, 503, 504]
        )
        
        http = requests.Session()
        http.verify = False       # Desativa verificação SSL na sessão
        http.trust_env = False    # Ignora proxies do sistema (importante para .testing)
        
        http.mount("https://", HTTPAdapter(max_retries=retry_strategy))
        http.mount("http://", HTTPAdapter(max_retries=retry_strategy))
        
        http.headers.update({
            "apikey": getattr(settings, 'MAXIMO_API_KEY', ''),
            "Content-Type": "application/json",
            "Properties": "*"
        })

        api_url = getattr(settings, 'MAXIMO_API_URL', '')

        # 3. Obter Usuário Bot
        bot_user = self._get_system_user()

        # 4. Buscar Tickets Locais
        tickets = Ticket.objects.exclude(maximo_id__isnull=True).exclude(maximo_id='')
        
        total_importado = 0

        for ticket in tickets:
            try:
                # Busca logs específicos deste ticket
                params = {
                    "oslc.where": f'ticketid="{ticket.maximo_id}"',
                    # ALTERADO: Adicionado 'logtype' na solicitação
                    "oslc.select": "ticketid,worklog{recordkey,createby,modifyby,logtype,createdate,description,description_longdescription}",
                    "lean": 1
                }
                
                response = http.get(api_url, params=params, timeout=10)
                
                if response.status_code == 200:
                    data = response.json()
                    members = data.get('member', [])
                    
                    if members:
                        # Pega logs do primeiro item
                        worklogs = members[0].get('worklog', [])
                        count = self._processar_logs(ticket, worklogs, bot_user)
                        total_importado += count
                        if count > 0:
                            self.stdout.write(f"Ticket #{ticket.maximo_id}: {count} novos logs.")
                else:
                    # Aviso silencioso no log, não polui terminal
                    logger.warning(f"Ticket {ticket.maximo_id}: HTTP {response.status_code}")

            except Exception as e:
                # Mostra erro mas continua o loop
                self.stderr.write(f"Erro no ticket {ticket.maximo_id}: {e}")

        self.stdout.write(self.style.SUCCESS(f"--- Fim. Total importado: {total_importado} ---"))

    def _get_system_user(self):
        """Cria ou recupera o usuário robô"""
        email_bot = "maximo.integracao@itconsol.com"
        user, created = User.objects.get_or_create(
            email=email_bot,
            defaults={
                'username': email_bot,
                'first_name': 'Maximo',
                'last_name': 'System',
                'is_staff': True,
                'is_active': False
            }
        )
        return user

    def _clean_html(self, raw_html: str) -> str:
        if not raw_html:
            return ""
        
        # 1. Remove comentários específicos do Maximo
        texto = re.sub(r'', '', raw_html, flags=re.IGNORECASE)

        # 2. Converte tags de quebra de linha visual para quebra de linha de texto (\n)
        texto = re.sub(r'<(br\s*/?|/p|/div)>', '\n', texto, flags=re.IGNORECASE)

        # 3. Remove todas as outras tags HTML
        texto = strip_tags(texto)

        # 4. Decodifica entidades HTML
        texto = html.unescape(texto)

        # 5. Limpeza final de espaços extras nas pontas
        return texto.strip()

    def _processar_logs(self, ticket, logs, bot_user) -> int:
        count = 0
        # Cache local de usuários para evitar várias idas ao banco de dados no loop
        user_cache = {}
        
        for log in logs:
            
            tipo = log.get("logtype", "").upper()
            autor_criacao_log = log.get("modifyby", "SUPORTE").upper()
            autor = log.get("createby", "").upper()

            
            if tipo != "CLIENTNOTE" or autor_criacao_log == "MXINTADM":
                continue

            # Pega descrição (Longa tem prioridade)
            texto_bruto = log.get("description_longdescription") or log.get("description")
            
            # Chama a função simplificada de limpeza
            msg_final_limpa = self._clean_html(texto_bruto)
            
            if not msg_final_limpa:
                continue

            # Verifica se o 'autor' do Maximo possui vínculo com um usuário do Portal via person_id
            if autor not in user_cache:
                user_cache[autor] = User.objects.filter(person_id__iexact=autor).first()
            
            usuario_vinculado = user_cache[autor]
            
            if usuario_vinculado:
                autor_interacao = usuario_vinculado
                mensagem_formatada = msg_final_limpa
            else:
                autor_interacao = bot_user
                mensagem_formatada = f"📋 [{autor}]\n\n{msg_final_limpa}"

            # Verifica se já existe (Idempotência considerando o formato antigo e o novo)
            mensagem_legada = f"📋 [{autor}]\n\n{msg_final_limpa}"
            if TicketInteracao.objects.filter(ticket=ticket, mensagem__in=[mensagem_formatada, mensagem_legada]).exists():
                continue

            # Cria a interação
            interacao = TicketInteracao.objects.create(
                ticket=ticket,
                autor=autor_interacao,
                mensagem=mensagem_formatada,
                anexo=None
            )

            # Ajusta a data retroativa
            data_str = log.get("createdate")
            if data_str:
                data_log = parse_datetime(data_str)
                if data_log:
                    TicketInteracao.objects.filter(pk=interacao.pk).update(data_criacao=data_log)
            
            count += 1
        return count