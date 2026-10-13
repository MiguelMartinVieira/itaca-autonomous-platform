"""
Workflow: Recordatorio de pagos a padres/alumnos.
Envía mensajes WhatsApp a padres por clases pendientes de pago.
"""

import logging
import re
import time
from datetime import datetime

import pandas as pd
import requests

from config.settings import (
    ADMIN_PHONE,
    APPS_SCRIPT_WEBAPP_URL,
    FILE_ALUMNOS,
    FILE_CLASES_ACTUALES,
    FILE_CLASES_POR_PAGAR,
    FILE_CONTACTOS,
    FILE_PAGOS,
    NUMERO_BIZUM_DAVID,
    NUMERO_BIZUM_ITACA,
    NUMERO_BIZUM_MIGUEL,
    NUMERO_BIZUM_PATI,
    PAUSA_ENTRE_MENSAJES,
    SHEET_COL_CHECK_PADRES,
    SHEET_GIDS,
    SHEET_KEY_BBDD,
    TEST_MODE,
    TEST_NUMBER,
)
from infrastructure.admin_notifier import enviar_alerta_faltantes
from infrastructure.sheets_client import get_client
from infrastructure.whatsapp_sender import enviar_mensaje_rapido, registrar_error
from shared.contacts import cargar_mapa_contactos
from shared.csv_utils import encontrar_columna, leer_csv_robusto
from shared.currency_utils import limpiar_moneda
from shared.date_utils import MESES_ES, es_momento_de_enviar_padres, obtener_saludo_y_despedida
from shared.phone_utils import formatear_telefono_espacios, limpiar_telefono
from shared.text_utils import (
    extraer_horas,
    formatear_nombre_alumno,
    formatear_periodo,
    limpiar_nombre_display,
)

logger = logging.getLogger(__name__)

CSVS_NEEDED = [
    "Pagos",
    "Alumnos",
    "Clases Actuales",
    "Clases Por Pagar Padres",
    "Clases Por Cobrar Itaca",
]


# ============================================================================
# GOOGLE SHEETS - FUNCIONES ESPECÍFICAS
# ============================================================================

# Caché lazy del worksheet de Pagos (evita reconectar en cada llamada).
# Se accede sólo vía _get_pagos_ws() para que los tests puedan resetearlo.
_pagos_ws = None


def _get_pagos_ws():
    """Devuelve (cacheado) el worksheet de Pagos, o None si la conexión falla."""
    global _pagos_ws
    if _pagos_ws is not None:
        return _pagos_ws
    try:
        client = get_client()
        _pagos_ws = client.get_worksheet_by_gid(SHEET_KEY_BBDD, SHEET_GIDS["Pagos"])
    except Exception as e:
        logger.error(f"❌ Error conectando a Google Sheets: {e}")
        _pagos_ws = None
    return _pagos_ws


def conectar_google_sheets():
    """Backwards-compat: alias de _get_pagos_ws() para callers existentes."""
    return _get_pagos_ws()


def marcar_pagado_en_sheet(row_nums):
    """Marca la columna CHECK con TRUE para las filas dadas."""
    if TEST_MODE:
        logger.info(f"🛡️ TEST_MODE: Saltando actualización de Sheet (Filas {row_nums})")
        return

    ws = _get_pagos_ws()
    if not ws:
        return

    if isinstance(row_nums, int):
        row_nums = [row_nums]

    if not row_nums:
        return

    logger.info(f"   📝 Actualizando Google Sheet (Filas {row_nums})...")
    try:
        updates = [(f"{SHEET_COL_CHECK_PADRES}{r}", True) for r in row_nums]
        client = get_client()
        client.update_cells(ws, updates)
    except Exception as e:
        logger.error(f"   ⚠️ Error actualizando Sheet: {e}")


