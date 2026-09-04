"""
cli.py: delegation-core v0.8.0 command-line interface.

Commands:
  setup          Interactive setup wizard (run once per machine).
  run            Start the MCP server (used in Claude Desktop config).
  status         Check vault, binary, model, llama.cpp health, and feature config.
  reindex        Rebuild the ChromaDB search index from vault folders.
  maintain       Run inbox maintenance once and exit (used by the SessionStart hook).
  dashboard-api  Run the local JSON API used by the Tauri dashboard (standalone/debug).
  ingest         Index files from an external folder without moving them.
  relink         Add wikilinks to notes in a vault subfolder.
  search         Query the vault (BGE similarity search, no LLM required).
  compress       Extract key facts/decisions/action items from text via llama.cpp.
  note           write / read / update / list / find-similar: direct vault note access.
  graph          build / list / report / affected / hook install|uninstall|status : 
                 the vendored code-graph pipeline (opt-in [graph] extra).
  process        create / list / update / get: cross-session process tracking.

v0.7.1: added search/compress/note/graph/process: previously these were only
reachable through an MCP client (Claude Desktop/Code); this repo already had a
real, installed CLI (this file) but it only covered operational/maintenance
commands, not the actual knowledge-work tools. This closes that gap so the
vault and the code-graph pipeline are usable standalone from a terminal.

v0.11: reindex/maintain/ingest hand their work to the running HTTP daemon
instead of opening a second ChromaDB and loading a second copy of BGE. They
still do the work in-process when no daemon answers: and with --local when
told to: so a machine without the service keeps working. See daemon.py.

v0.8.0: added dashboard-api, the local JSON sidecar the Tauri dashboard (see
dashboard/) talks to. Not an MCP tool: a separate small HTTP process, since
FastMCP's mcp.run() serves one transport at a time (stdio here) and can't
also serve HTTP for a UI in the same process.
"""

import argparse
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _read_content(file_arg: str | None, what: str = "content") -> str:
    """Read text from --file if given, else stdin if piped, else error.

    Mirrors common CLI UX (`cat file | tool` or `tool --file x`) rather than
    requiring an inline positional argument, which gets unwieldy for anything
    beyond a one-liner.
    """
    if file_arg:
        return Path(file_arg).expanduser().read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        data = sys.stdin.read()
        if data.strip():
            return data
    sys.stderr.write(f"No {what} provided: pass --file PATH or pipe it via stdin.\n")
    sys.exit(1)


def _add_local_flag(parser):
    """Opt out of daemon routing for a command that writes to the index.

    The escape hatch matters because routing is the default: if the daemon is
    up but wedged, or a person wants the work to happen in the process they are
    watching, there has to be a way to say so out loud. Silently falling back
    would be the same second writer, just undocumented.
    """
    parser.add_argument(
        "--local", action="store_true",
        help="Do the work in this process instead of handing it to the running daemon",
    )


def cmd_setup(_args):
    from .wizard import run_wizard
    run_wizard()


def cmd_run(args):
    import asyncio
    import os
    from .config import Config, CONFIG_DIR

    cfg = Config.load()
    if not cfg.is_configured():
        sys.stderr.write("delegation-core is not configured.\nRun: delegation-core setup\n")
        sys.exit(1)

    if getattr(args, "recalibrate", False):
        cfg.tok_sec = 0.0
        cfg.save()
        sys.stderr.write("Calibration reset: will recalibrate on startup.\n")

    # Prefer cached model weights, but only when they exist: see prefer_offline().
    from .embeddings import prefer_offline
    prefer_offline(cfg.bge_model)
    # Suppress FastMCP update check on startup
    os.environ.setdefault("FASTMCP_DISABLE_UPDATE_CHECK", "1")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            RotatingFileHandler(
                str(cfg.log_path),
                maxBytes=5 * 1024 * 1024,   # 5 MB per file
                backupCount=3,
                encoding="utf-8",
            ),
            logging.StreamHandler(sys.stderr),
        ],
    )

    if cfg.budget_mode == "auto" and cfg.tok_sec == 0.0:
        from .engine import DelegationEngine

        async def _calibrate():
            engine = DelegationEngine(cfg)
            try:
                logging.info("Auto-calibration: starting llama.cpp and measuring tok/sec...")
                await engine.ensure_running()
                await engine.calibrate()
            finally:
                await engine.aclose()

        asyncio.run(_calibrate())

    from .server import run_server
    run_server(cfg)


def cmd_mcp_stdio(args):
    """Serve MCP on stdio, forwarding to the daemon (for Claude Desktop)."""
    from . import stdio_bridge
    from .config import Config
    return stdio_bridge.run(Config.load())


def cmd_update(args):
    """Bring the installed package up to date with its source."""
    from rich.console import Console

    from . import installer

    console = Console()
    resultado = installer.update(check_only=args.check, restart=not args.no_restart)
    estado = resultado.get("status")

    if estado == "cannot_locate_source":
        console.print(f"[red]Cannot update:[/red] {resultado['detail']}")
        return 1
    if estado == "refused_dirty_checkout":
        console.print(f"[yellow]Refused:[/yellow] {resultado['detail']}")
        return 1

    if args.check:
        raiz = resultado.get("root")
        passo = resultado["steps"][0]
        console.print(f"[bold]version[/bold]: {resultado.get('version')}")
        console.print(f"[bold]source[/bold]:  {raiz}"
                      + ("  (editable)" if passo.get("editable") else ""))
        if passo.get("git"):
            atras = passo.get("behind")
            console.print(f"[bold]branch[/bold]:  {passo.get('branch')} @ {passo.get('commit')}")
            if passo.get("upstream") is None:
                console.print("[yellow]no upstream branch: nothing to compare against[/yellow]")
            elif atras:
                console.print(f"[yellow]{atras} commit(s) behind {passo['upstream']}[/yellow]")
            else:
                console.print("[green]up to date[/green]")
        else:
            console.print("[yellow]not a git checkout: cannot tell whether it is current[/yellow]")
        return 0

    for passo in resultado.get("steps", []):
        marca = "[green]OK[/green]  " if passo["ok"] else "[red]FALHOU[/red]"
        console.print(f"  {marca} {passo['step']}")

    if estado == "ok":
        console.print(f"\n[green]Updated.[/green] {resultado.get('version_before')} "
                      f"-> run `delegation-core status` to confirm the new version.")
    else:
        console.print(f"\n[red]{estado}[/red]: {resultado.get('detail', '')}")

    sobras = resultado.get("stale_dist_copies") or []
    if sobras:
        console.print(
            f"\n[dim]Note: {', '.join(sobras)} are byte-identical to the files they "
            f"sit beside. Older installers wrote those whenever the destination "
            f"merely existed, so they signal a customisation that was never made. "
            f"Safe to delete.[/dim]")

    return 0 if estado == "ok" else 1


def cmd_repair_empty_source(args):
    """Encontra e conserta notas sintetizadas a partir de arquivos sem texto."""
    from rich.console import Console

    from . import repair
    from .config import Config

    console = Console()
    cfg = Config.load()
    if not cfg.is_configured():
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        return 1

    if args.apply or args.archive:
        r = repair.aplicar(cfg, arquivar=args.archive)
    else:
        r = repair.encontrar(cfg)

    if not r["found"]:
        console.print("[green]Nothing to repair.[/green] "
                      "No processed source turned out to be text-free.")
        if r["sources_without_note"]:
            console.print(f"[dim]{len(r['sources_without_note'])} text-free source(s) "
                          f"with no note of their own.[/dim]")
        return 0

    console.print(f"[bold]{r['notes_total']} note(s)[/bold] from "
                  f"{len(r['found'])} text-free source(s):\n")
    for achado in r["found"]:
        console.print(f"  [yellow]{achado['source']}[/yellow]")
        for n in achado["notes"]:
            console.print(f"    -> {n}")

    if not (args.apply or args.archive):
        console.print("\n[dim]Nothing was changed. Re-run with --apply to replace each "
                      "note's body with the source record, or --archive to move them "
                      "to _dump/_archive/.[/dim]")
        return 0

    console.print(f"\n[green]{len(r['applied'])} note(s) "
                  f"{'archived' if args.archive else 'replaced with the source record'}.[/green]")
    for f in r["failures"]:
        console.print(f"  [red]failed[/red] {f['note']}: {f['error']}")
    return 0 if r["status"] == "ok" else 1


