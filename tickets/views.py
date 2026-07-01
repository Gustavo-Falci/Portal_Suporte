from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpRequest, FileResponse
from django.contrib import messages
from django.urls import reverse
from .models import Ticket, TicketInteracao, Cliente, Notificacao, MAXIMO_STATUS_CHOICES, TicketAnexo, InteracaoAnexo
from .forms import TicketForm, TicketInteracaoForm
from django.db.models import Q, QuerySet
from django.db import transaction
from .services import MaximoEmailService, NotificationService, MaximoSenderService
from django.template.loader import render_to_string
from django.http import JsonResponse
from django.core.paginator import Paginator
from django.views.decorators.http import require_http_methods
from django.utils.http import url_has_allowed_host_and_scheme
from django.contrib.auth import login as auth_login, update_session_auth_hash
from django.contrib.auth.forms import SetPasswordForm
from .forms import EmailAuthenticationForm
from django.conf import settings
from django.http import Http404
from django.http import StreamingHttpResponse
from . import logtail
from typing import Any
import logging
import os
import threading

logger = logging.getLogger(__name__)
from . import audit


# Sufixo de email tratado como conta genérica (não-cliente corporativo).
GMAIL_SUFFIXO = "@gmail.com"


def _email_eh_gmail(user: Cliente) -> bool:
    """True se o email do usuário é uma conta @gmail (genérica, não corporativa)."""
    return (user.email or "").strip().lower().endswith(GMAIL_SUFFIXO)


# --- FUNÇÃO AUXILIAR DE PERMISSÕES ---
def _usuario_tem_acesso_ticket(user: Cliente, ticket: Ticket) -> bool:
    is_dono = (ticket.cliente == user)
    is_staff = getattr(user, 'is_support_team', False) or user.is_superuser
    is_lider = user.groups.filter(name="lider_suporte").exists()

    is_owner_assigned = False
    if user.person_id and ticket.owner:
        is_owner_assigned = (ticket.owner.lower() == user.person_id.lower())

    # Seguidor designado pela liderança: acesso de leitura+interação.
    is_seguidor = ticket.seguidores.filter(pk=user.pk).exists()

    loc_user = (user.location or "").strip()
    loc_ticket = (ticket.cliente.location or "").strip()
    # Mesma empresa = mesma location E mesmo "mundo" de email (gmail só com gmail).
    is_mesma_empresa = (
        bool(loc_user)
        and loc_user.lower() == loc_ticket.lower()
        and _email_eh_gmail(user) == _email_eh_gmail(ticket.cliente)
    )

    return is_dono or is_staff or is_lider or is_owner_assigned or is_mesma_empresa or is_seguidor


def _tickets_visiveis_cliente(user: Cliente) -> QuerySet:
    """Queryset de tickets visíveis para um cliente comum.

    Agrupa por empresa via Cliente.location (match case-insensitive).
    Contas @gmail são separadas dos clientes corporativos: dentro da mesma
    location, gmail só enxerga gmail e corporativo só enxerga corporativo.
    Guard: location vazio/null => vê apenas os próprios tickets.
    """
    loc = (user.location or "").strip()
    if not loc:
        return Ticket.objects.filter(cliente=user)

    qs = Ticket.objects.filter(cliente__location__iexact=loc)
    if _email_eh_gmail(user):
        return qs.filter(cliente__email__iendswith=GMAIL_SUFFIXO)
    return qs.exclude(cliente__email__iendswith=GMAIL_SUFFIXO)


# PÁGINA INICIAL
def pagina_inicial(request: HttpRequest) -> HttpResponse:

    if not request.user.is_authenticated:
        return redirect("tickets:login")

    user = request.user
    
    # 1. QuerySet Base
    if user.is_support_team or user.is_lider_suporte:
        qs_tickets = Ticket.objects.exclude(maximo_id__isnull=True)
    elif user.is_consultor:
        # Próprios (owner == person_id) + tickets que segue (designado pela liderança).
        qs_tickets = Ticket.objects.exclude(maximo_id__isnull=True)
        if user.person_id:
            qs_tickets = qs_tickets.filter(
                Q(owner__iexact=user.person_id) | Q(seguidores=user)
            ).distinct()
        else:
            qs_tickets = qs_tickets.filter(seguidores=user).distinct()
    else:
        qs_tickets = _tickets_visiveis_cliente(user)

    # 2. Estatísticas Rápidas
    # Consideramos "Em Aberto" tudo que não está Fechado, Resolvido ou Cancelado
    status_encerrados = ['RESOLVED', 'CLOSED', 'CANCELLED']
    
    total_abertos = qs_tickets.exclude(status_maximo__in=status_encerrados).count()
    total_geral = qs_tickets.count()

    # 3. Últimos 3 Tickets Em Aberto (Para acesso rápido)
    ultimos_tickets = qs_tickets.exclude(status_maximo__in=status_encerrados).order_by('-data_criacao')[:3]

    context = {
        "total_abertos": total_abertos,
        "total_geral": total_geral,
        "ultimos_tickets": ultimos_tickets,
        "primeiro_nome": user.get_short_name() or user.username
    }

    return render(request, "tickets/bem_vindo.html", context)