def ejecutar_script_google_sheets(function_name):
    """Ejecuta un script de Google Apps Script via Web App."""
    if TEST_MODE:
        logger.info(f"🛡️ TEST_MODE: Saltando Google Apps Script ({function_name})")
        return

    if not APPS_SCRIPT_WEBAPP_URL:
        logger.warning("⚠️ Falta APPS_SCRIPT_WEBAPP_URL.")
        return

    logger.info(f"⚙️  Ejecutando script: {function_name}...")

    try:
        response = requests.get(f"{APPS_SCRIPT_WEBAPP_URL}?function={function_name}", timeout=60)

        if response.status_code == 200:
            logger.info(f"📡 Respuesta: {response.text}")
            logger.info(f"✅ Script {function_name} ejecutado.")
        else:
            logger.error(f"❌ Error (Status {response.status_code}): {response.text[:200]}")

    except Exception as e:
        logger.error(f"⚠️ No se pudo ejecutar el script: {e}")


# ============================================================================
# CARGA DE DATOS CON CACHÉ
# ============================================================================

_clases_actuales_cache = None
_clases_por_pagar_cache = None


def cargar_clases_actuales():
    """Carga el CSV de clases actuales (lazy loading con caché)."""
    global _clases_actuales_cache
    if _clases_actuales_cache is not None:
        return _clases_actuales_cache

    if not FILE_CLASES_ACTUALES.exists():
        print(f"⚠️ No se encontró {FILE_CLASES_ACTUALES}")
        _clases_actuales_cache = pd.DataFrame()
        return _clases_actuales_cache

    try:
        _clases_actuales_cache = leer_csv_robusto(FILE_CLASES_ACTUALES)
        if not _clases_actuales_cache.empty:
            logger.info(f"📚 Clases actuales cargadas: {len(_clases_actuales_cache)} clases")
        return _clases_actuales_cache
    except Exception as e:
        logger.error(f"❌ Error cargando clases actuales: {e}")
        _clases_actuales_cache = pd.DataFrame()
        return _clases_actuales_cache


def cargar_clases_por_pagar():
    """Carga el CSV de clases por pagar (lazy loading con caché)."""
    global _clases_por_pagar_cache
    if _clases_por_pagar_cache is not None:
        return _clases_por_pagar_cache

    if not FILE_CLASES_POR_PAGAR.exists():
        print(f"⚠️ No se encontró {FILE_CLASES_POR_PAGAR}")
        _clases_por_pagar_cache = pd.DataFrame()
        return _clases_por_pagar_cache

    try:
        _clases_por_pagar_cache = leer_csv_robusto(FILE_CLASES_POR_PAGAR)
        if not _clases_por_pagar_cache.empty:
            logger.info(f"📚 Clases por pagar cargadas: {len(_clases_por_pagar_cache)} clases")
        return _clases_por_pagar_cache
    except Exception as e:
        logger.error(f"❌ Error cargando clases por pagar: {e}")
        _clases_por_pagar_cache = pd.DataFrame()
        return _clases_por_pagar_cache


# ============================================================================
# DESGLOSE DE CLASES
# ============================================================================