def cmd_post_install(args):
    """Finish an install: docs, hooks, skills, dashboard, service, clients.

    Called by install.sh / install.bat once the venv exists and the package is
    in it. Everything it does used to be transcribed into both of those scripts
    and had already drifted between them.
    """
    from rich.console import Console

    from . import installer

    console = Console()
    raiz = Path(args.root).expanduser().resolve() if args.root else installer.source_root()
    if raiz is None:
        console.print("[red]Cannot finish the install:[/red] no source directory given "
                      "and none recorded. Pass --root PATH.")
        return 1

    r = installer.post_install(raiz)

    docs = r["docs_and_hooks"]
    console.print(f"  docs and hooks: {len(docs['installed'])} installed, "
                  f"{len(docs['unchanged'])} unchanged, {len(docs['kept_yours'])} kept yours")
    pele = r["skills"]
    if pele["available"]:
        console.print(f"  skills: {len(pele['installed'])} installed, "
                      f"{len(pele['kept_yours'])} kept yours")
    painel = r["dashboard"]
    console.print(f"  dashboard: {painel['status']}"
                  + (f" ({painel['method']})" if painel.get("method") else ""))
    if painel["status"] not in ("installed",):
        console.print(f"    [dim]{painel.get('detail', '')}[/dim]")

    if r["needs_wizard"]:
        if args.no_wizard:
            console.print("\n[bold]Fresh install.[/bold] Run `delegation-core setup` next.")
            return 0
        # The CLI launches the wizard, not `installer.post_install`. The library
        # function reports that setup is owed; handing the terminal to an
        # interactive program is a decision for the layer that has the terminal,
        # and post_install is also reached from non-interactive paths.
        console.print("\n[bold]Fresh install.[/bold] Launching setup wizard...\n")
        from .wizard import run_wizard
        run_wizard()
        return 0

    console.print(f"  service: {r['service'].get('status')}")
    for cliente, estado in r["clients"].items():
        console.print(f"  {cliente}: {estado.get('status')}")
        if estado.get("status") == "not_configured":
            console.print(f"    [dim]{estado.get('detail', '')}[/dim]")
    console.print("\n[green]Upgrade complete.[/green] Restart your MCP client to load the new code.")
    return 0


def cmd_uninstall(args):
    """Remove the installation, keeping the vault and the downloaded models."""
    from rich.console import Console

    from . import installer

    console = Console()

    if not args.dry_run and not args.yes:
        previa = installer.uninstall(dry_run=True)
        if previa["status"] not in ("dry_run",):
            console.print(f"[yellow]{previa['status']}[/yellow]: {previa.get('detail', '')}")
            return 1
        plano = previa["plan"]
        console.print("[bold]This will remove[/bold]")
        for rotulo, itens in (("service registrations", plano["services"]),
                              ("directories", plano["dirs"]),
                              ("files", plano["files"] + plano["globs"])):
            for item in itens:
                console.print(f"  - [{rotulo}] {item}")
        console.print(f"  - [venv] {installer.CONFIG_DIR / 'venv'}  (by the wrapper, after this exits)")
        console.print("\n[bold]This will NEVER touch[/bold]")
        for chave, valor in previa["kept"].items():
            console.print(f"  - {chave}: {valor}")
        try:
            resposta = input("\nType 'yes' to proceed: ").strip()
        except (EOFError, KeyboardInterrupt):
            resposta = ""
        if resposta.lower() != "yes":
            console.print("Aborted. Nothing was removed.")
            return 0
        console.print("")

    resultado = installer.uninstall(dry_run=args.dry_run)
    estado = resultado["status"]

    if estado == "dry_run":
        console.print_json(data=resultado["plan"])
        return 0

    for passo in resultado.get("steps", []):
        marca = "[green]OK[/green]  " if passo["ok"] else "[red]FALHOU[/red]"
        console.print(f"  {marca} {passo['step']}")

    if estado in ("refused_vault_inside_config_dir", "refused_daemon_still_up"):
        console.print(f"\n[red]{estado}[/red]: {resultado['detail']}")
        return 1
    if estado == "nothing_to_uninstall":
        console.print(f"\n{resultado['detail']}")
        return 0

    console.print(f"\n[green]Uninstalled.[/green] {len(resultado.get('removed', []))} item(s) removed.")
    for chave, valor in resultado["kept"].items():
        console.print(f"  kept {chave}: {valor}")
    for falha in resultado.get("failures", []):
        console.print(f"  [red]failed[/red] {falha['path']}: {falha['error']}")
    return 0 if estado == "ok" else 1


def cmd_service(args):
    """Manage the daemon's per-user service registration."""
    from rich.console import Console
    from . import service as _service

    console = Console()
    result = {"install": _service.install,
              "uninstall": _service.uninstall,
              "status": _service.status}[args.action]()

    for key, value in result.items():
        console.print(f"[bold]{key}[/bold]: {value}")
    if result.get("status") == "unsupported":
        return 1
    return 0


def cmd_clients(args):
    """Point MCP clients at the HTTP daemon.

    Needed because v0.11 is a breaking transport change: an existing
    {"command": ..., "args": ["run"]} entry no longer starts a private server
    for that client, it starts a *second* daemon that competes with the real one
    for the port, the index, and the GPU.
    """
    from rich.console import Console
    from .config import Config
    from . import clients as _clients

    console = Console()
    cfg = Config.load()
    token = cfg.ensure_server_token()

    if args.show or not (args.claude_code or args.claude_desktop or args.codex or args.antigravity):
        console.print(f"[bold]URL[/bold]    {cfg.server_url}")
        console.print(f"[bold]Header[/bold] Authorization: Bearer {token}")
        console.print(f"[bold]Env[/bold]    {_clients.CODEX_TOKEN_ENV_VAR}={token}")
        if not args.show:
            console.print("\n[dim]Nothing written. Pass --claude-code, --claude-desktop, --codex "
                          "and/or --antigravity to update a client config.[/dim]")
        return 0

    if args.claude_code:
        result = _clients.install_claude_code(cfg)
        console.print(f"[green]claude-code[/green] {result['status']}: {result['path']}")
        if result.get("replaced"):
            console.print(f"  [dim]replaced: {result['replaced']}[/dim]")
        console.print("  [yellow]Reconnect the client for this to take effect.[/yellow]")

    if args.claude_desktop:
        result = _clients.install_claude_desktop(cfg)
        colour = "red" if result["status"] == "error" else "green"
        console.print(f"[{colour}]claude-desktop[/{colour}] {result['status']}: {result['path']}")
        if result.get("detail"):
            console.print(f"  [yellow]{result['detail']}[/yellow]")
        else:
            console.print("  [yellow]Restart Claude Desktop for this to take effect.[/yellow]")

    if args.codex:
        result = _clients.install_codex(cfg)
        console.print(f"[green]codex[/green] {result['status']}: {result['path']}")
        if result.get("note"):
            console.print(f"  [yellow]{result['note']}[/yellow]")
            console.print(result["block"])
        console.print(f"  [yellow]Export the token before starting Codex:[/yellow] "
                      f"export {_clients.CODEX_TOKEN_ENV_VAR}={token}")

    if args.antigravity:
        result = _clients.install_antigravity(cfg)
        colour = "red" if result["status"] == "error" else "green"
        console.print(f"[{colour}]antigravity[/{colour}] {result['status']}: {result['path']}")
        if result.get("detail"):
            console.print(f"  [yellow]{result['detail']}[/yellow]")
        else:
            console.print("  [yellow]Restart agy for this to take effect.[/yellow]")
    return 0


