import logging
import json
import requests
import os
import mimetypes
from urllib.parse import urlparse
from django.core.mail import EmailMessage
from django.conf import settings
from .models import Ticket, TicketInteracao, Cliente, Notificacao
from django.urls import reverse
from django.db.models import Q
from django.utils.html import strip_tags, escape

logger = logging.getLogger(__name__)


class MaximoEmailService:

    @staticmethod
    def gerar_corpo_maximo(ticket: Ticket, usuario: Cliente) -> str:

        """
        Gera o corpo técnico exigido pelo Maximo Listener.
        """

        descricao_limpa = strip_tags(ticket.descricao).replace('\n', '<br>')
        sumario_limpo = strip_tags(ticket.sumario)
        prioridade = ticket.prioridade
        asset_num = ticket.ambiente.numero_ativo if ticket.ambiente else ""

        corpo = f"Descrição do problema: {descricao_limpa}<br><br>"
        corpo += "#MAXIMO_EMAIL_BEGIN<br>"
        corpo += f"SR#DESCRIPTION={sumario_limpo}<br>;<br>"
        corpo += f"SR#ASSETNUM={asset_num}<br>;<br>"
        corpo += f"SR#REPORTEDPRIORITY={prioridade}<br>;<br>"

        if ticket.area:
            corpo += f"SR#ITC_AREA={ticket.area.nome_area}<br>;<br>"

        location = getattr(usuario, "location", None)
        if location:
            corpo += f"SR#LOCATION={location}<br>;<br>"

        person_id = getattr(usuario, "person_id", None)
        if person_id:
            corpo += f"SR#AFFECTEDPERSONID={person_id}<br>;<br>"

        corpo += """
        SR#SITEID=ITCBR<br>;<br>
        LSNRACTION=CREATE<br>;<br>
        LSNRAPPLIESTO=SR<br>;<br>
        SR#CLASS=SR<br>;<br>
        SR#TICKETID=&AUTOKEY&<br>;<br>
        #MAXIMO_EMAIL_END<br><br>
        """
        return corpo

    @classmethod
    def enviar_ticket_maximo(
        cls, ticket: Ticket, usuario: Cliente, arquivos_upload: list | None = None
    ):
        
        """
        Orquestra o envio do e-mail de abertura para o Maximo.
        Agora suporta uma lista de múltiplos anexos.
        """

        destinatario = settings.EMAIL_DESTINATION
        remetente = settings.DEFAULT_FROM_EMAIL

        corpo_email = cls.gerar_corpo_maximo(ticket, usuario)

        email = EmailMessage(
            subject=f"Novo Ticket - {ticket.sumario}",
            body=corpo_email,
            from_email=remetente,
            to=[destinatario],
            reply_to=[usuario.email],
        )
        email.content_subtype = "html"

        if arquivos_upload:
            for arquivo in arquivos_upload:
                try:
                    # 1. Abre o arquivo salvo fisicamente em modo de leitura binária ('rb')
                    arquivo.open('rb')
                    arquivo.seek(0)
                    
                    # 2. Pega apenas o nome do arquivo final, ignorando o caminho da pasta
                    # (Ex: em vez de 'tickets/2026/arquivo.docx', fica só 'arquivo.docx')
                    nome = os.path.basename(arquivo.name)
                    
                    # 3. Lê os bytes do arquivo
                    conteudo = arquivo.read()
                    
                    # 4. Anexa ao e-mail (O próprio Django infere o content_type pelo nome do arquivo)
                    email.attach(nome, conteudo)
                    
                except Exception as e:
                    logger.error(f"Erro ao anexar arquivo '{getattr(arquivo, 'name', '?')}' no service: {e}")
                
                finally:

                    # 5. Segurança: Fecha o arquivo para liberar memória do servidor
                    if hasattr(arquivo, 'closed') and not arquivo.closed:
                        arquivo.close()

        try:
            email.send()
            logger.info(f"E-mail de abertura enviado com sucesso para {destinatario} (Ticket {ticket.id})")

        except Exception as e:
            logger.error(
                f"Erro crítico ao enviar e-mail para Maximo (Ticket {ticket.id}) [TO: {destinatario}]: {e}"
            )
            raise e


