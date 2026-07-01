"""Leitura segura dos arquivos de log do portal para exibição via web.

Isola a lógica pura (whitelist, resolução de path, tail, streaming SSE) das
views, para poder testar sem HTTP. Só o superuser acessa isto (garantido nas
views); ainda assim validamos todo nome de arquivo contra uma whitelist para
impedir path traversal.
"""
import os
import time
from collections import deque
from typing import Iterator

from django.conf import settings
from django.http import Http404

# Basename do log ativo, derivado da config de logging (fonte única da verdade).
LOG_BASENAME: str = os.path.basename(
    settings.LOGGING["handlers"]["file"]["filename"]
)
# backupCount do RotatingFileHandler: gera .1 .. .N.
_BACKUP_COUNT: int = int(settings.LOGGING["handlers"]["file"].get("backupCount", 0))


def _allowed_names() -> list[str]:
    """Nomes permitidos: ativo + rotacionados, independente de existirem."""
    return [LOG_BASENAME] + [f"{LOG_BASENAME}.{i}" for i in range(1, _BACKUP_COUNT + 1)]


def available_log_files() -> list[str]:
    """Nomes permitidos que existem em BASE_DIR, ativo primeiro."""
    base = str(settings.BASE_DIR)
    return [n for n in _allowed_names() if os.path.isfile(os.path.join(base, n))]


def resolve_log_path(name: str) -> str:
    """Retorna o abspath validado do log `name`, ou levanta Http404.

    Bloqueia path traversal: `name` precisa estar na whitelist, o arquivo
    precisa existir e o path resolvido precisa continuar dentro de BASE_DIR.
    """
    if name not in _allowed_names():
        raise Http404("Arquivo de log inválido.")
    base = os.path.realpath(str(settings.BASE_DIR))
    caminho = os.path.realpath(os.path.join(base, name))
    if os.path.commonpath([base, caminho]) != base:
        raise Http404("Caminho de log inválido.")
    if not os.path.isfile(caminho):
        raise Http404("Arquivo de log não encontrado.")
    return caminho


def tail_lines(path: str, n: int = 200) -> list[str]:
    """Últimas `n` linhas do arquivo, sem o `\\n` final."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return [linha.rstrip("\n") for linha in deque(f, n)]


def stream_events(
    path: str,
    pos: int,
    duration: float = 30.0,
    poll_interval: float = 0.25,
) -> Iterator[str]:
    """Gera eventos SSE lendo linhas novas de `path` a partir do byte `pos`.

    Encerra após `duration` segundos; o client reabre a conexão com o último
    offset recebido (evento `pos`), então o worker fica preso no máximo
    `duration` segundos por ciclo. Lê em modo binário para controlar o offset
    em bytes com precisão (a config de log usa encoding utf-8).
    """
    deadline = time.time() + duration
    with open(path, "rb") as f:
        # Rotação/truncamento: arquivo menor que o offset pedido → recomeça.
        if os.path.getsize(path) < pos:
            pos = 0
            yield "event: rotated\ndata: 0\n\n"
        f.seek(pos)
        while time.time() < deadline:
            emitiu = False
            while True:
                inicio = f.tell()
                bruto = f.readline()
                if not bruto.endswith(b"\n"):
                    # Linha parcial (ainda sendo escrita): volta e espera completar.
                    f.seek(inicio)
                    break
                linha = bruto.rstrip(b"\n").decode("utf-8", errors="replace")
                pos = f.tell()
                emitiu = True
                yield f"data: {linha}\n\n"
            if emitiu:
                yield f"event: pos\ndata: {pos}\n\n"
            else:
                yield ": ping\n\n"
            time.sleep(poll_interval)
