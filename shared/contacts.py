import logging
import re

from config.settings import FILE_CONTACTOS
from shared.csv_utils import leer_contactos_csv
from shared.phone_utils import limpiar_telefono
from shared.text_utils import limpiar_nombre_display

logger = logging.getLogger(__name__)


def cargar_mapa_contactos(contactos_path=None):
    """
    Carga contactos y retorna mapa de códigos → info de contacto.

    Soporta códigos de profesor (P001), alumno (A001) y padre (MA001, PA001).

    Returns:
        dict: {código: {"Padre_Display": str, "Padre_Clean": str, "Telef_Clean": str}}
    """
    path = contactos_path or FILE_CONTACTOS
    df = leer_contactos_csv(path)
    if df.empty:
        return {}

    # Limpiar teléfonos
    if "Telef_Clean" in df.columns:
        df["Telef_Clean"] = df["Telef_Clean"].apply(limpiar_telefono)
    else:
        col_telf = next(
            (c for c in df.columns if "telef" in c.lower() or "movil" in c.lower()),
            None,
        )
        if col_telf:
            df["Telef_Clean"] = df[col_telf].apply(limpiar_telefono)
        else:
            df["Telef_Clean"] = ""

    mapa = {}
    col_nombre = df.columns[0]
    col_nombre_idx = df.columns.get_loc(col_nombre)
    col_telf_idx = df.columns.get_loc("Telef_Clean")

    for row in df.itertuples(index=False):
        nombre = str(row[col_nombre_idx])
        telef = str(row[col_telf_idx])

        if not telef or len(telef) < 5 or telef.lower() == "nan":
            continue

        # Extraer todos los códigos: P001, A001, MA001, PA001
        codigos = re.findall(r"\b([MP]?A\d+|P\d+)\b", nombre)
        nombre_limpio = limpiar_nombre_display(nombre)

        for codigo in codigos:
            mapa[codigo] = {
                "Padre_Display": nombre,
                "Padre_Clean": nombre_limpio,
                "Telef_Clean": telef,
            }

    logger.info(f"   📞 Contactos mapeados: {len(mapa)}")
    return mapa


def cargar_mapa_telefono_a_contacto(contactos_path=None):
    """
    Mapa inverso: teléfono (sin +) → nombre completo con código.

    Útil para resolver from_number del servicio WhatsApp (ej: "34682871687")
    al nombre del contacto (ej: "Alberto Sánchez del Campo P124").
    """
    path = contactos_path or FILE_CONTACTOS
    df = leer_contactos_csv(path)
    if df.empty:
        return {}

    col_nombre = df.columns[0]
    col_telf = next(
        (
            c
            for c in df.columns
            if "telef" in c.lower() or "movil" in c.lower() or c == "Telef_Clean"
        ),
        None,
    )
    if not col_telf:
        return {}

    mapa = {}
    col_nombre_idx = df.columns.get_loc(col_nombre)
    col_telf_idx = df.columns.get_loc(col_telf)

    for row in df.itertuples(index=False):
        nombre = str(row[col_nombre_idx]).strip()
        telf = limpiar_telefono(str(row[col_telf_idx]))

        if not telf or len(telf) < 5 or telf.lower() == "nan":
            continue

        # Sin '+' para coincidir con from_number del servicio WA
        mapa[telf.lstrip("+")] = nombre

    return mapa