# SUCESSO
@login_required(login_url="/login/")
def ticket_sucesso(request: HttpRequest) -> HttpResponse:
    # Recupera o ticket recém-criado (id deixado na sessão por criar_ticket),
    # consumindo a chave para que refresh/acesso direto caia na versão genérica.
    ticket = None
    tid = request.session.pop("ticket_sucesso_id", None)
    if tid:
        candidato = (
            Ticket.objects.filter(pk=tid)
            .select_related("ambiente")
            .first()
        )
        # ACL: só o dono (ou quem tem acesso) vê o resumo do chamado.
        if candidato and _usuario_tem_acesso_ticket(request.user, candidato):
            ticket = candidato

    return render(request, "tickets/sucesso.html", {"ticket": ticket})


# LISTAGEM DE TICKETS
@login_required(login_url="/login/")
def meus_tickets(request: HttpRequest) -> HttpResponse:

    """
    Lista os tickets visíveis ao usuário: próprios + da mesma empresa (location).
    Suporta filtro de escopo (todos/meus/equipe), status e busca textual.
    """

    tickets = (
        _tickets_visiveis_cliente(request.user)
        .select_related('area', 'ambiente', 'cliente')
        .order_by('-data_criacao')
    )

    # Filtros de refinamento (aplicados antes do escopo, para contagem coerente)
    status_filters = request.GET.getlist("status")
    search_query = request.GET.get("q")

    if status_filters:
        tickets = tickets.filter(status_maximo__in=status_filters)

    if search_query:
        tickets = tickets.filter(
            Q(maximo_id__icontains=search_query)
            | Q(sumario__icontains=search_query)
            | Q(descricao__icontains=search_query)
        )

    # Por padrão, oculta tickets encerrados das listas. Continuam pesquisáveis:
    # qualquer busca textual ou filtro explícito de status os revela.
    status_encerrados = ['RESOLVED', 'CLOSED', 'CANCELLED']
    if not status_filters and not search_query:
        tickets = tickets.exclude(status_maximo__in=status_encerrados)

    # Contadores por escopo (sobre o queryset já filtrado por status/busca)
    count_todos = tickets.count()
    count_meus = tickets.filter(cliente=request.user).count()
    count_equipe = count_todos - count_meus

    # Aplica o escopo selecionado
    escopo = request.GET.get("escopo")
    if escopo == "meus":
        tickets = tickets.filter(cliente=request.user)
    elif escopo == "equipe":
        tickets = tickets.exclude(cliente=request.user)

    paginator = Paginator(tickets, 10)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Salva a URL completa (com filtros e paginação) na sessão
    request.session['last_meus_tickets_url'] = request.get_full_path()

    context = {
        "tickets": page_obj,
        "status_choices": MAXIMO_STATUS_CHOICES,
        "filtros_atuais": request.GET,
        "status_selecionados": status_filters,
        "escopo_atual": escopo or "",
        "count_todos": count_todos,
        "count_meus": count_meus,
        "count_equipe": count_equipe,
    }

    return render(request, "tickets/meus_tickets.html", context)


# CRIAR TICKET

def _subir_anexos_criacao_e_marcar(ticket_id: int, doclinks_url: str, arquivos: list) -> None:
    """Sobe os anexos da abertura aos DOCLINKS da SR e marca o resultado no
    ticket. Roda em thread: usa update() (1 write atômico) e o ID (não a
    instância) para não depender de estado compartilhado entre threads."""
    ok = MaximoSenderService.enviar_anexos_criacao(doclinks_url, arquivos)
    Ticket.objects.filter(pk=ticket_id).update(anexos_sincronizados=ok)
    if not ok:
        logger.error(
            f"Ticket {ticket_id}: falha ao sincronizar {len(arquivos)} anexo(s) "
            f"com o Maximo (DOCLINKS). anexos_sincronizados=False (pendente de retry)."
        )


