"""
Descargador unificado de CSVs desde Google Sheets.
Descarga todos los CSVs necesarios para los 3 workflows.
"""

import io
import logging
import os
import sys
import time

import pandas as pd
import requests

from config.settings import (
    ADMIN_PHONE,
    CSV_BASE_URL,
    CSV_SOURCES,
    DATA_DIR,
    DRIVE_CONTACTOS_ID,
    FILE_CONTACTOS,
)
from infrastructure.whatsapp_sender import enviar_mensaje_rapido
from shared.csv_utils import leer_contactos_csv
from shared.phone_utils import limpiar_telefono

logger = logging.getLogger(__name__)

# Fix UTF-8 para Windows
if sys.platform == "win32":
    import codecs

    try:
        sys.stdout = codecs.getwriter("utf-8")(sys.stdout.buffer, "strict")
        sys.stderr = codecs.getwriter("utf-8")(sys.stderr.buffer, "strict")
    except Exception:
        pass


def descargar_desde_drive(file_id, filename):
    """
    Descarga un archivo desde Google Drive.

    Args:
        file_id: ID del archivo en Google Drive
        filename: Nombre del archivo destino

    Returns:
        bool: True si tuvo éxito
    """
    logger.info(f"⬇️  Descargando desde Drive: {filename}...")
    try:
        url = f"https://drive.google.com/uc?id={file_id}&export=download"
        response = requests.get(url, timeout=30)
        response.encoding = "utf-8"
        response.raise_for_status()

        df = pd.read_csv(io.StringIO(response.text))

        ruta_destino = DATA_DIR / filename
        temp_path = ruta_destino.with_suffix(ruta_destino.suffix + ".tmp")
        df.to_csv(temp_path, index=False, encoding="utf-8")
        os.replace(temp_path, ruta_destino)
        logger.info(f"✅ {filename}: {len(df)} filas")
        return True

    except Exception as e:
        logger.error(f"❌ ERROR descargando {filename} desde Drive: {e}")
        return False


def build_csv_url(gid):
    """Construye la URL pública de descarga CSV para un GID dado."""
    return f"{CSV_BASE_URL}?gid={gid}&single=true&output=csv"


def descargar_csv(nombre, gid, filename):
    """
    Descarga un CSV desde Google Sheets y lo guarda localmente.

    Args:
        nombre: Nombre descriptivo (para logs)
        gid: ID de la pestaña en Google Sheets
        filename: Nombre del archivo destino

    Returns:
        bool: True si tuvo éxito
    """
    logger.info(f"⬇️  Descargando: {nombre}...")
    try:
        url = f"{build_csv_url(gid)}&t={int(time.time())}"
        response = requests.get(url, timeout=30)
        response.encoding = "utf-8"
        response.raise_for_status()

        df = pd.read_csv(io.StringIO(response.text))

        ruta_destino = DATA_DIR / filename
        temp_path = ruta_destino.with_suffix(ruta_destino.suffix + ".tmp")
        df.to_csv(temp_path, index=False, encoding="utf-8")
        os.replace(temp_path, ruta_destino)
        logger.info(f"✅ {nombre}: {len(df)} filas → {filename}")
        return True

    except Exception as e:
        logger.error(f"❌ ERROR descargando {nombre}: {e}")
        return False