def cmd_status(_args):
    from rich.console import Console
    from rich.table import Table
    from .config import Config

    console = Console()
    cfg = Config.load()

    if not cfg.is_configured():
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)

    ok   = "[green]✓[/green]"
    fail = "[red]✗[/red]"

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("key", style="dim", min_width=16)
    table.add_column("value")

    vault_ok  = Path(cfg.vault_path).exists()
    binary_ok = Path(cfg.llama_binary).exists() if cfg.llama_binary else False
    model_ok  = Path(cfg.llama_model).exists()  if cfg.llama_model  else False

    table.add_row("Vault",   f"{ok if vault_ok  else fail}  {cfg.vault_path}")
    table.add_row("Binary",  f"{ok if binary_ok else fail}  {cfg.llama_binary}")
    table.add_row("Model",   f"{ok if model_ok  else fail}  {cfg.llama_model}")
    table.add_row("Folders", ", ".join(cfg.vault_folders))

    table.add_row("engine_mode", cfg.engine_mode)

    # In agent mode ensure_running() refuses to spawn llama at all (engine.py),
    # so probing /health and reporting "will start on first tool call" was
    # actively misleading: it never will.
    if cfg.is_agent_mode:
        llama_str = "[dim]not used: generation delegated to the calling agent[/dim]"
    else:
        import requests
        on_demand = "will start on first big/bulk task" if cfg.is_hybrid_mode \
            else "will start on first tool call"
        try:
            r = requests.get(f"{cfg.llama_url}/health", timeout=3)
            llama_str = (
                f"[green]online[/green]  ({cfg.llama_url})"
                if r.status_code == 200
                else f"[yellow]unhealthy[/yellow]  ({cfg.llama_url})"
            )
        except Exception:
            llama_str = f"[dim]offline: {on_demand}[/dim]  ({cfg.llama_url})"
    table.add_row("llama.cpp", llama_str)

    # Where the running code actually comes from. An editable install serves the
    # daemon straight out of a working tree, so an uncommitted edit is one
    # service restart away from production and nothing anywhere says so. Showing
    # the path does not prevent that, but it stops it being invisible.
    try:
        import delegation_core as _pkg
        origin = Path(_pkg.__file__).resolve().parent
        editable = not any(part == "site-packages" for part in origin.parts)
        table.add_row("code", f"{origin}" + ("  [yellow](editable: live working tree)[/yellow]"
                                             if editable else ""))
    except Exception:
        pass

    try:
        import chromadb
        # Same settings every other caller uses (vault.py, doctor.py). chromadb
        # caches one System per path and rejects a second client on that path
        # with different settings, so a bare client here poisons the
        # VaultManager built later in this same process: that is how
        # `embed-model --reindex` came to index 0 notes without failing.
        client = chromadb.PersistentClient(
            path=str(cfg.chroma_path),
            settings=chromadb.Settings(anonymized_telemetry=False))
        # cfg.collection_name, not the historical literal: every other caller
        # derives the name from the embedding model, so on any install using
        # something other than bge-base this looked up a collection that does
        # not exist and reported "not initialized: run: delegation-core
        # reindex" over a perfectly healthy index: recommending a rebuild that
        # costs hours on a real vault.
        col = client.get_collection(cfg.collection_name)
        # "rows", not "notes": since v0.12 a note is one row per chunk, so this
        # number runs well ahead of the note count and calling it notes invites
        # exactly the wrong conclusion about the size of the vault.
        table.add_row("ChromaDB", f"[green]✓[/green]  {col.count()} rows indexed")
    except Exception:
        table.add_row("ChromaDB", "[dim]not initialized: run: delegation-core reindex[/dim]")

    # v0.2 feature flags
    table.add_row("budget_mode",  cfg.budget_mode)
    table.add_row("synthesis",
                  f"{'on' if cfg.synthesis_enabled else 'off'} ({cfg.synthesis_lang})")

    console.print()
    console.print(table)
    console.print()


def _delegate(cfg, args, tool: str, arguments: dict, say) -> dict | None:
    """Hand index work to the running daemon; None means "do it yourself".

    Every command that writes to ChromaDB goes through here. When the daemon is
    up it owns the index and the resident BGE model, so a second process opening
    the same directory is both a concurrent writer and a second ~2.4 GiB copy of
    the model on the GPU: see daemon.py for what that cost in practice, and for
    why a *failed* daemon call is not allowed to fall back to local work.

    `say` reports routing on the command's own output channel, since `maintain`
    speaks JSON on stdout and must keep it parseable.
    """
    if getattr(args, "local", False):
        return None

    from . import daemon
    from .config import CONFIG_DIR

    def _fall_back(reason: str):
        """Take the local path, or refuse it when this machine has opted out.

        Refusing exits 0, not 1: a hook that skipped a reindex on purpose has
        not failed, and a non-zero exit here would surface as an error on every
        session start. The message names the switch so the skip is legible in
        the log rather than looking like nothing happened.
        """
        if cfg.local_index_fallback_allowed():
            say(f"{reason}. Running in this process.")
            return None
        say(f"{reason}. Refusing to write the index from this process.")
        say("Local index fallback is off (config allow_local_index_fallback, or "
            f"{CONFIG_DIR / 'no_auto_reindex'}). Start the daemon, or pass --local.")
        sys.exit(0)

    if not daemon.is_listening(cfg):
        return _fall_back(f"No daemon on {cfg.server_host}:{cfg.server_port}")
    try:
        say(f"Delegating to the daemon at {cfg.server_url} ...")
        timeout = getattr(args, "timeout", None)
        return daemon.submit_and_wait(cfg, tool, arguments, timeout=timeout)
    except daemon.DaemonUnavailable as exc:
        return _fall_back(f"Daemon went away ({exc})")
    except daemon.DaemonCallFailed as exc:
        # Deliberately fatal. Retrying locally would start the second writer the
        # daemon exists to prevent, on top of a daemon that is already unwell.
        say(f"Error: {exc}")
        say("Re-run with --local to do this work in this process anyway.")
        sys.exit(1)


def cmd_reindex(args):
    from rich.console import Console
    from .config import Config

    console = Console()
    cfg = Config.load()
    if not cfg.is_configured():
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)

    force = getattr(args, "force", False)
    console.print(f"Reindexing [bold]{cfg.vault_path}[/bold]{' (full)' if force else ''} ...")

    job = _delegate(cfg, args, "vault_reindex_bg", {"force": force},
                    lambda msg: console.print(f"[dim]{msg}[/dim]"))
    if job is not None:
        console.print(f"[green]✓[/green]  {job.get('result')} notes indexed (by the daemon).")
        return

    from .vault import VaultManager
    vault = VaultManager(cfg)
    count = vault.reindex_vault(force=force)
    console.print(f"[green]✓[/green]  {count} notes indexed.")


def cmd_dashboard_api(args):
    from . import dashboard_api
    dashboard_api.run(port=args.port, host=args.host)


def cmd_maintain(args):
    import asyncio
    from .config import Config

    cfg = Config.load()
    if not cfg.is_configured():
        sys.stderr.write("delegation-core is not configured.\nRun: delegation-core setup\n")
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )

    # Routing notes go to stderr: stdout is the maintenance result as JSON, and
    # the SessionStart hook redirects both into maintenance.log.
    job = _delegate(cfg, args, "run_maintenance_bg", {},
                    lambda msg: sys.stderr.write(f"maintain: {msg}\n"))
    if job is not None:
        print(json.dumps(job.get("result"), indent=2))
        return

    from .engine import DelegationEngine
    from .vault import VaultManager
    from . import organizer

    vault  = VaultManager(cfg)
    engine = DelegationEngine(cfg)
    result = asyncio.run(organizer.run(engine, vault))
    print(json.dumps(result, indent=2))