def _integrar_maximo_criacao(request: HttpRequest, ticket: Ticket, todos_anexos: list) -> None:
    """Cria a SR no Maximo via REST ou cai no e-mail (Listener). Isolado da
    persistência: qualquer falha aqui é aviso, NUNCA rollback do ticket."""
    sr = MaximoSenderService.criar_sr(ticket, request.user)

    if sr:
        ticket.maximo_id = sr["ticketid"]
        ticket.save(update_fields=["maximo_id"])
        logger.info(
            f"Ticket #{ticket.id} criado com sucesso: SR {ticket.maximo_id} "
            f"aberta no Maximo via REST (user={request.user.username})."
        )

        # Anexos -> DOCLINKS da SR recém-criada (href já vem na resposta).
        doclinks_url = (sr.get("doclinks") or {}).get("href")
        if not doclinks_url and sr.get("href"):
            doclinks_url = f'{sr["href"]}/doclinks'

        if todos_anexos and doclinks_url:
            # Pendente até a thread confirmar o upload.
            ticket.anexos_sincronizados = False
            ticket.save(update_fields=["anexos_sincronizados"])
            threading.Thread(
                target=_subir_anexos_criacao_e_marcar,
                args=(ticket.id, doclinks_url, todos_anexos),
            ).start()
        elif todos_anexos and not doclinks_url:
            ticket.anexos_sincronizados = False
            ticket.save(update_fields=["anexos_sincronizados"])
            logger.warning(
                f"Ticket {ticket.id}: SR {sr.get('ticketid')} criada mas sem doclinks_url; "
                f"{len(todos_anexos)} anexo(s) NAO enviado(s) ao Maximo."
            )

        # Sucesso não precisa de flash: a tela de sucesso (sucesso.html) já
        # confirma a criação com nº da SR e resumo. Avisos/erros seguem via
        # messages (renderizados pelo bloco messages do base, agora restaurado).

    else:
        # Maximo REST indisponível -> fallback no e-mail pro Listener.
        logger.warning(
            f"Ticket #{ticket.id}: criar_sr REST falhou; usando fallback por "
            f"e-mail (Listener). O maximo_id sera recuperado pelo sincronizar_maximo."
        )
        try:
            MaximoEmailService.enviar_ticket_maximo(ticket, request.user, todos_anexos)
            logger.info(
                f"Ticket #{ticket.id} criado: enviado ao Maximo via e-mail fallback "
                f"(Listener) com sucesso (user={request.user.username})."
            )

        except Exception as e:
            logger.error(f"Erro no envio de e-mail fallback (Ticket {ticket.id}): {e}")
            messages.warning(
                request,
                "O ticket foi guardado no portal, mas houve um erro ao enviar a notificação para a nossa equipe de suporte. "
                "Por favor, entre em contato via telefone ou chat para confirmar a receção."
            )


@login_required(login_url="/login/")
def criar_ticket(request: HttpRequest) -> HttpResponse:

    if request.method == "POST":
        form = TicketForm(request.POST, request.FILES, user=request.user)

        if form.is_valid():
            # 1. Persistência (transação isolada). Falha AQUI é erro real:
            #    rollback total + erro + re-render do form (permite retry sem
            #    deixar ticket órfão e sem convidar a reenvio/duplicação).
            try:
                with transaction.atomic():
                    ticket = form.save(commit=False)
                    ticket.cliente = request.user

                    doc_requisicao = request.FILES.get("documento_requisicao")
                    if doc_requisicao:
                        ticket.documento_requisicao = doc_requisicao

                    ticket.save()

                    for arquivo_temp in request.FILES.getlist("arquivo"):
                        TicketAnexo.objects.create(ticket=ticket, arquivo=arquivo_temp)

            except Exception as e:
                logger.error(
                    f"Erro ao persistir ticket no banco (user={request.user.username}): {e}"
                )
                # Erro mostrado INLINE no card (não via messages): a página
                # suprime o bloco messages para não exibir flashes acima do form.
                return render(
                    request, "tickets/criar_ticket.html",
                    {
                        "form": form,
                        "erro_persistencia": (
                            "Ocorreu um erro ao guardar o ticket. Por segurança, "
                            "reanexe os arquivos e tente novamente. Se o erro "
                            "persistir, contate o suporte por telefone ou chat."
                        ),
                    },
                )

            # A PARTIR DAQUI o ticket está GARANTIDAMENTE salvo. Nada abaixo pode
            # provocar rollback nem mostrar "erro ao guardar" (evita duplicação:
            # falha de integração não deve fazer o usuário reenviar o formulário).
            audit.registrar(request.user, f"criou Ticket #{ticket.id}")

            todos_anexos = []
            if ticket.documento_requisicao:
                todos_anexos.append(ticket.documento_requisicao)
            for anexo_obj in ticket.anexos.all():
                todos_anexos.append(anexo_obj.arquivo)

            # 2. Integração Maximo — isolada; falha inesperada vira aviso, não erro.
            try:
                _integrar_maximo_criacao(request, ticket, todos_anexos)
            except Exception as e:
                logger.error(f"Erro inesperado na integração Maximo (Ticket {ticket.id}): {e}")
                messages.warning(
                    request,
                    "Ticket criado, mas houve instabilidade ao registrar no Maximo. "
                    "Nossa equipe foi avisada e fará o acompanhamento."
                )

            request.session["ticket_sucesso_id"] = ticket.id
            return redirect("tickets:ticket_sucesso")

        else:
            # Criação rejeitada na validação. Loga só os NOMES dos campos com erro
            # (nunca os valores) para não vazar dados sensíveis no log.
            logger.warning(
                f"Criação de ticket REJEITADA (form inválido) user={request.user.username}; "
                f"campos com erro: {list(form.errors.keys())}"
            )
            return render(request, "tickets/criar_ticket.html", {"form": form})

    else:
        form = TicketForm(user=request.user)

    return render(request, "tickets/criar_ticket.html", {"form": form})