def procesar_contactos():
    """
    Procesa el archivo de contactos: limpia teléfonos y valida formato.
    Si no existe, intenta descargarlo desde Google Drive.
    """
    logger.info(f"📥 Descargando {FILE_CONTACTOS.name} desde Google Drive...")
    if not descargar_desde_drive(DRIVE_CONTACTOS_ID, "contactos_auto.csv"):
        logger.warning(
            "⚠️  No se pudo descargar contactos desde Drive. Se usará la versión local si existe."
        )

    if not FILE_CONTACTOS.exists():
        logger.error(f"❌ {FILE_CONTACTOS} sigue sin existir después de descarga.")
        return False

    logger.info(f"🧹 Procesando contactos: {FILE_CONTACTOS}...")
    try:
        df = leer_contactos_csv(FILE_CONTACTOS)
        if df.empty:
            return False

        col_telf = next(
            (c for c in df.columns if "telef" in c.lower() or "movil" in c.lower()),
            None,
        )

        if not col_telf:
            df["Telef_Clean"] = ""
            temp_path = FILE_CONTACTOS.with_suffix(FILE_CONTACTOS.suffix + ".tmp")
            df.to_csv(temp_path, index=False, encoding="utf-8")
            os.replace(temp_path, FILE_CONTACTOS)
            logger.info(f"✅ Contactos procesados: {len(df)} filas")
            return True

        logger.info(f"   Columna de teléfono: '{col_telf}'")
        df["Telef_Clean"] = df[col_telf].apply(limpiar_telefono)

        # Una sola pasada: validación + detección de duplicados
        hay_errores = False
        numeros_vistos = {}
        duplicados = []
        col_nombre_idx = 0
        col_telf_clean_idx = df.columns.get_loc("Telef_Clean")

        logger.info("   🔍 Revisando números irregulares y duplicados:")
        for idx, row in enumerate(df.itertuples(index=False)):
            nombre = str(row[col_nombre_idx])
            telf_clean = row[col_telf_clean_idx]
            nombre_lower = nombre.lower()

            if "contestador" in nombre_lower or "pepephone" in nombre_lower:
                continue
            if not telf_clean:
                continue

            if len(telf_clean) < 9 or len(telf_clean) > 15:
                logger.warning(
                    f"      ⚠️  Fila {idx+2}: {nombre} → Longitud extraña "
                    f"({len(telf_clean)}): {telf_clean}"
                )
                hay_errores = True
            elif not telf_clean.startswith("+34"):
                logger.warning(f"      ⚠️  Fila {idx+2}: {nombre} → Sin prefijo +34: {telf_clean}")
                hay_errores = True

            if telf_clean in numeros_vistos:
                duplicados.append(
                    f"{nombre} comparte número con {numeros_vistos[telf_clean]} → {telf_clean}"
                )
            else:
                numeros_vistos[telf_clean] = nombre

        if duplicados:
            msg = "⚠️ *Contactos duplicados detectados*\n\n" + "\n".join(
                "- " + d for d in duplicados
            )
            logger.warning(f"      ⚠️  {len(duplicados)} número(s) duplicado(s):")
            for d in duplicados:
                logger.warning(f"         - {d}")
            try:
                enviar_mensaje_rapido(ADMIN_PHONE, msg)
            except Exception as e:
                logger.error(f"Error WhatsApp: {e}")
            hay_errores = True
        else:
            logger.info("      ✅ Sin duplicados.")

        if not hay_errores:
            logger.info("      ✅ Todos los teléfonos correctos.")

        temp_path = FILE_CONTACTOS.with_suffix(FILE_CONTACTOS.suffix + ".tmp")
        df.to_csv(temp_path, index=False, encoding="utf-8")
        os.replace(temp_path, FILE_CONTACTOS)
        logger.info(f"✅ Contactos procesados: {len(df)} filas")
        return True

    except Exception as e:
        logger.error(f"❌ ERROR procesando contactos: {e}")
        return False


def ejecutar_limpieza(solo=None):
    """
    Descarga CSVs y procesa contactos.

    Args:
        solo: Lista opcional de nombres de CSV a descargar.
              Si None, descarga todos.
              Ej: solo=["Pagos", "Clases Por Cobrar Itaca"]

    Returns:
        bool: True si todo fue exitoso
    """
    logger.info("\n=== ACTUALIZANDO BASES DE DATOS ===")
    DATA_DIR.mkdir(exist_ok=True)

    exito_total = True

    for nombre, info in CSV_SOURCES.items():
        if solo is not None and nombre not in solo:
            continue
        if not descargar_csv(nombre, info["gid"], info["file"]):
            exito_total = False

    if not procesar_contactos():
        exito_total = False

    if exito_total:
        logger.info("=== ACTUALIZACIÓN COMPLETADA ===\n")
    else:
        logger.warning("=== HUBO ERRORES EN LA ACTUALIZACIÓN ===\n")

    return exito_total


if __name__ == "__main__":
    ejecutar_limpieza()
