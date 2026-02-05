import logging
import requests
import re
import urllib3
import html
from datetime import datetime
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
    help = 'Importa Worklogs do IBM Maximo para o Chat do Portal'

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
                    "oslc.select": "ticketid,worklog{recordkey,createby,createdate,description,description_longdescription}",
                    "lean": 1
                }
                
                response = http.get(api_url, params=params, timeout=10) # verify já está na session
                
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
        """
        Limpa tags HTML, coment�rios do Maximo e converte quebras de linha
        para texto plano compat�vel com o chat (pre-wrap).
        """
        if not raw_html:
            return ""
        
        # 1. Remove coment�rios espec�ficos do Maximo ()
        # O flag re.IGNORECASE garante que pegue Rich Text, RICH TEXT, etc.
        texto = re.sub(r'', '', raw_html, flags=re.IGNORECASE)

        # 2. Converte tags de quebra de linha visual para quebra de linha de texto (\n)
        # Substitui <br>, <br/>, </p>, </div> por \n
        texto = re.sub(r'<(br\s*/?|/p|/div)>', '\n', texto, flags=re.IGNORECASE)

        # 3. Remove todas as outras tags HTML (ex: <b>, <span>, <font>)
        texto = strip_tags(texto)

        # 4. Decodifica entidades HTML (ex: &nbsp; vira espa�o, &gt; vira >)
        texto = html.unescape(texto)

        # 5. Limpeza final de espa�os extras nas pontas
        return texto.strip()
        
        # Simplificado: Retorna direto o resultado do regex, sem variável intermediária
        return re.sub(r'', '', raw_html, flags=re.DOTALL).strip()

    def _processar_logs(self, ticket, logs, bot_user) -> int:
        count = 0
        for log in logs:
            # Pega descrição (Longa tem prioridade)
            texto_bruto = log.get("description_longdescription") or log.get("description")
            
            # Chama a função simplificada
            msg_final_limpa = self._clean_html(texto_bruto)
            
            if not msg_final_limpa:
                continue

            autor = log.get("createby", "SUPORTE")
            mensagem_formatada = f"📋 [Log do Maximo - {autor}]\n\n{msg_final_limpa}"

            # Verifica se já existe (Idempotência)
            if TicketInteracao.objects.filter(ticket=ticket, mensagem=mensagem_formatada).exists():
                continue

            # Cria a interação
            interacao = TicketInteracao.objects.create(
                ticket=ticket,
                autor=bot_user,
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