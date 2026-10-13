"""
Identificación robusta de remitentes WhatsApp, tolerante a LIDs.

## Problema
El servicio Node.js (`whatsapp-web.js`) a veces expone un LID opaco (Linked ID
de multi-device / WhatsApp Business) en lugar del E.164 real en `msg.from`.
Ejemplo real observado en 2026-04-10:
  - Profesora: Amaia Olaizola P122
  - Teléfono real (en contactos_auto.csv): +34611048944
  - `from_number` en BD: 121006576427192  ← LID opaco

Cualquier consumidor que haga `WHERE from_number = '34611048944'` o
`mapa_telefonos[from_number]` **FALLA silenciosamente** en estos casos.
Incidentes reales causados por el bug:
  - Nudge de domingo: Amaia aparecía como "no respondió" aunque sí lo había hecho
  - Interceptor: Gemini veía "Amaia Olaizola" (pushname, sin P-code) en vez
    de "Amaia Olaizola P122" — perdía la referencia a la fila exacta

## Solución
`chat_name` es la clave estable: viene del nombre del contacto guardado en
la agenda del móvil de la academia, y Miguel los tiene anotados con el
P-code al final ("Amaia Olaizola P122"). Estos helpers centralizan la lógica
de resolución para que TODO consumidor use chat_name como fuente primaria y
from_number sólo como respaldo.

## Reglas de uso
- Para identificar "mensajes de un profe" → usa `profe_ha_escrito_desde()` o
  las funciones SQL helpers.
- Para mostrar un nombre humano al LLM → usa `resolver_nombre_remitente()`.
- Para extraer el P-code de cualquier string → usa `extraer_pcode_de_nombre()`.

## Si escribes código nuevo que consulte `messages`...
JAMÁS filtres por `from_number = <teléfono>` sin pasar antes por este módulo.
El día que un nuevo contacto acabe con LID, tu código se romperá en silencio.
"""

import re
from typing import Optional

# ── Regex del P-code ─────────────────────────────────────────────────────
# Match con word boundary para distinguir P12 de P122 cuando aparezca suelto.
_PCODE_RE = re.compile(r"\b(P\d+)\b", re.IGNORECASE)


# ============================================================================
# HELPERS INTERNOS
# ============================================================================


def _get_msg_field(mensaje, key):
    """Accede a un campo del mensaje (soporta dict y sqlite3.Row)."""
    try:
        return mensaje.get(key) if hasattr(mensaje, "get") else mensaje[key]
    except (KeyError, IndexError):
        return None


# ============================================================================
# EXTRACCIÓN / NORMALIZACIÓN
# ============================================================================


def extraer_pcode_de_nombre(nombre: Optional[str]) -> Optional[str]:
    """Extrae el primer P-code encontrado en un string (siempre en mayúsculas).

    >>> extraer_pcode_de_nombre("Amaia Olaizola P122")
    'P122'
    >>> extraer_pcode_de_nombre("p007 minúsculas")
    'P007'
    >>> extraer_pcode_de_nombre("Elena López P052")
    'P052'
    >>> extraer_pcode_de_nombre("Alumna María")
    None
    >>> extraer_pcode_de_nombre("")
    None
    """
    if not nombre:
        return None
    m = _PCODE_RE.search(nombre)
    return m.group(1).upper() if m else None


# ============================================================================
# RESOLUCIÓN DE NOMBRE HUMANO
# ============================================================================


