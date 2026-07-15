import logging
import json
import requests
import os
import mimetypes
from urllib.parse import urlparse
from django.core.mail import EmailMessage
from django.conf import settings
from .models import Ticket, TicketInteracao, Cliente, Notificacao, EmailPendente
from django.urls import reverse
from django.utils.html import strip_tags, escape
from django.utils import timezone

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


def _links_ticket(ticket: Ticket) -> tuple[str, str]:
    """
    Retorna (link_relativo, link_absoluto) da página de detalhe do ticket.
    Centraliza o padrão reverse + SITE_URL usado em todas as notificações.
    """
    link_relativo = reverse("tickets:detalhe_ticket", kwargs={"pk": ticket.pk})
    base_url = getattr(settings, "SITE_URL", "http://localhost:8000").rstrip("/")
    return link_relativo, f"{base_url}{link_relativo}"


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
            # Guarda o e-mail já renderizado para o retry (reprocessar_emails_pendentes).
            # Uma linha por destinatário: a falha e a retentativa são individuais.
            agora = timezone.now()
            for endereco in destinatarios:
                EmailPendente.objects.create(
                    destinatario=endereco,
                    assunto=assunto,
                    corpo_html=corpo_html,
                    tentativas=1,
                    ultimo_erro=str(e),
                    ultima_tentativa_em=agora,
                )

    @classmethod
    def notificar_mudanca_status(cls, ticket: Ticket, status_anterior_display: str):

        """
        Notifica o Cliente quando o status do chamado muda.
        1. Cria notificação interna.
        2. Envia e-mail.
        """

        status_novo = ticket.get_status_maximo_display()
        link_relativo, full_link = _links_ticket(ticket)

        # Destinatários: dono do ticket + colegas notificados (dedupe via set).
        destinatarios = {ticket.cliente}
        destinatarios.update(ticket.colegas_notificados.all())

        assunto = f"[Atualização] Ticket #{ticket.maximo_id} mudou para {status_novo}"
        status_ant_safe = escape(status_anterior_display)
        status_novo_safe = escape(status_novo)

        notificacoes_db = []
        for usuario in destinatarios:
            # 1. Notificação Interna (Sino)
            notificacoes_db.append(
                Notificacao(
                    destinatario=usuario,
                    ticket=ticket,
                    titulo="Status Atualizado",
                    tipo="status",
                    mensagem=f"O chamado agora está: {status_novo}",
                    link=link_relativo,
                )
            )

            # 2. E-mail (apenas para quem tem endereço)
            nome_dest = escape(usuario.first_name or usuario.username)
            corpo = f"""
            Olá, {nome_dest}.<br><br>

            O status do chamado <strong>#{escape(str(ticket.maximo_id))}</strong> foi atualizado.<br><br>

            <div style="border: 1px solid #ccc; padding: 15px; background-color: #f4f4f4;">
                <p><strong>De:</strong> <span style="color: #666;">{status_ant_safe}</span></p>
                <p><strong>Para:</strong> <span style="color: #0f62fe; font-weight: bold;">{status_novo_safe}</span></p>
            </div>
            <br>
            <a href="{full_link}">Clique aqui para acessar o portal e ver os detalhes.</a>
            """
            if usuario.email:
                cls._enviar_email_generico([usuario.email], assunto, corpo)

        if notificacoes_db:
            Notificacao.objects.bulk_create(notificacoes_db)

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
            # Todos os envolvidos entram no set e recebem o SINO; o e-mail é
            # enviado no loop abaixo apenas para quem tem endereço cadastrado.
            destinatarios = set()

            # A. Adiciona o Cliente (Dono do Ticket)
            if ticket.cliente:
                destinatarios.add(ticket.cliente)

            # B. Adiciona o Consultor Responsável (Owner)
            # O campo ticket.owner é uma string (PersonID). Precisamos do objeto Cliente/User.
            if ticket.owner:

                # Busca Case-Insensitive pelo person_id
                consultor = Cliente.objects.filter(person_id__iexact=ticket.owner).first()
                if consultor:
                    destinatarios.add(consultor)

            # C. Adiciona o Grupo de Líderes
            destinatarios.update(Cliente.objects.filter(groups__name="lider_suporte"))

            # C2. Adiciona os Seguidores designados (consultores extras no ticket)
            for seguidor in ticket.seguidores.all():
                destinatarios.add(seguidor)

            # C3. Adiciona os colegas notificados (escolhidos pelo solicitante)
            for colega in ticket.colegas_notificados.all():
                destinatarios.add(colega)

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
            link_relativo, full_link = _links_ticket(ticket)

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
                
                # Envia individualmente para cada destinatário (se tiver e-mail)
                if usuario.email:
                    cls._enviar_email_generico([usuario.email], assunto, corpo_email)

            # 4. Grava notificações no banco em lote
            if notificacoes_db:
                Notificacao.objects.bulk_create(notificacoes_db)

        except Exception as e:
            logger.error(f"Erro no fluxo de notificação (Ticket {ticket.id}): {e}")

    @classmethod
    def notificar_novo_ticket(cls, ticket: Ticket) -> None:

        """
        Notifica os membros do grupo 'lider_suporte' quando um ticket é criado.
        1. Cria notificação interna (Sino) para cada líder.
        2. Envia e-mail individual (apenas para quem tem e-mail cadastrado).
        * O criador do ticket nunca é notificado (mesmo que seja líder).
        """

        try:
            lideres = Cliente.objects.filter(groups__name="lider_suporte")
            destinatarios = [lider for lider in lideres if lider != ticket.cliente]

            if not destinatarios:
                return

            nome_cliente = ticket.cliente.get_full_name() or ticket.cliente.username
            ticket_ref = str(ticket.maximo_id or ticket.id)
            preview_msg = f"{nome_cliente}: {ticket.sumario[:60]}..."
            preview_msg = preview_msg[:255]
            assunto = f"[Portal Suporte] Novo ticket #{ticket_ref} - {nome_cliente}"

            # Versões escapadas p/ injeção segura no corpo HTML do e-mail
            nome_cliente_safe = escape(nome_cliente)
            ticket_ref_safe = escape(ticket_ref)
            sumario_safe = escape(ticket.sumario)
            descricao_safe = escape(ticket.descricao[:200]).replace("\n", "<br>")

            link_relativo, full_link = _links_ticket(ticket)

            notificacoes_db = []

            for lider in destinatarios:
                # --- A. Notificação Interna (Sino) ---
                notificacoes_db.append(
                    Notificacao(
                        destinatario=lider,
                        ticket=ticket,
                        titulo="Novo Ticket",
                        tipo="novo_ticket",
                        mensagem=preview_msg,
                        link=link_relativo,
                    )
                )

                # --- B. Envio de E-mail ---
                if lider.email:
                    nome_lider_safe = escape(lider.first_name or lider.username)
                    corpo_email = f"""
                    Olá, {nome_lider_safe}.<br><br>
                    Um novo ticket <strong>#{ticket_ref_safe}</strong> foi aberto por <strong>{nome_cliente_safe}</strong>.<br><br>

                    <div style="background-color: #f4f4f4; padding: 15px; border-left: 4px solid #0f62fe;">
                        <strong>{sumario_safe}</strong><br>
                        {descricao_safe}
                    </div>
                    <br>
                    <a href="{full_link}">Clique aqui para ver o ticket no portal</a>
                    """
                    cls._enviar_email_generico([lider.email], assunto, corpo_email)

            if notificacoes_db:
                Notificacao.objects.bulk_create(notificacoes_db)

        except Exception as e:
            logger.error(
                f"Erro ao notificar líderes de novo ticket (Ticket {ticket.id}): {e}"
            )


