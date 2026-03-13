import os
import mimetypes
from typing import Any

from django import forms
from django.contrib.auth.forms import AuthenticationForm, SetPasswordForm
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.conf import settings

from .models import Ambiente, Area, Ticket, TicketInteracao

# Obtém o modelo de cliente atual
Cliente = get_user_model()

# --- UTILITÁRIO DE VALIDAÇÃO (DRY & Segurança) ---

def _validar_anexo_comum(arquivo: Any) -> Any:

    """
    Validação centralizada para uploads (Ticket e Chat).
    Verifica tamanho, extensão e MIME type.
    """

    if not arquivo:
        return None

    # 1. Validar tamanho (Limite: 150MB)
    max_size_bytes = getattr(settings, 'FILE_UPLOAD_MAX_MEMORY_SIZE', 150 * 1024 * 1024)
    if arquivo.size > max_size_bytes:
        limit_mb = max_size_bytes / (1024 * 1024)
        raise ValidationError(
            f"O arquivo é muito grande. Máximo permitido: {int(limit_mb)}MB."
        )

    # 2. Validar extensão
    ext = os.path.splitext(arquivo.name)[1].lower()
    extensoes_validas = [
        ".pdf", ".png", ".jpg", ".jpeg", ".txt", ".xlsx", ".xls",
        ".docx", ".doc", ".csv", ".zip", ".rar", ".xml", ".pptx", ".ppt",
    ]

    if ext not in extensoes_validas:
        raise ValidationError(
            f"Arquivo '{ext}' não permitido. Use apenas PDF, Imagens, Word, zip..."
        )

    # 3. Validação de MIME type (Segurança reforçada)
    content_type_guess, _ = mimetypes.guess_type(arquivo.name)

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
            
    return arquivo


# 1. FORMULÁRIOS DE AUTENTICAÇÃO E SENHA

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
                "id": "floatingEmail",
                "placeholder": "nome@exemplo.com"
            }
        ),
    )

    password = forms.CharField(
        label="Senha",
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "id": "floatingPassword",
                "placeholder": "Senha"
            }
        ),
    )

    error_messages = {
        "invalid_login": "Login inválido. E-mail ou senha incorretos.",
        "inactive": "Esta conta está inativa. Contacte o suporte.",
    }


class NovaSenhaForm(SetPasswordForm):

    """
    Formulário de Troca de Senha Obrigatória no Primeiro Acesso.
    Herda as validações nativas do Django (AUTH_PASSWORD_VALIDATORS).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Remove os textos de ajuda nativos gigantes do Django 
        # para usarmos a nossa validação visual do frontend limpa.
        for field in self.fields.values():
            field.help_text = ''
            field.widget.attrs.update({'class': 'form-control'})


# 2. FORMULÁRIO DE ABERTURA DE TICKET

class TicketForm(forms.ModelForm):

    """
    Formulário principal de abertura de chamados.
    """

    documento_requisicao = forms.FileField(
        required=True,
        widget=forms.FileInput(attrs={"class": "form-control"}),
        label="Documento de Requisição de Ticket",
        error_messages={
            'required': 'O anexo do Documento de Requisição é obrigatório.'
        }
    )

    arquivo = forms.FileField(
        required=False,
        widget=forms.FileInput(attrs={"class": "form-control"}),
        label="Anexos de Evidência",
    )

    class Meta:

        model = Ticket
        fields = ["sumario", "descricao", "ambiente", "prioridade", "area"]

        widgets = {
            "sumario": forms.TextInput(attrs={"class": "form-control", "placeholder": "Resumo curto do problema"}),
            "descricao": forms.Textarea(attrs={"class": "form-control", "rows": 5, "placeholder": "Descreva detalhadamente..."}),
            "ambiente": forms.Select(attrs={"class": "form-select"}),
            "prioridade": forms.Select(attrs={"class": "form-select"}),
            "area": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        if user:

            self.fields["ambiente"].queryset = Ambiente.objects.filter(clientes=user)

            location_str = str(user.location).upper() if getattr(user, "location", None) else ""
            empresas_com_area = ["PAMPA", "ABL"]
            
            tem_acesso_area = any(empresa in location_str for empresa in empresas_com_area)

            if tem_acesso_area:
                self.fields["area"].queryset = Area.objects.filter(cliente=user)
                self.fields["area"].required = False
            else:
                self.fields["area"].queryset = Area.objects.none()
                self.fields["area"].required = False
                self.fields["area"].widget = forms.HiddenInput()

    def clean_documento_requisicao(self) -> Any:

        doc = self.cleaned_data.get("documento_requisicao")

        if doc:
            ext = os.path.splitext(doc.name)[1].lower()
            if ext != '.docx':
                raise ValidationError("Formato inválido. O Documento de Requisição deve ser um arquivo .docx.")
            return _validar_anexo_comum(doc)
        
        return doc
    
    def clean_arquivo(self) -> Any:

        arquivos = self.files.getlist("arquivo")
        if not arquivos:
            return None

        for f in arquivos:
            _validar_anexo_comum(f)

        return arquivos

    def save(self, commit: bool = True) -> Any:

        ticket = super().save(commit=False)

        if commit:
            ticket.save()
        return ticket


# 3. FORMULÁRIO DE INTERAÇÃO (RESPOSTAS)

class TicketInteracaoForm(forms.ModelForm):

    class Meta:

        model = TicketInteracao
        fields = ["mensagem", "anexo"]
        widgets = {
            "mensagem": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "anexo": forms.FileInput(attrs={"class": "form-control"}),
        }

    def clean_anexo(self) -> Any:

        return _validar_anexo_comum(self.cleaned_data.get("anexo"))
    