# DETALHE DO TICKET
@login_required(login_url="/login/")
def detalhe_ticket(request: HttpRequest, pk: int) -> HttpResponse:

    ticket = get_object_or_404(Ticket, pk=pk)
    origem = request.GET.get("origin")

    # Lógica de bloqueio unificada
    if not _usuario_tem_acesso_ticket(request.user, ticket):
        messages.error(request, "Você não tem permissão para visualizar este ticket.")
        return redirect("tickets:meus_tickets")

    if request.method == "POST":

        if ticket.is_closed:
            msg_erro = "Este ticket já foi encerrado/resolvido. Não é possível enviar novas mensagens."

            # Se a requisição veio via AJAX (JS), retorna JSON de erro
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({
                    "status": "error", 
                    "errors": {"global": msg_erro} # Estrutura genérica de erro
                }, status=403) # 403 Forbidden

            # Se for requisição normal, Flash Message e Redirect
            messages.error(request, msg_erro)
            return redirect("tickets:detalhe_ticket", pk=pk)

        form = TicketInteracaoForm(request.POST, request.FILES)
        
        if form.is_valid():
            interacao = form.save(commit=False)
            interacao.ticket = ticket
            interacao.autor = request.user
            
            interacao.save()

            # Múltiplos anexos: cria 1 InteracaoAnexo por arquivo enviado
            for arquivo_recebido in request.FILES.getlist("arquivo"):
                InteracaoAnexo.objects.create(interacao=interacao, arquivo=arquivo_recebido)
            audit.registrar(request.user, f"adicionou interação ao Ticket #{ticket.id}")

            interacao_salva = TicketInteracao.objects.get(id=interacao.id)

            sincronizado = MaximoSenderService.enviar_interacao(ticket, interacao)
            
            if not sincronizado:
                # Adiciona aviso visual (aparecerá se a página recarregar ou se o JS tratar mensagens)
                messages.warning(request, "Mensagem salva localmente, mas houve instabilidade na sincronização com o IBM Maximo.")

            # ENVIO DE ANEXOS AO MAXIMO (DOCLINKS) EM SEGUNDO PLANO
            # Upload de arquivos pode ser lento; roda em thread para não travar o usuário.
            anexos_interacao = list(interacao_salva.anexos.all())
            if anexos_interacao:
                threading.Thread(
                    target=MaximoSenderService.enviar_anexos,
                    args=(ticket, anexos_interacao),
                ).start()

            # 1. ENVIO DE E-MAIL EM SEGUNDO PLANO (THREADING)
            # Isso impede que o usuário fique esperando o SMTP responder
            email_thread = threading.Thread(
                target=NotificationService.notificar_nova_interacao,
                args=(ticket, interacao_salva),
            )
            email_thread.start()

            # Atualiza data de modificação
            ticket.save()

            # 2. RESPOSTA PARA AJAX (SEM REFRESH)
            # Verifica se a requisição veio do JavaScript
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                # Renderiza apenas o pedacinho do chat novo
                html_mensagem = render_to_string(
                    "tickets/partials/chat_message.html",
                    {"interacao": interacao_salva, "request": request},
                )
                return JsonResponse({"status": "success", "html": html_mensagem})

            # Fallback para navegador sem JS (comportamento antigo)
            url_destino = reverse("tickets:detalhe_ticket", args=[pk])

            if origem:
                return redirect(f"{url_destino}?origin={origem}")
            
            return redirect(url_destino)

        else:

            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse(
                    {"status": "error", "errors": form.errors}, status=400
                )
            
            messages.error(request, "Erro ao enviar mensagem.")

    else:
        form = TicketInteracaoForm()

    # Determina a URL de retorno com base na origem preservando filtros e paginação
    voltar_url = reverse("tickets:meus_tickets")
    if origem == "fila":
        voltar_url = request.session.get('last_fila_url', reverse("tickets:fila_atendimento"))
    elif origem == "meus":
        voltar_url = request.session.get('last_meus_tickets_url', reverse("tickets:meus_tickets"))

    interacoes = ticket.interacoes.select_related("autor").all()

    # --- Seguidores (consultores extras designados pela liderança) ---
    pode_gerenciar_seguidores = _pode_gerenciar_seguidores(request.user)
    consultores_disponiveis = None
    seguidores_ids = []
    proprietario = (
        Cliente.objects.filter(person_id__iexact=ticket.owner).first()
        if ticket.owner else None
    )
    if pode_gerenciar_seguidores:
        consultores_disponiveis = (
            Cliente.objects.filter(groups__name="Consultores")
            .order_by("first_name", "username")
        )
        # Proprietário do ticket já é dono; não pode ser designado consultor.
        if ticket.owner:
            consultores_disponiveis = consultores_disponiveis.exclude(
                person_id__iexact=ticket.owner
            )
        seguidores_ids = list(ticket.seguidores.values_list("pk", flat=True))

    context = {
        "ticket": ticket,
        "interacoes": interacoes,
        "form": form,
        "origem": origem,
        "voltar_url": voltar_url,
        "pode_gerenciar_seguidores": pode_gerenciar_seguidores,
        "consultores_disponiveis": consultores_disponiveis,
        "seguidores_ids": seguidores_ids,
        "proprietario": proprietario,
    }
    return render(request, "tickets/detalhe_ticket.html", context)


