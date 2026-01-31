from django.apps import AppConfig


class TicketsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "tickets"
    verbose_name = "Gestão de Suporte"

    def ready(self):
        # Importa os sinais quando a app estiver pronta
        import tickets.signals