def generar_desglose_clases(alumno_texto, profesor_texto, deuda_total=None):
    """Genera un desglose de clases pendientes de pago para un alumno."""
    df_clases_por_pagar = cargar_clases_por_pagar()
    df_clases_actuales = cargar_clases_actuales()

    if df_clases_por_pagar.empty:
        return ""

    codigos_alumno = re.findall(r"A\d+", alumno_texto)
    codigos_profesor = re.findall(r"P\d+", profesor_texto)

    if not codigos_alumno or not codigos_profesor:
        return ""

    col_fecha = encontrar_columna(df_clases_por_pagar, "fecha indicada")
    col_profesor = encontrar_columna(df_clases_por_pagar, "profesor")
    col_alumno = encontrar_columna(df_clases_por_pagar, "alumno")
    col_horas = encontrar_columna(df_clases_por_pagar, "horas")

    if not col_fecha or not col_profesor or not col_alumno or not col_horas:
        return ""

    idx_prof = df_clases_por_pagar.columns.get_loc(col_profesor)
    idx_alum = df_clases_por_pagar.columns.get_loc(col_alumno)
    idx_fecha = df_clases_por_pagar.columns.get_loc(col_fecha)
    idx_horas = df_clases_por_pagar.columns.get_loc(col_horas)

    clases_filtradas = []
    for row in df_clases_por_pagar.itertuples(index=False):
        texto_prof = str(row[idx_prof])
        texto_alum = str(row[idx_alum])

        prof_match = any(codigo in texto_prof for codigo in codigos_profesor)
        alumno_match = any(codigo in texto_alum for codigo in codigos_alumno)

        if prof_match and alumno_match:
            clases_filtradas.append((row[idx_fecha], row[idx_horas]))

    if not clases_filtradas:
        return ""

    clases_por_fecha = {}
    for fecha_val, horas_val in clases_filtradas:
        fecha_str = str(fecha_val)
        horas = float(horas_val)

        try:
            fecha = datetime.strptime(fecha_str, "%Y-%m-%d")
        except Exception:
            continue

        fecha_key = fecha.strftime("%d/%m")
        if fecha_key not in clases_por_fecha:
            clases_por_fecha[fecha_key] = {"fecha_obj": fecha, "horas": 0}
        clases_por_fecha[fecha_key]["horas"] += horas

    if not clases_por_fecha:
        return ""

    clases_ordenadas = sorted(clases_por_fecha.items(), key=lambda x: x[1]["fecha_obj"])

    # Obtener precio por hora
    precio_por_hora = None
    if not df_clases_actuales.empty:
        col_profesor_actual = encontrar_columna(df_clases_actuales, "profesor")
        col_alumno_actual = encontrar_columna(df_clases_actuales, "alumno")
        col_precio = encontrar_columna(df_clases_actuales, "pago padre")

        if col_profesor_actual and col_alumno_actual and col_precio:
            idx_prof_act = df_clases_actuales.columns.get_loc(col_profesor_actual)
            idx_alum_act = df_clases_actuales.columns.get_loc(col_alumno_actual)
            idx_precio_act = df_clases_actuales.columns.get_loc(col_precio)
            for row in df_clases_actuales.itertuples(index=False):
                prof_match = any(codigo in str(row[idx_prof_act]) for codigo in codigos_profesor)
                alumno_match = any(codigo in str(row[idx_alum_act]) for codigo in codigos_alumno)

                if prof_match and alumno_match:
                    precio_limpio = limpiar_moneda(str(row[idx_precio_act]))
                    if precio_limpio > 0:
                        precio_por_hora = precio_limpio
                        break

    # Generar desglose
    lineas_desglose = ["Desglose:"]
    total_horas = 0

    for fecha_formateada, data in clases_ordenadas:
        horas = data["horas"]
        total_horas += horas

        if horas == 1:
            texto_horas = "1 hora"
        elif horas == int(horas):
            texto_horas = f"{int(horas)} horas"
        else:
            texto_horas = f"{horas} horas"

        lineas_desglose.append(f"{fecha_formateada} - {texto_horas}")

    lineas_desglose.append("-" * 20)

    if total_horas == 1:
        texto_total_horas = "1 hora"
    elif total_horas == int(total_horas):
        texto_total_horas = f"{int(total_horas)} horas"
    else:
        texto_total_horas = f"{total_horas} horas"

    if deuda_total is not None:
        if deuda_total == int(deuda_total):
            texto_total = f"Total {texto_total_horas} - {int(deuda_total)}€"
        else:
            texto_total = f"Total {texto_total_horas} - {deuda_total:.2f}€"
    elif precio_por_hora:
        total_euros = total_horas * precio_por_hora
        if total_euros == int(total_euros):
            texto_total = f"Total {texto_total_horas} - {int(total_euros)}€"
        else:
            texto_total = f"Total {texto_total_horas} - {total_euros:.2f}€"
    else:
        texto_total = f"Total {texto_total_horas}"

    lineas_desglose.append(texto_total)
    return "\n" + "\n".join(lineas_desglose)


# ============================================================================
# GENERACIÓN DE MENSAJES
# ============================================================================


