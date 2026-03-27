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
