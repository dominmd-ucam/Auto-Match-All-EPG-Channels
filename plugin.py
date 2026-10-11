"""Auto Match All Channels - plugin de Dispatcharr.

Replica el boton "Auto Match" de la edicion individual de un canal
(POST /api/channels/channels/<id>/match-epg/ -> tasks.match_single_channel_epg ->
apps.channels.epg_matching.run_single_channel_epg_match) y lo ejecuta para
muchos canales en un hilo en segundo plano, con progreso y resumen final.
"""
import json
import logging
import os
import threading
import time

logger = logging.getLogger("plugins.auto_match_all")

# Dispatcharr usa el nombre de la carpeta (minusculas, espacios -> _) como clave del plugin.
PLUGIN_KEY = os.path.basename(os.path.dirname(os.path.abspath(__file__))).lower().replace(" ", "_")

_lock = threading.Lock()
_state = {
    "running": False,
    "stop": False,
    "total": 0,
    "done": 0,
    "matched": 0,
    "unchanged": 0,
    "unmatched": 0,
    "errors": 0,
    "current": "",
    "started": None,
    "finished": None,
    "summary": "",
}


def _notify(message, **extra):
    try:
        from core.utils import send_websocket_update
        payload = {"type": "plugin", "plugin": PLUGIN_KEY, "message": message}
        payload.update(extra)
        send_websocket_update("updates", "update", payload)
    except Exception as exc:  # el aviso a la UI nunca debe romper el trabajo
        logger.warning("No se pudo enviar el aviso por websocket: %s", exc)


def _bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "si")
    return bool(value)


def _build_queryset(settings):
    from apps.channels.models import Channel

    qs = Channel.objects.all()
    if _bool(settings.get("only_without_epg"), True):
        # Mismo criterio que el filtro "No EPG" de la tabla: una EPG puesta
        # solo por override cuenta como asignada.
        qs = qs.filter(epg_data__isnull=True, override__epg_data__isnull=True)

    groups = [g.strip().lower() for g in str(settings.get("group_filter") or "").split(",") if g.strip()]
    if groups:
        from django.db.models.functions import Lower
        qs = qs.annotate(_grp=Lower("channel_group__name")).filter(_grp__in=groups)

    qs = qs.order_by("channel_number", "id")
    try:
        limit = int(settings.get("max_channels") or 0)
    except (TypeError, ValueError):
        limit = 0
    ids = list(qs.values_list("id", flat=True))
    if limit > 0:
        ids = ids[:limit]
    return ids


def _progress(extra=""):
    s = _state
    pct = round(s["done"] / s["total"] * 100, 1) if s["total"] else 0
    msg = (
        f"Auto Match: {s['done']}/{s['total']} ({pct}%) - "
        f"con match: {s['matched'] + s['unchanged']}, sin match: {s['unmatched']}, errores: {s['errors']}"
    )
    if extra:
        msg += f" - {extra}"
    return msg


def _finish(prefix):
    s = _state
    s["finished"] = time.time()
    elapsed = int(s["finished"] - (s["started"] or s["finished"]))
    s["summary"] = (
        f"{prefix}: {s['done']}/{s['total']} canales en {elapsed}s. "
        f"Nuevos matches: {s['matched']}, ya correctos: {s['unchanged']}, "
        f"sin match: {s['unmatched']}, errores: {s['errors']}."
    )
    s["running"] = False
    s["current"] = ""
    logger.info(s["summary"])
    _notify(s["summary"], refresh_channels=True, finished=True)
    try:
        from core.utils import send_websocket_update
        send_websocket_update("updates", "update", {
            "success": True, "type": "epg_match", "refresh_channels": True,
            "matches_count": s["matched"], "message": s["summary"], "associations": [],
        })
    except Exception:
        pass


def _worker_individual(channel_ids):
    """Llama a run_single_channel_epg_match por cada canal (lo mismo que el boton)."""
    from django.db import close_old_connections
    from apps.channels import epg_matching

    s = _state
    # run_single_channel_epg_match descarga el modelo ML en cada llamada; lo
    # aplazamos al final de la tanda para no recargarlo canal a canal.
    original_cleanup = epg_matching.cleanup_after_matching
    epg_matching.cleanup_after_matching = lambda: None
    try:
        for idx, cid in enumerate(channel_ids, 1):
            if s["stop"]:
                break
            close_old_connections()
            try:
                from apps.channels.models import Channel
                s["current"] = Channel.objects.filter(id=cid).values_list("name", flat=True).first() or str(cid)
                result = epg_matching.run_single_channel_epg_match(cid) or {}
                if result.get("matched"):
                    if result.get("unchanged"):
                        s["unchanged"] += 1
                    else:
                        s["matched"] += 1
                else:
                    msg = str(result.get("message", ""))
                    if msg.startswith("Error during matching") or msg == "Channel not found":
                        s["errors"] += 1
                    else:
                        s["unmatched"] += 1
            except Exception as exc:
                logger.error("Error en canal %s: %s", cid, exc, exc_info=True)
                s["errors"] += 1
            s["done"] = idx
            if idx % 10 == 0 or idx == len(channel_ids):
                _notify(_progress(s["current"]))
        _finish("Detenido" if s["stop"] else "Completado")
    except Exception as exc:
        logger.error("Fallo general del Auto Match masivo: %s", exc, exc_info=True)
        _finish(f"Interrumpido por error ({exc})")
    finally:
        epg_matching.cleanup_after_matching = original_cleanup
        try:
            epg_matching.cleanup_after_matching()
        except Exception:
            pass
        try:
            from django.db import connection
            connection.close()
        except Exception:
            pass