class NotificationService:

    """
    Responsabilidade Única: Centralizar a comunicação com humanos.
    Gerencia notificações internas (Sino/Banco) e envios de E-mail (SMTP)
    para mudanças de status e novas mensagens no chat.
    """

    @staticmethod
    def _enviar_email_generico(destinatarios: list, assunto: str, corpo_html: str):

        """
        Método auxiliar privado para evitar repetição de código de envio de e-mail.
        """

        if not destinatarios:
            return

        try:
            email = EmailMessage(
                subject=assunto,
                body=corpo_html,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=destinatarios,
            )
            email.content_subtype = "html"  # Define que o corpo é HTML
            email.send()

        except Exception as e:
            logger.error(f"Erro ao enviar notificação por e-mail: {e}")

    @classmethod
    def notificar_mudanca_status(cls, ticket: Ticket, status_anterior_display: str):

        """
        Notifica o Cliente quando o status do chamado muda.
        1. Cria notificação interna.
        2. Envia e-mail.
        """

        status_novo = ticket.get_status_maximo_display()

        # 1. Notificação Interna (Sino)
        Notificacao.objects.create(
            destinatario=ticket.cliente,
            ticket=ticket,
            titulo="Status Atualizado",
            tipo="status",
            mensagem=f"O chamado agora está: {status_novo}",
            link=reverse("tickets:detalhe_ticket", kwargs={"pk": ticket.pk}),
        )

        # 2. Preparar e Envio de E-mail
        link_relativo = reverse("tickets:detalhe_ticket", kwargs={"pk": ticket.pk})
        base_url = getattr(settings, "SITE_URL", "http://localhost:8000").rstrip("/")
        full_link = f"{base_url}{link_relativo}"
        
        assunto = f"[Atualização] Ticket #{ticket.maximo_id} mudou para {status_novo}"

        # Escapa tudo que deriva de dados do usuário/banco antes de injetar no HTML
        nome_cliente = escape(ticket.cliente.first_name or ticket.cliente.username)
        status_ant_safe = escape(status_anterior_display)
        status_novo_safe = escape(status_novo)

        corpo = f"""
        Olá, {nome_cliente}.<br><br>

        O status do seu chamado <strong>#{escape(str(ticket.maximo_id))}</strong> foi atualizado.<br><br>

        <div style="border: 1px solid #ccc; padding: 15px; background-color: #f4f4f4;">
            <p><strong>De:</strong> <span style="color: #666;">{status_ant_safe}</span></p>
            <p><strong>Para:</strong> <span style="color: #0f62fe; font-weight: bold;">{status_novo_safe}</span></p>
        </div>
        <br>
        <a href="{full_link}">Clique aqui para acessar o portal e ver os detalhes.</a>
        """

        cls._enviar_email_generico([ticket.cliente.email], assunto, corpo)

    @classmethod
    def notificar_nova_interacao(cls, ticket: Ticket, interacao: TicketInteracao):

        """
        Envia notificação apenas para os envolvidos:
        1. Cliente dono do ticket.
        2. Consultor responsável (Owner do Ticket).
        3. Membros do grupo 'lider_suporte'.
        * O autor da mensagem nunca é notificado.
        """

        try:

            # 1. Identificar Destinatários (Set para evitar duplicatas)
            destinatarios = set()

            # A. Adiciona o Cliente (Dono do Ticket)
            if ticket.cliente and ticket.cliente.email:
                destinatarios.add(ticket.cliente)

            # B. Adiciona o Consultor Responsável (Owner)
            # O campo ticket.owner é uma string (PersonID). Precisamos do objeto Cliente/User.
            if ticket.owner:

                # Busca Case-Insensitive pelo person_id
                consultor = Cliente.objects.filter(person_id__iexact=ticket.owner).first()
                if consultor and consultor.email:
                    destinatarios.add(consultor)

            # C. Adiciona o Grupo de Líderes
            lideres = Cliente.objects.filter(groups__name="lider_suporte")
            for lider in lideres:
                if lider.email:
                    destinatarios.add(lider)

            # D. Remove o Autor da Mensagem (quem mandou não deve receber notificação)
            if interacao.autor in destinatarios:
                destinatarios.remove(interacao.autor)

            # 2. Preparar Conteúdo
            autor_nome = interacao.autor.get_full_name() or interacao.autor.username
            preview_msg = f"{autor_nome}: {interacao.mensagem[:60]}..."
            assunto = f"[Portal Suporte] Nova mensagem no Ticket #{ticket.maximo_id or ticket.id}"

            # Versões escapadas p/ injeção segura no corpo HTML do e-mail
            autor_nome_safe = escape(autor_nome)
            sumario_safe = escape(ticket.sumario)
            ticket_ref_safe = escape(str(ticket.maximo_id or ticket.id))
            mensagem_safe = escape(interacao.mensagem).replace("\n", "<br>")
            
            # Link para o ticket
            link_relativo = reverse("tickets:detalhe_ticket", kwargs={"pk": ticket.pk})

            # Pegamos a URL do settings e removemos qualquer barra no final (rstrip) para evitar //
            base_url = getattr(settings, "SITE_URL", "http://localhost:8000").rstrip("/") 

            full_link = f"{base_url}{link_relativo}"

            notificacoes_db = []

            # 3. Loop de Envio
            for usuario in destinatarios:
                # --- A. Notificação Interna (Sino) ---
                notificacoes_db.append(
                    Notificacao(
                        destinatario=usuario,
                        ticket=ticket,
                        titulo="Nova Mensagem",
                        tipo="mensagem",
                        mensagem=preview_msg,
                        link=link_relativo,
                    )
                )

                # --- B. Envio de E-mail ---
                nome_dest_safe = escape(usuario.first_name or usuario.username)
                corpo_email = f"""
                Olá, {nome_dest_safe}.<br><br>
                Houve uma nova interação no ticket <strong>#{ticket_ref_safe}</strong> - {sumario_safe}.<br><br>

                <div style="background-color: #f4f4f4; padding: 15px; border-left: 4px solid #0f62fe;">
                    <strong>{autor_nome_safe}:</strong><br>
                    {mensagem_safe}
                </div>
                <br>
                <a href="{full_link}">Clique aqui para responder no portal</a>
                """
                
                # Envia individualmente para cada destinatário
                cls._enviar_email_generico([usuario.email], assunto, corpo_email)

            # 4. Grava notificações no banco em lote
            if notificacoes_db:
                Notificacao.objects.bulk_create(notificacoes_db)

        except Exception as e:
            logger.error(f"Erro no fluxo de notificação (Ticket {ticket.id}): {e}")


