from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpRequest, FileResponse
from django.contrib import messages
from django.urls import reverse
from .models import Ticket, TicketInteracao, Cliente, Notificacao, MAXIMO_STATUS_CHOICES, TicketAnexo
from .forms import TicketForm, TicketInteracaoForm
from django.db.models import Q
from django.db import transaction
from .services import MaximoEmailService, NotificationService, MaximoSenderService
from django.template.loader import render_to_string
from django.http import JsonResponse
from django.core.paginator import Paginator
from django.contrib.auth import login as auth_login, update_session_auth_hash
from django.contrib.auth.forms import SetPasswordForm
from .forms import EmailAuthenticationForm
import logging
import os
import threading

logger = logging.getLogger(__name__)


# PÁGINA INICIAL
def pagina_inicial(request: HttpRequest) -> HttpResponse:
    if not request.user.is_authenticated:
        return redirect("tickets:login")

    user = request.user
    
    # 1. QuerySet Base (Apenas tickets deste cliente)
    qs_tickets = Ticket.objects.filter(cliente=user)

    # 2. Estatísticas Rápidas
    # Consideramos "Em Aberto" tudo que não está Fechado, Resolvido ou Cancelado
    status_encerrados = ['RESOLVED', 'CLOSED', 'CANCELLED']
    
    total_abertos = qs_tickets.exclude(status_maximo__in=status_encerrados).count()
    total_geral = qs_tickets.count()

    # 3. Últimos 3 Tickets (Para acesso rápido)
    ultimos_tickets = qs_tickets.order_by('-data_criacao')[:3]

    context = {
        "total_abertos": total_abertos,
        "total_geral": total_geral,
        "ultimos_tickets": ultimos_tickets,
        # O nome do usuário já vem no request.user, mas podemos formatar se quiser
        "primeiro_nome": user.get_short_name() or user.username
    }

    return render(request, "tickets/bem_vindo.html", context)


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
        # Passamos user=request.user para filtrar a Area no __init__ do form
        form = TicketForm(request.POST, request.FILES, user=request.user)

        if form.is_valid():
            try:
                with transaction.atomic():
                    # 1. Salva o Ticket
                    ticket = form.save(commit=False)
                    ticket.cliente = request.user
                    ticket.save()

                    # 2. Processa Anexos no Banco
                    anexos_upload = request.FILES.getlist("arquivo")
                    if anexos_upload:
                        for arquivo_temp in anexos_upload:
                            TicketAnexo.objects.create(ticket=ticket, arquivo=arquivo_temp)
                
                # 3. Envio de E-mail (Fora da transação atômica do banco)
                # Se o e-mail falhar, o ticket continua salvo (o que é bom, pois o suporte pode ver)
                try:
                    MaximoEmailService.enviar_ticket_maximo(ticket, request.user, anexos_upload)
                except Exception as e:
                    logger.error(f"Erro no envio de e-mail (Ticket {ticket.id}): {e}")
                    # Opcional: messages.warning(request, "Ticket criado, mas houve erro no envio do e-mail.")

                return redirect("tickets:ticket_sucesso")

            except Exception as e:
                # Se der erro ao salvar no banco (ticket ou anexos), nada é salvo
                logger.error(f"Erro ao criar ticket no banco: {e}")
                messages.error(request, "Ocorreu um erro ao salvar o ticket. Tente novamente.")
    else:
        form = TicketForm(user=request.user)

    return render(request, "tickets/criar_ticket.html", {"form": form})