def generar_texto_mensaje(row):
    """
    Genera el texto del mensaje de recordatorio de pago para un padre.
    Tono amigable, empático y personalizado.
    """
    if hasattr(row, "_asdict"):
        row = row._asdict()

    padre = row["Padre_Clean"]
    alumno = row["Alumno_Clean"]
    profesor = row["Profesor_Clean"]
    periodo_raw = row["Periodo_Clean"]
    deuda = row.get("Deuda_Total", 0)
    destino_pago = row.get("Destino_Pago", "Standard")
    periodo_txt = formatear_periodo(periodo_raw)
    es_final_mes = isinstance(periodo_raw, str) and "final mes" in periodo_raw.lower()
    mes_actual = MESES_ES.get(datetime.now().month, "")

    saludo_tiempo, despedida_tiempo = obtener_saludo_y_despedida()

    # Formatear deuda
    if deuda == int(deuda):
        deuda_str = f"{int(deuda)}"
    else:
        deuda_str = f"{deuda:.2f}"

    # Construir partes del mensaje
    # 1. SALUDO
    if es_final_mes:
        texto_saludo = (
            f"{saludo_tiempo} {padre}, al ser el último sábado del mes de {mes_actual}, si pudieses"
        )
    else:
        texto_saludo = f"{saludo_tiempo} {padre}, si pudieses"

    # 2. PETICIÓN DE PAGO
    if destino_pago == "Todo David":
        dest = f"+34 {NUMERO_BIZUM_DAVID} (David)"
        texto_pago = f"hacer el pago de {deuda_str}€ al {dest} por las clases impartidas {periodo_txt} a {alumno}"
    elif destino_pago == "Todo Pati":
        dest = f"+34 {NUMERO_BIZUM_PATI} (Patricia Pérez De Lema)"
        texto_pago = f"hacer el pago de {deuda_str}€ al {dest} por las clases impartidas {periodo_txt} a {alumno}"
    elif destino_pago == "Todo Ítaca":
        dest = f"+34 {NUMERO_BIZUM_ITACA} (Miguel Martín Vieira)"
        texto_pago = f"hacer el pago de {deuda_str}€ al {dest} por las clases impartidas {periodo_txt} a {alumno}"
    elif destino_pago == "Todo Miguel":
        dest = f"+34 {NUMERO_BIZUM_MIGUEL} (Miguel Martín Vieira)"
        texto_pago = f"hacer el pago de {deuda_str}€ al {dest} por las clases impartidas {periodo_txt} a {alumno}"
    else:
        prof_telf = row.get("Profesor_Telefono")
        if prof_telf:
            profesor_str = f"{profesor} ({prof_telf})"
        else:
            profesor_str = profesor
        texto_pago = f"hacer el pago de {deuda_str}€ a {profesor_str} por las clases impartidas {periodo_txt} a {alumno}"

    # 3. CIERRE EMPÁTICO Y CÁLIDO
    texto_cierre = f"te lo agradeceríamos. En caso de que ya se haya realizado el pago disculpa las molestias y muchas gracias. {despedida_tiempo}"

    # Armar mensaje principal
    msg = f"{texto_saludo} {texto_pago} {texto_cierre}"

    # 4. DESGLOSE (al final, más legible)
    alumno_display = row.get("Alumno_Display", "")
    profesor_display = row.get("Profesor_Display", "")
    if alumno_display and profesor_display:
        desglose = generar_desglose_clases(alumno_display, profesor_display, deuda)
        msg += desglose

    return msg


# ============================================================================
# EXTRACCIÓN DE CÓDIGOS
# ============================================================================


def extract_student_codes(text):
    """Extrae códigos de alumnos (A001, A002, etc.)."""
    if not isinstance(text, str):
        return []
    return list(set(re.findall(r"A(\d+)", text)))


# ============================================================================
# CARGA Y CRUCE DE DATOS
# ============================================================================