@login_required(login_url="/login/")
def fila_atendimento(request: HttpRequest) -> HttpResponse:

    """
    Exibe a fila de tickets.
    - Suporte (Staff) / Liderança: Veem TODOS os tickets.
    - Consultores (sem cargo de liderança): Veem APENAS os seus tickets.
    """
    
    # 1. Identificação de Perfil
    is_consultor = request.user.groups.filter(name="Consultores").exists()
    
    # Verifica se o usuário é do grupo 'lider_suporte'
    is_lider = request.user.groups.filter(name="lider_suporte").exists()
    
    # Verifica se é equipe de suporte (Staff/Admin)
    is_support = getattr(request.user, 'is_support_team', False) or request.user.is_superuser

    # 2. Segurança: Acesso permitido para Suporte, Líderes ou Consultores
    if not is_support and not is_consultor and not is_lider:
        messages.warning(request, "Acesso restrito.")
        return redirect("tickets:meus_tickets")

    # 3. Base da Query
    tickets = (
        Ticket.objects.exclude(maximo_id__isnull=True)
        .select_related("cliente", "ambiente")
        .order_by("-data_criacao")
    )

    # LÓGICA DE VISIBILIDADE 
    if is_consultor and not is_support and not is_lider:

        if request.user.person_id:
            # Próprios (owner) + tickets que segue (designado pela liderança).
            tickets = tickets.filter(
                Q(owner__iexact=request.user.person_id) | Q(seguidores=request.user)
            ).distinct()

        else:
            # Sem ID Maximo ainda pode ver tickets em que foi colocado como seguidor.
            tickets = tickets.filter(seguidores=request.user).distinct()
            if not tickets.exists():
                messages.warning(request, "Seu usuário não possui um ID Maximo configurado.")

    # 4. Captura dos Filtros via GET
    status_filters = request.GET.getlist("status")
    location_filter = request.GET.get("location")
    search_query = request.GET.get("q")
    prioridade_filter = request.GET.get("prioridade")

    # Verifica se existe algum filtro de busca/refinamento aplicado (ignorando a paginação)
    tem_filtros = bool(status_filters or location_filter or search_query or prioridade_filter)

    # 5. Aplicação dos Filtros Opcionais
    if status_filters:
        tickets = tickets.filter(status_maximo__in=status_filters)

    if location_filter:
        tickets = tickets.filter(cliente__location=location_filter)

    if prioridade_filter:
        tickets = tickets.filter(prioridade=prioridade_filter)
        # Ignora os tickets críticos que já foram finalizados (a menos que um status específico seja filtrado)
        if prioridade_filter == "1" and not status_filters:
            tickets = tickets.exclude(status_maximo__in=['RESOLVED', 'CLOSED', 'CANCELLED'])

    if search_query:
        tickets = tickets.filter(
            Q(maximo_id__icontains=search_query)
            | Q(sumario__icontains=search_query)
            | Q(descricao__icontains=search_query)
            | Q(cliente__username__icontains=search_query)
            | Q(cliente__first_name__icontains=search_query)
            | Q(cliente__location__icontains=search_query)
            | Q(owner__icontains=search_query)
        )

    # Por padrão, oculta tickets encerrados da lista. Continuam pesquisáveis:
    # busca textual ou filtro explícito de status os revela.
    if not status_filters and not search_query:
        tickets = tickets.exclude(status_maximo__in=['RESOLVED', 'CLOSED', 'CANCELLED'])

    # 6. Dados para Dropdowns
    lista_locations = (
        Cliente.objects.values_list("location", flat=True)
        .exclude(location__isnull=True)
        .exclude(location__exact="")
        .distinct()
        .order_by("location")
    )

    status_choices = MAXIMO_STATUS_CHOICES

    # 7. Estatísticas
    status_encerrados = ['RESOLVED', 'CLOSED', 'CANCELLED']
    stats = {
        "total": tickets.count(),
        "criticos": tickets.filter(prioridade=1).exclude(status_maximo__in=status_encerrados).count(),
        "novos": tickets.filter(status_maximo="NEW").count(),
    }

    # 8. Paginação
    paginator = Paginator(tickets, 15)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # Salva a URL completa (com filtros e paginação) na sessão
    request.session['last_fila_url'] = request.get_full_path()

    context = {
        "tickets": page_obj,
        "lista_locations": lista_locations,
        "status_choices": status_choices,
        "filtros_atuais": request.GET,
        "status_selecionados": status_filters,
        "stats": stats,
        "is_consultor": is_consultor,
        "is_lider": is_lider,
        "stats": stats,
        "tem_filtros": tem_filtros,
    }
    
    return render(request, "tickets/fila_atendimento.html", context)

