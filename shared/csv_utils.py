"""
Utilidades para lectura robusta de archivos CSV con encoding variable y búsqueda difusa de columnas.
"""

import logging
from io import StringIO
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


def leer_csv_robusto(path):
    """
    Lee cualquier CSV con detección automática de encoding y limpieza de null bytes.
    Intenta utf-8 → utf-16 → latin-1.

    Args:
        path: Ruta al archivo CSV (Path o str).

    Returns:
        pd.DataFrame limpio, o pd.DataFrame vacío si falla.
    """
    path = Path(path)
    if not path.exists():
        logger.warning(f"Archivo no encontrado: {path}")
        return pd.DataFrame()

    try:
        raw_bytes = path.read_bytes()
        content = None

        # Intentar decodificar
        for enc in ["utf-8", "utf-16", "latin-1"]:
            try:
                decoded = raw_bytes.decode(enc)
                if "," in decoded or "\n" in decoded:
                    content = decoded
                    break
            except Exception:
                continue

        if content is None:
            logger.error(f"No se pudo decodificar {path.name}")
            return pd.DataFrame()

        # Limpiar null bytes
        content = content.replace("\x00", "")

        # Parsear con pandas
        df = pd.read_csv(StringIO(content))

        # Limpiar nombres de columnas
        df.columns = [str(c).strip() for c in df.columns]

        # Limpiar null bytes de los valores de tipo string
        for col in df.select_dtypes(include=["object"]).columns:
            df[col] = df[col].astype(str).str.replace("\x00", "", regex=False).str.strip()

        return df

    except Exception as e:
        logger.error(f"Error procesando {path.name}: {e}")
        return pd.DataFrame()


def encontrar_columna(df, pattern):
    """
    Busca una columna en el DataFrame que coincida con un patrón (regex o substring).
    Útil para manejar variaciones como "Profesor", "PROFESOR", "profesor_id".

    Args:
        df: pd.DataFrame
        pattern: string (substring) o regex pattern

    Returns:
        Nombre exacto de la columna o None si no se encuentra.
    """
    import re

    if df.empty:
        return None

    for col in df.columns:
        if re.search(pattern, col, re.IGNORECASE):
            return col
    return None


# Mantener compatibilidad con el nombre anterior si se usa
def leer_contactos_csv(path):
    return leer_csv_robusto(path)
