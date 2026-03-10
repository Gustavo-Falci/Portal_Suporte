from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import Cliente, Ambiente, Area, Ticket, TicketInteracao, Notificacao, TicketAnexo

# Customização do Cabeçalho
admin.site.site_header = "Portal de Suporte | Administração"
admin.site.site_title = "IT Consol Admin"
admin.site.index_title = "Gestão de Utilizadores e Ativos"


# Chat dentro do Ticket
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

class TicketAnexoInline(admin.TabularInline):

    model = TicketAnexo
    extra = 0
    readonly_fields = ('data_envio',)
    # Opcional: Para evitar que deletem arquivos acidentalmente
    # can_delete = False 

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

    list_select_related = ("cliente", "area", "ambiente")
    autocomplete_fields = ["cliente"]
    readonly_fields = ("data_criacao", "data_atualizacao", "maximo_id")
    ordering = ("-data_criacao",)

    inlines = [TicketAnexoInline, TicketInteracaoInline]

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
                "fields": ("area", "ambiente") 
            },
        ),
        (
            "Integração Maximo",
            {
                "fields": ("maximo_id", "data_criacao", "data_atualizacao"),
                "classes": ("collapse",),
            },
        ),
    )


@admin.register(Ambiente)
class AmbienteAdmin(admin.ModelAdmin):

    # Alteramos para usar uma função customizada para exibir os donos
    list_display = ("nome_ambiente", "numero_ativo", "get_clientes_vinculados")
    
    # Busca ajustada para o novo nome do campo (clientes__username)
    search_fields = ("nome_ambiente", "numero_ativo", "clientes__username")

    # ManyToMany funciona melhor com filter_horizontal ou autocomplete_fields
    autocomplete_fields = ["clientes"] 

    # Função para listar os nomes na tabela do admin
    def get_clientes_vinculados(self, obj):
        return ", ".join([c.username for c in obj.clientes.all()])
    
    get_clientes_vinculados.short_description = "Clientes Vinculados"


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


# Registar Notificações ajuda a debugar se o "sininho" não funcionar
@admin.register(Notificacao)
class NotificacaoAdmin(admin.ModelAdmin):

    list_display = ("destinatario", "titulo", "lida", "data_criacao")
    list_filter = ("lida", "tipo")
    search_fields = ("destinatario__username", "mensagem")