def cargar_y_cruzar_datos():
    """Carga y cruza datos de pagos, contactos y alumnos."""

    if not FILE_CONTACTOS.exists():
        logger.error("❌ Falta archivo de Contactos.")
        return pd.DataFrame(), [], []

    # Cargar pagos
    if not FILE_PAGOS.exists():
        logger.error(f"❌ No se encontró: {FILE_PAGOS}")
        return pd.DataFrame(), [], []

    logger.info("📂 Cargando datos de pagos...")
    try:
        df_pagos = leer_csv_robusto(FILE_PAGOS)
        df_pagos["_Sheet_Row"] = range(2, 2 + len(df_pagos))

        col_alumno_check = encontrar_columna(df_pagos, "alumno")
        if col_alumno_check:
            filas_antes = len(df_pagos)
            df_pagos = df_pagos.dropna(subset=[col_alumno_check])
            df_pagos = df_pagos[df_pagos[col_alumno_check].astype(str).str.strip() != ""]
            filas_despues = len(df_pagos)
            if filas_antes != filas_despues:
                logger.info(f"   🧹 Eliminadas {filas_antes - filas_despues} filas vacías")

    except Exception as e:
        logger.error(f"❌ Error leyendo CSV de Pagos: {e}")
        return pd.DataFrame(), [], []

    # Cargar contactos
    logger.info("📂 Cargando contactos y alumnos...")
    try:
        df_contactos = leer_csv_robusto(FILE_CONTACTOS)
    except Exception as e:
        logger.error(f"Error cargando contactos: {e}")
        df_contactos = pd.DataFrame()

    if "Telef_Clean" in df_contactos.columns:
        df_contactos["Telef_Clean"] = df_contactos["Telef_Clean"].apply(limpiar_telefono)
    else:
        col_telf = encontrar_columna(df_contactos, r"telef|movil")
        if col_telf:
            df_contactos["Telef_Clean"] = df_contactos[col_telf].apply(limpiar_telefono)
        else:
            df_contactos["Telef_Clean"] = ""

    # Cargar mapa de responsables desde Alumnos
    mapa_responsables = {}
    if FILE_ALUMNOS.exists():
        try:
            df_alumnos = leer_csv_robusto(FILE_ALUMNOS)
            col_cod_lum = encontrar_columna(df_alumnos, r"código|codigo")
            col_resp = encontrar_columna(df_alumnos, "responsable")
            if col_cod_lum:
                idx_cod = df_alumnos.columns.get_loc(col_cod_lum)
                idx_resp = df_alumnos.columns.get_loc(col_resp) if col_resp else None
                for row in df_alumnos.itertuples(index=False):
                    codigo = str(row[idx_cod]).strip()
                    if idx_resp is not None:
                        resp = str(row[idx_resp]).strip().upper()
                        if codigo:
                            mapa_responsables[codigo] = resp
        except Exception as e:
            logger.warning(f"⚠️ Error leyendo Alumnos: {e}")

    # Detectar columnas
    col_deuda = encontrar_columna(df_pagos, r"pagar.*padres")
    col_alumno = encontrar_columna(df_pagos, "alumno")
    col_profesor = encontrar_columna(df_pagos, "profesor")
    col_periodo = encontrar_columna(df_pagos, "periodo")
    col_a_quien = encontrar_columna(df_pagos, r"quién|quien")
    col_gestion_pagos = encontrar_columna(df_pagos, r"gesti")
    col_padres_pagado = encontrar_columna(df_pagos, "padres pagado")

    if not col_deuda or not col_alumno:
        return pd.DataFrame(), [], []

    if not col_profesor:
        col_profesor = "UNKNOWN_PROF"
        df_pagos[col_profesor] = "el profesor"
    if not col_periodo:
        col_periodo = "UNKNOWN_PERIOD"
        df_pagos[col_periodo] = "período"

    df_pagos["Deuda_Num"] = df_pagos[col_deuda].apply(limpiar_moneda)

    # Filtrar ya pagados
    if col_padres_pagado:
        mask_ya_pagado = (
            df_pagos[col_padres_pagado]
            .astype(str)
            .str.strip()
            .str.lower()
            .isin(["true", "1", "yes", "sí", "si"])
        )
        n_ya_pagado = mask_ya_pagado.sum()
        if n_ya_pagado > 0:
            logger.info(f"   ✅ {n_ya_pagado} filas ya pagadas → excluidas")
            df_pagos = df_pagos[~mask_ya_pagado].copy()

    # Filas gestión Profesor
    rows_profesor = []
    if col_gestion_pagos:
        mask_profesor = df_pagos[col_gestion_pagos].astype(str).str.lower().str.contains("profesor")
        mask_deuda = df_pagos["Deuda_Num"] > 0
        rows_profesor = df_pagos[mask_profesor & mask_deuda]["_Sheet_Row"].tolist()

    df_pendientes = df_pagos[df_pagos["Deuda_Num"] > 0].copy()
    logger.info(f"   📊 Filas con deuda pendiente: {len(df_pendientes)}")

    if df_pendientes.empty:
        return pd.DataFrame(), rows_profesor, []

    # Crear mapa de contactos (usando módulo compartido)
    mapa_contactos = cargar_mapa_contactos()

    # Cruzar datos
    datos_finales = []
    contactos_faltantes = []  # alumnos con deuda pero sin contacto registrado
    logger.info("⏳ Filtrando y cruzando datos...")

    idx_alumno = df_pendientes.columns.get_loc(col_alumno)
    idx_periodo = df_pendientes.columns.get_loc(col_periodo)
    idx_profesor = df_pendientes.columns.get_loc(col_profesor)
    idx_deuda_num = df_pendientes.columns.get_loc("Deuda_Num")
    idx_sheet_row = (
        df_pendientes.columns.get_loc("_Sheet_Row")
        if "_Sheet_Row" in df_pendientes.columns
        else None
    )
    idx_gestion = df_pendientes.columns.get_loc(col_gestion_pagos) if col_gestion_pagos else None
    idx_a_quien = df_pendientes.columns.get_loc(col_a_quien) if col_a_quien else None

    for row in df_pendientes.itertuples(index=False):
        texto_alumno = str(row[idx_alumno])
        texto_periodo = str(row[idx_periodo])
        sheet_row = row[idx_sheet_row] if idx_sheet_row is not None else None

        es_gestion_profesor = False
        if idx_gestion is not None:
            valor_gestion = str(row[idx_gestion]).lower()
            if "profesor" in valor_gestion:
                es_gestion_profesor = True

        if es_gestion_profesor:
            continue

        texto_profesor = str(row[idx_profesor])

        if not es_momento_de_enviar_padres(texto_periodo):
            continue

        deuda = row[idx_deuda_num]
        horas_row = extraer_horas(texto_alumno)

        destino_pago = "Standard"
        if idx_a_quien is not None:
            val_a_quien = str(row[idx_a_quien]).strip()
            if "todo pati" in val_a_quien.lower():
                destino_pago = "Todo Pati"
            elif "todo ítaca" in val_a_quien.lower() or "todo itaca" in val_a_quien.lower():
                destino_pago = "Todo Ítaca"
            elif "todo david" in val_a_quien.lower():
                destino_pago = "Todo David"
            elif "todo miguel" in val_a_quien.lower():
                destino_pago = "Todo Miguel"
            else:
                destino_pago = "Standard"

        # Extraer y verificar contacto del profesor
        ids_profesores = re.findall(r"P\d+", texto_profesor)
        id_prof = ids_profesores[0] if ids_profesores else texto_profesor.strip()
        prof_telf_formateado = ""
        if ids_profesores:
            prof_codigo = ids_profesores[0]
            if prof_codigo in mapa_contactos:
                prof_telf_raw = mapa_contactos[prof_codigo].get("Telef_Clean", "")
                prof_telf_formateado = formatear_telefono_espacios(prof_telf_raw)

        # Si el destino es Standard (pago al profesor) y no se tiene su teléfono, se salta y se avisa al admin
        if destino_pago == "Standard" and not prof_telf_formateado:
            deuda_str = f"{deuda:.2f}€" if deuda != int(deuda) else f"{int(deuda)}€"
            msg_error = (
                f"🚨 *Error Pagos Padres*: No se encontró el contacto/teléfono de {texto_profesor} "
                f"para el cobro de {limpiar_nombre_display(texto_alumno)} ({deuda_str}). "
                f"Se ha saltado este recordatorio."
            )
            logger.error(msg_error)
            try:
                enviar_mensaje_rapido(ADMIN_PHONE, msg_error)
            except Exception as e:
                logger.error(f"Error notificando al admin por falta de contacto del profesor: {e}")
            continue

        ids_alumnos = extract_student_codes(texto_alumno)
        contact_info = None
        responsable_tipo = "M"

        if ids_alumnos:
            alumno_id = ids_alumnos[0]
            codigo_alumno = f"A{alumno_id}"
            responsable_tipo = mapa_responsables.get(codigo_alumno, "M")
            target_code = f"MA{alumno_id}"
            if responsable_tipo == "P":
                target_code = f"PA{alumno_id}"
            elif responsable_tipo == "I":
                target_code = codigo_alumno

            if target_code in mapa_contactos:
                contact_info = mapa_contactos[target_code]
            else:
                for alt in [f"MA{alumno_id}", f"PA{alumno_id}", codigo_alumno]:
                    if alt in mapa_contactos:
                        contact_info = mapa_contactos[alt]
                        break

        entry_base = {
            "Alumno_Display": texto_alumno,
            "Alumno_Clean": formatear_nombre_alumno(limpiar_nombre_display(texto_alumno)),
            "Profesor_Display": texto_profesor,
            "Profesor_Clean": limpiar_nombre_display(texto_profesor),
            "Profesor_Telefono": prof_telf_formateado,
            "Periodo_Clean": texto_periodo.strip(),
            "Deuda_Total": deuda,
            "Horas_Total": horas_row,
            "Destino_Pago": destino_pago,
            "Sheet_Rows": [sheet_row] if sheet_row else [],
            "Responsable_Tipo": responsable_tipo,
        }

        # Para pagos estándar a profesores, separar por profesor
        group_suffix = f"_{id_prof}" if destino_pago == "Standard" else ""

        if contact_info:
            entry = contact_info.copy()
            entry.update(entry_base)
            entry["Key_Group"] = f"{entry['Telef_Clean']}_{destino_pago}{group_suffix}"
            datos_finales.append(entry)
        else:
            # Registrar para alerta operativa
            codigo_buscado = target_code if ids_alumnos else "?"
            contactos_faltantes.append(
                {
                    "alumno": limpiar_nombre_display(texto_alumno),
                    "deuda": deuda,
                    "periodo": texto_periodo.strip(),
                    "codigo_buscado": codigo_buscado,
                }
            )
            logger.info(
                f"   ⚠️  Sin contacto: {codigo_buscado} ({limpiar_nombre_display(texto_alumno)}) — {deuda}€"
            )
            entry_base.update(
                {
                    "Padre_Display": "Desconocido",
                    "Padre_Clean": "Familia",
                    "Telef_Clean": None,
                    "Key_Group": f"unknown_{texto_alumno}_{destino_pago}{group_suffix}",
                }
            )
            datos_finales.append(entry_base)

    df_resultado = pd.DataFrame(datos_finales)
    logger.info(f"   📊 Mensajes candidatos: {len(df_resultado)}")

    if df_resultado.empty:
        return df_resultado, rows_profesor, contactos_faltantes

    def join_unique(series):
        vals = [s for s in series if s and isinstance(s, str)]
        unique_vals = sorted(set(vals))
        if len(unique_vals) <= 1:
            return unique_vals[0] if unique_vals else ""
        return " y ".join(unique_vals)

    def flatten_rows(series):
        flat = []
        for x in series:
            if isinstance(x, list):
                flat.extend(x)
            else:
                flat.append(x)
        return list(set(flat))

    grupo = (
        df_resultado.groupby("Key_Group")
        .agg(
            {
                "Padre_Clean": "first",
                "Telef_Clean": "first",
                "Deuda_Total": "sum",
                "Horas_Total": "sum",
                "Alumno_Clean": join_unique,
                "Alumno_Display": join_unique,
                "Profesor_Clean": join_unique,
                "Profesor_Display": join_unique,
                "Profesor_Telefono": "first",
                "Periodo_Clean": join_unique,
                "Destino_Pago": "first",
                "Sheet_Rows": flatten_rows,
                "Responsable_Tipo": "first",
            }
        )
        .reset_index()
    )

    return grupo[grupo["Telef_Clean"].notna()].copy(), rows_profesor, contactos_faltantes


