"""
Testes de keepawake.py — bloqueio de suspensao enquanto ha job de fundo.

O que estes testes protegem e a CONTAGEM, nao a chamada ao Windows. A chamada
so acontece no Windows e so pode ser observada de fora com privilegio elevado
(`powercfg /requests`), o que nao cabe numa suite. O que cabe, e o que de fato
quebraria em uso, e a aritmetica de sobreposicao: tres jobs simultaneos foram
o caso normal em uso real, e sem refcount o primeiro a terminar liberaria a
suspensao com dois ainda rodando.
"""

import threading

from delegation_core import keepawake


def _zerar():
    """Devolve o modulo ao repouso entre testes."""
    while keepawake.active_count() > 0:
        keepawake.release()


def test_um_job_sobe_e_desce_o_contador():
    _zerar()
    assert keepawake.active_count() == 0
    keepawake.acquire()
    assert keepawake.active_count() == 1
    keepawake.release()
    assert keepawake.active_count() == 0


def test_jobs_sobrepostos_nao_soltam_o_bloqueio_uns_dos_outros():
    """O caso que motiva o refcount inteiro.

    Sem ele, o release do primeiro job derrubaria o bloqueio com os outros dois
    ainda rodando -- e a maquina suspenderia no meio de um graph_build.
    """
    _zerar()
    for _ in range(3):
        keepawake.acquire()
    assert keepawake.active_count() == 3

    keepawake.release()
    assert keepawake.active_count() == 2, "primeiro job saindo nao pode zerar"
    keepawake.release()
    assert keepawake.active_count() == 1, "segundo job saindo nao pode zerar"
    keepawake.release()
    assert keepawake.active_count() == 0, "so o ultimo zera"


def test_release_extra_nao_deixa_o_contador_negativo():
    """Um contador negativo prenderia o bloqueio ligado para sempre.

    Um acquire posterior levaria o contador de -1 para 0, o holder nunca veria
    (_count > 0) e a suspensao ficaria destravada durante o job seguinte -- ou,
    dependendo da ordem, travada sem job nenhum. O piso em zero e barato.
    """
    _zerar()
    keepawake.release()
    keepawake.release()
    assert keepawake.active_count() == 0

    keepawake.acquire()
    assert keepawake.active_count() == 1, "acquire apos release extra deve valer 1, nao 0"
    keepawake.release()


def test_holder_e_unico_e_daemon():
    """Uma thread dedicada, criada uma vez, que nao segura o encerramento.

    SetThreadExecutionState e por THREAD: se cada job pedisse na sua propria
    thread, o pedido morreria junto com ela ao terminar, sem erro nenhum. Daí a
    thread dedicada. Ela precisa ser daemon para nao impedir o processo de sair.
    """
    _zerar()
    keepawake.acquire()
    keepawake.acquire()

    holders = [t for t in threading.enumerate() if t.name == "keepawake"]
    if keepawake._IS_WINDOWS:
        assert len(holders) == 1, "uma thread por processo, nao uma por job"
        assert holders[0].daemon, "nao pode segurar o encerramento do daemon"
    else:
        assert holders == [], "fora do Windows o modulo e no-op"

    _zerar()


def test_fora_do_windows_e_no_op(monkeypatch):
    """Num host nao-Windows nada deve acontecer, nem contador nem thread."""
    monkeypatch.setattr(keepawake, "_IS_WINDOWS", False)
    _zerar()
    antes = keepawake.active_count()
    keepawake.acquire()
    assert keepawake.active_count() == antes, "acquire deve ser inerte fora do Windows"
    keepawake.release()
