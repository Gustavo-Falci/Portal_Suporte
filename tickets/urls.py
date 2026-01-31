from django.urls import path
from django.contrib.auth.views import LoginView, LogoutView
from django.conf import settings
from django.conf.urls.static import static
from . import views, forms

# BEST PRACTICE: Define um namespace para facilitar reversão de URLs
# Ex: reverse('tickets:detalhe_ticket') em vez de apenas 'detalhe_ticket'
app_name = "tickets"

urlpatterns = [
    # Páginas Públicas / Iniciais
    path("", views.pagina_inicial, name="pagina_inicial"),
    # Autenticação
    path(
        "login/",
        LoginView.as_view(
            template_name="tickets/login.html",
            authentication_form=forms.EmailAuthenticationForm,
        ),
        name="login",
    ),
    # Dica: Certifique-se de ter LOGOUT_REDIRECT_URL = 'login' no settings.py
    path("logout/", LogoutView.as_view(), name="logout"),
    # Fluxo de Tickets
    path("criar/", views.criar_ticket, name="criar_ticket"),
    path("sucesso/", views.ticket_sucesso, name="ticket_sucesso"),
    path("meus-tickets/", views.meus_tickets, name="meus_tickets"),
    path("ticket/<int:pk>/", views.detalhe_ticket, name="detalhe_ticket"),
    # Área de Suporte
    path("fila-atendimento/", views.fila_atendimento, name="fila_atendimento"),
    # Funcionalidades Auxiliares (Anexos e Notificações)
    path(
        "interacao/anexo/<int:interacao_id>/",
        views.download_anexo_interacao,
        name="download_anexo",
    ),
    path(
        "notificacao/ler/<int:notificacao_id>/",
        views.marcar_notificacao_lida,
        name="marcar_notificacao_lida",
    ),
]

# Configuração para servir arquivos de mídia (Uploads) em ambiente de desenvolvimento
# IMPORTANTE: Em produção, isso deve ser gerenciado pelo servidor web (Nginx/Apache) ou S3.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