@login_required(login_url="/login/")
def download_anexo_interacao(request: HttpRequest, interacao_id: int) -> HttpResponse:
    """
    Gera uma URL segura ou serve o arquivo físico caso o sistema esteja em Fallback de erro.
    """
    interacao = get_object_or_404(TicketInteracao, pk=interacao_id)
    ticket = interacao.ticket

    if not _usuario_tem_acesso_ticket(request.user, ticket):
        messages.error(request, "Você não tem permissão para acessar este arquivo.")
        return redirect("tickets:meus_tickets")

    if not interacao.anexo:
        messages.warning(request, "Esta interação não possui anexo.")
        return redirect("tickets:detalhe_ticket", pk=ticket.id)

    try:
        audit.registrar(request.user, f"baixou anexo da interação #{interacao.id} (Ticket #{ticket.id})")
        if getattr(settings, 'USE_S3', False):
            # O sistema pergunta ao Storage se o arquivo caiu no disco local por falha da nuvem
            if hasattr(interacao.anexo.storage, 'is_local') and interacao.anexo.storage.is_local(interacao.anexo.name):
                arquivo = interacao.anexo.open('rb')
                filename = os.path.basename(interacao.anexo.name)
                return FileResponse(arquivo, as_attachment=True, filename=filename)
            else:
                # O arquivo está são e salvo na Oracle Cloud
                url_assinada = interacao.anexo.url
                return redirect(url_assinada)
        else:
            # Comportamento padrão 100% offline
            arquivo = interacao.anexo.open('rb')
            filename = os.path.basename(interacao.anexo.name)
            return FileResponse(arquivo, as_attachment=True, filename=filename)

    except FileNotFoundError:
        logger.error(f"Arquivo não encontrado no disco: {interacao.anexo.name}")
        messages.error(request, "Arquivo indisponível, contate o suporte.")
        return redirect("tickets:detalhe_ticket", pk=ticket.id)

    except Exception as e:
        logger.error(f"Erro inesperado ao servir anexo da interação {interacao_id}: {e}")
        messages.error(request, "Ocorreu um erro interno ao processar o download.")
        return redirect("tickets:detalhe_ticket", pk=ticket.id)


@login_required(login_url="/login/")
def download_anexo_multiplo(request: HttpRequest, anexo_id: str) -> HttpResponse:
    """
    Download de um anexo individual de interação (modelo InteracaoAnexo).
    Mesma lógica de fallback local/Oracle do download_anexo_interacao.
    """
    anexo = get_object_or_404(InteracaoAnexo, pk=anexo_id)
    ticket = anexo.interacao.ticket

    if not _usuario_tem_acesso_ticket(request.user, ticket):
        messages.error(request, "Você não tem permissão para acessar este arquivo.")
        return redirect("tickets:meus_tickets")

    try:
        audit.registrar(request.user, f"baixou anexo #{anexo.id} da interação #{anexo.interacao_id} (Ticket #{ticket.id})")
        if getattr(settings, 'USE_S3', False):
            if hasattr(anexo.arquivo.storage, 'is_local') and anexo.arquivo.storage.is_local(anexo.arquivo.name):
                arquivo = anexo.arquivo.open('rb')
                filename = os.path.basename(anexo.arquivo.name)
                return FileResponse(arquivo, as_attachment=True, filename=filename)
            else:
                return redirect(anexo.arquivo.url)
        else:
            arquivo = anexo.arquivo.open('rb')
            filename = os.path.basename(anexo.arquivo.name)
            return FileResponse(arquivo, as_attachment=True, filename=filename)

    except FileNotFoundError:
        logger.error(f"Arquivo não encontrado no disco: {anexo.arquivo.name}")
        messages.error(request, "Arquivo indisponível, contate o suporte.")
        return redirect("tickets:detalhe_ticket", pk=ticket.id)

    except Exception as e:
        logger.error(f"Erro inesperado ao servir anexo {anexo_id}: {e}")
        messages.error(request, "Ocorreu um erro interno ao processar o download.")
        return redirect("tickets:detalhe_ticket", pk=ticket.id)


