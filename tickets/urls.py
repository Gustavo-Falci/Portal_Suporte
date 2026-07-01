# tickets/urls.py
from django.urls import path
from django.contrib.auth.views import LogoutView
from django.conf import settings
from django.conf.urls.static import static
from . import views

app_name = "tickets"

urlpatterns = [
    # Páginas Públicas / Iniciais
    path("", views.pagina_inicial, name="pagina_inicial"),
    path("login/", views.login_view, name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),

    # Fluxo de Tickets
    path("criar/", views.criar_ticket, name="criar_ticket"),
    path("sucesso/", views.ticket_sucesso, name="ticket_sucesso"),
    path("meus-tickets/", views.meus_tickets, name="meus_tickets"),
    path("ticket/<int:pk>/", views.detalhe_ticket, name="detalhe_ticket"),
    path(
        "ticket/<int:pk>/seguidores/",
        views.gerenciar_seguidores,
        name="gerenciar_seguidores",
    ),

    # Área de Suporte
    path("fila-atendimento/", views.fila_atendimento, name="fila_atendimento"),

    # Logs em tempo real (somente superuser)
    path("logs/", views.logs_viewer, name="logs_viewer"),
    path("logs/stream/", views.logs_stream, name="logs_stream"),

    # Funcionalidades Auxiliares
    path(
        "interacao/anexo/<int:interacao_id>/",
        views.download_anexo_interacao,
        name="download_anexo",
    ),
    path(
        "interacao/anexo-multiplo/<uuid:anexo_id>/",
        views.download_anexo_multiplo,
        name="download_anexo_multiplo",
    ),
    path(
        "notificacao/ler/<int:notificacao_id>/",
        views.marcar_notificacao_lida,
        name="marcar_notificacao_lida",
    ),
    path(
        "notificacao/ler-todas/",
        views.marcar_todas_notificacoes_lidas,
        name="marcar_todas_notificacoes_lidas",
    ),
    path(
        "notificacoes/badge/",
        views.notificacoes_badge,
        name="notificacoes_badge",
    ),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)