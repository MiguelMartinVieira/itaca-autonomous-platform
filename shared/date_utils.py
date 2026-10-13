"""
Utilidades de formateo de fechas y periodos en español.
"""

from datetime import datetime, timedelta

MESES_ES = {
    1: "enero",
    2: "febrero",
    3: "marzo",
    4: "abril",
    5: "mayo",
    6: "junio",
    7: "julio",
    8: "agosto",
    9: "septiembre",
    10: "octubre",
    11: "noviembre",
    12: "diciembre",
}


def formatear_fecha(fecha_str):
    """Convierte 'YYYY-MM-DD' a 'D de Mes'."""
    try:
        partes = fecha_str.split("-")
        if len(partes) != 3:
            return fecha_str
        _, mes, dia = partes
        nombre_mes = MESES_ES.get(int(mes), "")
        return f"{int(dia)} de {nombre_mes}"
    except Exception:
        return fecha_str


def es_proximo_sabado_ultimo_mes(fecha_ref=None):
    """
    Determina si el sábado de la presente semana es el último sábado del mes.
    """
    if fecha_ref is None:
        fecha_ref = datetime.now()
    dias_al_sabado = (5 - fecha_ref.weekday()) % 7
    proximo_sabado = fecha_ref + timedelta(days=dias_al_sabado)
    siguiente_sabado = proximo_sabado + timedelta(days=7)
    return siguiente_sabado.month != proximo_sabado.month


def es_momento_de_enviar_padres(periodo_raw):
    """
    Determina si es momento de enviar un mensaje basado en el periodo de cobro.
    - Semanal: siempre True
    - Final de mes: solo el último sábado del mes
    - 2 semanas/quincenal: obsoleto, devuelve False
    """
    periodo_normalizado = periodo_raw.lower()

    if "2 semanas" in periodo_normalizado or "quincenal" in periodo_normalizado:
        return False

    ahora = datetime.now()

    if (
        "mes" in periodo_normalizado.split()
        or "final mes" in periodo_normalizado
        or "mensual" in periodo_normalizado
    ):
        es_sabado = ahora.weekday() == 5
        es_ultimo_sabado = es_sabado and ((ahora + timedelta(days=7)).month != ahora.month)
        return es_ultimo_sabado

    elif "semanal" in periodo_normalizado:
        return True

    return False


def obtener_saludo_y_despedida():
    """Devuelve saludo y despedida según la hora actual."""
    hora = datetime.now().hour
    if hora < 13:
        return "Buenos días", "¡Pasa un buen día!"
    else:
        return "Buenas tardes", "¡Pasa una buena tarde!"
