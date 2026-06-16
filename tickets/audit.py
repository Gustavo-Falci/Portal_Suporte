import logging
from typing import Any

logger = logging.getLogger("portal.audit")


def registrar(user: Any, acao: str) -> None:
    """Registra uma ação de negócio: `user=<username|anon> <acao>`."""
    nome = getattr(user, "username", None) or "anon"
    logger.info(f"user={nome} {acao}")