def cmd_ingest(args):
    from rich.console import Console
    from .config import Config

    console = Console()
    cfg = Config.load()
    if not cfg.is_configured():
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)

    recursive = not getattr(args, "no_recursive", False)
    force = bool(getattr(args, "force", False))
    raw_exclude = getattr(args, "exclude", None)
    if isinstance(raw_exclude, str) and raw_exclude.strip():
        exclude = [p.strip() for p in raw_exclude.split(",") if p.strip()]
    elif isinstance(raw_exclude, list):
        exclude = raw_exclude
    else:
        exclude = None

    # Resolved before it goes anywhere: the daemon runs as a service with its own
    # working directory, so a relative path that means one thing in this shell
    # means something else (or nothing) there. Doing it for the local path too
    # keeps one interpretation of the argument.
    source = str(Path(args.path).expanduser().resolve())
    flags = []
    if not recursive:
        flags.append("non-recursive")
    if force:
        flags.append("force")
    if exclude:
        flags.append(f"exclude={exclude}")
    extra_info = f" ({', '.join(flags)})" if flags else ""
    console.print(f"Ingesting [bold]{source}[/bold]{extra_info} ...")

    job = _delegate(cfg, args, "ingest_folder_bg",
                    {"source_path": source, "recursive": recursive, "force": force, "exclude": exclude},
                    lambda msg: console.print(f"[dim]{msg}[/dim]"))
    result = job.get("result") if job is not None else None

    if result is None:
        from .vault import VaultManager
        from .ingest import IngestManager

        vault  = VaultManager(cfg)
        ingest = IngestManager(vault)
        result = ingest.ingest(source, recursive=recursive, force=force, exclude=exclude)

    if "error" in result:
        console.print(f"[red]Error:[/red] {result['error']}")
        sys.exit(1)

    excluded_info = f", {result['excluded']} excluded" if "excluded" in result else ""
    console.print(
        f"[green]✓[/green]  {result['indexed']} files indexed, "
        f"{result['skipped']} skipped{excluded_info}, {len(result['errors'])} errors."
    )
    if result["errors"]:
        console.print("[dim]Errors:[/dim]")
        for e in result["errors"]:
            console.print(f"  {e}")


def cmd_relink(args):
    from rich.console import Console
    from .config import Config
    from .vault import VaultManager
    from .linker import relink_folder

    console = Console()
    cfg = Config.load()
    if not cfg.is_configured():
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)

    days = getattr(args, "days", None)
    min_sim = getattr(args, "min_similarity", None)
    max_links = getattr(args, "max_links", 8)

    console.print(f"Relinking [bold]{args.folder}[/bold] ...")
    vault  = VaultManager(cfg)
    result = relink_folder(vault, args.folder, days=days,
                           min_similarity=min_sim, max_links_per_note=max_links)
    console.print(f"[green]✓[/green]  {result.get('linked_notes', 0)} notes updated, "
                  f"{result.get('links_added', 0)} links added.")
    if result.get("errors"):
        for e in result["errors"]:
            console.print(f"  [red]{e}[/red]")


def cmd_search(args):
    from rich.console import Console
    from .config import Config
    from .vault import VaultManager

    console = Console()
    cfg = Config.load()
    if not cfg.is_configured():
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)

    vault = VaultManager(cfg)
    hits = vault.search(args.query, limit=args.limit)
    if hits and "error" in hits[0]:
        console.print(f"[red]Error:[/red] {hits[0]['error']}")
        sys.exit(1)
    if not hits:
        console.print("[dim]No results above similarity threshold.[/dim]")
        return

    for h in hits:
        console.print(f"[bold]{h['title']}[/bold]  [dim]({h['similarity']:.2f}) {h['path']}[/dim]")
        console.print(f"  {h['snippet'][:240].strip()}")
        console.print()


def cmd_compress(args):
    import asyncio
    from rich.console import Console
    from .config import Config, with_lang
    from .engine import DelegationEngine

    console = Console()
    cfg = Config.load()
    if not cfg.is_configured():
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)

    raw = _read_content(args.file, "content to compress")
    limit = 1500 if cfg.is_cpu_budget else 6000

    async def _run():
        engine = DelegationEngine(cfg)
        try:
            return await engine.invoke(
                f"Extract only key facts, decisions, and action items. No preamble.\n"
                f"Source: {args.source}\n\n{raw[:limit]}",
                system=with_lang("Compression Engine. Be extremely concise.", cfg),
                max_tokens=engine.budget("compress", 1200),
                temperature=0.2,
                task="compress",
            )
        finally:
            await engine.aclose()

    result = asyncio.run(_run())
    console.print(result)


# ── note ─────────────────────────────────────────────────────────────────────

def _inject_related_links(vault, note_path, rel_path: str, folder: str, stem: str) -> None:
    """Best-effort forward wikilinks + backlinks after a direct CLI write, mirroring
    server.py's _post_write_links(): BGE search only, no llama.cpp call needed."""
    from .linker import inject_backlinks, wikilinks
    try:
        content = note_path.read_text(encoding="utf-8")
        hits = [h for h in vault.search(content[:600], limit=6) if h.get("path") != rel_path][:5]
        links = wikilinks(hits, vault.cfg.merge_threshold)
        if links:
            updated = content.rstrip() + f"\n\n## Related\n{links}\n"
            note_path.write_text(updated, encoding="utf-8")
            vault.index_note(updated, {"title": stem, "path": rel_path, "folder": folder})
            inject_backlinks(vault, stem, [h["path"] for h in hits
                                           if h.get("similarity", 0) >= vault.cfg.merge_threshold])
    except Exception:
        pass  # best-effort, matches server.py's tolerance for this step


def cmd_note_write(args):
    from datetime import datetime
    from rich.console import Console
    from .config import Config
    from .vault import VaultManager, resolve_vault_folder, safe_filename, yaml_quote_scalar

    console = Console()
    cfg = Config.load()
    if not cfg.is_configured():
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)
    resolved_folder = resolve_vault_folder(cfg, args.folder)
    if not resolved_folder:
        console.print(f"[red]Invalid folder[/red] '{args.folder}'. Valid: {cfg.vault_folders}")
        sys.exit(1)
    args.folder = resolved_folder

    content = _read_content(args.file, "note content")
    vault = VaultManager(cfg)
    safe = safe_filename(args.title)
    dest = cfg.vault / args.folder / f"{datetime.now().strftime('%Y-%m-%d')}-{safe}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    full = (
        f"---\ntitle: {yaml_quote_scalar(args.title)}\ndate: {datetime.now().strftime('%Y-%m-%d')}\n"
        f"ai_generated: false\n---\n\n{content}"
    )
    dest.write_text(full, encoding="utf-8")
    rel = str(dest.relative_to(cfg.vault))
    vault.index_note(full, {"title": args.title, "path": rel, "folder": args.folder})
    _inject_related_links(vault, dest, rel, args.folder, dest.stem)
    console.print(f"[green]✓[/green]  {rel}")


def cmd_note_read(args):
    from rich.console import Console
    from .config import Config
    from .vault import VaultManager

    console = Console()
    cfg = Config.load()
    if not cfg.is_configured():
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)
    vault = VaultManager(cfg)
    matches = vault.find_notes_by_stem(args.name)
    if not matches:
        console.print(f"[red]Not found:[/red] {args.name}")
        sys.exit(1)
    console.print(matches[0].read_text(encoding="utf-8"))


def cmd_note_update(args):
    from rich.console import Console
    from .config import Config
    from .vault import VaultManager

    console = Console()
    cfg = Config.load()
    if not cfg.is_configured():
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)

    content = _read_content(args.file, "content to append")
    vault = VaultManager(cfg)
    result = vault.update_note(args.name, content)
    if "error" in result:
        console.print(f"[red]Error:[/red] {result['error']}")
        sys.exit(1)
    matches = vault.find_notes_by_stem(args.name)
    if matches:
        f = matches[0]
        _inject_related_links(vault, f, result["path"], f.parent.name, f.stem)
    console.print(f"[green]✓[/green]  appended {result['appended_chars']} chars to {result['path']}")


