from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError
from .models import Ambiente, Area, Ticket, TicketInteracao
import os
import mimetypes

# --- UTILITÁRIO DE VALIDAÇÃO (DRY & Segurança) ---


def _validar_anexo_comum(arquivo):
    """
    Validação centralizada para uploads (Ticket e Chat).
    Verifica tamanho, extensão e MIME type.
    """
    if not arquivo:
        return None

    # 1. Validar tamanho (Limite: 150MB)
    limit_mb = 150
    if arquivo.size > limit_mb * 1024 * 1024:
        raise ValidationError(
            f"O arquivo é muito grande. Máximo permitido: {limit_mb}MB."
        )

    # 2. Validar extensão
    ext = os.path.splitext(arquivo.name)[1].lower()
    extensoes_validas = [
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".txt",
        ".xlsx",
        ".xls",
        ".docx",
        ".doc",
        ".csv",
        ".zip",
        ".rar",
        ".xml",
        ".pptx",
        ".ppt",
    ]

    if ext not in extensoes_validas:
        raise ValidationError(
            f"Arquivo '{ext}' não permitido. Use apenas PDF, Imagens, Word, zip..."
        )

    # 3. Validação de MIME type (Segurança reforçada)
    # Adivinha o tipo baseado no nome do ficheiro (não é perfeito, mas ajuda)
    content_type_guess, _ = mimetypes.guess_type(arquivo.name)

    # Lista de tipos seguros
    allowed_mimes = [
        "application/pdf",
        "image/png",
        "image/jpeg",
        "text/plain",
        "text/csv",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-powerpoint", 
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ]

    if content_type_guess:
        is_text = "text" in content_type_guess
        is_valid = content_type_guess in allowed_mimes

        if not (is_valid or is_text):
            raise ValidationError(
                f"Formato de arquivo inválido ({content_type_guess})."
            )
            # pass
    return arquivo


# 1. FORMULÁRIO DE LOGIN


class EmailAuthenticationForm(AuthenticationForm):
    """
    Formulário de autenticação customizado para usar E-mail como login.
    """

    username = forms.CharField(
        label="E-mail",
        max_length=254,
        widget=forms.EmailInput(
            attrs={
                "autofocus": True,
                "class": "form-control",
            }
        ),
    )
    password = forms.CharField(
        label="Senha",
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
            }
        ),
    )

    error_messages = {
        "invalid_login": "Login inválido. E-mail ou senha incorretos.",
        "inactive": "Esta conta está inativa. Contacte o suporte.",
    }


# 2. FORMULÁRIO DE ABERTURA DE TICKET


class TicketForm(forms.ModelForm):
    """
    Formulário principal de abertura de chamados.
    Adaptado para aceitar o campo HTML 'arquivo' e salvar no Model 'anexo'.
    """

    # Campo explícito para bater com o name="arquivo" do seu HTML
    arquivo = forms.FileField(
        required=False,
        widget=forms.FileInput(attrs={"class": "form-control"}),
        label="Anexo de Evidência",
    )

    class Meta:
        model = Ticket
        # Removemos 'anexo' da lista automática, pois vamos tratar manualmente via 'arquivo'
        fields = ["sumario", "descricao", "ambiente", "prioridade", "area"]

        widgets = {
            "sumario": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Resumo curto do problema",
                }
            ),
            "descricao": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 5,
                    "placeholder": "Descreva detalhadamente o que aconteceu...",
                }
            ),
            "ambiente": forms.Select(attrs={"class": "form-select"}),
            "prioridade": forms.Select(attrs={"class": "form-select"}),
            "area": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        if user:
            self.fields["ambiente"].queryset = Ambiente.objects.filter(cliente=user)

            location_str = (
                str(user.location).upper() if getattr(user, "location", None) else ""
            )
            empresas_com_area = ["PAMPA", "ABL"]
            tem_acesso_area = any(
                empresa in location_str for empresa in empresas_com_area
            )

            if tem_acesso_area:
                self.fields["area"].queryset = Area.objects.filter(cliente=user)
                self.fields["area"].required = True
            else:
                self.fields["area"].queryset = Area.objects.none()
                self.fields["area"].required = False
                self.fields["area"].widget = forms.HiddenInput()

    def clean_arquivo(self):
        """Valida o campo 'arquivo' vindo do HTML."""
        # Usa a função de validação comum que criamos anteriormente
        return _validar_anexo_comum(self.cleaned_data.get("arquivo"))

    def save(self, commit=True):
        """Move o arquivo validado para o campo correto do modelo antes de salvar."""
        ticket = super().save(commit=False)

        # Pega o arquivo limpo e validado
        arquivo_validado = self.cleaned_data.get("arquivo")

        if arquivo_validado:
            # Mapeia: Form 'arquivo' -> Model 'anexo'
            ticket.anexo = arquivo_validado

        if commit:
            ticket.save()
        return ticket


# 3. FORMULÁRIO DE INTERAÇÃO (RESPOSTAS)


class TicketInteracaoForm(forms.ModelForm):
    """
    Formulário para adicionar comentários/respostas ao ticket.
    """

    class Meta:
        model = TicketInteracao
        fields = ["mensagem", "anexo"]
        widgets = {
            "mensagem": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 3,
                }
            ),
            "anexo": forms.FileInput(attrs={"class": "form-control"}),
        }

    def clean_anexo(self):
        return _validar_anexo_comum(self.cleaned_data.get("anexo"))