@login_required
def marcar_notificacao_lida(request: Any, notificacao_id: int) -> Any:
    """
    Versão Sênior: Processa a leitura de notificações garantindo acesso 
    para a equipe de suporte/liderança mesmo em notificações de terceiros.
    """

    notificacao = get_object_or_404(Notificacao, pk=notificacao_id)

    pode_acessar = (notificacao.destinatario == request.user) or request.user.is_staff

    if not pode_acessar:
        raise Http404("Notificação não encontrada ou acesso negado.")

    if notificacao.destinatario == request.user and not notificacao.lida:
        notificacao.lida = True
        notificacao.save()
        audit.registrar(request.user, f"marcou notificação #{notificacao.id} como lida")

    if notificacao.ticket:
        return redirect("tickets:detalhe_ticket", pk=notificacao.ticket.pk)
    
    if hasattr(notificacao, 'link') and notificacao.link:
        return redirect(notificacao.link)

    return redirect("tickets:pagina_inicial")


@login_required(login_url="/login/")
@require_http_methods(["POST"])
def marcar_todas_notificacoes_lidas(request: HttpRequest) -> HttpResponse:
    """Marca todas as notificações não-lidas do usuário como lidas (bulk)."""
    Notificacao.objects.filter(destinatario=request.user, lida=False).update(lida=True)
    audit.registrar(request.user, "marcou todas notificações como lidas")
    return redirect(request.META.get("HTTP_REFERER", reverse("tickets:pagina_inicial")))


def _pode_gerenciar_seguidores(user: Cliente) -> bool:
    """Só equipe de suporte (staff) e liderança designam seguidores."""
    return bool(
        getattr(user, "is_support_team", False)
        or user.is_superuser
        or user.groups.filter(name="lider_suporte").exists()
    )


@login_required(login_url="/login/")
@require_http_methods(["POST"])
def gerenciar_seguidores(request: HttpRequest, pk: int) -> HttpResponse:
    """Define os seguidores de um ticket (consultores extras que ganham
    acesso de leitura+interação e passam a receber notificações).
    Restrito a suporte/liderança."""
    ticket = get_object_or_404(Ticket, pk=pk)

    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    if not _pode_gerenciar_seguidores(request.user):
        if is_ajax:
            return JsonResponse(
                {"status": "error", "message": "Sem permissão."}, status=403
            )
        messages.error(request, "Você não tem permissão para gerenciar seguidores.")
        return redirect("tickets:detalhe_ticket", pk=pk)

    ids = request.POST.getlist("seguidores")
    # Apenas usuários do grupo Consultores podem ser seguidores.
    consultores = Cliente.objects.filter(pk__in=ids, groups__name="Consultores")
    # Proprietário do ticket já é dono; não pode ser designado consultor.
    if ticket.owner:
        consultores = consultores.exclude(person_id__iexact=ticket.owner)
    ticket.seguidores.set(consultores)

    total = consultores.count()
    audit.registrar(
        request.user,
        f"atualizou seguidores do Ticket #{ticket.id} (total: {total})",
    )

    if is_ajax:
        return JsonResponse({"status": "success", "total": total})

    messages.success(request, "Seguidores do ticket atualizados.")
    return redirect("tickets:detalhe_ticket", pk=pk)


@login_required(login_url="/login/")
def notificacoes_badge(request: HttpRequest) -> JsonResponse:
    """Endpoint leve p/ polling AJAX do sino: retorna a contagem de
    notificações não-lidas + o HTML da lista do dropdown, para o JS
    atualizar número e conteúdo sem recarregar a página."""
    qs_nao_lidas = Notificacao.objects.filter(destinatario=request.user, lida=False)
    count = qs_nao_lidas.count()
    ultimas = qs_nao_lidas.order_by("-data_criacao")[:5]

    html = render_to_string(
        "tickets/partials/notificacoes_lista.html",
        {"notificacoes_list": ultimas, "notificacoes_count": count},
        request=request,
    )
    return JsonResponse({"count": count, "html": html})


