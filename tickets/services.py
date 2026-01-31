import logging
import json
import requests
import urllib3
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
        # ... (Mantenha sua lógica atual de formatação das tags SR# aqui) ...
        # Copie o conteúdo do seu método gerar_corpo_email atual para cá
        # Estou renomeando para ser mais específico
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
        cls, ticket: Ticket, usuario: Cliente, arquivo_upload=None
    ):
        """
        Orquestra o envio do e-mail de abertura para o Maximo.
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

        if arquivo_upload:
            try:
                # Lógica de anexo encapsulada
                arquivo_upload.seek(0)
                nome = arquivo_upload.name
                conteudo = arquivo_upload.read()
                content_type = getattr(
                    arquivo_upload, "content_type", "application/octet-stream"
                )
                email.attach(nome, conteudo, content_type)
            except Exception as e:
                logger.error(f"Erro ao anexar arquivo no service: {e}")

        try:
            email.send()
        except Exception as e:
            logger.error(
                f"Erro crítico ao enviar e-mail para Maximo (Ticket {ticket.id}): {e}"
            )
            # Opcional: Levantar exceção se quiser que a View trate o erro visualmente
            # raise e

    @staticmethod
    def enviar_notificacao_chat(
        ticket: Ticket, interacao: TicketInteracao, autor: Cliente
    ):
        """
        Envia notificação de interação (Chat).
        """
        email_suporte = getattr(
            settings, "SUPPORT_EMAIL_ADDRESS", "suportebr@itconsol.com"
        )
        remetente = settings.DEFAULT_FROM_EMAIL

        is_support_msg = autor.is_support_team

        if is_support_msg:
            # Suporte -> Cliente
            assunto = f"[Portal Suporte] Nova resposta no Ticket #{ticket.maximo_id} - {ticket.sumario}"
            destinatarios = [ticket.cliente.email]
            corpo = f"""
            Olá, {ticket.cliente.first_name or ticket.cliente.username}.<br><br>
            A equipe de suporte respondeu ao ticket <strong>#{ticket.maximo_id}</strong>.<br>
            <div style="background-color: #f4f4f4; padding: 10px; border-left: 4px solid #0f62fe;">
                {interacao.mensagem}
            </div>
            """
        else:
            # Cliente -> Suporte
            assunto = f"[Alerta] Cliente respondeu o Ticket #{ticket.maximo_id} - {ticket.sumario}"
            destinatarios = [email_suporte]
            location = getattr(ticket.cliente, "location", "N/A")
            corpo = f"""
            O cliente <strong>{ticket.cliente.username}</strong> ({location}) enviou mensagem.<br>
            <strong>Ticket:</strong> #{ticket.maximo_id}<br>
            <div style="background-color: #f4f4f4; padding: 10px; border-left: 4px solid #198038;">
                {interacao.mensagem}
            </div>
            """

        if destinatarios:
            msg = EmailMessage(
                subject=assunto, body=corpo, from_email=remetente, to=destinatarios
            )
            msg.content_subtype = "html"
            msg.send()


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
        Gerencia TUDO relacionado a uma nova mensagem no chat (substitui enviar_notificacao_chat).
        Lógica:
        - Se SUPORTE respondeu -> Notifica CLIENTE.
        - Se CLIENTE respondeu -> Notifica EQUIPA DE SUPORTE.
        """
        autor = interacao.autor

        # Pega e-mail de suporte do settings ou usa um padrão de segurança
        email_suporte_geral = getattr(
            settings, "SUPPORT_EMAIL_ADDRESS", "suportebr@itconsol.com"
        )

        # Texto de pré-visualização para a notificação do sininho
        preview_msg = (
            f"{autor.first_name or autor.username}: {interacao.mensagem[:60]}..."
        )

        # === DEFINIÇÃO DE QUEM RECEBE ===
        if autor.is_support_team:
            # CENÁRIO A: Suporte respondeu -> O Cliente é o destinatário
            destinatarios_internos = [ticket.cliente]
            destinatarios_email = [ticket.cliente.email]

            titulo_notif = "Nova Resposta"
            assunto_email = (
                f"[Portal Suporte] Nova resposta no Ticket #{ticket.maximo_id}"
            )

            corpo_email = f"""
            Olá, {ticket.cliente.first_name or ticket.cliente.username}.<br><br>
            A equipe de suporte respondeu ao ticket <strong>#{ticket.maximo_id}</strong>.<br><br>
            <div style="background-color: #f4f4f4; padding: 15px; border-left: 4px solid #0f62fe;">
                {interacao.mensagem}
            </div>
            <br>Acesse o portal para responder.
            """

        else:
            # CENÁRIO B: Cliente respondeu -> A Equipa de Suporte é o destinatário
            # 1. Busca todos os utilizadores que são Staff ou Consultores
            staff_users = Cliente.objects.filter(
                Q(is_staff=True) | Q(groups__name="Consultores")
            ).distinct()

            destinatarios_internos = staff_users
            destinatarios_email = [
                email_suporte_geral
            ]  # E-mail vai para a caixa partilhada

            titulo_notif = "Cliente Respondeu"
            assunto_email = f"[Alerta] Cliente respondeu Ticket #{ticket.maximo_id}"
            local_cliente = getattr(ticket.cliente, "location", "Local N/A")

            corpo_email = f"""
            O cliente <strong>{ticket.cliente.username}</strong> ({local_cliente}) enviou uma mensagem.<br><br>
            <strong>Ticket:</strong> #{ticket.maximo_id}<br>
            <strong>Sumário:</strong> {ticket.sumario}<br><br>
            <div style="background-color: #f4f4f4; padding: 15px; border-left: 4px solid #198038;">
                {interacao.mensagem}
            </div>
            """

        # === 1. CRIAÇÃO DAS NOTIFICAÇÕES INTERNAS (Bulk Create) ===
        # Usamos bulk_create para ser rápido, mesmo se houver 50 consultores.
        notificacoes_db = []
        for usuario in destinatarios_internos:
            # Se for staff, adiciona ?origin=fila ao link para facilitar a navegação
            link_destino = reverse("tickets:detalhe_ticket", kwargs={"pk": ticket.pk})
            if usuario.is_support_team:
                link_destino += "?origin=fila"

            notificacoes_db.append(
                Notificacao(
                    destinatario=usuario,
                    ticket=ticket,
                    titulo=titulo_notif,
                    tipo="mensagem",
                    mensagem=preview_msg,
                    link=link_destino,
                )
            )

        if notificacoes_db:
            Notificacao.objects.bulk_create(notificacoes_db)

        # === 2. ENVIO DO E-MAIL ===
        cls._enviar_email_generico(destinatarios_email, assunto_email, corpo_email)

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
