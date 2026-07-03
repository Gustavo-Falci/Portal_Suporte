from django import template
from django.template.defaultfilters import stringfilter
from django.utils.html import urlize as django_urlize
from django.utils.safestring import mark_safe

register = template.Library()

@register.filter(is_safe=True, needs_autoescape=True)
@stringfilter
def urlize_target_blank(value, autoescape=True):
    """
    Converte URLs no texto em links clicáveis, abrindo em uma nova aba.
    Funciona exatamente como o urlize nativo do Django, mas adiciona target="_blank".
    """
    urlized_text = django_urlize(value, autoescape=autoescape)
    return mark_safe(urlized_text.replace('<a ', '<a target="_blank" '))


# Extensões tratadas como imagem no chat (abrem em modal/lightbox).
_EXT_IMAGEM = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg')


@register.filter
@stringfilter
def is_imagem(value):
    """
    Retorna True se o nome do arquivo aparenta ser uma imagem (por extensão).
    Usado no chat para decidir entre abrir em lightbox ou baixar.
    """
    if not value:
        return False
    return value.lower().endswith(_EXT_IMAGEM)


@register.filter
def pode_editar(interacao, user):
    """
    True se o usuário pode editar esta interação (autor + dentro da janela de 24h).
    Usado no chat para exibir o botão de edição só quando cabível.
    """
    try:
        return interacao.pode_editar(user)
    except AttributeError:
        return False