class MaximoSenderService:

    """
    Serviço responsável por enviar interações do Portal para o IBM Maximo (Worklogs).
    """

    # URL configurada conforme seu POSTMAN
    MAXIMO_API_URL = getattr(settings, 'MAXIMO_API_URL_LOG', '')

    # SiteID fixo da operação (era hardcoded no corpo do e-mail do Listener)
    MAXIMO_SITEID = "ITCBR"

    @classmethod
    def criar_sr(cls, ticket: Ticket, usuario: Cliente) -> dict | None:

        """
        Cria a Service Request (SR) diretamente no Maximo via REST (POST no
        Object Structure ITC_PORTAL_API), substituindo o fluxo por e-mail.

        O Maximo devolve o 'ticketid' de forma SÍNCRONA (header 'properties: *'),
        dispensando o match por texto do 'sincronizar_maximo'. A resposta também
        traz 'doclinks.href', usado para subir anexos sem GET extra.

        Retorna o registro da SR (dict) em caso de sucesso, ou None em falha.
        O chamador persiste ticket.maximo_id e dispara os anexos.
        """

        base_url = getattr(settings, 'MAXIMO_API_URL', '')
        if not base_url:
            logger.error("MAXIMO_API_URL não configurada; impossível criar SR via REST.")
            return None

        # Obrigatórios (Maximo auto-preenche status/reportdate/orgid)
        payload = {
            "class": "SR",
            "siteid": cls.MAXIMO_SITEID,
            "description": strip_tags(ticket.sumario),
            "description_longdescription": strip_tags(ticket.descricao),
        }

        # Prioridade: Maximo espera inteiro (1/2/3)
        try:
            payload["reportedpriority"] = int(ticket.prioridade)
        except (TypeError, ValueError):
            logger.warning(
                f"Prioridade inválida no Ticket {ticket.id} ('{ticket.prioridade}'); "
                "SR criada sem reportedpriority."
            )

        # Opcionais: só enviados quando preenchidos (evita BMXAA por valor vazio)
        if ticket.ambiente and ticket.ambiente.numero_ativo:
            payload["assetnum"] = ticket.ambiente.numero_ativo

        if ticket.area:
            payload["itc_area"] = ticket.area.nome_area

        location = getattr(usuario, "location", None)
        if location:
            payload["location"] = location

        # Solicitante: afetado e reportado são o mesmo cliente do portal
        person_id = getattr(usuario, "person_id", None)
        if person_id:
            payload["affectedpersonid"] = person_id
            payload["reportedby"] = person_id

        # 'properties: *' força o Maximo a devolver o registro criado (ticketid + doclinks)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "properties": "*",
            "apikey": getattr(settings, 'MAXIMO_API_KEY', ''),
        }

        verify_ssl = getattr(settings, 'MAXIMO_VERIFY_SSL', True)

        try:
            logger.info(f"Criando SR no Maximo via REST para Ticket #{ticket.id}...")

            response = requests.post(
                base_url,
                params={"lean": 1},
                data=json.dumps(payload),
                headers=headers,
                verify=verify_ssl,
                timeout=15,
            )

            if response.status_code not in (200, 201):
                logger.error(
                    f"Erro ao criar SR (Ticket {ticket.id}) "
                    f"[{response.status_code}]: {response.text}"
                )
                return None

            data = response.json()
            ticketid = data.get("ticketid")
            if not ticketid:
                logger.error(
                    f"SR criada para Ticket {ticket.id} mas resposta sem ticketid: {data}"
                )
                return None

            logger.info(f"SR #{ticketid} criada no Maximo (Ticket local {ticket.id}).")
            return data

        except Exception as e:
            logger.error(f"Exceção ao criar SR no Maximo (Ticket {ticket.id}): {e}")
            return None

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
            interacao.autor.is_consultor or
            interacao.autor.is_lider_suporte
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
                verify=getattr(settings, 'MAXIMO_VERIFY_SSL', True),
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
            resp = requests.get(
                base_url,
                params=params,
                headers=headers,
                verify=getattr(settings, 'MAXIMO_VERIFY_SSL', True),
                timeout=10,
            )
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

    @staticmethod
    def _doclink_request(doclinks_url: str, arquivo, apikey: str):

        """
        POST de UM arquivo (FieldFile/UploadedFile) para a URL de doclinks de uma SR.
        Retorna a Response do requests (ou None em exceção). Não interpreta status:
        o chamador decide sucesso e, se quiser, extrai o id do doclink criado.
        """

        try:
            arquivo.open('rb')
            arquivo.seek(0)
            conteudo = arquivo.read()
            nome = os.path.basename(arquivo.name)
            content_type = mimetypes.guess_type(nome)[0] or 'application/octet-stream'

            headers = {
                "Content-Type": content_type,
                "slug": nome,
                "x-document-meta": "FILE/Attachments",
                "x-document-description": f"Anexo do portal - {nome}",
                "apikey": apikey,
            }

            return requests.post(
                doclinks_url,
                data=conteudo,
                headers=headers,
                verify=getattr(settings, 'MAXIMO_VERIFY_SSL', True),
                timeout=30,
            )

        except Exception as e:
            logger.error(f"Exceção ao enviar anexo '{getattr(arquivo, 'name', '?')}': {e}")
            return None

        finally:
            if hasattr(arquivo, 'closed') and not arquivo.closed:
                arquivo.close()

    @classmethod
    def _post_doclink(cls, doclinks_url: str, arquivo, apikey: str) -> bool:

        """Envia UM arquivo para os doclinks e retorna apenas sucesso/falha."""

        resp = cls._doclink_request(doclinks_url, arquivo, apikey)
        nome = os.path.basename(getattr(arquivo, 'name', '?'))
        if resp is not None and resp.status_code in (200, 201, 204):
            logger.info(f"Anexo '{nome}' enviado para {doclinks_url}")
            return True
        if resp is not None:
            logger.error(f"Erro DOCLINKS '{nome}' ({resp.status_code}): {resp.text}")
        return False

    @staticmethod
    def _extrair_doclink_id(resp) -> "str | None":

        """
        Descobre o identificador do doclink recém-criado a partir da resposta do POST.
        Ordem: header Location -> href/docinfoid/identifier no corpo JSON.
        Loga o que encontrou para validação no ambiente de dev.
        """

        try:
            loc = resp.headers.get("Location")
        except Exception:
            loc = None
        if loc:
            logger.info(f"DOCLINK criado, Location={loc}")
            return str(loc)

        try:
            data = resp.json()
            if isinstance(data, dict):
                cand = data.get("href") or data.get("docinfoid") or data.get("identifier")
                if cand:
                    logger.info(f"DOCLINK criado, id do corpo={cand}")
                    return str(cand)
        except Exception:
            pass

        logger.warning("DOCLINK criado sem id identificável na resposta; remoção usará fallback por filename.")
        return None

    @staticmethod
    def _delete_doclink(url: str, apikey: str, filename: str = "?") -> bool:

        """
        DELETE de UM doclink pela sua URL. Se o gateway bloquear DELETE, tenta
        POST com x-method-override: DELETE (mesmo padrão do SYNC). Best-effort.
        """

        headers = {"apikey": apikey, "Accept": "application/json"}
        verify = getattr(settings, 'MAXIMO_VERIFY_SSL', True)
        try:
            resp = requests.delete(url, headers=headers, verify=verify, timeout=15)
            if resp.status_code in (200, 204):
                logger.info(f"DOCLINK '{filename}' removido no Maximo ({url}).")
                return True

            logger.warning(
                f"DELETE doclink '{filename}' status {resp.status_code}; tentando x-method-override."
            )
            resp2 = requests.post(
                url,
                headers={**headers, "x-method-override": "DELETE"},
                verify=verify,
                timeout=15,
            )
            if resp2.status_code in (200, 204):
                logger.info(f"DOCLINK '{filename}' removido via override ({url}).")
                return True

            logger.error(f"Falha ao remover DOCLINK '{filename}' ({resp2.status_code}): {resp2.text}")
            return False

        except Exception as e:
            logger.error(f"Exceção ao remover DOCLINK '{filename}': {e}")
            return False

    @classmethod
    def _achar_doclink_por_nome(cls, member_href: str, apikey: str, filename: str) -> "str | None":

        """
        Fallback (estratégia A): GET na coleção /doclinks da SR e localiza o member
        cujo nome de arquivo bate com `filename`. Retorna o href do member ou None.
        """

        alvo = os.path.basename(filename or "")
        if not alvo:
            return None

        url = f"{member_href}/doclinks"
        headers = {"apikey": apikey, "Accept": "application/json"}
        params = {"oslc.select": "*", "lean": 1}
        try:
            resp = requests.get(
                url, params=params, headers=headers,
                verify=getattr(settings, 'MAXIMO_VERIFY_SSL', True), timeout=15,
            )
            if resp.status_code != 200:
                logger.error(f"GET doclinks {url} status {resp.status_code}: {resp.text}")
                return None

            data = resp.json()
            membros = data.get("member") or data.get("rdfs:member") or []
            for m in membros:
                # O nome do arquivo pode vir em urlname/fileName/document conforme o Maximo.
                nome = m.get("urlname") or m.get("fileName") or m.get("document") or ""
                if os.path.basename(str(nome)) == alvo:
                    href = m.get("href") or m.get("rdf:about")
                    if href and not str(href).startswith("http"):
                        p = urlparse(url)
                        href = f"{p.scheme}://{p.netloc}/{str(href).lstrip('/')}"
                    logger.info(f"DOCLINK '{alvo}' localizado por filename: {href}")
                    return href

            logger.warning(f"Nenhum doclink casou com '{alvo}' entre {len(membros)} membros da SR.")
            return None

        except Exception as e:
            logger.error(f"Exceção ao buscar doclink '{alvo}': {e}")
            return None

    @classmethod
    def remover_anexo_doclink(cls, maximo_id, doclink_id, filename) -> bool:

        """
        Remove UM anexo dos DOCLINKS da SR no Maximo.
        Estratégia B: usa `doclink_id` exato (capturado no upload) quando existe.
        Fallback A: localiza por filename na coleção de doclinks.
        Best-effort: nunca levanta exceção; loga tudo para validação em dev.
        """

        if not maximo_id:
            logger.warning("remover_anexo_doclink: ticket sem maximo_id; nada a remover no Maximo.")
            return False

        apikey = getattr(settings, 'MAXIMO_API_KEY', '')

        # Estratégia B: id exato do doclink.
        if doclink_id:
            url = str(doclink_id)
            if not url.startswith("http"):
                member_href = cls._get_member_href(str(maximo_id), apikey)
                if not member_href:
                    return False
                url = f"{member_href}/doclinks/{doclink_id}"
            return cls._delete_doclink(url, apikey, filename or "?")

        # Fallback A: acha por filename.
        member_href = cls._get_member_href(str(maximo_id), apikey)
        if not member_href:
            return False

        alvo = cls._achar_doclink_por_nome(member_href, apikey, filename)
        if not alvo:
            logger.warning(
                f"DOCLINK de '{filename}' não localizado na SR {maximo_id}; nada removido no Maximo."
            )
            return False
        return cls._delete_doclink(alvo, apikey, filename or "?")

    @classmethod
    def enviar_anexos_criacao(cls, doclinks_url: str, arquivos: list) -> bool:

        """
        Sobe anexos da abertura do ticket (documento de requisição + evidências)
        para os DOCLINKS da SR recém-criada via REST. O doclinks_url já vem na
        resposta de criar_sr() — não precisa do GET de _get_member_href.
        Roda em thread na view para não bloquear o usuário.
        """

        if not arquivos:
            return True

        apikey = getattr(settings, 'MAXIMO_API_KEY', '')
        sucesso_total = True
        for arquivo in arquivos:
            if not cls._post_doclink(doclinks_url, arquivo, apikey):
                sucesso_total = False
        return sucesso_total

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
            resp = cls._doclink_request(doclinks_url, anexo.arquivo, apikey)
            nome = os.path.basename(getattr(anexo.arquivo, 'name', '?'))

            if resp is not None and resp.status_code in (200, 201, 204):
                logger.info(f"Anexo '{nome}' enviado para {doclinks_url}")
                # Captura o id do doclink p/ permitir remoção exata depois (estratégia B).
                doclink_id = cls._extrair_doclink_id(resp)
                if doclink_id and getattr(anexo, 'pk', None):
                    anexo.maximo_doclink_id = doclink_id
                    try:
                        anexo.save(update_fields=["maximo_doclink_id"])
                    except Exception as e:
                        logger.error(f"Falha ao salvar doclink_id do anexo {anexo.pk}: {e}")
            else:
                sucesso_total = False
                if resp is not None:
                    logger.error(f"Erro DOCLINKS '{nome}' ({resp.status_code}): {resp.text}")

        return sucesso_total