class MaximoSenderService:

    """
    Serviço responsável por enviar interações do Portal para o IBM Maximo (Worklogs).
    """
    
    # URL configurada conforme seu POSTMAN
    MAXIMO_API_URL = getattr(settings, 'MAXIMO_API_URL_LOG', '')

    @staticmethod
    def enviar_interacao(ticket: Ticket, interacao: TicketInteracao) -> bool:

        """
        Envia uma nova mensagem do chat para o Worklog do Maximo.
        Gatilho: Botão 'Enviar' no detalhe do ticket.
        """

        if not ticket.maximo_id:
            logger.warning(f"Tentativa de envio para Maximo falhou: Ticket {ticket.id} não possui maximo_id.")
            return False

        # 1. Definição do Tipo de Log e Autor
        # Regra: Se for Staff, Support Team, Grupo Consultores OU Grupo Lider Suporte -> WORK

        eh_interno = (
            interacao.autor.is_staff or 
            getattr(interacao.autor, 'is_support_team', False) or
            interacao.autor.groups.filter(name__in=["Consultores", "lider_suporte"]).exists()
        )

        if eh_interno:
            log_type = "WORK"
            descricao_curta = "Nota do Consultor"
        else:
            log_type = "CLIENTNOTE"
            descricao_curta = "Mensagem do Cliente"

        # O createby no Maximo aceita string livre nesta integração
        # Usamos o nome completo ou o email (username)
        autor_nome = interacao.autor.get_full_name() or interacao.autor.username

        # 2. Montagem do Payload JSON
        payload = {
            "ticketid": str(ticket.maximo_id),
            "class": "SR", # Obrigatório conforme regra
            "worklog": [
                {
                    "description": descricao_curta,
                    "description_longdescription": interacao.mensagem,
                    "logtype": log_type,
                    "createby": autor_nome.upper(), # Maximo costuma gostar de UPPERCASE
                }
            ]
        }

        # 3. Configuração de Headers
        headers = {
            "Content-Type": "application/json",
            "x-method-override": "SYNC", 
            "patchtype": "MERGE",
            "apikey": getattr(settings, 'MAXIMO_API_KEY', ''),
        }

        try:
            logger.info(f"Enviando Worklog para Ticket Maximo #{ticket.maximo_id}...")
            
            
            response = requests.post(
                MaximoSenderService.MAXIMO_API_URL,
                data=json.dumps(payload),
                headers=headers,
                verify=False, # Ignora SSL conforme ambiente de teste
                timeout=10
            )

            if response.status_code in [200, 201, 204]:
                logger.info(f"Sucesso envio Maximo: {response.status_code}")
                return True
            else:
                logger.error(f"Erro Maximo API ({response.status_code}): {response.text}")
                return False

        except Exception as e:
            logger.error(f"Exceção ao conectar com Maximo: {e}")
            return False

    # URL base do Object Structure (sem ?lean=1) usada para localizar a SR
    MAXIMO_API_URL_OS = getattr(settings, 'MAXIMO_API_URL', '')

    @staticmethod
    def _get_member_href(maximo_id: str, apikey: str) -> str | None:

        """
        Localiza o href (rest id) do registro da SR no Maximo a partir do ticketid.
        Necessário para montar a URL de doclinks (anexos).
        """

        base_url = MaximoSenderService.MAXIMO_API_URL_OS
        if not base_url:
            logger.error("MAXIMO_API_URL não configurada; impossível localizar SR para anexo.")
            return None

        params = {
            "oslc.where": f'ticketid="{maximo_id}"',
            "oslc.select": "href",
            "lean": 1,
        }
        headers = {"apikey": apikey, "Accept": "application/json"}

        try:
            resp = requests.get(base_url, params=params, headers=headers, verify=False, timeout=10)
            if resp.status_code != 200:
                logger.error(f"Erro ao localizar SR #{maximo_id} ({resp.status_code}): {resp.text}")
                return None

            data = resp.json()
            membros = data.get("member") or data.get("rdfs:member") or []
            if not membros:
                logger.error(f"SR #{maximo_id} não encontrada no Maximo para envio de anexo.")
                return None

            href = membros[0].get("href")
            if href and not href.startswith("http"):
                p = urlparse(base_url)
                href = f"{p.scheme}://{p.netloc}/{href.lstrip('/')}"
            return href

        except Exception as e:
            logger.error(f"Exceção ao localizar SR #{maximo_id} no Maximo: {e}")
            return None

    @classmethod
    def enviar_anexos(cls, ticket: Ticket, anexos: list) -> bool:

        """
        Envia anexos do chat (InteracaoAnexo) para os DOCLINKS da SR no Maximo.
        Fluxo: localiza href da SR -> POST de cada arquivo (bytes raw) em /doclinks.
        Roda em segundo plano (thread) para não bloquear o usuário.
        """

        if not ticket.maximo_id:
            logger.warning(f"Envio de anexo abortado: Ticket {ticket.id} sem maximo_id.")
            return False

        if not anexos:
            return True

        apikey = getattr(settings, 'MAXIMO_API_KEY', '')

        member_href = cls._get_member_href(str(ticket.maximo_id), apikey)
        if not member_href:
            return False

        doclinks_url = f"{member_href}/doclinks"
        sucesso_total = True

        for anexo in anexos:
            try:
                anexo.arquivo.open('rb')
                anexo.arquivo.seek(0)
                conteudo = anexo.arquivo.read()
                nome = os.path.basename(anexo.arquivo.name)
                content_type = mimetypes.guess_type(nome)[0] or 'application/octet-stream'

                headers = {
                    "Content-Type": content_type,
                    "slug": nome,
                    "x-document-meta": "FILE/Attachments",
                    "x-document-description": f"Anexo do chat - {nome}",
                    "apikey": apikey,
                }

                resp = requests.post(
                    doclinks_url,
                    data=conteudo,
                    headers=headers,
                    verify=False,
                    timeout=30,
                )

                if resp.status_code in [200, 201, 204]:
                    logger.info(f"Anexo '{nome}' enviado ao Maximo SR #{ticket.maximo_id}")
                else:
                    logger.error(f"Erro DOCLINKS '{nome}' ({resp.status_code}): {resp.text}")
                    sucesso_total = False

            except Exception as e:
                logger.error(f"Exceção ao enviar anexo '{getattr(anexo.arquivo, 'name', '?')}' ao Maximo: {e}")
                sucesso_total = False

            finally:
                if hasattr(anexo.arquivo, 'closed') and not anexo.arquivo.closed:
                    anexo.arquivo.close()

        return sucesso_total