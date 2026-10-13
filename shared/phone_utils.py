"""
Utilidades de limpieza y validación de números de teléfono.
"""

import math
import re


def limpiar_telefono(valor):
    """
    Limpia y normaliza un número de teléfono.
    - Maneja NaN, floats con .0, espacios, guiones
    - Añade +34 para móviles españoles de 9 dígitos (6xx/7xx)
    - Añade + para números que empiezan por 34 y tienen 11+ dígitos
    """
    if valor is None or (isinstance(valor, float) and math.isnan(valor)):
        return ""
    if str(valor).strip() == "" or str(valor).lower() == "nan":
        return ""

    s = str(valor).strip()

    # Quitar .0 de conversión float->str
    if s.endswith(".0"):
        s = s[:-2]

    # Limpiar caracteres no numéricos excepto +
    s = s.replace(" ", "").replace("-", "").replace(".", "")

    # Asegurar prefijo internacional
    if not s.startswith("+"):
        if s.startswith("34") and len(s) >= 11:
            s = "+" + s
        elif len(s) == 9 and s[0] in ("6", "7"):
            s = "+34" + s
        else:
            s = "+34" + s

    return s


def validar_telefono_e164(telefono):
    """Valida que el formato sea E.164 válido (ej: +34666777888)."""
    return bool(re.match(r"^\+\d{8,16}$", str(telefono)))


def formatear_telefono_espacios(telefono):
    """
    Formatea un número de teléfono con espacios legibles.
    Para móviles españoles (+34 y 9 dígitos): '+34 682 30 30 59'.
    """
    if not telefono:
        return ""
    tel_limpio = limpiar_telefono(telefono)
    if not tel_limpio:
        return ""
    # Móvil español: +34 seguido de 9 dígitos (longitud total 12)
    if tel_limpio.startswith("+34") and len(tel_limpio) == 12:
        return f"+34 {tel_limpio[3:6]} {tel_limpio[6:8]} {tel_limpio[8:10]} {tel_limpio[10:12]}"
    return tel_limpio