def _worker_bulk(channel_ids, dry_run):
    """Misma logica que la tarea match_epg_channels, restringida a los canales elegidos."""
    from django.db import close_old_connections
    from apps.channels import epg_matching
    from apps.channels.models import Channel

    s = _state
    try:
        close_old_connections()
        region = epg_matching.get_preferred_region_code()
        channels_data = [
            epg_matching.prepare_channel_match_data(c)
            for c in Channel.objects.filter(id__in=channel_ids)
        ]
        epg_data, index = epg_matching.build_epg_matching_catalog()
        result = epg_matching.match_channels_to_epg(
            channels_data, epg_data, region, use_ml=True, send_progress=True, epg_tvg_id_index=index
        )
        to_update = result["channels_to_update"]
        unchanged = result.get("unchanged_channels", [])
        if dry_run:
            s["matched"] = len(to_update)
        else:
            changed = epg_matching.apply_matched_epg_to_channels(to_update)
            s["matched"] = len(changed)
        s["unchanged"] = len(unchanged)
        s["done"] = s["total"]
        s["unmatched"] = max(s["total"] - s["matched"] - s["unchanged"], 0)
        _finish("Simulacion completada (sin cambios)" if dry_run else "Completado")
    except Exception as exc:
        logger.error("Fallo general del Auto Match masivo: %s", exc, exc_info=True)
        s["errors"] += 1
        _finish(f"Interrumpido por error ({exc})")
    finally:
        try:
            epg_matching.cleanup_after_matching()
            from django.db import connection
            connection.close()
        except Exception:
            pass


def _manifest():
    try:
        with open(os.path.join(os.path.dirname(__file__), "plugin.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


_MANIFEST = _manifest()


class Plugin:
    name = "Auto Match All Channels"
    version = "1.0.0"
    description = "Auto Match de EPG masivo, canal a canal, con filtro por grupo."
    author = "Domingo"
    help_url = "https://github.com/Dispatcharr/Dispatcharr/blob/main/Plugins.md"

    # Los campos y acciones se leen de plugin.json
    fields = _MANIFEST.get("fields", [])
    actions = _MANIFEST.get("actions", [])

    def run(self, action, params, context):
        settings = (context or {}).get("settings", {}) or {}
        if action == "run_auto_match_all":
            return self._start(settings)
        if action == "status":
            return self._status()
        if action == "stop":
            return self._stop()
        return {"status": "error", "message": f"Accion desconocida: {action}"}

    def stop(self, context):
        _state["stop"] = True

    def _start(self, settings):
        with _lock:
            if _state["running"]:
                return {"status": "error", "message": "Ya hay una ejecucion en curso. " + _progress()}
            ids = _build_queryset(settings)
            if not ids:
                return {"status": "ok", "message": "No hay canales que cumplan el filtro. Nada que hacer."}
            mode = str(settings.get("mode") or "individual")
            dry_run = _bool(settings.get("dry_run")) and mode == "bulk"
            _state.update(
                running=True, stop=False, total=len(ids), done=0, matched=0, unchanged=0,
                unmatched=0, errors=0, current="", started=time.time(), finished=None, summary="",
            )
            if mode == "bulk":
                target, args = _worker_bulk, (ids, dry_run)
            else:
                target, args = _worker_individual, (ids,)
            threading.Thread(target=target, args=args, daemon=True, name="auto-match-all").start()

        msg = f"Iniciado Auto Match ({mode}) sobre {len(ids)} canales en segundo plano."
        _notify(msg)
        return {"status": "ok", "message": msg, "total": len(ids)}

    def _status(self):
        if _state["running"]:
            return {"status": "ok", "message": _progress(_state["current"]), "running": True}
        if _state["summary"]:
            return {"status": "ok", "message": _state["summary"], "running": False}
        return {"status": "ok", "message": "Sin ejecuciones todavia.", "running": False}

    def _stop(self):
        if not _state["running"]:
            return {"status": "ok", "message": "No hay ninguna ejecucion en curso."}
        _state["stop"] = True
        return {"status": "ok", "message": "Parada solicitada; terminara tras el canal actual."}