def resolver_nombre_remitente(
    mensaje: dict,
    mapa_telefonos: Optional[dict] = None,
) -> str:
    """Devuelve el mejor nombre humano para el remitente de un mensaje.

    Orden de preferencia (el primero que exista):
      1. `from_number == 'ACADEMIA'` → `"ACADEMIA ÍTACA"` (saliente)
      2. **DMs (1-on-1)**: `chat_name` — viene de la agenda del móvil y es
         100% fiable (incluye el P-code anotado por Miguel). No depende de
         que el LID opaco se pueda resolver.
      3. **Grupos**: `mapa_telefonos[from_number]` si se pasa — funciona sólo
         si el from_number es un E.164 real presente en contactos_auto.csv.
      4. `from_name` (pushname del contacto en WhatsApp) — puede no tener
         P-code pero al menos identifica a la persona.
      5. `from_number` como último recurso (puede ser un LID opaco).

    Args:
        mensaje: dict con claves `from_number`, `chat_name`, `from_name`,
                 `is_group` (formato del servicio Node.js, JSON o sqlite.Row).
        mapa_telefonos: dict opcional `{telf_sin_plus: nombre_completo}` como
                        el que devuelve `shared.contacts.cargar_mapa_telefono_a_contacto()`.

    Returns:
        String con el mejor nombre posible, nunca vacío.
    """
    if not mensaje:
        return "?"

    from_number = (_get_msg_field(mensaje, "from_number") or "").strip()
    if from_number == "ACADEMIA":
        return "ACADEMIA ÍTACA"

    chat_name = (_get_msg_field(mensaje, "chat_name") or "").strip()
    from_name = (_get_msg_field(mensaje, "from_name") or "").strip()
    is_group = bool(_get_msg_field(mensaje, "is_group"))

    # DM 1-on-1: chat_name es el contacto directo → fiable incluso con LID
    if not is_group and chat_name:
        return chat_name

    # Grupo: intentar mapa por E.164 (si from_number es real, funciona)
    if mapa_telefonos and from_number:
        nombre = mapa_telefonos.get(from_number)
        if nombre:
            return nombre

    # Pushname de WhatsApp como penúltimo recurso
    if from_name:
        return from_name

    return from_number or "?"


# ============================================================================
# HELPERS SQL PARA CONSULTAR `messages` POR P-CODE
# ============================================================================


def pcode_sql_pattern(p_code: str) -> str:
    """Devuelve el patrón LIKE para matchear un P-code anclado por espacios.

    Se usa junto con `SQL_CHAT_NAME_MATCHES_PCODE`. Los espacios delimitadores
    evitan colisiones tipo P12 ↔ P122 sin recurrir a regex.

    >>> pcode_sql_pattern("P122")
    '% P122 %'
    """
    return f"% {p_code} %"


# Fragmento SQL reutilizable. Pádea chat_name con espacios para habilitar
# matching anclado por palabra vía LIKE.
SQL_CHAT_NAME_MATCHES_PCODE = "(' ' || COALESCE(chat_name, '') || ' ') LIKE ?"


def profe_ha_escrito_desde(cursor, p_code: str, desde_ts: int) -> bool:
    """¿El profe con este P-code ha escrito algún mensaje humano en su DM
    con la academia después de `desde_ts`?

    **Robusto a LIDs**: resuelve por `chat_name` en vez de `from_number`.

    Filtra:
      - `chat_name` contiene el P-code (anclado por espacios)
      - `is_group = 0` (solo DM 1-on-1)
      - `from_number != 'ACADEMIA'` (excluye mensajes salientes del bot/academia)
      - `is_system = 0` (excluye mensajes marcados como sistema)
      - `body != ''` (excluye eventos sin cuerpo — stickers, acks)
      - `timestamp > desde_ts`

    Args:
        cursor: cursor de sqlite3 (o similar DB-API 2.0) conectado a messages.db
        p_code: código del profesor (ej. "P122")
        desde_ts: timestamp Unix epoch UTC (segundos)

    Returns:
        True si existe al menos un mensaje que cumple, False si no.
    """
    cursor.execute(
        f"""
        SELECT 1 FROM messages
        WHERE {SQL_CHAT_NAME_MATCHES_PCODE}
          AND is_group = 0
          AND from_number != 'ACADEMIA'
          AND is_system = 0
          AND timestamp > ?
          AND body != ''
        LIMIT 1
        """,
        (pcode_sql_pattern(p_code), desde_ts),
    )
    return cursor.fetchone() is not None
