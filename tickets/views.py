from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpRequest, FileResponse
from django.contrib import messages
from django.urls import reverse
from .models import Ticket, TicketInteracao, Cliente, Notificacao, MAXIMO_STATUS_CHOICES
from .forms import TicketForm, TicketInteracaoForm
from django.db.models import Q
from .services import MaximoEmailService, NotificationService, MaximoSenderService
from django.template.loader import render_to_string
from django.http import JsonResponse
from django.core.paginator import Paginator
import logging
import os
import threading

logger = logging.getLogger(__name__)


# PÁGINA INICIAL
def pagina_inicial(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return render(request, "tickets/bem_vindo.html")
    else:
        return redirect("tickets:login")


# SUCESSO
@login_required(login_url="/login/")
def ticket_sucesso(request: HttpRequest) -> HttpResponse:
    return render(request, "tickets/sucesso.html")


# LISTAGEM DE TICKETS
@login_required(login_url="/login/")
def meus_tickets(request: HttpRequest) -> HttpResponse:
    """
    Exibe a lista de tickets abertos pelo usuário logado.
    """
    # select_related busca as ForeignKeys numa única query SQL (JOIN)
    # 1. Mantém a sua busca atual (Exemplo genérico)
    tickets = Ticket.objects.filter(cliente=request.user).select_related('area', 'ambiente').order_by('-data_criacao')

    # 2. APLICA A PAGINAÇÃO (Limite de 10)
    paginator = Paginator(tickets, 10) 
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        # Passamos 'page_obj' mas com o nome 'tickets' para não quebrar o loop do HTML
        "tickets": page_obj, 
    }
    
    return render(request, "tickets/meus_tickets.html", context)