# DETALHE DO TICKET
@login_required(login_url="/login/")
def detalhe_ticket(request: HttpRequest, pk: int) -> HttpResponse:
    ticket = get_object_or_404(Ticket, pk=pk)
    origem = request.GET.get("origin")

    is_dono = (ticket.cliente == request.user)
    is_staff = getattr(request.user, 'is_support_team', False) or request.user.is_superuser
    is_lider = request.user.groups.filter(name="lider_suporte").exists()
    
    # Verifica se é o consultor dono do ticket
    is_owner_assigned = False
    if request.user.person_id and ticket.owner:
        is_owner_assigned = (ticket.owner.lower() == request.user.person_id.lower())

    # Lógica de bloqueio: Se não for nenhuma das opções acima, expulsa
    if not (is_dono or is_staff or is_lider or is_owner_assigned):
        messages.error(request, "Você não tem permissão para visualizar este ticket.")
        return redirect("tickets:meus_tickets")

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
    Exibe a fila de tickets.
    - Suporte (Staff) / Liderança: Veem TODOS os tickets.
    - Consultores (sem cargo de liderança): Veem APENAS os seus tickets.
    """
    
    # 1. Identificação de Perfil
    is_consultor = request.user.groups.filter(name="Consultores").exists()
    
    # NOVO: Verifica se o usuário é do grupo 'lider_suporte'
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

    # --- LÓGICA DE VISIBILIDADE ---
    # O filtro por dono só é aplicado se for Consultor E NÃO FOR (Suporte OU Líder)
    # Se FOLIVEIRA estiver no grupo 'lider_suporte', ele pula este if e vê tudo.
    if is_consultor and not is_support and not is_lider:
        if request.user.person_id:
            tickets = tickets.filter(owner__iexact=request.user.person_id)
        else:
            tickets = Ticket.objects.none()
            messages.warning(request, "Seu usuário não possui um ID Maximo configurado.")
    # ------------------------------

    # 4. Captura dos Filtros via GET
    status_filter = request.GET.get("status")
    location_filter = request.GET.get("location")
    search_query = request.GET.get("q")

    # 5. Aplicação dos Filtros Opcionais
    if status_filter:
        tickets = tickets.filter(status_maximo=status_filter)

    if location_filter:
        tickets = tickets.filter(cliente__location=location_filter)

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
    stats = {
        "total": tickets.count(),
        "criticos": tickets.filter(prioridade=1).count(),
        "novos": tickets.filter(status_maximo="NEW").count(),
    }

    # 8. Paginação
    paginator = Paginator(tickets, 15)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    context = {
        "tickets": page_obj,
        "lista_locations": lista_locations,
        "status_choices": status_choices,
        "filtros_atuais": request.GET,
        "stats": stats,
        # Passamos as flags para o template
        "is_consultor": is_consultor,
        "is_lider": is_lider, 
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
    
def login_view(request: HttpRequest) -> HttpResponse:
    """
    Gerencia o login e a troca de senha obrigatória na mesma tela.
    """
    if request.user.is_authenticated:
        return redirect("tickets:pagina_inicial")

    form = EmailAuthenticationForm(request, data=request.POST or None)
    form_troca_senha = None

    # Verificamos se há um ID de usuário na sessão (indicando que ele passou pela fase 1)
    user_id_pendente = request.session.get('user_id_troca_senha')
    
    if request.method == "POST":
        # CASO 2: O usuário já autenticou e agora enviou a NOVA SENHA
        if user_id_pendente:
            user = get_object_or_404(Cliente, pk=user_id_pendente)
            form_troca_senha = SetPasswordForm(user, request.POST)
            
            if form_troca_senha.is_valid():
                user = form_troca_senha.save()
                user.precisa_trocar_senha = False  # Flag que criamos no models.py
                user.save()
                
                # Autentica e limpa a sessão temporária
                auth_login(request, user, backend='tickets.backend.EmailBackend')
                update_session_auth_hash(request, user)
                del request.session['user_id_troca_senha']
                
                messages.success(request, "Senha definida com sucesso! Bem-vindo ao portal.")
                return redirect("tickets:pagina_inicial")
        
        # CASO 1: Tentativa de login normal (E-mail e Senha Padrão)
        elif form.is_valid():
            user = form.get_user()
            
            if user.precisa_trocar_senha:
                # Não logamos ainda! Guardamos o ID dele para a fase 2
                request.session['user_id_troca_senha'] = user.id
                form_troca_senha = SetPasswordForm(user)
                # Mantemos o 'form' de login vazio para o template saber que mudou de fase
            else:
                auth_login(request, user, backend='tickets.backend.EmailBackend')
                return redirect("tickets:pagina_inicial")

    context = {
        "form": form,
        "form_troca_senha": form_troca_senha,
        "is_troca_senha": bool(form_troca_senha)
    }
    return render(request, "tickets/login.html", context)
