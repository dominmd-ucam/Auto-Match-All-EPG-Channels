# Auto Match All Channels

Plugin para [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) que ejecuta el **Auto Match de EPG de forma masiva**, canal a canal, sobre todos los canales (o solo los que no tienen EPG asignada), con filtro por grupo, progreso y resumen final.

---

## Que hace

El boton "Auto Match" de la edicion individual de un canal solo trabaja con un canal cada vez. Este plugin automatiza ese proceso:

- Recorre los canales sin EPG asignada (o todos, si lo configuras).
- Para cada canal ejecuta la misma funcion que el boton individual.
- Muestra el progreso mientras trabaja y un resumen al terminar.
- Permite filtrar por grupo de canales.
- Trabaja en segundo plano, asi que la interfaz de Dispatcharr no se bloquea.

## Como funciona por dentro

El boton individual de Dispatcharr sigue esta cadena:

```
POST /api/channels/channels/<id>/match-epg/
  -> tasks.match_single_channel_epg
    -> apps.channels.epg_matching.run_single_channel_epg_match(channel_id)
```

`run_single_channel_epg_match` busca la EPG en este orden:

1. `tvg_id` exacto.
2. `gracenote_id` exacto.
3. Coincidencia por nombre (fuzzy, con ML si esta disponible).

El plugin llama directamente a esa misma funcion para cada canal, por lo que el resultado es identico al del boton. Al estar dentro del proceso de Dispatcharr, no necesita credenciales ni llamadas HTTP.

Detalles de implementacion:

- Se ejecuta en un hilo en segundo plano; `run()` devuelve de inmediato.
- Aplaza la liberacion del modelo ML hasta el final de la tanda, en vez de recargarlo tras cada canal.
- "Sin EPG" usa el mismo criterio que el filtro "No EPG" de la tabla de canales: una EPG puesta solo por override cuenta como asignada.
- Solo permite una ejecucion a la vez.

---

## Instalacion

- [ ] Descarga `auto_match_all_channels.zip` (el zip debe contener la carpeta `auto_match_all_channels/` con `plugin.json` y `plugin.py`).
- [ ] En Dispatcharr, abre la pagina de **Plugins** y pulsa **Import**.
- [ ] Sube el zip.
- [ ] Activa el plugin (los plugins vienen desactivados por defecto).
- [ ] Ajusta las opciones y pulsa **Run Auto Match All Channels**.

Instalacion manual: copia la carpeta `auto_match_all_channels/` a `/app/data/plugins/` y recarga la lista de plugins.

---

## Opciones

| Opcion | Por defecto | Descripcion |
|---|---|---|
| Solo canales sin EPG asignado | Si | Si lo desactivas, procesa todos los canales y puede reasignar EPG ya existente. |
| Filtrar por grupo de canales | vacio | Nombres de grupo separados por comas, sin distinguir mayusculas. Vacio = todos. |
| Modo de matching | Individual | `Individual`: igual que el boton, canal a canal. `Masivo`: umbrales conservadores, mas rapido. |
| Maximo de canales por ejecucion | 0 | 0 = sin limite. Util para probar con pocos canales. |
| Simulacion | No | Solo en modo Masivo: calcula los matches sin guardarlos. |

### Modo Individual vs Masivo

- **Individual**: usa la funcion del boton de cada canal (umbrales mas permisivos). Mas matches por nombre, pero mas lento con muchos canales.
- **Masivo**: usa la logica de la tarea interna `match_epg_channels` (umbrales conservadores), procesando todos los canales de una vez. Mas rapido, con menos matches por nombre.

---

## Acciones

| Accion | Descripcion |
|---|---|
| Run Auto Match All Channels | Lanza el proceso (pide confirmacion). |
| Ver estado | Muestra el progreso actual o el resumen de la ultima ejecucion. |
| Detener | Para la ejecucion tras terminar el canal actual. |

## Progreso y resultado

Durante la ejecucion se envian avisos a la interfaz por WebSocket, por ejemplo:

```
Auto Match: 120/400 (30.0%) - con match: 85, sin match: 33, errores: 2
```

Al terminar se muestra y se registra un resumen:

```
Completado: 400/400 canales en 312s. Nuevos matches: 240, ya correctos: 20,
sin match: 135, errores: 5.
```

- **Nuevos matches**: canales a los que se ha asignado EPG.
- **Ya correctos**: canales cuya EPG ya era la que habria elegido el Auto Match.
- **Sin match**: no se ha encontrado una EPG adecuada.
- **Errores**: fallos al procesar el canal (consulta los logs).

La lista de canales de la interfaz se refresca automaticamente al finalizar.

---

## Estructura

```
auto_match_all_channels/
  plugin.json    Manifiesto: nombre, version, campos y acciones
  plugin.py      Clase Plugin con la logica
```

## Recomendaciones

- La primera vez, prueba con "Maximo de canales" a 5 o 10.
- Revisa que tienes fuentes EPG activas y descargadas antes de lanzarlo; el matching solo usa EPG de fuentes activas.
- Con miles de canales el modo Individual puede tardar bastante. Puedes seguir el avance con "Ver estado".
- Usa la simulacion (modo Masivo) para ver cuantos matches saldrian antes de guardar cambios.

## Compatibilidad

Escrito sobre el codigo de la rama `main` de Dispatcharr (`apps/channels/epg_matching.py`). El plugin depende de funciones internas, asi que una version futura de Dispatcharr que las renombre o cambie podria requerir actualizar el plugin.

## Documentacion util

- [Documentacion oficial de plugins](https://github.com/Dispatcharr/Dispatcharr/blob/main/Plugins.md)
- [Repositorio de Dispatcharr](https://github.com/Dispatcharr/Dispatcharr)
