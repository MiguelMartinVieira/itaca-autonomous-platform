"""
Workflow: INTERCEPTOR DE CANCELACIONES

Propósito:
    Analizar mensajes de WhatsApp recibidos por la academia para detectar
    cancelaciones o cambios de clase, y actualizar "Clases Esperadas"
    automáticamente antes de que se lancen los recordatorios del viernes.

Flujo:
    1. Pide mensajes no procesados al servicio WhatsApp (Node.js)
    2. Descarga Clases Esperadas actual (Google Sheets)
    3. Envía a Gemini Flash (gratis) con contexto para clasificar
    4. confianza >= 0.50 → elimina de Clases Esperadas
    5. confianza < 0.50 → manda resumen al admin por WhatsApp
    6. Marca mensajes como procesados

Ejecución:
    python launcher.py interceptor --auto
    python launcher.py interceptor --auto --skip-update
"""

import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime

import pandas as pd
import pytz
import requests

from config import settings
from config.settings import (
    FILE_CLASES_ESPERADAS,
    GEMINI_API_CIRCUIT_BREAKER_FAILURES,
    GEMINI_API_KEY,
    GEMINI_API_KEY_PAID,
    GEMINI_API_TIMEOUT,
    GEMINI_CIRCUIT_BREAKER_BACKOFF,
    GEMINI_CIRCUIT_BREAKER_FAILURES,
    GEMINI_CIRCUIT_BREAKER_TIMEOUT,
    GEMINI_MODEL_CASCADE,
    GRUPO_ACADEMIA_ID,
    INTERCEPTOR_ADMIN_PHONE,
    INTERCEPTOR_CONFIANZA_UMBRAL,
    LOG_ONLY_MODE,
    SHEET_GIDS,
    SHEET_KEY_BBDD,
    TEST_MODE,
    WHATSAPP_SERVICE_URL,
)
from infrastructure.sheets_client import get_client
from infrastructure.whatsapp_sender import enviar_mensaje_rapido
from shared.circuit_breaker import CircuitBreakerOpenException, circuit_breaker
from shared.contacts import cargar_mapa_telefono_a_contacto
from shared.persistent_state import PersistentState
from shared.retry_utils import retry_on_exception
from shared.text_utils import normalizar_identidad
from shared.wa_identity import resolver_nombre_remitente

logger = logging.getLogger(__name__)

CSVS_NEEDED = ["Clases Esperadas"]

# ── Estado persistente entre ejecuciones del cron ──────────────────────


def _get_gemini_state():
    """Obtiene la instancia de estado Gemini (crea una nueva si es necesario)."""
    return PersistentState(settings.GEMINI_STATE_FILE)


def _leer_fallos_gemini():
    """Devuelve el número de fallos consecutivos de Gemini registrados."""
    return _get_gemini_state().get("consecutive_failures", 0)


def _guardar_fallos_gemini(n):
    """Persiste el contador de fallos consecutivos de Gemini."""
    state = _get_gemini_state()
    state.set("consecutive_failures", n)
    state.save()


def _obtener_primer_fallo_free_gemini():
    """Devuelve el timestamp Unix (float) del primer fallo consecutivo de modelos gratuitos, o None."""
    return _get_gemini_state().get("primer_fallo_free_ts")


def _registrar_fallo_free_gemini(ts=None):
    """Registra la marca de tiempo del primer fallo gratuito si no existe.

    Devuelve el timestamp del primer fallo.
    """
    state = _get_gemini_state()
    primer_fallo = state.get("primer_fallo_free_ts")
    if primer_fallo is None:
        primer_fallo = ts if ts is not None else time.time()
        state.set("primer_fallo_free_ts", primer_fallo)
        state.save()
    return primer_fallo


def _limpiar_fallo_free_gemini():
    """Limpia la marca del primer fallo de modelos gratuitos tras procesar con éxito."""
    state = _get_gemini_state()
    if state.get("primer_fallo_free_ts") is not None:
        state.set("primer_fallo_free_ts", None)
        state.save()


def es_ventana_pre_viernes(dt=None) -> bool:
    """Determina si estamos en la ventana crítica previa a los formularios del viernes.

    Comprende:
      - Jueves a partir de las 20:00 (incluye la ejecución del cron de las 23:00).
      - Viernes antes de las 09:00 (antes del envío de formularios a profesores).
    """
    if dt is None:
        tz_madrid = pytz.timezone("Europe/Madrid")
        dt = datetime.now(tz_madrid)

    dia_semana = dt.weekday()  # Lunes=0, Jueves=3, Viernes=4
    hora = dt.hour

    if dia_semana == 3 and hora >= 20:
        return True
    if dia_semana == 4 and hora < 9:
        return True
    return False


def puede_usar_modelo_pago(primer_fallo_ts=None, dt=None) -> tuple[bool, str]:
    """Determina si está autorizado el fallback a modelos de pago.

    Condiciones:
      1. Ventana crítica pre-formularios del viernes (jueves noche o viernes temprano).
      2. Han transcurrido >= INTERCEPTOR_PAID_FALLBACK_HOURS (48h) continuas desde el primer fallo gratuito.

    Devuelve (autorizado: bool, motivo: str).
    """
    if es_ventana_pre_viernes(dt):
        return True, "ventana crítica pre-formularios del viernes (jueves noche / viernes < 09:00)"

    if primer_fallo_ts is not None:
        ahora = time.time()
        horas_fallando = (ahora - primer_fallo_ts) / 3600.0
        umbral = getattr(settings, "INTERCEPTOR_PAID_FALLBACK_HOURS", 48.0)
        if horas_fallando >= umbral:
            return (
                True,
                f"{horas_fallando:.1f}h continuas de fallos en modelos gratuitos (umbral: {umbral:.0f}h)",
            )

    return False, "acumulando mensajes (menos de 48h de fallos y fuera de ventana pre-viernes)"


# ── Gemini API ─────────────────────────────────────────────────────────

GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
)


