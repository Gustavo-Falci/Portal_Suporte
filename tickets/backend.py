import logging
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model
from django.http import HttpRequest
from typing import Optional, Any

# Configurar logger
logger = logging.getLogger(__name__)
Cliente = get_user_model()


class EmailBackend(ModelBackend):

    """
    Autenticação via E-mail (Case Insensitive).
    """

    def authenticate(
        self,
        request: Optional[HttpRequest],
        username: Optional[str] = None,
        password: Optional[str] = None,
        **kwargs: Any,
    ) -> Optional[Any]:
        
        # Suporte caso o campo venha como 'email' nos kwargs
        if username is None:
            username = kwargs.get("email")

        if not username or not password:
            return None

        logger.info(f"Tentativa de login para: {username}")

        try:
            
            # 1. Busca Case-Insensitive (__iexact)
            # Resolve o problema de 'User@Example.com' vs 'user@example.com'
            user = Cliente.objects.get(email__iexact=username)

        except Cliente.DoesNotExist:

            # 2. Mitigação de Timing Attack (Enumeração de Usuários)
            # Mesmo que o usuário não exista, rodamos um hash de senha dummy
            # para que o tempo de resposta seja similar ao de um usuário existente.
            # Isso impede que hackers descubram quais emails existem medindo o tempo.
            Cliente().set_password(password)
            logger.warning(f"Login falhou: Usuário não encontrado para {username}")
            return None

        except Cliente.MultipleObjectsReturned:

            # Isso indica sujeira no banco de dados (emails duplicados)
            logger.critical(
                f"INTEGRIDADE COMPROMETIDA: Múltiplos usuários com email {username}"
            )
            return None

        # 3. Verifica a senha e se o usuário está ativo (is_active=True)
        if user.check_password(password) and self.user_can_authenticate(user):
            logger.info(f"Login bem-sucedido para: {username}")
            return user

        logger.warning(f"Login falhou: Senha incorreta para {username}")
        return None
