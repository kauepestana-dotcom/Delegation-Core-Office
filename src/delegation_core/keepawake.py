"""
keepawake.py — impede a suspensao por inatividade enquanto houver job de fundo.

Um graph_build sobre um repositorio real leva meia hora; um ingest de milhares
de arquivos leva mais. Se a maquina suspende no meio, o trabalho para e o
usuario descobre depois, com um job "running" que nunca mais avancou.

Nao mexe em plano de energia. `SetThreadExecutionState` e um pedido vivo, preso
ao processo: morreu o daemon, morreu o pedido, sem estado a limpar. Fora do
Windows todo este modulo e no-op.

DUAS ARMADILHAS, e por que o desenho e este:

1. **O estado e por THREAD, nao por processo.** Cada job de fundo roda na sua
   propria daemon thread. Se o job pedisse o bloqueio, o pedido morreria junto
   com a thread ao terminar -- silenciosamente, sem erro nenhum. Por isso existe
   uma thread dedicada (`_holder_loop`) que nasce no primeiro acquire e nunca
   morre: ela e a unica que fala com a API, e enquanto ela vive o pedido vale.

2. **Jobs se sobrepoem.** Tres jobs simultaneos foram o caso normal em uso real.
   Sem contagem de referencia, o primeiro a terminar liberaria a suspensao com
   dois ainda rodando. Daí `_count`: solta so quando o ultimo sai.

Nao pede ES_DISPLAY_REQUIRED de proposito. A tela pode apagar -- ninguem precisa
olhar um ingest -- e manter o monitor aceso a noite inteira gasta energia sem
motivo. O que nao pode e a MAQUINA dormir.
"""

import ctypes
import logging
import platform
import threading

logger = logging.getLogger(__name__)

ES_CONTINUOUS      = 0x80000000   # o pedido vale ate ser trocado
ES_SYSTEM_REQUIRED = 0x00000001   # nao suspenda o sistema

_IS_WINDOWS = platform.system() == "Windows"

_cond = threading.Condition()
_count = 0            # jobs de fundo ativos; protegido por _cond
_holder: threading.Thread | None = None


def _holder_loop() -> None:
    """Unica thread que fala com SetThreadExecutionState. Vive pelo processo.

    Dorme ate que o estado desejado (_count > 0) difira do estado atual, e so
    entao chama a API. Sem polling e sem re-afirmar o que ja esta afirmado.
    """
    kernel32 = ctypes.windll.kernel32
    held = False
    while True:
        with _cond:
            while (_count > 0) == held:
                _cond.wait()
            want = _count > 0

        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if want else 0)
        if kernel32.SetThreadExecutionState(flags) != 0:
            held = want
            logger.info("keepawake: suspensao %s", "bloqueada" if want else "liberada")
        else:
            # Retorno 0 e falha. Nao atualiza `held`, entao o loop tenta de novo
            # na proxima mudanca em vez de acreditar num bloqueio que nao existe.
            err = ctypes.get_last_error()
            logger.warning("keepawake: SetThreadExecutionState falhou (erro %s)", err)


def acquire() -> None:
    """Registra mais um trabalho ativo. Bloqueia a suspensao no primeiro."""
    global _count, _holder
    if not _IS_WINDOWS:
        return
    with _cond:
        _count += 1
        if _holder is None:
            _holder = threading.Thread(target=_holder_loop, daemon=True,
                                       name="keepawake")
            _holder.start()
        _cond.notify_all()


def release() -> None:
    """Registra o fim de um trabalho. Libera a suspensao no ultimo."""
    global _count
    if not _IS_WINDOWS:
        return
    with _cond:
        # max(0, ...) porque um release a mais nao pode deixar o contador
        # negativo: isso travaria o bloqueio ligado para sempre.
        _count = max(0, _count - 1)
        _cond.notify_all()


def active_count() -> int:
    """Quantos trabalhos seguram o bloqueio agora. Para diagnostico."""
    with _cond:
        return _count
