import logging
import json
import requests
from django.core.mail import EmailMessage
from django.conf import settings
from .models import Ticket, TicketInteracao, Cliente, Notificacao
from django.urls import reverse
from django.db.models import Q
from django.utils.html import strip_tags

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
                    arquivo.seek(0)
                    
                    nome = arquivo.name
                    conteudo = arquivo.read()
                    
                    content_type = getattr(
                        arquivo, "content_type", "application/octet-stream"
                    )
                    
                    email.attach(nome, conteudo, content_type)
                    
                except Exception as e:
                    logger.error(f"Erro ao anexar arquivo '{getattr(arquivo, 'name', '?')}' no service: {e}")

        try:
            email.send()
        except Exception as e:
            logger.error(
                f"Erro crítico ao enviar e-mail para Maximo (Ticket {ticket.id}): {e}"
            )
            # Opcional: Levantar exceção se quiser que a View trate o erro visualmente
            # raise e


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

        # 2. Envio de E-mail
        assunto = f"[Atualização] Ticket #{ticket.maximo_id} mudou para {status_novo}"

        corpo = f"""
        Olá, {ticket.cliente.first_name or ticket.cliente.username}.<br><br>
        
        O status do seu chamado <strong>#{ticket.maximo_id}</strong> foi atualizado.<br><br>
        
        <div style="border: 1px solid #ccc; padding: 15px; background-color: #f4f4f4;">
            <p><strong>De:</strong> <span style="color: #666;">{status_anterior_display}</span></p>
            <p><strong>Para:</strong> <span style="color: #0f62fe; font-weight: bold;">{status_novo}</span></p>
        </div>
        <br>
        Acesse o portal para ver detalhes.
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
            
            # Link para o ticket
            link_relativo = reverse("tickets:detalhe_ticket", kwargs={"pk": ticket.pk})
            base_url = getattr(settings, "SITE_URL", "") # Define SITE_URL no settings.py para links absolutos
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
                corpo_email = f"""
                Olá, {usuario.first_name or usuario.username}.<br><br>
                Houve uma nova interação no ticket <strong>#{ticket.maximo_id or ticket.id}</strong> - {ticket.sumario}.<br><br>
                
                <div style="background-color: #f4f4f4; padding: 15px; border-left: 4px solid #0f62fe;">
                    <strong>{autor_nome}:</strong><br>
                    {interacao.mensagem}
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
        # Se for Staff/Suporte = WORK, Se for Cliente = CLIENTNOTE
        if interacao.autor.is_staff or getattr(interacao.autor, 'is_support_team', False):
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