# ============================================================================
# FUNCIÓN PRINCIPAL
# ============================================================================


def run(auto=False):
    """Ejecuta el workflow de pagos a padres."""
    logger.info("🚀 INICIANDO SISTEMA - PADRES")
    _dias = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
    _ahora = datetime.now()
    logger.info(f"   📅 {_ahora.strftime('%Y-%m-%d %H:%M')} ({_dias[_ahora.weekday()]})")

    if TEST_MODE:
        logger.info(f"🛡️ MODO PRUEBA → {TEST_NUMBER}")

    ES_AUTO = auto

    df_envios, rows_profesor, contactos_faltantes = cargar_y_cruzar_datos()

    if df_envios.empty and not rows_profesor:
        logger.info("✅ No hay mensajes ni filas de Profesores para marcar.")
        if contactos_faltantes:
            enviar_alerta_faltantes("Padres", contactos_faltantes)
        return

    # Solo filas de profesores
    if df_envios.empty and rows_profesor:
        logger.info(f"ℹ️ {len(rows_profesor)} filas de gestión 'Profesor' para marcar.")
        if not ES_AUTO:
            confirm = input("¿Marcar en Google Sheets? (si/no): ")
            if confirm.lower() not in ["si", "s", "yes", "y"]:
                return
        marcar_pagado_en_sheet(rows_profesor)
        ejecutar_script_google_sheets("procesarPagosPadres")
        return

    # Preview
    logger.info(f"\n--- 📝 PREVISUALIZACIÓN ({len(df_envios)} mensajes) ---")
    for row in df_envios.head(3).itertuples(index=False):
        msg = generar_texto_mensaje(row)
        logger.info(f"Para: {row.Padre_Clean} | Msg: {msg[:50]}...")
        if row.Sheet_Rows:
            logger.info(f"   → Filas Sheet: {row.Sheet_Rows}")

    # Confirmación
    if not ES_AUTO:
        confirm = input(f"\n¿Enviar {len(df_envios)} mensajes? (si/no): ")
        if confirm.lower() not in ["si", "s", "yes", "y"]:
            return
    else:
        logger.info("\n🤖 MODO AUTOMÁTICO")

    # Enviar
    logger.info("\n🚀 Enviando...")
    time.sleep(2)

    enviados = 0
    errores = 0
    fallidos = []
    total = len(df_envios)

    for pos, row in enumerate(df_envios.itertuples(index=False)):
        msg = ""
        logger.info(f"[{pos + 1}/{total}] {row.Padre_Clean}...")

        try:
            msg = generar_texto_mensaje(row)
            success = enviar_mensaje_rapido(row.Telef_Clean, msg)

            if success:
                enviados += 1
                if row.Sheet_Rows:
                    marcar_pagado_en_sheet(row.Sheet_Rows)
                time.sleep(PAUSA_ENTRE_MENSAJES)
            else:
                errores += 1
                fallidos.append(row.Padre_Clean)
        except Exception as e:
            errores += 1
            fallidos.append(row.Padre_Clean)
            logger.error(f"   ❌ Excepción: {e}")
            registrar_error(row.Telef_Clean, f"Excepción: {str(e)}", msg[:50])
            continue

    # Marcar filas Profesor
    if rows_profesor:
        logger.info(f"\n📝 Marcando {len(rows_profesor)} filas (Gestión Profesor)...")
        marcar_pagado_en_sheet(rows_profesor)

    logger.info(f"\n🏁 FIN. Enviados: {enviados} | Errores: {errores}")

    # Ejecutar script post-envío
    if enviados > 0 or rows_profesor:
        ejecutar_script_google_sheets("procesarPagosPadres")

    # Alerta si hay alumnos con deuda sin contacto registrado
    if contactos_faltantes:
        enviar_alerta_faltantes("Padres", contactos_faltantes)

    return {"enviados": enviados, "total": total, "fallidos": fallidos}