def _get_next_url(request: HttpRequest) -> str | None:
    """
    Recupera o destino pós-login (parâmetro 'next') de forma segura.

    Valida host/esquema para evitar open redirect. Retorna None se ausente
    ou inválido.
    """
    nxt = request.POST.get("next") or request.GET.get("next")
    if nxt and url_has_allowed_host_and_scheme(
        nxt,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return nxt
    return None


def login_view(request: HttpRequest) -> HttpResponse:

    """
    Gerencia o login e a troca de senha obrigatória na mesma tela.
    """

    if request.user.is_authenticated:
        return redirect(_get_next_url(request) or "tickets:pagina_inicial")

    form = EmailAuthenticationForm(request, data=request.POST or None)
    form_troca_senha = None

    user_id_pendente = request.session.get('user_id_troca_senha')
    
    if request.method == "POST":
        if user_id_pendente:
            user = get_object_or_404(Cliente, pk=user_id_pendente)
            form_troca_senha = SetPasswordForm(user, request.POST)
            
            if form_troca_senha.is_valid():
                user = form_troca_senha.save()
                user.precisa_trocar_senha = False 
                user.save()
                
                # Autentica e limpa a sessão temporária
                auth_login(request, user, backend='tickets.backend.EmailBackend')
                update_session_auth_hash(request, user)
                
                remember_me = request.session.get('remember_me_pending', False)

                if remember_me:
                    request.session.set_expiry(1209600)

                else:
                    request.session.set_expiry(0)
                
                # Limpa os dados temporários da sessão
                del request.session['user_id_troca_senha']
                if 'remember_me_pending' in request.session:
                    del request.session['remember_me_pending']
                
                messages.success(request, "Senha definida com sucesso! Bem-vindo ao portal.")

                next_url = request.session.pop('next_url', None)
                return redirect(next_url or "tickets:pagina_inicial")

        elif form.is_valid():
            user = form.get_user()
            
            remember_me = request.POST.get('remember_me') == 'true'
            
            if user.precisa_trocar_senha:
                request.session['user_id_troca_senha'] = user.id
                request.session['remember_me_pending'] = remember_me
                request.session['next_url'] = _get_next_url(request)
                form_troca_senha = SetPasswordForm(user)

            else:
                auth_login(request, user, backend='tickets.backend.EmailBackend')

                if remember_me:
                    request.session.set_expiry(1209600)

                else:
                    request.session.set_expiry(0)

                return redirect(_get_next_url(request) or "tickets:pagina_inicial")

    context = {
        "form": form,
        "form_troca_senha": form_troca_senha,
        "is_troca_senha": bool(form_troca_senha),
        "next": _get_next_url(request) or "",
    }
    
    return render(request, "tickets/login.html", context)


def _exige_superuser(request: HttpRequest) -> None:
    """Bloqueia não-superuser. Autenticado-mas-não-superuser → 404 (esconde a
    existência da página); anônimo será redirecionado pelo @login_required."""
    if not request.user.is_superuser:
        raise Http404()


@login_required(login_url="/login/")
def logs_viewer(request: HttpRequest) -> HttpResponse:
    """Página de visualização de logs em tempo real (somente superuser)."""
    _exige_superuser(request)
    arquivos = logtail.available_log_files()
    selecionado = request.GET.get("file") or logtail.LOG_BASENAME
    caminho = logtail.resolve_log_path(selecionado)
    size = os.path.getsize(caminho)
    linhas, top_offset = logtail.read_lines_before(caminho, size, 200)
    pos = size
    contexto = {
        "arquivos": arquivos,
        "selecionado": selecionado,
        "linhas": linhas,
        "pos": pos,
        "top_offset": top_offset,
    }
    return render(request, "tickets/logs_viewer.html", contexto)


@login_required(login_url="/login/")
def logs_stream(request: HttpRequest) -> StreamingHttpResponse:
    """Endpoint SSE: emite linhas novas do log escolhido (somente superuser)."""
    _exige_superuser(request)
    selecionado = request.GET.get("file") or logtail.LOG_BASENAME
    caminho = logtail.resolve_log_path(selecionado)
    try:
        pos = int(request.GET.get("pos", "0"))
    except ValueError:
        pos = 0
    try:
        duration = float(request.GET.get("duration", "30"))
    except ValueError:
        duration = 30.0
    duration = max(0.0, min(duration, 30.0))  # teto de 30s por conexão

    resposta = StreamingHttpResponse(
        logtail.stream_events(caminho, pos, duration=duration),
        content_type="text/event-stream",
    )
    resposta["Cache-Control"] = "no-cache"
    resposta["X-Accel-Buffering"] = "no"  # impede buffering de SSE por proxy/nginx
    return resposta


@login_required(login_url="/login/")
def logs_history(request: HttpRequest) -> JsonResponse:
    """Paginação pra trás do histórico de logs (somente superuser)."""
    _exige_superuser(request)
    selecionado = request.GET.get("file") or logtail.LOG_BASENAME
    caminho = logtail.resolve_log_path(selecionado)
    try:
        offset = int(request.GET.get("offset", "0"))
    except ValueError:
        offset = 0
    try:
        n = int(request.GET.get("n", "500"))
    except ValueError:
        n = 500
    n = max(1, min(n, 2000))
    # Clampa o offset ao tamanho do arquivo: um offset gigante (ex. via GET
    # cross-origin) faria read_lines_before varrer o arquivo de trás pra frente
    # em passos de 64KB por ~offset/65536 iterações, prendendo o worker.
    offset = max(0, min(offset, os.path.getsize(caminho)))

    linhas, start_offset = logtail.read_lines_before(caminho, offset, n)
    if start_offset <= 0:
        older = logtail.older_file(selecionado)
        if older:
            older_path = logtail.resolve_log_path(older)
            cursor = {"file": older, "offset": os.path.getsize(older_path)}
        else:
            cursor = None
    else:
        cursor = {"file": selecionado, "offset": start_offset}
    return JsonResponse({"lines": linhas, "cursor": cursor})