def cmd_note_list(args):
    from rich.console import Console
    from .config import Config
    from .vault import VaultManager, resolve_vault_folder

    console = Console()
    cfg = Config.load()
    if not cfg.is_configured():
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)
    resolved_folder = resolve_vault_folder(cfg, args.folder)
    if not resolved_folder:
        console.print(f"[red]Invalid folder[/red] '{args.folder}'. Valid: {cfg.vault_folders}")
        sys.exit(1)
    args.folder = resolved_folder
    vault = VaultManager(cfg)
    notes = vault.list_notes(args.folder, limit=args.limit)
    if not notes:
        console.print("[dim]No notes.[/dim]")
        return
    for n in notes:
        console.print(f"[bold]{n['title']}[/bold]  [dim]{n['date']}  {n['path']}[/dim]")


def cmd_note_find_similar(args):
    from rich.console import Console
    from .config import Config
    from .vault import VaultManager

    console = Console()
    cfg = Config.load()
    if not cfg.is_configured():
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)
    vault = VaultManager(cfg)
    results = vault.find_similar(args.name, threshold=args.threshold, limit=args.limit)
    if results and "error" in results[0]:
        console.print(f"[red]Error:[/red] {results[0]['error']}")
        sys.exit(1)
    if not results:
        console.print("[dim]No similar notes found.[/dim]")
        return
    for r in results:
        console.print(f"[bold]{r['title']}[/bold]  [dim]({r['similarity']:.2f}) {r['path']}[/dim]")


# ── graph ────────────────────────────────────────────────────────────────────

def _graph_config():
    from .config import Config
    cfg = Config.load()
    if not cfg.is_configured():
        return None
    return cfg


def cmd_graph_build(args):
    import asyncio
    from rich.console import Console
    from . import graphbridge
    from .vault import VaultManager

    console = Console()
    cfg = _graph_config()
    if cfg is None:
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)

    try:
        vault = VaultManager(cfg)
        result = asyncio.run(
            graphbridge.build_graph(cfg, vault, args.path, name=args.name or None, force=args.force)
        )
    except ModuleNotFoundError as e:
        console.print(f"[red]Code-graph pipeline not installed:[/red] {e}\n"
                      'Run: pip install "delegation-core[graph]"')
        sys.exit(1)

    if "error" in result:
        console.print(f"[red]Error:[/red] {result['error']}")
        sys.exit(1)
    console.print(f"[green]✓[/green]  '{result['name']}': {result.get('node_count', 0)} nodes, "
                  f"{result.get('edge_count', 0)} edges, {result.get('community_count', 0)} communities")
    if result.get("graph_html"):
        console.print(f"  graph.html:    {result['graph_html']}")
    if result.get("callflow_html"):
        console.print(f"  callflow.html: {result['callflow_html']}")
    if result.get("wiki_articles"):
        console.print(f"  wiki articles: {result['wiki_articles']}")


def cmd_graph_preview(args):
    from rich.console import Console
    from rich.table import Table
    from . import graphbridge

    console = Console()
    cfg = _graph_config()
    if cfg is None:
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)

    try:
        result = graphbridge.preview_graph(cfg, args.path, args.name or None)
    except ModuleNotFoundError as e:
        console.print(f"[red]Code-graph pipeline not installed:[/red] {e}\n"
                      'Run: pip install "delegation-core[graph]"')
        sys.exit(1)

    if "error" in result:
        console.print(f"[red]{result['error']}[/red]")
        sys.exit(1)

    console.print(f"\n[bold]{result['name']}[/bold]  {result['source_path']}")
    counts = result.get("counts") or {}
    console.print("  " + " · ".join(f"{k}: {v}" for k, v in counts.items() if v))
    console.print(f"  {result.get('total_files')} arquivos · ~{result.get('total_words'):,} palavras"
                  .replace(",", "."))
    by_ext = result.get("code_by_extension") or {}
    if by_ext:
        console.print("  código: " + ", ".join(f"{e} {n}" for e, n in by_ext.items()))
    console.print(f"  gravaria em: [cyan]{result['would_write_to']}[/cyan]")

    if result.get("previous_build"):
        pb = result["previous_build"]
        console.print(f"  [yellow]já construído[/yellow] em {pb['built_at']}: "
                      f"{pb['node_count']} nós, {pb['community_count']} comunidades, "
                      f"{pb['vault_notes_filed']} notas no vault")

    scale = result.get("scale_reference") or []
    if scale:
        t = Table(title="grafos já construídos nesta máquina (referência de escala)")
        for c in ("grafo", "nós", "arestas", "comunidades", "notas no vault"):
            t.add_column(c)
        for s in scale:
            t.add_row(s["name"], str(s["node_count"]), str(s["edge_count"]),
                      str(s["community_count"]), str(s["vault_notes_filed"]))
        console.print(t)

    if result.get("source_map_hint"):
        console.print(f"\n[yellow]{result['source_map_hint']}[/yellow]")
        for m in result.get("source_maps", []):
            console.print(f"  {m['reconstructable_sources']} fontes em {m['map']}")
    if result.get("status") == "empty":
        console.print(f"\n[yellow]{result.get('message')}[/yellow]")


def cmd_graph_extract_sources(args):
    from rich.console import Console
    from . import graphbridge

    console = Console()
    result = graphbridge.extract_source_maps(args.path, args.out_dir)
    if "error" in result:
        console.print(f"[red]{result['error']}[/red]")
        sys.exit(1)
    if result.get("status") == "empty":
        console.print(f"[yellow]{result['message']}[/yellow]")
        return
    console.print(f"[green]✓[/green]  {result['files_written']} arquivos de "
                  f"{result['maps_used']} source map(s) → {result['out_dir']}")
    for root, n in (result.get("top_level") or {}).items():
        console.print(f"  {n:5d}  {root}/")
    console.print(f"\nAgora: delegation-core graph build {result['out_dir']}")


def cmd_embed_model(args):
    """List the calibrated embedding models, or switch to one."""
    from rich.console import Console
    from rich.table import Table
    from .config import Config
    from .embeddings import MODEL_PROFILES, collection_name_for, profile_for

    console = Console()
    cfg = Config.load()
    if not cfg.is_configured():
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)

    if not args.model:
        import chromadb
        counts = {}
        try:
            # Same settings every other caller uses (vault.py, doctor.py). chromadb
            # caches one System per path and rejects a second client on that path
            # with different settings, so a bare client here poisons the
            # VaultManager built later in this same process: that is how
            # `embed-model --reindex` came to index 0 notes without failing.
            client = chromadb.PersistentClient(
                path=str(cfg.chroma_path),
                settings=chromadb.Settings(anonymized_telemetry=False))
            counts = {c.name: c.count() for c in client.list_collections()}
        except Exception:
            pass

        table = Table(title="Modelos de embedding calibrados")
        for col in ("", "modelo", "dim", "ctx", "limiar", "idiomas", "indexado"):
            table.add_column(col)
        for name, p in MODEL_PROFILES.items():
            n = counts.get(p["collection"])
            table.add_row(
                "→" if name == cfg.bge_model else "",
                name, str(p["dim"]), str(p["max_seq"]), str(p["search_threshold"]),
                p["languages"],
                f"{n} linhas" if n else "[dim]não indexado[/dim]",
            )
        console.print(table)
        for name, p in MODEL_PROFILES.items():
            console.print(f"  [dim]{name}: {p['summary']}[/dim]")
        console.print("\nTrocar:  delegation-core embed-model <modelo> [--reindex]")
        return

    target = args.model
    profile = profile_for(target)
    collection = collection_name_for(target)
    if target == cfg.bge_model:
        console.print(f"[yellow]Já em uso:[/yellow] {target}")
        return

    previous, previous_threshold = cfg.bge_model, cfg.search_threshold
    cfg.bge_model = target
    cfg.search_threshold = profile["search_threshold"]
    cfg.save()
    console.print(f"[green]✓[/green]  {previous} → [bold]{target}[/bold]")
    console.print(f"  coleção: {collection}  ·  search_threshold: "
                  f"{previous_threshold} → {cfg.search_threshold}")

    import chromadb
    existing = 0
    try:
        # Same settings every other caller uses (vault.py, doctor.py). chromadb
        # caches one System per path and rejects a second client on that path
        # with different settings, so a bare client here poisons the
        # VaultManager built later in this same process: that is how
        # `embed-model --reindex` came to index 0 notes without failing.
        client = chromadb.PersistentClient(
            path=str(cfg.chroma_path),
            settings=chromadb.Settings(anonymized_telemetry=False))
        existing = client.get_collection(collection).count()
    except Exception:
        pass

    if existing and not args.reindex:
        console.print(f"  [green]{existing} linhas já indexadas nessa coleção: pronto para usar.[/green]")
    elif not args.reindex:
        console.print("  [yellow]Coleção vazia.[/yellow] Rode: delegation-core embed-model "
                      f"{target} --reindex")
    if args.reindex:
        _reindex_everything(console, cfg)

    console.print("  [dim]Reinicie o servidor MCP para o cliente pegar a troca.[/dim]")