# CRIAR TICKET
@login_required(login_url="/login/")
def criar_ticket(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = TicketForm(request.POST, request.FILES, user=request.user)

        if form.is_valid():
            ticket = form.save(commit=False)
            ticket.cliente = request.user

            # Tratamento de upload simplificado na view
            anexo_upload = request.FILES.get("arquivo")
            if anexo_upload:
                ticket.anexo = anexo_upload

            ticket.save()

            # O código "sujo" de e-mail foi substituído por uma única linha:
            MaximoEmailService.enviar_ticket_maximo(ticket, request.user, anexo_upload)

            return redirect("tickets:ticket_sucesso")
    else:
        form = TicketForm(user=request.user)

    return render(request, "tickets/criar_ticket.html", {"form": form})


# DETALHE DO TICKET
@login_required(login_url="/login/")
def detalhe_ticket(request: HttpRequest, pk: int) -> HttpResponse:
    ticket = get_object_or_404(Ticket, pk=pk)
    origem = request.GET.get("origin")

    # Permissão (mantida)
    if not (request.user.is_support_team or ticket.cliente == request.user):
        messages.error(request, "Você não tem permissão para visualizar este ticket.")
        return redirect("meus_tickets")

    if request.method == "POST":
        form = TicketInteracaoForm(request.POST, request.FILES)
        if form.is_valid():
            interacao = form.save(commit=False)
            interacao.ticket = ticket
            interacao.autor = request.user
            interacao.save()

            sincronizado = MaximoSenderService.enviar_interacao(ticket, interacao)
            
            if not sincronizado:
                # Adiciona aviso visual (aparecerá se a página recarregar ou se o JS tratar mensagens)
                messages.warning(request, "Mensagem salva localmente, mas houve instabilidade na sincronização com o IBM Maximo.")

            # --- 1. ENVIO DE E-MAIL EM SEGUNDO PLANO (THREADING) ---
            # Isso impede que o usuário fique esperando o SMTP responder
            email_thread = threading.Thread(
                target=NotificationService.notificar_nova_interacao,
                args=(ticket, interacao),
            )
            email_thread.start()

            # Atualiza data de modificação
            ticket.save()

            # --- 2. RESPOSTA PARA AJAX (SEM REFRESH) ---
            # Verifica se a requisição veio do JavaScript
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                # Renderiza apenas o pedacinho do chat novo
                html_mensagem = render_to_string(
                    "tickets/partials/chat_message.html",
                    {"interacao": interacao, "request": request},
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

    interacoes = ticket.interacoes.select_related("autor").all()

    context = {
        "ticket": ticket,
        "interacoes": interacoes,
        "form": form,
        "origem": origem,
    }
    return render(request, "tickets/detalhe_ticket.html", context)


@login_required(login_url="/login/")
def fila_atendimento(request: HttpRequest) -> HttpResponse:
    """
    Exibe TODOS os tickets com filtros avançados para a equipe de suporte.
    """
    # 1. Segurança
    if not request.user.is_support_team:
        messages.warning(request, "Acesso restrito à equipe de suporte.")
        return redirect("tickets:meus_tickets")

    # 2. Base da Query
    tickets = (
        Ticket.objects.all()
        .select_related("cliente", "ambiente")
        .order_by("-data_criacao")
    )

    # 3. Captura dos Filtros via GET
    status_filter = request.GET.get("status")
    location_filter = request.GET.get("location")
    search_query = request.GET.get("q")

    # 4. Aplicação dos Filtros
    if status_filter:
        tickets = tickets.filter(status_maximo=status_filter)

    if location_filter:
        # Filtra tickets onde o 'location' do cliente é igual ao selecionado
        tickets = tickets.filter(cliente__location=location_filter)

    if search_query:
        # Busca por ID, Título, Descrição ou Nome do Cliente
        tickets = tickets.filter(
            Q(maximo_id__icontains=search_query)
            | Q(sumario__icontains=search_query)
            | Q(descricao__icontains=search_query)
            | Q(cliente__username__icontains=search_query)
            | Q(cliente__first_name__icontains=search_query)
            | Q(cliente__location__icontains=search_query)
        )

    # 5. Dados para popular os Dropdowns do Filtro
    # Pega apenas clientes que não são staff/suporte para limpar a lista
    lista_locations = (
        Cliente.objects.values_list("location", flat=True)
        .exclude(location__isnull=True)
        .exclude(location__exact="")
        .distinct()
        .order_by("location")
    )

    status_choices = MAXIMO_STATUS_CHOICES

    # 6. Estatísticas Rápidas (Opcional, mas fica pro)
    stats = {
        "total": tickets.count(),
        "criticos": tickets.filter(prioridade=1).count(),
        "novos": tickets.filter(status_maximo="NEW").count(),
    }

    # Define quantos tickets aparecem por página (ex: 15)
    paginator = Paginator(tickets, 15)

    # Pega o número da página da URL (?page=2)
    page_number = request.GET.get("page")

    # Obtém apenas os tickets daquela página específica
    page_obj = paginator.get_page(page_number)

    context = {
        "tickets": page_obj,
        "lista_locations": lista_locations,
        "status_choices": status_choices,
        "filtros_atuais": request.GET,  # Para manter o form preenchido
        "stats": stats,
    }
    return render(request, "tickets/fila_atendimento.html", context)


@login_required(login_url="/login/")
def download_anexo_interacao(request: HttpRequest, interacao_id: int) -> HttpResponse:
    """
    Serve o anexo de forma segura e trata erros caso o arquivo não exista.
    """
    # 1. Busca a interação ou retorna 404 se o ID não existir no banco
    interacao = get_object_or_404(TicketInteracao, pk=interacao_id)
    ticket = interacao.ticket

    # 2. Segurança: Verifica se o usuário é o dono do ticket OU da equipe de suporte
    # (Reaproveitando a lógica is_support_team do seu Model Cliente)
    if ticket.cliente != request.user and not request.user.is_support_team:
        messages.error(request, "Você não tem permissão para acessar este arquivo.")
        return redirect("tickets:detalhe_ticket", pk=ticket.id)

    # 3. Verifica se o campo anexo está preenchido
    if not interacao.anexo:
        messages.warning(request, "Esta interação não possui anexo.")
        return redirect("tickets:detalhe_ticket", pk=ticket.id)

    try:
        # 4. Tenta abrir o arquivo
        # O .path pode falhar dependendo do Storage (S3 vs Local),
        # mas .open() é o método agnóstico do Django.
        arquivo = interacao.anexo.open()

        # Opcional: Definir o nome do arquivo no download
        filename = os.path.basename(interacao.anexo.name)

        # Retorna o arquivo como download (as_attachment=True)
        # ou visualização no navegador (as_attachment=False)
        return FileResponse(arquivo, as_attachment=True, filename=filename)

    except FileNotFoundError:
        # 5. Tratamento de Erro: Arquivo consta no banco, mas não no disco
        messages.error(request, "Arquivo indisponivel, contate o suporte.")
        return redirect("tickets:detalhe_ticket", pk=ticket.id)

    except Exception as e:
        # 6. Erro genérico (ex: permissão de leitura no disco, erro de IO)
        messages.error(request, f"Erro ao tentar abrir o anexo: {str(e)}")
        return redirect("tickets:detalhe_ticket", pk=ticket.id)


@login_required
def marcar_notificacao_lida(request, notificacao_id):
    notificacao = get_object_or_404(
        Notificacao, pk=notificacao_id, destinatario=request.user
    )

    notificacao.lida = True
    notificacao.save()

    # Redireciona para o link da notificação (ex: detalhe do ticket)
    if notificacao.link:
        return redirect(notificacao.link)
    return redirect("tickets:pagina_inicial")
