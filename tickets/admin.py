from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import Cliente, Ambiente, Area, Ticket, TicketInteracao, Notificacao

# Customização do Cabeçalho
admin.site.site_header = "Portal de Suporte | Administração"
admin.site.site_title = "IT Consol Admin"
admin.site.index_title = "Gestão de Utilizadores e Ativos"


# --- INLINE: Chat dentro do Ticket ---
class TicketInteracaoInline(admin.TabularInline):
    model = TicketInteracao
    extra = 0
    # Campos que não devem ser editados para manter integridade histórica
    readonly_fields = ("data_criacao",)
    fields = ("autor", "mensagem", "anexo", "data_criacao")

    # Impede deleção de mensagens para fins de auditoria
    can_delete = False

    # Opcional: Impede edição também, se quiseres um log imutável
    # def has_change_permission(self, request, obj=None):
    #     return False


@admin.register(Cliente)
class ClienteAdmin(UserAdmin):
    list_display = (
        "username",
        "email",
        "get_full_name",
        "location",
        "person_id",
        "is_staff",
        "is_active",
    )
    list_filter = ("is_staff", "is_active", "location", "groups")

    # 'search_fields' é OBRIGATÓRIO para o autocomplete_fields funcionar noutros models
    search_fields = ("username", "first_name", "last_name", "email", "person_id")

    fieldsets = UserAdmin.fieldsets + (
        (
            "Integração Maximo",
            {
                "fields": ("location", "person_id"),
            },
        ),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        (
            "Integração Maximo",
            {
                "fields": ("location", "person_id"),
            },
        ),
    )


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "sumario",
        "cliente",
        "prioridade",
        "status_maximo",
        "maximo_id",
        "data_criacao",
    )
    list_filter = ("status_maximo", "prioridade", "data_criacao", "area")
    search_fields = (
        "sumario",
        "descricao",
        "cliente__username",
        "cliente__email",
        "maximo_id",
    )

    # PERFORMANCE: Evita queries duplicadas ao listar tickets
    list_select_related = ("cliente", "area", "ambiente")

    # UX: Transforma o dropdown gigante numa caixa de busca
    autocomplete_fields = ["cliente"]

    # Protege campos de auditoria e integração
    readonly_fields = ("data_criacao", "data_atualizacao", "maximo_id")

    ordering = ("-data_criacao",)
    inlines = [TicketInteracaoInline]

    fieldsets = (
        (
            "Dados do Chamado",
            {
                "fields": (
                    "sumario",
                    "descricao",
                    "cliente",
                    "status_maximo",
                    "prioridade",
                )
            },
        ),
        (
            "Classificação",
            {
                # CORREÇÃO CRÍTICA: Mudado de 'arquivo' para 'anexo'
                "fields": ("area", "ambiente", "anexo")
            },
        ),
        (
            "Integração Maximo",
            {
                # Adicionei 'maximo_id' aqui como readonly (definido acima)
                "fields": ("maximo_id", "data_criacao", "data_atualizacao"),
                "classes": ("collapse",),
            },
        ),
    )


@admin.register(Ambiente)
class AmbienteAdmin(admin.ModelAdmin):
    list_display = ("nome_ambiente", "numero_ativo", "cliente")
    search_fields = ("nome_ambiente", "numero_ativo", "cliente__username")

    # Melhora performance e UX
    list_select_related = ("cliente",)
    autocomplete_fields = ["cliente"]


@admin.register(Area)
class AreaAdmin(admin.ModelAdmin):
    list_display = ("nome_area", "cliente")
    # Adicionado busca pelo cliente também
    search_fields = ("nome_area", "cliente__username")

    list_select_related = ("cliente",)
    autocomplete_fields = ["cliente"]


@admin.register(TicketInteracao)
class TicketInteracaoAdmin(admin.ModelAdmin):
    list_display = ("id", "ticket", "autor", "data_criacao", "tem_anexo")
    list_filter = ("data_criacao", "autor__username")
    search_fields = ("mensagem", "ticket__sumario")

    # Performance
    list_select_related = ("ticket", "autor")

    @admin.display(boolean=True, description="Anexo?")
    def tem_anexo(self, obj):
        return bool(obj.anexo)


# BÓNUS: Registar Notificações ajuda a debugar se o "sininho" não funcionar
@admin.register(Notificacao)
class NotificacaoAdmin(admin.ModelAdmin):
    list_display = ("destinatario", "titulo", "lida", "data_criacao")
    list_filter = ("lida", "tipo")
    search_fields = ("destinatario__username", "mensagem")