def _source_row_count(vault, source: str) -> int:
    """How many rows this ingest source still has in the collection.

    Used to tell "the index was rebuilt and this source is gone" from "the
    source is indexed and merely unchanged": the two cases a reindex must
    treat differently. Counting is deliberately cheap and ids-only; the answer
    only has to distinguish zero from non-zero. Any failure reads as 0, which
    errs toward re-ingesting: costly, but never silently leaves a source out.
    """
    try:
        vault._ensure_ready()
        if not vault.collection:
            return 0
        got = vault.collection.get(where={"source_folder": source},
                                   limit=1, include=[])
        return len(got.get("ids") or [])
    except Exception:
        return 0


def _reindex_everything(console, cfg):
    """Rebuild the vault index AND replay every ingested external folder.

    reindex_vault only walks cfg.vault_folders, so a model switch that only
    reindexed the vault silently dropped every ingest_folder'd file: 76 of them
    on this machine, from seven registered sources. The registry is what makes
    them recoverable, so it is replayed here rather than left to be noticed later.
    """
    import json
    from .config import CONFIG_DIR
    from .ingest import IngestManager
    from .vault import VaultManager

    vault = VaultManager(cfg)
    console.print(f"  reindexando {cfg.vault_path} ...")
    count = vault.reindex_vault(force=True)
    console.print(f"  [green]✓[/green]  {count} notas indexadas")

    registry_path = CONFIG_DIR / "ingested_sources.json"
    if not registry_path.exists():
        return
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception as e:
        console.print(f"  [yellow]registry de ingestão ilegível: {e}[/yellow]")
        return

    ingest = IngestManager(vault)
    total = 0
    recovered = 0
    for source, meta in registry.items():
        # Force this source only when its rows are actually gone.
        #
        # Both extremes are wrong. Never forcing means v0.12's per-file
        # mtime+size cache makes every file compare as unchanged, so the command
        # re-embeds nothing: exactly when it is being run to recover, after a
        # Chroma rebuild or a model switch left the new collection empty while
        # the registry insists everything is indexed. Always forcing costs a
        # full re-embed of the corpus on every invocation: 4.8 hours for 6,637
        # files in one field report, which is how a recovery command stops being
        # run at all.
        #
        # An empty row count is precisely the condition that distinguishes them.
        missing = _source_row_count(vault, source) == 0
        recovered += missing
        result = ingest.ingest(source, recursive=meta.get("recursive", True),
                               force=missing)
        total += result.get("indexed", 0)
    if registry:
        note = f" ({recovered} recuperada(s) do zero)" if recovered else " (nenhuma faltando)"
        console.print(f"  [green]✓[/green]  {total} arquivo(s) externo(s) reindexado(s) "
                      f"de {len(registry)} fonte(s){note}")


def cmd_doctor(args):
    from rich.console import Console
    from rich.table import Table
    from . import doctor

    console = Console()
    cfg = _graph_config()
    if cfg is None:
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)

    if getattr(args, "clean_orphans", False):
        cleaned = doctor.clean_orphan_segments(cfg)
        console.print(f"[green]✓[/green] Cleaned {cleaned} orphan segment directory(ies).")

    if getattr(args, "rebuild_fts", False):
        rebuilt = doctor.rebuild_fts(cfg)
        if rebuilt:
            console.print("[green]✓[/green] Rebuilt full-text search index successfully.")
        else:
            console.print("[red]✗[/red] Failed to rebuild full-text search index.")

    result = doctor.run_all(cfg)
    icon = {"ok": "[green]✓[/green]", "warn": "[yellow]![/yellow]",
            "error": "[red]✗[/red]", "skip": "[dim]-[/dim]"}

    table = Table(show_header=False, box=None, padding=(0, 1))
    for check in result["checks"]:
        table.add_row(icon[check["status"]], check["check"], check["detail"])
        if check.get("fix"):
            table.add_row("", "", f"[dim]→ {check['fix']}[/dim]")
    console.print(table)

    c = result["counts"]
    console.print(f"\n{c['ok']} ok · {c['warn']} avisos · {c['error']} erros"
                  + (f" · {c['skip']} pulados" if c["skip"] else ""))
    # Non-zero exit on error only: warnings are informational, and a CI/cron
    # caller should not fail a run over a stale registry entry.
    sys.exit(1 if result["status"] == "error" else 0)


def cmd_graph_list(_args):
    from rich.console import Console
    from . import graphbridge

    console = Console()
    cfg = _graph_config()
    if cfg is None:
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)
    result = graphbridge.list_graphs(cfg)
    if not result["graphs"]:
        console.print("[dim]No graphs built yet. Run: delegation-core graph build <path>[/dim]")
        return
    for name, info in result["graphs"].items():
        console.print(f"[bold]{name}[/bold]  [dim]{info['source_path']}[/dim]")
        console.print(f"  {info['node_count']} nodes, {info['edge_count']} edges, "
                      f"{info['community_count']} communities: built {info['built_at']}")


def cmd_graph_report(args):
    from rich.console import Console
    from . import graphbridge

    console = Console()
    cfg = _graph_config()
    if cfg is None:
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)
    result = graphbridge.get_report(cfg, args.name)
    if "error" in result:
        console.print(f"[red]Error:[/red] {result['error']}")
        sys.exit(1)
    console.print(result["report"])


def cmd_graph_affected(args):
    from rich.console import Console
    from . import graphbridge

    console = Console()
    cfg = _graph_config()
    if cfg is None:
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)
    try:
        result = graphbridge.get_affected(cfg, args.name, args.query, depth=args.depth)
    except ModuleNotFoundError as e:
        console.print(f"[red]Code-graph pipeline not installed:[/red] {e}")
        sys.exit(1)
    if "error" in result:
        console.print(f"[red]Error:[/red] {result['error']}")
        sys.exit(1)
    console.print(result["report"])


def cmd_graph_hook_install(args):
    from rich.console import Console
    from . import graph_hook

    console = Console()
    result = graph_hook.install(args.path, name=args.name or None)
    if "error" in result:
        console.print(f"[red]Error:[/red] {result['error']}")
        sys.exit(1)
    console.print(f"[green]✓[/green]  {result['status']}  {result.get('path', '')}")


def cmd_graph_hook_uninstall(args):
    from rich.console import Console
    from . import graph_hook

    console = Console()
    result = graph_hook.uninstall(args.path)
    if "error" in result:
        console.print(f"[red]Error:[/red] {result['error']}")
        sys.exit(1)
    console.print(f"[green]✓[/green]  {result['status']}")


def cmd_graph_hook_status(args):
    from rich.console import Console
    from . import graph_hook

    console = Console()
    result = graph_hook.status(args.path)
    if "error" in result:
        console.print(f"[red]Error:[/red] {result['error']}")
        sys.exit(1)
    state = "[green]installed[/green]" if result.get("installed") else "[dim]not installed[/dim]"
    console.print(state)


# ── process ──────────────────────────────────────────────────────────────────

