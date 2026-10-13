"""
Utilidades de limpieza de valores monetarios.
"""

import re


def limpiar_moneda(valor):
    """
    Convierte un valor a float, limpiando símbolos y soportando paréntesis para negativos.
    Ej: "(91,06)" → -91.06, "15,50€" → 15.5
    """
    if isinstance(valor, (int, float)):
        return float(valor)
    if isinstance(valor, str):
        # Limpiar espacios
        limpio = valor.strip()
        es_negativo = "(" in limpio and ")" in limpio

        # Eliminar todo lo que no sea dígito, coma o punto
        limpio = re.sub(r"[^\d,.-]", "", limpio)

        # Lógica de separadores:
        # Si tiene punto y coma, el punto es de miles (vía Española 1.200,50)
        if "." in limpio and "," in limpio:
            limpio = limpio.replace(".", "").replace(",", ".")
        # Si tiene coma pero no punto, la coma es el decimal
        elif "," in limpio:
            limpio = limpio.replace(",", ".")
        # Si tiene múltiples puntos, son separadores de miles
        elif limpio.count(".") > 1:
            limpio = limpio.replace(".", "")

        try:
            num = float(limpio)
            return -num if es_negativo else num
        except (ValueError, TypeError):
            return 0.0
    return 0.0
