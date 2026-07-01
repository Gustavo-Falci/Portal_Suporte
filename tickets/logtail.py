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


def read_lines_before(
    path: str,
    end_offset: int,
    n: int = 500,
) -> tuple[list[str], int]:
    """Lê até `n` linhas completas que terminam antes de `end_offset`.

    Lê o arquivo em blocos de 64KB de trás pra frente (não carrega tudo).
    Retorna as linhas em ordem cronológica (sem `\\n`) e o byte onde a
    primeira linha retornada começa — usar esse valor como `end_offset` da
    próxima chamada continua o histórico sem gap nem duplicata. `start_offset`
    é 0 quando alcançou o início do arquivo.
    """
    if end_offset <= 0:
        return [], 0
    if n <= 0:
        return [], end_offset
    block = 64 * 1024
    data = b""
    pos = end_offset
    with open(path, "rb") as f:
        # Acumula blocos até ter mais de `n` quebras de linha OU chegar ao início.
        while pos > 0 and data.count(b"\n") <= n:
            read_size = min(block, pos)
            pos -= read_size
            f.seek(pos)
            data = f.read(read_size) + data
    start_offset = pos  # byte inicial do buffer `data` no arquivo
    # Se não chegamos ao início, a primeira linha do buffer é parcial → descarta.
    if start_offset > 0:
        nl = data.find(b"\n")
        start_offset += nl + 1
        data = data[nl + 1:]
    # `data` cobre [start_offset, end_offset), só linhas completas. `end_offset`
    # é sempre alinhado a início de linha (size após \n, ou um start anterior),
    # então tira o \n final antes do split.
    body = data[:-1] if data.endswith(b"\n") else data
    raw = body.split(b"\n") if body else []
    # Mantém só as últimas `n`; avança start_offset pelos bytes das descartadas.
    if len(raw) > n:
        extras = raw[:-n]
        start_offset += sum(len(l) + 1 for l in extras)  # +1 pelo \n de cada
        raw = raw[-n:]
    linhas = [l.rstrip(b"\r").decode("utf-8", errors="replace") for l in raw]
    return linhas, start_offset


def older_file(name: str) -> str | None:
    """Próximo arquivo de log mais antigo que existe após `name`.

    Ordem newest→oldest: LOG_BASENAME, LOG_BASENAME.1, ... Retorna o primeiro
    nome existente depois de `name`, ou None se não houver (ou `name` inválido).
    """
    nomes = _allowed_names()
    if name not in nomes:
        return None
    base = str(settings.BASE_DIR)
    for cand in nomes[nomes.index(name) + 1:]:
        if os.path.isfile(os.path.join(base, cand)):
            return cand
    return None