def _process_tracker():
    from .config import Config
    from .tracker import ProcessTracker
    cfg = Config.load()
    if not cfg.is_configured():
        return None
    return ProcessTracker(cfg.processes_path)


def cmd_process_create(args):
    from rich.console import Console
    console = Console()
    tracker = _process_tracker()
    if tracker is None:
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)
    steps = [s.strip() for s in args.steps.split(",") if s.strip()] if args.steps else []
    proc = tracker.create(name=args.name, description=args.description or "", steps=steps)
    console.print(f"[green]✓[/green]  {proc['id']}  {proc['name']}")


def cmd_process_list(args):
    from rich.console import Console
    console = Console()
    tracker = _process_tracker()
    if tracker is None:
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)
    processes = tracker.list_processes(status=args.status, query=args.query or "")
    if not processes:
        console.print("[dim]No processes.[/dim]")
        return
    for p in processes:
        done = sum(s["done"] for s in p["steps"]) if p["steps"] else 0
        total = len(p["steps"])
        console.print(f"[bold]{p['id']}[/bold]  {p['name']}  [dim]({done}/{total}: {p['status']})[/dim]")


def cmd_process_update(args):
    from rich.console import Console
    console = Console()
    tracker = _process_tracker()
    if tracker is None:
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)
    proc = tracker.update(
        process_id=args.process_id, note=args.note or "",
        step_done=args.step_done if args.step_done is not None else -1,
        status=args.status or "",
    )
    if proc is None:
        console.print(f"[red]Not found:[/red] {args.process_id}")
        sys.exit(1)
    console.print(f"[green]✓[/green]  {proc['id']} updated")


def cmd_process_get(args):
    from rich.console import Console
    console = Console()
    tracker = _process_tracker()
    if tracker is None:
        console.print("[yellow]Not configured.[/yellow] Run: delegation-core setup")
        sys.exit(1)
    proc = tracker.get(args.process_id)
    if proc is None:
        console.print(f"[red]Not found:[/red] {args.process_id}")
        sys.exit(1)
    console.print(json.dumps(proc, indent=2))