@circuit_breaker(
    "gemini_api",
    max_failures=GEMINI_API_CIRCUIT_BREAKER_FAILURES,
    reset_timeout=GEMINI_CIRCUIT_BREAKER_TIMEOUT,
    backoff_multiplier=GEMINI_CIRCUIT_BREAKER_BACKOFF,
)
def _llamar_gemini_protegido(modelo, api_key, prompt, timeout=None):
    """Llamada a Gemini protegida con circuit breaker."""
    if timeout is None:
        timeout = GEMINI_API_TIMEOUT
    url = GEMINI_API_URL.format(model=modelo, key=api_key)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    resp = requests.post(url, json=payload, timeout=timeout)

    if resp.status_code != 200:
        raise RuntimeError(f"API error {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError("API devolvió respuesta vacía")

    text = candidates[0]["content"]["parts"][0]["text"]
    return text


def _llamar_gemini_modelo(modelo, api_key, prompt, timeout=None):
    """Una sola llamada a un modelo concreto. Devuelve (status_code, texto_o_error).

    Si `timeout` es None, usa `GEMINI_API_TIMEOUT` (definido en settings.py).
    """
    try:
        texto = _llamar_gemini_protegido(modelo, api_key, prompt, timeout)
        return 200, texto
    except CircuitBreakerOpenException as e:
        logger.warning(f"Circuit breaker abierto: {e}")
        return None, str(e)
    except requests.exceptions.RequestException as e:
        return None, f"network: {e}"
    except RuntimeError as e:
        return 500, str(e)
    except Exception as e:
        return 500, f"parse: {e}"


def llamar_gemini(prompt):
    """Envía prompt a Gemini priorizando modelos gratuitos y acumulando mensajes hasta 48h.

    Flujo:
      1. Itera todos los modelos "free" en GEMINI_MODEL_CASCADE.
      2. Si alguno responde 200 OK:
         - Limpia el timestamp de fallo gratuito (_limpiar_fallo_free_gemini).
         - Devuelve el texto generado.
      3. Si todos los gratuitos fallan:
         - Registra el timestamp del primer fallo en gemini_state.json si no existía.
         - Evalúa si se autoriza el tier de pago:
           * Sí, si han pasado >= 48h continuas de fallos gratuitos.
           * Sí, si estamos en la ventana pre-formularios del viernes (jueves noche).
           * En caso contrario: lanza RuntimeError. Los mensajes quedan acumulados
             en la base de datos WhatsApp para reintentarse en el siguiente cron.
      4. Si el pago está autorizado:
         - Itera los modelos "paid" en GEMINI_MODEL_CASCADE con GEMINI_API_KEY_PAID.
         - Si responde 200 OK: limpia el fallo gratuito y devuelve el texto.
         - Si todos fallan: lanza RuntimeError.
    """
    errores = []
    gemini_key = getattr(settings, "GEMINI_API_KEY", GEMINI_API_KEY)
    gemini_key_paid = getattr(settings, "GEMINI_API_KEY_PAID", GEMINI_API_KEY_PAID)
    cascade = getattr(settings, "GEMINI_MODEL_CASCADE", GEMINI_MODEL_CASCADE)

    # 1. Probar modelos gratuitos
    modelos_free = [(m, t) for m, t in cascade if t == "free"]
    for modelo, tier in modelos_free:
        if not gemini_key:
            continue

        logger.info(f"  Probando {modelo} ({tier})...")
        status, texto = _llamar_gemini_modelo(modelo, gemini_key, prompt)

        if status == 200 and texto:
            logger.info(f"  ✅ {modelo} ({tier}) OK")
            _limpiar_fallo_free_gemini()
            return texto

        # Fallo: registrar y pasar al siguiente gratuito
        err = f"{modelo}/{tier}: {status} {str(texto)[:100] if texto else ''}"
        errores.append(err)
        logger.warning(f"  ❌ {err}")
        time.sleep(2)

    # Todos los modelos gratuitos han fallado
    ts_primer_fallo = _registrar_fallo_free_gemini()
    autorizado_pago, motivo = puede_usar_modelo_pago(ts_primer_fallo)

    if not autorizado_pago:
        horas_acumuladas = (time.time() - ts_primer_fallo) / 3600.0
        umbral = getattr(settings, "INTERCEPTOR_PAID_FALLBACK_HOURS", 48.0)
        msg_espera = (
            f"Modelos gratuitos no disponibles ({horas_acumuladas:.1f}h / {umbral:.0f}h acumuladas). "
            f"Fallback de pago bloqueado ({motivo}). Mensajes retenidos para la próxima ejecución."
        )
        logger.warning(f"  ⏳ {msg_espera}")
        raise RuntimeError(f"{msg_espera}\n  " + "\n  ".join(errores))

    # 2. Si el pago está autorizado (han pasado 48h o es jueves noche):
    logger.warning(f"  ⚠️ Autorizado fallback a modelos de pago: {motivo}")
    modelos_paid = [(m, t) for m, t in cascade if t == "paid"]
    for modelo, tier in modelos_paid:
        if not gemini_key_paid:
            continue

        logger.info(f"  Probando {modelo} ({tier})...")
        status, texto = _llamar_gemini_modelo(modelo, gemini_key_paid, prompt)

        if status == 200 and texto:
            logger.info(f"  ✅ {modelo} ({tier}) OK")
            _limpiar_fallo_free_gemini()
            return texto

        err = f"{modelo}/{tier}: {status} {str(texto)[:100] if texto else ''}"
        errores.append(err)
        logger.warning(f"  ❌ {err}")
        time.sleep(2)

    # Toda la cascada autorizada falló
    raise RuntimeError(
        "Todos los modelos Gemini (gratuitos y de pago autorizados) fallaron:\n  "
        + "\n  ".join(errores)
    )


# ── Cargar datos ───────────────────────────────────────────────────────


def cargar_clases_esperadas_csv():
    """Carga Clases Esperadas desde CSV local.

    Incluye todas las filas EXCEPTO las que ya están OK (registradas).
    Esto permite detectar cancelaciones de clases futuras, pendientes
    de hoy, o pasadas sin registrar.
    """
    try:
        df = pd.read_csv(FILE_CLASES_ESPERADAS, encoding="utf-8")
        # Excluir solo OK (ya registradas) — mantener SIN REGISTRO, HOY – pendiente, FUTURA, etc.
        if "Estado" in df.columns:
            df = df[df["Estado"].str.strip().str.upper() != "OK"]
        return df
    except Exception as e:
        logger.error(f"Error cargando Clases Esperadas CSV: {e}")
        return pd.DataFrame()


# ── Comunicación con servicio WhatsApp ─────────────────────────────────

PATRONES_GRUPOS_COMUNIDAD = [
    r"academia\s+[ií]taca\s*\(grupo\)",
    r"[ií]taca\s+profesores",
    r"ofertas\s+de\s+trabajo",
    r"profes\s+y\s+\+\s+hoy\s+x\s+ti",
]


def es_chat_valido_interceptor(chat_id: str, chat_name: str) -> bool:
    """Determina si un chat debe ser analizado por el interceptor.

    Se analizan:
      - Mensajes directos (DMs, no terminan en @g.us).
      - Grupos de clases individuales (tienen código de alumno A### o profesor P###,
        o prefijos habituales de clases: "Cl.", "Clases", "C. Part.", etc.).

    Se descartan:
      - Grupos generales o comunitarios (ej. Academia Ítaca (Grupo), Ítaca Profesores),
        que tienen mucho tráfico y no corresponden a clases particulares individuales.
    """
    if not chat_id:
        return False

    # Los DMs siempre se analizan
    if not chat_id.endswith("@g.us"):
        return True

    # Descartar ID del grupo general de la academia
    if GRUPO_ACADEMIA_ID and chat_id == GRUPO_ACADEMIA_ID:
        return False

    name_clean = (chat_name or "").strip().lower()

    # Descartar grupos comunitarios conocidos por nombre
    for patron in PATRONES_GRUPOS_COMUNIDAD:
        if re.search(patron, name_clean):
            return False

    # Incluir si contiene un código de alumno (ej: A021, A100) o de profesor (ej: P046)
    if re.search(r"\b[AP]\d+\b", chat_name or "", re.IGNORECASE):
        return True

    # Incluir si empieza por prefijos habituales de clases particulares/apoyo
    if re.match(
        r"^(cl\.|clases|c\.\s*part|departamental|intensivo|preparaci[oó]n|matem[aá]ticas)",
        name_clean,
    ):
        return True

    # Grupos sin relación con clases individuales se descartan
    return False


# Cooldown: segundos sin actividad para considerar una conversación "fría"
COOLDOWN_SECONDS = 7 * 3600  # 7 horas (> intervalo entre ciclos de 6h, evita solapamiento)


def _api_get(path, params=None):
    """GET al servicio Node.js con manejo de errores."""
    try:
        resp = requests.get(
            f"{WHATSAPP_SERVICE_URL}{path}",
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.ConnectionError:
        logger.error(
            f"No se pudo conectar al servicio WhatsApp en {WHATSAPP_SERVICE_URL}. "
            "¿Está corriendo el servicio Node.js?"
        )
        return None
    except Exception as e:
        logger.error(f"Error en GET {path}: {e}")
        return None


def obtener_chats_frios():
    """Devuelve chats con mensajes nuevos cuya conversación lleva >= 3h fría."""
    data = _api_get("/api/chats/cooled", {"cooldown": COOLDOWN_SECONDS})
    if data is None:
        return []
    return data.get("chats", [])


def obtener_hilo_chat(chat_id):
    """Devuelve el hilo completo de un chat (última semana) + IDs sin procesar."""
    encoded = requests.utils.quote(chat_id, safe="")
    data = _api_get(f"/api/chats/{encoded}/thread", {"days": 7})
    if data is None:
        return [], []
    return data.get("messages", []), data.get("unprocessed_ids", [])


def marcar_procesados(ids, category="PROCESADO", llm_response=None):
    """Marca mensajes como procesados en el servicio Node.js."""
    if not ids:
        return
    try:
        requests.post(
            f"{WHATSAPP_SERVICE_URL}/api/messages/processed",
            json={"ids": ids, "category": category, "llm_response": llm_response},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"Error marcando procesados: {e}")


# ── Construir prompt para Gemini ───────────────────────────────────────


def _formatear_hilo(hilo_mensajes, mapa_telefonos=None):
    """Formatea un hilo de mensajes resolviendo nombres de contacto.

    Usa `resolver_nombre_remitente` de `shared.wa_identity` para ser robusto
    frente a LIDs opacos (ver docstring de ese módulo). Esto garantiza que:
      - En DMs 1-a-1, el nombre viene de `chat_name` (que incluye el P-code
        anotado en la agenda del móvil) aunque `from_number` sea un LID.
      - El LLM ve "Amaia Olaizola P122" en vez de "Amaia Olaizola" (pushname),
        preservando la capacidad de mapear la conversación a una fila concreta.
    """
    texto = ""
    for m in hilo_mensajes:
        ts = datetime.fromtimestamp(m["timestamp"]).strftime("%d/%m %H:%M")
        nombre = resolver_nombre_remitente(m, mapa_telefonos)
        texto += f"  [{ts}] {nombre}: {m['body']}\n"
    return texto


def construir_prompt_batch(chats_con_hilos, clases_esperadas_df, mapa_telefonos=None):
    """Construye UN prompt con TODAS las conversaciones para una sola llamada a Gemini.

    Cada clase esperada se numera con un ID. Gemini debe devolver el ID
    de la fila a actuar — NUNCA placeholders ni texto inventado.
    """
    tz_madrid = pytz.timezone("Europe/Madrid")
    hoy = datetime.now(tz_madrid)
    hoy_str = hoy.strftime("%A %d de %B de %Y")  # ej: "viernes 09 de mayo de 2026"
    hoy_iso = hoy.strftime("%Y-%m-%d")

    # Formatear clases esperadas con ID numérico (1-indexed para legibilidad)
    if not clases_esperadas_df.empty:
        col_fecha_idx = (
            clases_esperadas_df.columns.get_loc("Fecha")
            if "Fecha" in clases_esperadas_df.columns
            else None
        )
        col_prof_idx = (
            clases_esperadas_df.columns.get_loc("Profesor")
            if "Profesor" in clases_esperadas_df.columns
            else None
        )
        col_alumno_idx = (
            clases_esperadas_df.columns.get_loc("Alumno")
            if "Alumno" in clases_esperadas_df.columns
            else None
        )
        lineas_clases = []
        for i, row in enumerate(clases_esperadas_df.itertuples(index=False), start=1):
            fecha = str(row[col_fecha_idx]).strip() if col_fecha_idx is not None else "?"
            prof = str(row[col_prof_idx]).strip() if col_prof_idx is not None else "?"
            alumno = str(row[col_alumno_idx]).strip() if col_alumno_idx is not None else "?"
            lineas_clases.append(f"  [{i}] {fecha} | {prof} | {alumno}")
        clases_texto = "\n".join(lineas_clases)
    else:
        clases_texto = "  (No hay clases esperadas cargadas)"

    # Formatear todas las conversaciones
    conversaciones_texto = ""
    for chat_name, hilo in chats_con_hilos:
        conversaciones_texto += f'\n### CHAT: "{chat_name}"\n'
        conversaciones_texto += _formatear_hilo(hilo, mapa_telefonos)

    prompt = f"""Eres un asistente de Academia Ítaca, una academia de clases particulares.

📅 **HOY es {hoy_str} ({hoy_iso}).** Usa esta fecha como referencia para interpretar expresiones como "el próximo lunes", "mañana", "este jueves", etc. Las fechas que devuelvas en `fecha_afectada` y `fecha_nueva` DEBEN estar en formato YYYY-MM-DD del año correcto.

Tu tarea es analizar VARIAS conversaciones de WhatsApp y determinar si alguna contiene CANCELACIONES o CAMBIOS DE DÍA de clases.

## CLASES ESPERADAS ESTA SEMANA (cada una con su ID entre corchetes):
{clases_texto}

## CONVERSACIONES DE WHATSAPP:
{conversaciones_texto}

## INSTRUCCIONES CRÍTICAS:

1. **Lee CADA conversación ENTERA antes de decidir.** Lo que importa es la CONCLUSIÓN FINAL:
   - Si alguien dice "cancelo" pero luego dice "al final sí puedo" → NO es cancelación
   - Si dice "no puedo el martes" y luego "lo pasamos al jueves" → es CAMBIO_DIA
   - Si después alguien revierte la decisión, NO la incluyas

2. **Tipos de detección (definición ESTRICTA):**
   - **CANCELACION**: una clase ya PLANIFICADA (que aparece literalmente en la lista de arriba) NO se va a dar y NO se recupera.
   - **CAMBIO_DIA**: una clase ya PLANIFICADA (que aparece literalmente en la lista de arriba) se MUEVE a OTRO día distinto.
   - **IMPORTANTE**: si SOLO cambia la hora pero el día es el mismo → NO lo incluyas (Clases Esperadas no rastrea horas).

3. **🚫 REGLA DE ORO — NO ALUCINES.** Si la conversación NO menciona explícitamente una cancelación o cambio de día de una clase que aparece en la lista de CLASES ESPERADAS, devuelve `[]`. Es INFINITAMENTE PEOR inventar una detección que perder una real.

4. **Casos que NO son detección (ejemplos reales — IGNÓRALOS y devuelve `[]`):**
   - 🚫 "A las 18:45 me viene mejor" + "Perfecto" → cambio de HORA, el DÍA sigue igual.
   - 🚫 "Qué te parece dar clase el sábado?" + "Perfecto" sin mencionar que venga de otro día → es una clase NUEVA acordada, NO un CAMBIO_DIA.
   - 🚫 "Nos vemos el lunes" / "Buen finde" → simple despedida, no implica cambio desde otro día.
   - 🚫 "Te comento cómo fue la clase..." / "Qué tal le fue" → feedback post-clase, la clase ya pasó.
   - 🚫 "Bizum clase hecho" / "Muchísimas gracias" → confirmación de pago tras una clase ya dada.
   - 🚫 "Lo hablamos el viernes" / "Ya te digo mañana" → una fecha mencionada de pasada NO significa que haya cambio hacia/desde esa fecha.
   - 🚫 Conversación que solo menciona nombres de días de la semana sin verbos como "cancelo", "muevo", "paso", "cambio", "recupero": NO es detección.

5. **Identifica la fila exacta de CLASES ESPERADAS afectada:**
   - Cruza el contenido del chat con la lista de CLASES ESPERADAS de arriba.
   - Devuelve el ID numérico [N] de la fila que se cancela o cambia.
   - **REQUISITO DURO**: `fecha_afectada` DEBE aparecer LITERALMENTE como Fecha de alguna fila de la lista de CLASES ESPERADAS. Si no aparece, NO hay detección válida → pon `clase_id: null` Y `confianza <= 0.5`.
   - Si hay ambigüedad (podría ser varias filas), pon `clase_id: null` Y baja la confianza.
   - **PROHIBIDO**: NUNCA inventes códigos como "P###" o "A###". Si no sabes el dato real, usa `null`. Cualquier texto con `###` es un BUG y se rechazará automáticamente.

6. **Confianza (calibrada estrictamente):**
   - 0.95-1.0: cambio/cancelación EXPLÍCITO en la conversación + clase_id localizado
   - 0.85-0.94: detección explícita pero algún detalle secundario dudoso
   - 0.50-0.84: duda real (ambigüedad, revertido, mención de pasada)
   - **< 0.50 OBLIGATORIO si `clase_id` es null, o si `fecha_afectada` no aparece en la lista de arriba.**

## FORMATO DE RESPUESTA (JSON array):

Devuelve un JSON array. Si NINGUNA conversación tiene cancelación ni cambio de día, devuelve [].

[
  {{
    "chat": "Nombre EXACTO del chat tal como aparece arriba",
    "tipo": "CANCELACION",
    "clase_id": 12,
    "fecha_afectada": "2026-04-09",
    "fecha_nueva": null,
    "confianza": 0.95,
    "razon": "El profesor confirma que el alumno está enfermo y no habrá clase el jueves"
  }},
  {{
    "chat": "Otro chat",
    "tipo": "CAMBIO_DIA",
    "clase_id": 7,
    "fecha_afectada": "2026-04-10",
    "fecha_nueva": "2026-04-12",
    "confianza": 0.92,
    "razon": "La clase del viernes se pasa al sábado a las 11:00"
  }}
]

Campos OBLIGATORIOS (todos):
- chat: nombre EXACTO del chat tal como aparece arriba (para enrutado interno)
- tipo: "CANCELACION" o "CAMBIO_DIA" (literal)
- clase_id: ID numérico de la fila en CLASES ESPERADAS (un entero), o `null` si no la identificas
- fecha_afectada: fecha de la clase original en formato YYYY-MM-DD
- fecha_nueva: solo para CAMBIO_DIA, la nueva fecha YYYY-MM-DD; null para CANCELACION
- confianza: número decimal entre 0.0 y 1.0
- razon: explicación breve (1 frase) basada en la conclusión del hilo

PROHIBICIONES ESTRICTAS:
- NO uses placeholders como "P###", "A###", "Nombre P###". Usa null si no sabes.
- NO inventes nombres ni códigos.
- NO incluyas detecciones donde fecha_afectada == fecha_nueva (eso es solo cambio de hora).

RESPONDE SOLO CON EL JSON, nada más."""

    return prompt


# ── Eliminar de Clases Esperadas ───────────────────────────────────────


def _ejecutar_con_reintento(func, max_intentos=3, delay_inicial=1.0):
    """Reintenta una función con backoff exponencial.

    Útil para mitigar caídas temporales o límites de cuota de Google Sheets (429/503).
    """
    return retry_on_exception(func, max_attempts=max_intentos, delay=delay_inicial, backoff=2.0)


def actualizar_clases_esperadas(acciones, clases_esperadas_df):
    """Aplica acciones sobre la hoja Clases Esperadas en Google Sheets.

    Cada acción es una detección con tipo CANCELACION o CAMBIO_DIA y un
    `clase_id` (1-indexed) que apunta a una fila del DataFrame
    `clases_esperadas_df` (el mismo que se incluyó en el prompt).

    - CANCELACION → borra la fila
    - CAMBIO_DIA  → actualiza la columna Fecha al valor `fecha_nueva`

    Devuelve dict con contadores y la lista de acciones que NO se pudieron aplicar:
        {"eliminadas": n, "modificadas": n, "no_aplicadas": [det1, det2, ...]}
    """
    resultado = {"eliminadas": 0, "modificadas": 0, "no_aplicadas": []}
    if not acciones:
        return resultado

    try:
        sheets = get_client()
        ws = sheets.get_worksheet_by_gid(SHEET_KEY_BBDD, SHEET_GIDS["Clases Esperadas"])
        all_data = _ejecutar_con_reintento(lambda: ws.get_all_values())

        if len(all_data) <= 1:
            logger.warning("Clases Esperadas está vacía")
            return resultado

        headers = all_data[0]
        idx_fecha = headers.index("Fecha") if "Fecha" in headers else 0
        idx_prof = headers.index("Profesor") if "Profesor" in headers else 1
        idx_alumno = headers.index("Alumno") if "Alumno" in headers else 2
        idx_estado = headers.index("Estado") if "Estado" in headers else -1
        # Columnas en formato A1 para updates
        col_fecha_letra = chr(ord("A") + idx_fecha)
        col_estado_letra = chr(ord("A") + idx_estado) if idx_estado >= 0 else None

        # Resolver cada acción a una fila concreta del worksheet
        # Estructura: {row_idx_sheet: ("CANCELACION", None) | ("CAMBIO_DIA", "2026-04-12")}
        plan = {}
        for det in acciones:
            clase_id = det.get("clase_id")
            tipo = det.get("tipo")

            if not isinstance(clase_id, int) or clase_id <= 0:
                logger.warning(
                    f"  Acción sin clase_id válido (id={clase_id}): {det.get('razon', '')}"
                )
                resultado["no_aplicadas"].append(det)
                continue

            # clase_id es 1-indexed sobre el DataFrame que se pasó al prompt
            df_idx = clase_id - 1
            if df_idx >= len(clases_esperadas_df):
                logger.warning(
                    f"  clase_id {clase_id} fuera de rango (df tiene {len(clases_esperadas_df)} filas)"
                )
                resultado["no_aplicadas"].append(det)
                continue

            row_df = clases_esperadas_df.iloc[df_idx]
            fecha_df = str(row_df.get("Fecha", "")).strip()
            prof_df = normalizar_identidad(str(row_df.get("Profesor", "")))
            alumno_df = normalizar_identidad(str(row_df.get("Alumno", "")))

            # Buscar la fila correspondiente en la worksheet completa
            row_idx_sheet = None
            for i, row in enumerate(all_data[1:], start=2):  # Sheets 1-indexed, headers en fila 1
                fecha_row = row[idx_fecha].strip() if idx_fecha < len(row) else ""
                prof_row = normalizar_identidad(row[idx_prof] if idx_prof < len(row) else "")
                alumno_row = normalizar_identidad(row[idx_alumno] if idx_alumno < len(row) else "")
                if fecha_row == fecha_df and prof_row == prof_df and alumno_row == alumno_df:
                    row_idx_sheet = i
                    break

            if row_idx_sheet is None:
                logger.warning(
                    f"  No se localizó en sheet la fila del clase_id {clase_id}: {fecha_df} | {prof_df} | {alumno_df}"
                )
                resultado["no_aplicadas"].append(det)
                continue

            if tipo == "CANCELACION":
                plan[row_idx_sheet] = ("CANCELACION", None)
            elif tipo == "CAMBIO_DIA":
                fecha_nueva = det.get("fecha_nueva")
                if not fecha_nueva:
                    logger.warning(f"  CAMBIO_DIA sin fecha_nueva en clase_id {clase_id}, ignorado")
                    resultado["no_aplicadas"].append(det)
                    continue
                plan[row_idx_sheet] = ("CAMBIO_DIA", fecha_nueva)
            else:
                logger.warning(f"  Tipo desconocido: {tipo}")
                resultado["no_aplicadas"].append(det)
                continue

        if not plan:
            return resultado

        if LOG_ONLY_MODE or TEST_MODE:
            for row_idx, (tipo, fecha_nueva) in plan.items():
                logger.info(
                    f"  [SIMULADO] {tipo} fila {row_idx}"
                    + (f" → {fecha_nueva}" if fecha_nueva else "")
                )
            return resultado

        # 1) Aplicar primero los CAMBIO_DIA (updates) en lote vía batch_update
        tz_madrid = pytz.timezone("Europe/Madrid")
        hoy_iso = datetime.now(tz_madrid).strftime("%Y-%m-%d")

        updates = []
        filas_modificadas = []
        for row_idx, (tipo, fecha_nueva) in plan.items():
            if tipo == "CAMBIO_DIA":
                cell = f"{col_fecha_letra}{row_idx}"
                updates.append((cell, fecha_nueva))
                filas_modificadas.append((row_idx, fecha_nueva))

                # Si la nueva fecha es futura respecto a hoy y Estado existe, actualizar a 'FUTURA'
                if col_estado_letra and fecha_nueva > hoy_iso:
                    fila_datos = all_data[row_idx - 1] if row_idx - 1 < len(all_data) else []
                    estado_actual = (
                        fila_datos[idx_estado].strip() if idx_estado < len(fila_datos) else ""
                    )
                    if estado_actual.upper() in (
                        "SIN REGISTRO",
                        "HOY – PENDIENTE",
                        "HOY - PENDIENTE",
                        "",
                    ):
                        cell_estado = f"{col_estado_letra}{row_idx}"
                        updates.append((cell_estado, "FUTURA"))
                        logger.info(
                            f"  📌  Preparado cambio Estado fila {row_idx} ({estado_actual} → FUTURA)"
                        )

        if updates:
            _ejecutar_con_reintento(lambda: sheets.update_cells(ws, updates))
            for row_idx, fecha_nueva in filas_modificadas:
                logger.info(f"  ✏️  Actualizada fila {row_idx}: Fecha → {fecha_nueva}")
                resultado["modificadas"] += 1

        # 2) Aplicar luego las CANCELACION (deletes) de abajo hacia arriba
        cancelaciones_idx = sorted(
            [r for r, (t, _) in plan.items() if t == "CANCELACION"],
            reverse=True,
        )
        for row_idx in cancelaciones_idx:
            _ejecutar_con_reintento(lambda r=row_idx: ws.delete_rows(r))
            logger.info(f"  🗑️  Eliminada fila {row_idx} de Clases Esperadas")
            resultado["eliminadas"] += 1
            time.sleep(0.5)

        return resultado

    except Exception as e:
        logger.error(f"Error actualizando Clases Esperadas: {e}")
        return resultado


def _existe_respaldo_csv(det, clases_esperadas_df, chat_name):
    """¿La detección de Gemini está respaldada por una fila real del CSV?

    Una detección tiene "respaldo" si existe una fila pendiente cuyas
    coordenadas (fecha + profesor/alumno) encajan con lo que Gemini dice.
    Devuelve False en caso contrario → la detección es probable alucinación.

    Tres niveles de verificación:
      1. Si Gemini devolvió `clase_id` válido, verificamos que la Fecha de
         esa fila coincida con `fecha_afectada` (consistencia interna).
      2. Si no hay clase_id, buscamos en el CSV filas con esa fecha Y con
         un P-code / A-code compatible con el chat_name.
      3. Si el chat_name no tiene códigos extraíbles (DM pelado), bastaría
         con que haya alguna fila en esa fecha.

    Args:
        det: dict con al menos `fecha_afectada`, opcional `clase_id`.
        clases_esperadas_df: DataFrame filtrado (excluye OK).
        chat_name: nombre del chat (para extraer P-codes y A-codes).

    Returns:
        bool — True si hay respaldo, False si es huérfana/alucinación.
    """
    fecha = (det.get("fecha_afectada") or "").strip()
    if not fecha or clases_esperadas_df is None or clases_esperadas_df.empty:
        return False

    # Resolver nombres de columnas de forma tolerante a mayúsculas
    col_fecha = next((c for c in clases_esperadas_df.columns if "fecha" in c.lower()), None)
    col_prof = next((c for c in clases_esperadas_df.columns if "profesor" in c.lower()), None)
    col_alumno = next((c for c in clases_esperadas_df.columns if "alumno" in c.lower()), None)

    if not col_fecha:
        return False

    # (1) Si hay clase_id, verificar consistencia interna: la fecha que dice
    #     Gemini debe coincidir con la Fecha de la fila referenciada.
    #     Si clase_id está fuera de rango o discrepa en fecha → no tiene respaldo → False.
    clase_id = det.get("clase_id")
    if isinstance(clase_id, int):
        if not (1 <= clase_id <= len(clases_esperadas_df)):
            # clase_id inválido (Gemini inventó un ID que no existe) → no tiene respaldo
            return False
        row = clases_esperadas_df.iloc[clase_id - 1]
        row_fecha = str(row.get(col_fecha, "")).strip()
        return row_fecha == fecha

    # (2) / (3) Sin clase_id: buscar filas compatibles
    mask_fecha = clases_esperadas_df[col_fecha].astype(str).str.strip() == fecha
    matching = clases_esperadas_df[mask_fecha]
    if matching.empty:
        return False

    pcodes = re.findall(r"\bP\d+\b", chat_name or "")
    acodes = re.findall(r"\bA\d+\b", chat_name or "")

    def _algun_match(codigos, columna):
        if not codigos or not columna:
            return False
        concat = " | ".join(matching[columna].astype(str).tolist())
        return any(c in concat for c in codigos)

    if pcodes or acodes:
        # El chat tiene identificador → exigimos que coincida prof o alumno
        return _algun_match(pcodes, col_prof) or _algun_match(acodes, col_alumno)

    # Chat sin P/A extraíbles (raro): conformarnos con el match por fecha
    return True


# ── Notificar al admin ─────────────────────────────────────────────────


def _resolver_fila_para_mensaje(det, clases_esperadas_df):
    """Devuelve un texto humanizado de la fila afectada (fecha | profesor | alumno) o '?'."""
    clase_id = det.get("clase_id")
    if not isinstance(clase_id, int) or clase_id <= 0:
        return None
    df_idx = clase_id - 1
    if df_idx >= len(clases_esperadas_df):
        return None
    row = clases_esperadas_df.iloc[df_idx]
    fecha = str(row.get("Fecha", "?")).strip()
    prof = str(row.get("Profesor", "?")).strip()
    alumno = str(row.get("Alumno", "?")).strip()
    return f"{fecha} | {prof} | {alumno}"


def notificar_admin_dudosas(dudosas, clases_esperadas_df):
    """Envía UN solo mensaje agrupado al admin con todas las detecciones dudosas.

    Cada bloque incluye nombre del chat, fila afectada (resuelta por clase_id),
    confianza, y la razón del LLM. Todo en un único mensaje.
    """
    if not dudosas:
        return

    lineas = [f"⚠️ *Interceptor — {len(dudosas)} detección(es) por revisar*", ""]

    for i, d in enumerate(dudosas, start=1):
        chat = d.get("chat", "?")
        tipo = d.get("tipo", "?")
        confianza_pct = int(round(d.get("confianza", 0) * 100))
        razon = d.get("razon", "").strip() or "(sin razón)"
        fila = _resolver_fila_para_mensaje(d, clases_esperadas_df)

        etiqueta_tipo = "❌ CANCELACIÓN" if tipo == "CANCELACION" else "🔄 CAMBIO DE DÍA"

        lineas.append(f"*{i}. {etiqueta_tipo}* ({confianza_pct}%) — {chat}")
        if fila:
            lineas.append(f"   📋 Fila: {fila}")
        else:
            lineas.append("   ⚠️ NO SE IDENTIFICÓ la fila — buscar manualmente")
            fecha_afectada = d.get("fecha_afectada")
            if fecha_afectada:
                lineas.append(f"      Fecha mencionada: {fecha_afectada}")

        if tipo == "CAMBIO_DIA":
            fecha_nueva = d.get("fecha_nueva")
            if fecha_nueva:
                lineas.append(f"   ➡️  Nueva fecha: {fecha_nueva}")

        lineas.append(f"   💬 {razon}")
        lineas.append("")

    lineas.append("📝 Revisa y actualiza manualmente las que sean correctas.")
    mensaje = "\n".join(lineas)

    logger.info(f"Enviando 1 mensaje agrupado con {len(dudosas)} detecciones al admin")
    enviar_mensaje_rapido(INTERCEPTOR_ADMIN_PHONE, mensaje)


# ── Ejecución principal ───────────────────────────────────────────────


def run(auto=False):
    """Punto de entrada del interceptor.

    Flujo:
      1. Pide chats con mensajes nuevos cuya conversación lleva >= 3h fría
      2. Para cada chat frio, obtiene el hilo COMPLETO de la semana
      3. Envía el hilo a Gemini para analizar la conclusión final
      4. Alta confianza → elimina de Clases Esperadas
      5. Dudosas → notifica al admin
      6. Marca los mensajes nuevos como procesados
    """
    logger.info("=" * 60)
    logger.info("INTERCEPTOR DE CANCELACIONES")
    logger.info("=" * 60)

    # 1. Obtener chats con conversaciones frías
    chats_frios = obtener_chats_frios()
    if not chats_frios:
        logger.info("No hay conversaciones frías con mensajes nuevos.")
        return None

    # Cargar mapa de contactos para resolución robusta de nombres
    mapa_telefonos = cargar_mapa_telefono_a_contacto()
    logger.info(f"Contactos mapeados (teléfono → nombre): {len(mapa_telefonos)}")

    # Filtrar chats fríos: procesar DMs y grupos de clase individuales (ej: "Cl. Matemáticas A094").
    # Excluir únicamente grupos comunitarios/generales de alto volumen sin alumno específico.
    chats_frios_validos = []
    chats_descartados = []

    for c in chats_frios:
        cid = c.get("chat_id", "")
        cname = (c.get("chat_name") or "").strip()
        if not cname:
            # Fallback a teléfono o agenda si está disponible
            clean_num = re.sub(r"@.*$", "", cid)
            cname = mapa_telefonos.get(clean_num, "") or cid
            c["chat_name"] = cname

        if es_chat_valido_interceptor(cid, cname):
            chats_frios_validos.append(c)
        else:
            chats_descartados.append(c)

    if chats_descartados:
        logger.info(f"Grupos comunitarios o no relevantes descartados: {len(chats_descartados)}")
        for c in chats_descartados:
            _, ids_descartados = obtener_hilo_chat(c["chat_id"])
            if ids_descartados:
                marcar_procesados(ids_descartados, "GRUPO_DESCARTADO")

    chats_frios = chats_frios_validos

    if not chats_frios:
        logger.info("No hay conversaciones frías válidas que procesar.")
        return None

    logger.info(f"Conversaciones frías para procesar: {len(chats_frios)}")
    for c in chats_frios:
        logger.info(f"  - {c.get('chat_name', c['chat_id'])} ({c['unprocessed_count']} nuevos)")

    # 2. Cargar contexto
    clases_esp_df = cargar_clases_esperadas_csv()
    logger.info(f"Clases Esperadas (pendientes): {len(clases_esp_df)}")

    if clases_esp_df.empty:
        logger.warning("No hay clases esperadas. Marcando mensajes como procesados.")
        for c in chats_frios:
            _, ids = obtener_hilo_chat(c["chat_id"])
            marcar_procesados(ids, "SIN_CONTEXTO")
        return None

    # 3. Recoger hilos de todas las conversaciones
    chats_data = []  # [(chat_name, hilo, ids_sin_procesar)]
    for chat_info in chats_frios:
        chat_id = chat_info["chat_id"]
        chat_name = chat_info.get("chat_name", chat_id)

        hilo, ids_sin_procesar = obtener_hilo_chat(chat_id)

        if not hilo:
            logger.info(f"  {chat_name}: hilo vacío, saltando")
            marcar_procesados(ids_sin_procesar, "HILO_VACIO")
            continue

        logger.info(f"  {chat_name}: {len(hilo)} msgs (semana), {len(ids_sin_procesar)} nuevos")
        chats_data.append((chat_name, hilo, ids_sin_procesar))

    if not chats_data:
        logger.info("Ningún chat con hilo válido.")
        return None

    # 4. UNA sola llamada a Gemini con TODAS las conversaciones
    logger.info(f"\nEnviando {len(chats_data)} conversaciones a Gemini en una sola llamada...")
    chats_con_hilos = [(name, hilo) for name, hilo, _ in chats_data]
    prompt = construir_prompt_batch(chats_con_hilos, clases_esp_df, mapa_telefonos)

    try:
        respuesta_raw = llamar_gemini(prompt)
        # Limpiar posibles bloques markdown que algunos modelos añaden
        texto_json = respuesta_raw.strip()
        texto_json = re.sub(r"^```(?:json)?\s*", "", texto_json, flags=re.IGNORECASE)
        texto_json = re.sub(r"\s*```\s*$", "", texto_json).strip()
        todas_detecciones = json.loads(texto_json)
        if not isinstance(todas_detecciones, list):
            todas_detecciones = []
        _guardar_fallos_gemini(0)
    except json.JSONDecodeError as e:
        logger.warning(f"Gemini devolvió JSON inválido: {e}")
        logger.warning(
            f"Respuesta raw (primeros 500 chars): {respuesta_raw[:500] if respuesta_raw else '(vacía)'}"
        )
        todas_detecciones = []
        _guardar_fallos_gemini(0)
    except Exception as e:
        logger.error(f"Error llamando a Gemini: {e}")
        fallos = _leer_fallos_gemini() + 1
        _guardar_fallos_gemini(fallos)
        logger.warning(
            f"Gemini falló — mensajes NO marcados, se reintentarán (fallo consecutivo #{fallos})"
        )
        if fallos >= GEMINI_CIRCUIT_BREAKER_FAILURES:
            enviar_mensaje_rapido(
                INTERCEPTOR_ADMIN_PHONE,
                "🚨 *Interceptor: Gemini caído*\n\n"
                f"Lleva *{fallos} ejecuciones seguidas* sin poder analizar mensajes.\n"
                "Los mensajes quedan pendientes y se reintentarán automáticamente.\n\n"
                f"_Error_: {str(e)[:200]}",
            )
        return None

    # 5. Agrupar detecciones por chat
    detecciones_por_chat = defaultdict(list)
    for d in todas_detecciones:
        detecciones_por_chat[d.get("chat", "")].append(d)

    # 6. Filtrar detecciones inválidas:
    #    - Mismo día (cambio de hora) → no afecta a Clases Esperadas
    #    - Sin tipo válido
    #    - Con placeholders en el JSON (defensa adicional al prompt)
    detecciones_validas = []
    detecciones_descartadas = 0
    for d in todas_detecciones:
        tipo = d.get("tipo")
        if tipo not in ("CANCELACION", "CAMBIO_DIA"):
            detecciones_descartadas += 1
            continue
        if tipo == "CAMBIO_DIA":
            fecha_afectada = (d.get("fecha_afectada") or "").strip()
            fecha_nueva = (d.get("fecha_nueva") or "").strip()
            if not fecha_nueva or fecha_afectada == fecha_nueva:
                logger.info(f"  Descartada (cambio de hora mismo día): {d.get('chat', '?')}")
                detecciones_descartadas += 1
                continue
        # Defensa: si algún campo lleva placeholder literal '###', rechazar
        razon = json.dumps(d, ensure_ascii=False)
        if "###" in razon:
            logger.warning(f"  Descartada por placeholder en respuesta LLM: {d}")
            detecciones_descartadas += 1
            continue
        detecciones_validas.append(d)

    if detecciones_descartadas:
        logger.info(f"Detecciones descartadas: {detecciones_descartadas}")

    # 6b. Validación dura contra CSV: silenciar alucinaciones y degradar
    #     detecciones no verificables. Una "detección con respaldo" es aquella
    #     cuya fecha_afectada coincide con una fila pendiente real Y
    #     (si el chat tiene P#/A#) también matchea el profesor o alumno.
    #       - Sin clase_id Y sin respaldo  → probable alucinación → SILENCIAR
    #       - Con clase_id pero sin respaldo → inconsistencia  → DEGRADAR a dudosa
    #     Esto ataja los falsos positivos en los que Gemini inventa fechas
    #     (ej. "se cambia del viernes 10 al sábado 11" cuando el viernes 10
    #     no aparece en CLASES ESPERADAS).
    detecciones_filtradas = []
    silenciadas = 0
    degradadas = 0
    for d in detecciones_validas:
        chat_ref = d.get("chat", "")
        tiene_respaldo = _existe_respaldo_csv(d, clases_esp_df, chat_ref)
        if tiene_respaldo:
            logger.info(
                f"  ✔  Respaldada por CSV: {chat_ref} | "
                f"tipo={d.get('tipo')} | clase_id={d.get('clase_id')} | "
                f"fecha={d.get('fecha_afectada')} | conf={d.get('confianza', 0):.2f}"
            )
            detecciones_filtradas.append(d)
            continue
        if d.get("clase_id") is None:
            logger.info(
                "  🤫 Silenciada (sin respaldo CSV, probable alucinación): "
                f"{chat_ref} | fecha={d.get('fecha_afectada')} | "
                f"{(d.get('razon') or '')[:80]}"
            )
            silenciadas += 1
            continue
        # clase_id presente pero no verificable → degradar a dudosa
        d["_forced_dudosa"] = True
        logger.info(
            f"  ⬇️  Degradada a dudosa (clase_id={d.get('clase_id')} sin respaldo CSV): "
            f"{chat_ref} | fecha={d.get('fecha_afectada')} | conf={d.get('confianza', 0):.2f}"
        )
        degradadas += 1
        detecciones_filtradas.append(d)

    detecciones_validas = detecciones_filtradas
    if silenciadas or degradadas:
        logger.info(f"Validación CSV: {silenciadas} silenciadas, {degradadas} degradadas a dudosa")

    # 7. Separar por confianza (respetando el flag _forced_dudosa del paso 6b)
    umbral = INTERCEPTOR_CONFIANZA_UMBRAL
    alta_confianza = [
        d
        for d in detecciones_validas
        if d.get("confianza", 0) >= umbral and not d.get("_forced_dudosa")
    ]
    dudosas = [
        d for d in detecciones_validas if d.get("confianza", 0) < umbral or d.get("_forced_dudosa")
    ]

    logger.info(
        f"\nDetecciones válidas: {len(detecciones_validas)} "
        f"({len(alta_confianza)} alta confianza, {len(dudosas)} dudosas)"
    )

    # 8. Confirmación interactiva si no es auto
    if not auto and alta_confianza:
        print(f"\n¿Aplicar {len(alta_confianza)} acciones automáticas? (s/n): ", end="")
        resp = input().strip().lower()
        if resp != "s":
            logger.info("Acciones automáticas canceladas por el usuario.")
            alta_confianza = []

    # 9. Aplicar acciones de alta confianza (silencioso, sin notificar)
    resultado_acciones = {"eliminadas": 0, "modificadas": 0, "no_aplicadas": []}
    if alta_confianza:
        for d in alta_confianza:
            logger.info(
                f"  [AUTO] {d.get('tipo')} | clase_id={d.get('clase_id')} | "
                f"{d.get('confianza', 0):.0%} | {d.get('chat', '?')} | {d.get('razon', '')}"
            )
        resultado_acciones = actualizar_clases_esperadas(alta_confianza, clases_esp_df)
        logger.info(
            f"  ✅ Aplicadas: {resultado_acciones['eliminadas']} eliminadas, "
            f"{resultado_acciones['modificadas']} modificadas, "
            f"{len(resultado_acciones['no_aplicadas'])} no localizadas"
        )

        # Las acciones de alta confianza que no se pudieron aplicar pasan a
        # dudosas para que el admin las revise manualmente
        if resultado_acciones["no_aplicadas"]:
            dudosas.extend(resultado_acciones["no_aplicadas"])

    # 10. Notificar al admin SOLO las dudosas, en UN solo mensaje agrupado
    if dudosas:
        notificar_admin_dudosas(dudosas, clases_esp_df)

    # 11. Marcar todos los mensajes de los chats procesados como PROCESADO
    for chat_name, hilo, ids_sin_procesar in chats_data:
        dets_del_chat = detecciones_por_chat.get(chat_name, [])
        marcar_procesados(
            ids_sin_procesar,
            "PROCESADO" if dets_del_chat else "IRRELEVANTE",
            json.dumps(dets_del_chat, ensure_ascii=False) if dets_del_chat else None,
        )

    # 12. Resumen final
    logger.info(
        f"\nResultado: {resultado_acciones['eliminadas']} eliminadas, "
        f"{resultado_acciones['modificadas']} modificadas, "
        f"{len(dudosas)} dudosas notificadas"
    )
    logger.info("=" * 60)

    # Solo devolvemos algo si hay dudosas (acciones automáticas son silenciosas)
    # Esto evita el "0/N enviados" del launcher cuando todo va bien.
    return None