def main():
    parser = argparse.ArgumentParser(
        prog="delegation-core",
        description="Local MCP delegation server: llama.cpp + BGE + ChromaDB + Obsidian vault",
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    sub.add_parser("setup",    help="Interactive setup wizard (run once per machine)")
    p_run = sub.add_parser("run", help="Start the MCP server (used by Claude Desktop)")
    p_run.add_argument(
        "--recalibrate", action="store_true",
        help="Reset and rerun tok/sec auto-calibration before starting (use after swapping models)",
    )
    sub.add_parser(
        "mcp-stdio",
        help="Serve MCP on stdio, forwarding to the daemon (how Claude Desktop connects)")

    p_update = sub.add_parser(
        "update", help="Update the installed package from its source, stopping the daemon first")
    p_update.add_argument("--check", action="store_true",
                          help="Report what would change; touch nothing")
    p_update.add_argument("--no-restart", action="store_true",
                          help="Leave the daemon stopped after updating")

    p_repair = sub.add_parser(
        "repair-empty-source",
        help="Find notes synthesised from files that had no extractable text")
    p_repair.add_argument("--apply", action="store_true",
                          help="Replace each note's body with the source record")
    p_repair.add_argument("--archive", action="store_true",
                          help="Move the notes to _dump/_archive/ instead of replacing")

    p_post = sub.add_parser(
        "post-install",
        help="Finish an install: docs, hooks, skills, dashboard, service, clients")
    p_post.add_argument("--root", default=None,
                        help="The source checkout; defaults to the recorded install source")
    p_post.add_argument("--no-wizard", action="store_true",
                        help="On a fresh install, report that setup is needed instead of running it")

    p_uninstall = sub.add_parser(
        "uninstall",
        help="Remove the installation (keeps your vault and downloaded models)")
    p_uninstall.add_argument("--yes", action="store_true",
                             help="Skip the confirmation prompt")
    p_uninstall.add_argument("--dry-run", action="store_true",
                             help="Print exactly what would be removed; touch nothing")

    p_service = sub.add_parser(
        "service", help="Install/remove the daemon as a per-user background service")
    p_service.add_argument("action", choices=["install", "uninstall", "status"])

    p_clients = sub.add_parser(
        "clients", help="Point MCP clients at the HTTP daemon (migrates from stdio)")
    p_clients.add_argument("--claude-code", action="store_true",
                           help="Rewrite delegation-core's entry in ~/.claude.json")
    p_clients.add_argument("--claude-desktop", action="store_true",
                           help="Point Claude Desktop (claude_desktop_config.json) at the daemon")
    p_clients.add_argument("--antigravity", action="store_true",
                           help="Point Antigravity / the Gemini CLI (agy) at the daemon")
    p_clients.add_argument("--codex", action="store_true",
                           help="Append delegation-core's table to ~/.codex/config.toml")
    p_clients.add_argument("--show", action="store_true",
                           help="Print URL and token without writing anything")
    sub.add_parser("status",   help="Check vault, model, binary, llama.cpp, and feature config")
    p_doctor = sub.add_parser("doctor",   help="Diagnose installation drift and vault hygiene problems")
    p_doctor.add_argument("--clean-orphans", action="store_true",
                          help="Remove orphan ChromaDB segment directories on disk")
    p_doctor.add_argument("--rebuild-fts", action="store_true",
                          help="Rebuild SQLite full-text search index if corrupted")
    p_reindex = sub.add_parser("reindex", help="Rebuild ChromaDB search index from vault folders")
    p_reindex.add_argument("--force", action="store_true",
                           help="Reindex every note, not just those changed since last run "
                                "(needed to backfill new metadata fields)")
    p_reindex.add_argument("--timeout", type=float, default=None,
                           help="Maximum seconds to wait for daemon job to complete (default 3600s, 0 for unlimited)")
    _add_local_flag(p_reindex)
    p_maintain = sub.add_parser("maintain", help="Run inbox maintenance once and exit")
    _add_local_flag(p_maintain)

    # cmd_embed_model existed and was never registered, while cmd_status told
    # users to run `delegation-core embed-model ...`: the command it named did
    # not exist. Same shape as the graph labeler nothing called.
    p_embed = sub.add_parser("embed-model",
                             help="List calibrated embedding models, or switch to one")
    p_embed.add_argument("model", nargs="?", default=None,
                         help="Model to switch to; omit to list what is available")
    p_embed.add_argument("--reindex", action="store_true",
                         help="Reindex the vault into the new model's collection")

    p_dashboard_api = sub.add_parser(
        "dashboard-api", help="Run the local JSON API used by the Tauri dashboard (standalone/debug)"
    )
    p_dashboard_api.add_argument("--port", type=int, default=0, help="Port to bind (0 = pick a free one)")
    p_dashboard_api.add_argument("--host", default="127.0.0.1")

    p_ingest = sub.add_parser("ingest", help="Index files from an external folder without moving them")
    p_ingest.add_argument("path",           help="Absolute path to a file or directory to index")
    p_ingest.add_argument("--no-recursive", action="store_true", help="Only index top-level files")
    p_ingest.add_argument("--force",        action="store_true", help="Re-index even if file mtime and size are unchanged")
    p_ingest.add_argument("--exclude",      default="", help="Comma-separated glob patterns to exclude (e.g. Logs,*.tmp)")
    p_ingest.add_argument("--timeout",      type=float, default=None,
                          help="Maximum seconds to wait for daemon job to complete (default 3600s, 0 for unlimited)")
    _add_local_flag(p_ingest)

    p_relink = sub.add_parser("relink", help="Add wikilinks to notes in a vault subfolder")
    p_relink.add_argument("folder",                   help="Vault-relative folder path (e.g. meetings)")
    p_relink.add_argument("--days",          type=int, default=None,
                          help="Restrict to notes modified within last N days")
    p_relink.add_argument("--min-similarity", dest="min_similarity", type=float, default=None,
                          help="Override similarity threshold (default from config)")
    p_relink.add_argument("--max-links",      dest="max_links",      type=int, default=8,
                          help="Maximum wikilinks per note (default 8)")

    p_search = sub.add_parser("search", help="Query the vault (BGE similarity search, no LLM required)")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=5)

    p_compress = sub.add_parser("compress", help="Extract key facts/decisions/action items from text via llama.cpp")
    p_compress.add_argument("source", help="Short label for what this text is (e.g. a filename or 'email thread')")
    p_compress.add_argument("--file", default=None, help="Read content from this file instead of stdin")

    # ── note ─────────────────────────────────────────────────────────────────
    p_note = sub.add_parser("note", help="Direct vault note access: write/read/update/list/find-similar")
    note_sub = p_note.add_subparsers(dest="note_command", metavar="note-command")

    p_note_write = note_sub.add_parser("write", help="Write a new note")
    p_note_write.add_argument("folder", help="Vault folder (see: delegation-core status)")
    p_note_write.add_argument("title")
    p_note_write.add_argument("--file", default=None, help="Read content from this file instead of stdin")

    p_note_read = note_sub.add_parser("read", help="Print a note's full content")
    p_note_read.add_argument("name", help="Filename stem, partial match")

    p_note_update = note_sub.add_parser("update", help="Append content to an existing note")
    p_note_update.add_argument("name", help="Filename stem, partial match")
    p_note_update.add_argument("--file", default=None, help="Read content from this file instead of stdin")

    p_note_list = note_sub.add_parser("list", help="List notes in a folder, newest first")
    p_note_list.add_argument("folder")
    p_note_list.add_argument("--limit", type=int, default=20)

    p_note_sim = note_sub.add_parser("find-similar", help="Find notes semantically similar to a given note")
    p_note_sim.add_argument("name")
    p_note_sim.add_argument("--threshold", type=float, default=0.80)
    p_note_sim.add_argument("--limit", type=int, default=5)

    # ── graph ────────────────────────────────────────────────────────────────
    p_graph = sub.add_parser("graph", help="Code-graph pipeline: build/list/report/affected/hook (needs [graph] extra)")
    graph_sub = p_graph.add_subparsers(dest="graph_command", metavar="graph-command")

    p_graph_build = graph_sub.add_parser("build", help="Build a code graph for a local directory")
    p_graph_build.add_argument("path")
    p_graph_build.add_argument("--name", default="", help="Graph name (default: directory basename)")
    p_graph_build.add_argument("--force", action="store_true", help="Rebuild even if already built")

    p_graph_preview = graph_sub.add_parser(
        "preview", help="Show what a build would cover, without building or writing anything")
    p_graph_preview.add_argument("path")
    p_graph_preview.add_argument("--name", default="", help="Graph name (default: directory basename)")

    p_graph_extract = graph_sub.add_parser(
        "extract-sources", help="Reconstruct original sources from .js.map sidecars, then graph those")
    p_graph_extract.add_argument("path")
    p_graph_extract.add_argument("out_dir", help="Destination directory (must be empty or absent)")

    graph_sub.add_parser("list", help="List previously built graphs")

    p_graph_report = graph_sub.add_parser("report", help="Print a graph's full GRAPH_REPORT.md")
    p_graph_report.add_argument("name")

    p_graph_affected = graph_sub.add_parser("affected", help="Blast-radius query: what's affected if X changes")
    p_graph_affected.add_argument("name", help="Graph name (see: delegation-core graph list)")
    p_graph_affected.add_argument("query", help="A file path or symbol label, e.g. auth.py or AuthService.login")
    p_graph_affected.add_argument("--depth", type=int, default=2)

    p_graph_hook = graph_sub.add_parser("hook", help="Git post-commit hook: auto-rebuild a graph after every commit")
    hook_sub = p_graph_hook.add_subparsers(dest="hook_command", metavar="hook-command")

    p_hook_install = hook_sub.add_parser("install", help="Install the post-commit hook")
    p_hook_install.add_argument("path", nargs="?", default=".", help="Path inside the git repo (default: cwd)")
    p_hook_install.add_argument("--name", default="", help="Graph name (default: repo directory basename)")

    p_hook_uninstall = hook_sub.add_parser("uninstall", help="Remove the post-commit hook")
    p_hook_uninstall.add_argument("path", nargs="?", default=".")

    p_hook_status = hook_sub.add_parser("status", help="Check whether the hook is installed")
    p_hook_status.add_argument("path", nargs="?", default=".")

    # ── process ──────────────────────────────────────────────────────────────
    p_process = sub.add_parser("process", help="Cross-session process tracking: create/list/update/get")
    process_sub = p_process.add_subparsers(dest="process_command", metavar="process-command")

    p_proc_create = process_sub.add_parser("create", help="Start tracking a new process")
    p_proc_create.add_argument("name")
    p_proc_create.add_argument("--description", default="")
    p_proc_create.add_argument("--steps", default="", help="Comma-separated step descriptions")

    p_proc_list = process_sub.add_parser("list", help="List tracked processes")
    p_proc_list.add_argument("--status", default="active", help="active|paused|done|cancelled|all")
    p_proc_list.add_argument("--query", default="")

    p_proc_update = process_sub.add_parser("update", help="Update a tracked process")
    p_proc_update.add_argument("process_id")
    p_proc_update.add_argument("--note", default="")
    p_proc_update.add_argument("--step-done", dest="step_done", type=int, default=None)
    p_proc_update.add_argument("--status", default="")

    p_proc_get = process_sub.add_parser("get", help="Full detail view of a tracked process")
    p_proc_get.add_argument("process_id")

    args = parser.parse_args()

    dispatch = {
        "setup":    cmd_setup,
        "run":      cmd_run,
        "service":  cmd_service,
        "update":   cmd_update,
        "repair-empty-source": cmd_repair_empty_source,
        "post-install": cmd_post_install,
        "uninstall": cmd_uninstall,
        "mcp-stdio": cmd_mcp_stdio,
        "clients":  cmd_clients,
        "status":   cmd_status,
        "doctor":   cmd_doctor,
        "reindex":  cmd_reindex,
        "maintain": cmd_maintain,
        "dashboard-api": cmd_dashboard_api,
        "ingest":   cmd_ingest,
        "relink":   cmd_relink,
        "search":   cmd_search,
        "compress": cmd_compress,
        "embed-model": cmd_embed_model,
    }

    note_dispatch = {
        "write":        cmd_note_write,
        "read":         cmd_note_read,
        "update":       cmd_note_update,
        "list":         cmd_note_list,
        "find-similar": cmd_note_find_similar,
    }

    graph_dispatch = {
        "build":    cmd_graph_build,
        "preview":  cmd_graph_preview,
        "extract-sources": cmd_graph_extract_sources,
        "list":     cmd_graph_list,
        "report":   cmd_graph_report,
        "affected": cmd_graph_affected,
    }

    graph_hook_dispatch = {
        "install":   cmd_graph_hook_install,
        "uninstall": cmd_graph_hook_uninstall,
        "status":    cmd_graph_hook_status,
    }

    process_dispatch = {
        "create": cmd_process_create,
        "list":   cmd_process_list,
        "update": cmd_process_update,
        "get":    cmd_process_get,
    }

    if args.command == "note":
        if args.note_command in note_dispatch:
            note_dispatch[args.note_command](args)
        else:
            p_note.print_help()
    elif args.command == "graph":
        if args.graph_command == "hook":
            if args.hook_command in graph_hook_dispatch:
                graph_hook_dispatch[args.hook_command](args)
            else:
                p_graph_hook.print_help()
        elif args.graph_command in graph_dispatch:
            graph_dispatch[args.graph_command](args)
        else:
            p_graph.print_help()
    elif args.command == "process":
        if args.process_command in process_dispatch:
            process_dispatch[args.process_command](args)
        else:
            p_process.print_help()
    elif args.command in dispatch:
        dispatch[args.command](args)
    else:
        parser.print_help()
